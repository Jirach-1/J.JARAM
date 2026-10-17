// Window capture without PrintWindow. All objects live on one Python-owned MTA
// thread; CreateFreeThreaded needs neither a Qt dispatcher nor a message pump.
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dwmapi.h>
#include <roapi.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Metadata.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <pybind11/pybind11.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <unordered_map>
#include <vector>

namespace py = pybind11;
namespace capture = winrt::Windows::Graphics::Capture;
namespace dx = winrt::Windows::Graphics::DirectX;
using Clock = std::chrono::steady_clock;

struct Apartment {
    DPI_AWARENESS_CONTEXT previous_dpi = nullptr;
    Apartment() {
        winrt::check_hresult(RoInitialize(RO_INIT_MULTITHREADED));
        previous_dpi = SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    ~Apartment() {
        if (previous_dpi) SetThreadDpiAwarenessContext(previous_dpi);
        RoUninitialize();
    }
};

struct Pixels {
    int width = 0, height = 0;
    std::vector<char> bytes;
};

struct FrameState {
    std::mutex mutex;
    capture::Direct3D11CaptureFrame latest{nullptr};
    std::string error;
    HRESULT error_code = S_OK;
    capture::Direct3D11CaptureFrame take() {
        std::lock_guard lock(mutex);
        if (!error.empty()) {
            if (error_code == DXGI_ERROR_DEVICE_REMOVED || error_code == DXGI_ERROR_DEVICE_RESET ||
                error_code == DXGI_ERROR_DEVICE_HUNG)
                throw winrt::hresult_error(winrt::hresult{error_code}, winrt::to_hstring(error));
            throw std::runtime_error(error);
        }
        auto frame = std::move(latest);
        latest = nullptr;
        return frame;
    }
    ~FrameState() {
        try { if (latest) latest.Close(); } catch (...) {}
    }
};

struct Session {
    DWORD pid = 0;
    capture::GraphicsCaptureItem item{nullptr};
    capture::Direct3D11CaptureFramePool pool{nullptr};
    capture::GraphicsCaptureSession session{nullptr};
    std::shared_ptr<FrameState> frames = std::make_shared<FrameState>();
    winrt::event_token frame_token{};
    bool subscribed = false;
    winrt::Windows::Graphics::SizeInt32 size{};
    winrt::com_ptr<ID3D11Texture2D> staging;
    Pixels last;
    Clock::time_point used = Clock::now();
    ~Session() {
        try { if (pool && subscribed) pool.FrameArrived(frame_token); } catch (...) {}
        try { if (session) session.Close(); } catch (...) {}
        try { auto frame = frames->take(); if (frame) frame.Close(); } catch (...) {}
        try { if (pool) pool.Close(); } catch (...) {}
    }
};

class Capturer {
    // Declared first so COM is uninitialized only after every COM object dies.
    Apartment apartment;
    winrt::com_ptr<ID3D11Device> device;
    winrt::com_ptr<ID3D11DeviceContext> context;
    dx::Direct3D11::IDirect3DDevice runtime_device{nullptr};
    std::unordered_map<HWND, std::unique_ptr<Session>> sessions;
    size_t session_limit = 64;
    size_t session_budget_bytes = size_t{1024} * 1024 * 1024;
    std::chrono::seconds idle_limit{15};

    static size_t estimated_bytes(winrt::Windows::Graphics::SizeInt32 size) {
        // Two GPU frames, staging, cached CPU pixels, and compositor overhead.
        return static_cast<size_t>(std::max(1, size.Width)) *
            static_cast<size_t>(std::max(1, size.Height)) * 20;
    }

    size_t reserved_bytes() const {
        size_t total = 0;
        for (auto const& [_, entry] : sessions) total += estimated_bytes(entry->size);
        return total;
    }

    void evict_to_fit(size_t additional) {
        while (!sessions.empty() &&
               (sessions.size() >= session_limit ||
                additional > session_budget_bytes - std::min(reserved_bytes(), session_budget_bytes))) {
            auto oldest = std::min_element(sessions.begin(), sessions.end(),
                [](auto const& a, auto const& b) { return a.second->used < b.second->used; });
            sessions.erase(oldest);
        }
    }

    void trim_after_resize(HWND current) {
        while (sessions.size() > 1 && reserved_bytes() > session_budget_bytes) {
            auto oldest = sessions.end();
            for (auto it = sessions.begin(); it != sessions.end(); ++it)
                if (it->first != current &&
                    (oldest == sessions.end() || it->second->used < oldest->second->used))
                    oldest = it;
            if (oldest == sessions.end()) break;
            sessions.erase(oldest);
        }
    }

