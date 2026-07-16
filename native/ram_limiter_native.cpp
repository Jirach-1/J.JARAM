#ifdef _WIN32
#  define NOMINMAX
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <psapi.h>
#  include <tlhelp32.h>
#else
#  error "ram_limiter_native is Windows-only."
#endif

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <chrono>
#include <cstdint>
#include <iomanip>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace py = pybind11;

namespace {

[[noreturn]] void throw_win_error(const char* prefix) {
  const DWORD err = ::GetLastError();
  std::ostringstream oss;
  oss << prefix << " (WinError " << err << ")";
  throw std::runtime_error(oss.str());
}

class ScopedHandle {
 public:
  ScopedHandle() = default;
  explicit ScopedHandle(HANDLE h) : h_(h) {}
  ~ScopedHandle() { reset(); }

  ScopedHandle(const ScopedHandle&) = delete;
  ScopedHandle& operator=(const ScopedHandle&) = delete;

  ScopedHandle(ScopedHandle&& other) noexcept : h_(other.h_) { other.h_ = nullptr; }
  ScopedHandle& operator=(ScopedHandle&& other) noexcept {
    if (this == &other) {
      return *this;
    }
    reset();
    h_ = other.h_;
    other.h_ = nullptr;
    return *this;
  }

  HANDLE get() const { return h_; }
  explicit operator bool() const { return h_ && h_ != INVALID_HANDLE_VALUE; }

  void reset(HANDLE h = nullptr) {
    if (h_ && h_ != INVALID_HANDLE_VALUE) {
      ::CloseHandle(h_);
    }
    h_ = h;
  }

 private:
  HANDLE h_ = nullptr;
};

std::wstring utf8_to_wide(std::string_view s) {
  if (s.empty()) {
    return {};
  }
  const int wlen =
      ::MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), nullptr, 0);
  if (wlen <= 0) {
    throw_win_error("MultiByteToWideChar(CP_UTF8) failed");
  }
  std::wstring out(static_cast<size_t>(wlen), L'\0');
  const int rc =
      ::MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), out.data(), wlen);
  if (rc <= 0) {
    throw_win_error("MultiByteToWideChar(CP_UTF8) failed");
  }
  return out;
}

std::string wide_to_utf8(std::wstring_view w) {
  if (w.empty()) {
    return {};
  }
  const int len =
      ::WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
  if (len <= 0) {
    throw_win_error("WideCharToMultiByte(CP_UTF8) failed");
  }
  std::string out(static_cast<size_t>(len), '\0');
  const int rc =
      ::WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), out.data(), len, nullptr, nullptr);
  if (rc <= 0) {
    throw_win_error("WideCharToMultiByte(CP_UTF8) failed");
  }
  return out;
}

double bytes_to_mb(std::uint64_t bytes) {
  return static_cast<double>(bytes) / (1024.0 * 1024.0);
}

std::optional<std::uint64_t> query_working_set_bytes(HANDLE process_handle) {
  PROCESS_MEMORY_COUNTERS_EX pmc{};
  pmc.cb = sizeof(pmc);
  if (!::GetProcessMemoryInfo(process_handle, reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&pmc), sizeof(pmc))) {
    return std::nullopt;
  }
  return static_cast<std::uint64_t>(pmc.WorkingSetSize);
}

bool trim_working_set(int pid) {
  const DWORD access = PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION;
  ScopedHandle h(::OpenProcess(access, FALSE, static_cast<DWORD>(pid)));
  if (!h) {
    return false;
  }
  return ::EmptyWorkingSet(h.get()) != 0;
}

std::vector<std::string> trim_targets(std::string process_name, py::object threshold_mb_obj) {
  std::optional<double> threshold_mb;
  if (!threshold_mb_obj.is_none()) {
    threshold_mb = threshold_mb_obj.cast<double>();
  }
  const std::wstring target_w = utf8_to_wide(process_name);
  py::gil_scoped_release release;

  ScopedHandle snap(::CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0));
  if (!snap) {
    throw_win_error("CreateToolhelp32Snapshot(PROCESS) failed");
  }

  PROCESSENTRY32W pe{};
  pe.dwSize = sizeof(pe);
  if (!::Process32FirstW(snap.get(), &pe)) {
    throw_win_error("Process32FirstW failed");
  }

  std::vector<std::string> logs;
  do {
    const std::wstring_view exe_w(pe.szExeFile);
    if (::CompareStringOrdinal(exe_w.data(), static_cast<int>(exe_w.size()), target_w.data(),
                               static_cast<int>(target_w.size()), TRUE) != CSTR_EQUAL) {
      continue;
    }

    const int pid = static_cast<int>(pe.th32ProcessID);
    const DWORD access = PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION;
    ScopedHandle proc(::OpenProcess(access, FALSE, static_cast<DWORD>(pid)));
    if (!proc) {
      std::ostringstream oss;
      oss << "[FAIL] Could not trim " << process_name << " (PID " << pid << ")";
      logs.push_back(oss.str());
      continue;
    }

    const auto before_bytes = query_working_set_bytes(proc.get());
    if (!before_bytes.has_value()) {
      std::ostringstream oss;
      oss << "[WARN] OSError while scanning processes: GetProcessMemoryInfo failed (PID " << pid << ")";
      logs.push_back(oss.str());
      continue;
    }

    const double before_mb = bytes_to_mb(*before_bytes);
    if (threshold_mb.has_value() && before_mb < *threshold_mb) {
      std::ostringstream oss;
      oss << "[SKIP] " << process_name << " (PID " << pid << ") " << std::fixed << std::setprecision(1) << before_mb
          << " MB (< " << std::fixed << std::setprecision(1) << *threshold_mb << " MB threshold)";
      logs.push_back(oss.str());
      continue;
    }

    if (::EmptyWorkingSet(proc.get()) == 0) {
      std::ostringstream oss;
      oss << "[FAIL] Could not trim " << process_name << " (PID " << pid << ")";
      logs.push_back(oss.str());
      continue;
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    const auto after_bytes = query_working_set_bytes(proc.get());
    const double after_mb = after_bytes.has_value() ? bytes_to_mb(*after_bytes) : before_mb;
    const double freed_mb = before_mb - after_mb;

    std::ostringstream oss;
    oss << "[OK]   " << process_name << " (PID " << pid << "): " << std::fixed << std::setprecision(1) << before_mb
        << " -> " << std::fixed << std::setprecision(1) << after_mb << " MB (freed " << std::fixed
        << std::setprecision(1) << freed_mb << " MB)";
    logs.push_back(oss.str());
  } while (::Process32NextW(snap.get(), &pe));

  const DWORD err = ::GetLastError();
  if (err != ERROR_NO_MORE_FILES) {
    throw_win_error("Process32NextW failed");
  }

  return logs;
}

}  // namespace

PYBIND11_MODULE(ram_limiter_native, m) {
  m.doc() = "Native Windows RAM limiter helpers (EmptyWorkingSet).";
  m.def("trim_working_set", &trim_working_set, py::arg("pid"),
        "Call EmptyWorkingSet(pid). Returns True on success.");
  m.def("trim_targets", &trim_targets, py::arg("process_name"), py::arg("threshold_mb") = py::none(),
        "Trim all processes by name; returns log lines.");
}