    Session& get_session(HWND hwnd, DWORD pid) {
        const auto now = Clock::now();
        for (auto it = sessions.begin(); it != sessions.end();) {
            DWORD live_pid = 0;
            GetWindowThreadProcessId(it->first, &live_pid);
            if (live_pid != it->second->pid || IsIconic(it->first) ||
                now - it->second->used > idle_limit)
                it = sessions.erase(it);
            else ++it;
        }
        auto existing = sessions.find(hwnd);
        if (existing != sessions.end()) {
            existing->second->used = now;
            return *existing->second;
        }
        auto entry = std::make_unique<Session>();
        entry->pid = pid;
        auto factory = winrt::get_activation_factory<capture::GraphicsCaptureItem,
            IGraphicsCaptureItemInterop>();
        winrt::check_hresult(factory->CreateForWindow(hwnd,
            winrt::guid_of<capture::GraphicsCaptureItem>(), winrt::put_abi(entry->item)));
        entry->size = entry->item.Size();
        if (entry->size.Width <= 0 || entry->size.Height <= 0)
            throw std::runtime_error("The window has no capturable content.");
        evict_to_fit(estimated_bytes(entry->size));
        entry->pool = capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
            runtime_device, dx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, entry->size);
        entry->session = entry->pool.CreateCaptureSession(entry->item);
        // Drain the pool continuously, but perform GPU-to-CPU copies only on OCR
        // requests. Polling only at OCR cadence otherwise returns queued old frames.
        auto state = entry->frames;
        entry->frame_token = entry->pool.FrameArrived([state](auto const& sender, auto const&) {
            try {
                auto frame = sender.TryGetNextFrame();
                if (!frame) return;
                std::lock_guard lock(state->mutex);
                if (state->latest) state->latest.Close();
                state->latest = std::move(frame);
            } catch (winrt::hresult_error const& error) {
                std::lock_guard lock(state->mutex);
                state->error = winrt::to_string(error.message());
                state->error_code = static_cast<HRESULT>(error.code());
            } catch (...) {
                std::lock_guard lock(state->mutex);
                state->error = "Windows Graphics Capture frame delivery failed.";
            }
        });
        entry->subscribed = true;
        if (winrt::Windows::Foundation::Metadata::ApiInformation::IsPropertyPresent(
            L"Windows.Graphics.Capture.GraphicsCaptureSession", L"IsCursorCaptureEnabled"))
            entry->session.IsCursorCaptureEnabled(false);
        entry->session.StartCapture();
        auto& result = *entry;
        sessions.emplace(hwnd, std::move(entry));
        return result;
    }

    void read_frame(Session& entry, capture::Direct3D11CaptureFrame const& frame) {
        auto access = frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
        winrt::com_ptr<ID3D11Texture2D> texture;
        winrt::check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D), texture.put_void()));
        D3D11_TEXTURE2D_DESC desc{};
        texture->GetDesc(&desc);
        const auto size = frame.ContentSize();
        if (size.Width <= 0 || size.Height <= 0 || size.Width > static_cast<int>(desc.Width) ||
            size.Height > static_cast<int>(desc.Height))
            throw std::runtime_error("Window resized during capture; retry after resizing.");
        D3D11_TEXTURE2D_DESC staging_desc{};
        if (entry.staging) entry.staging->GetDesc(&staging_desc);
        if (!entry.staging || staging_desc.Width != desc.Width || staging_desc.Height != desc.Height) {
            entry.staging = nullptr;
            desc.Usage = D3D11_USAGE_STAGING;
            desc.BindFlags = 0;
            desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            desc.MiscFlags = 0;
            winrt::check_hresult(device->CreateTexture2D(&desc, nullptr, entry.staging.put()));
        }
        context->CopyResource(entry.staging.get(), texture.get());
        Pixels next;
        next.width = size.Width;
        next.height = size.Height;
        next.bytes.resize(static_cast<size_t>(next.width) * next.height * 4);
        D3D11_MAPPED_SUBRESOURCE mapped{};
        winrt::check_hresult(context->Map(entry.staging.get(), 0, D3D11_MAP_READ, 0, &mapped));
        for (int y = 0; y < next.height; ++y)
            std::memcpy(next.bytes.data() + static_cast<size_t>(y) * next.width * 4,
                static_cast<char*>(mapped.pData) + static_cast<size_t>(y) * mapped.RowPitch,
                static_cast<size_t>(next.width) * 4);
        context->Unmap(entry.staging.get(), 0);
        entry.last = std::move(next);
    }

public:
    void configure(int active_windows, double revisit_seconds) {
        session_limit = static_cast<size_t>(std::clamp(active_windows, 0, 252) + 4);
        session_limit = std::max<size_t>(64, session_limit);
        const auto seconds = std::clamp(2.0 * revisit_seconds + 5.0, 15.0, 3600.0);
        idle_limit = std::chrono::seconds(static_cast<int>(std::ceil(seconds)));
    }

    Capturer() {
        if (!capture::GraphicsCaptureSession::IsSupported())
            throw std::runtime_error("Windows Graphics Capture is not supported on this system.");
        winrt::check_hresult(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
            device.put(), nullptr, context.put()));
        auto dxgi_device = device.as<IDXGIDevice>();
        winrt::com_ptr<IInspectable> inspectable;
        winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi_device.get(), inspectable.put()));
        runtime_device = inspectable.as<dx::Direct3D11::IDirect3DDevice>();
    }

    py::tuple grab(uintptr_t handle, int timeout_ms) {
        Pixels pixels;
        RECT outer{}, bounds{};
        int offset_x = 0, offset_y = 0;
        {
            py::gil_scoped_release release;
            HWND hwnd = reinterpret_cast<HWND>(handle);
            try {
                DWORD pid = 0;
                GetWindowThreadProcessId(hwnd, &pid);
                if (!pid || !IsWindowVisible(hwnd) || IsIconic(hwnd)) {
                    sessions.erase(hwnd);
                    throw std::runtime_error("Window is closed, hidden, or minimized. Restore it to capture.");
                }
                auto& entry = get_session(hwnd, pid);
                const auto deadline = Clock::now() + std::chrono::milliseconds(std::clamp(timeout_ms, 1, 2000));
                bool received = false;
                do {
                    auto frame = entry.frames->take();
                    if (frame) {
                        const auto size = frame.ContentSize();
                        if (size.Width != entry.size.Width || size.Height != entry.size.Height) {
                            frame.Close();
                            entry.last = {};
                            entry.staging = nullptr;
                            entry.size = size;
                            if (size.Width <= 0 || size.Height <= 0)
                                throw std::runtime_error("Window has no capturable content.");
                            entry.pool.Recreate(runtime_device,
                                dx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
                            trim_after_resize(hwnd);
                        } else {
                            read_frame(entry, frame);
                            frame.Close();
                            received = true;
                            break;
                        }
                    }
                    // WGC need not deliver another frame for unchanged content.
                    if (!entry.last.bytes.empty()) break;
                    Sleep(5);
                } while (Clock::now() < deadline);
                if (!received && entry.last.bytes.empty())
                    throw std::runtime_error("Timed out waiting for a Windows Graphics Capture frame.");
                if (IsIconic(hwnd) || !GetWindowRect(hwnd, &outer))
                    throw std::runtime_error("Window changed during capture.");
                pixels = entry.last;
                // PrintWindow uses GetWindowRect, which includes invisible resize borders.
                // WGC commonly uses DWM's visible bounds. Preserve existing normalized ROIs.
                if (pixels.width != outer.right - outer.left || pixels.height != outer.bottom - outer.top) {
                    winrt::check_hresult(DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                        &bounds, sizeof(bounds)));
                    if (pixels.width != bounds.right - bounds.left || pixels.height != bounds.bottom - bounds.top)
                        throw std::runtime_error("Window size changed during capture; retry after resizing.");
                    offset_x = bounds.left - outer.left;
                    offset_y = bounds.top - outer.top;
                }
            } catch (winrt::hresult_error const& error) {
                const auto code = static_cast<HRESULT>(error.code());
                const bool device_lost = code == DXGI_ERROR_DEVICE_REMOVED ||
                    code == DXGI_ERROR_DEVICE_RESET || code == DXGI_ERROR_DEVICE_HUNG;
                if (device_lost) sessions.clear();
                else sessions.erase(hwnd);
                throw std::runtime_error(std::string(device_lost ? "WGC device lost: " : "WGC window error: ") +
                    winrt::to_string(error.message()));
            } catch (...) {
                sessions.erase(hwnd);
                throw;
            }
        }
        return py::make_tuple(pixels.width, pixels.height,
            py::bytes(pixels.bytes.data(), pixels.bytes.size()),
            offset_x, offset_y, outer.right - outer.left, outer.bottom - outer.top);
    }
};

PYBIND11_MODULE(wgc_capture_native, module) {
    py::class_<Capturer>(module, "Capturer")
        .def(py::init([] {
            try { return std::make_unique<Capturer>(); }
            catch (winrt::hresult_error const& error) {
                throw std::runtime_error(winrt::to_string(error.message()));
            }
        }))
        .def("configure", &Capturer::configure, py::arg("active_windows"), py::arg("revisit_seconds"))
        .def("grab", &Capturer::grab, py::arg("hwnd"), py::arg("timeout_ms") = 750);
}
