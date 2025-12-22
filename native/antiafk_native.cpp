#ifdef _WIN32
#  define NOMINMAX
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  error "antiafk_native is Windows-only."
#endif

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

constexpr int kActionDelayMs = 30;
constexpr int kAltDelayMs = 400;
constexpr int kMaxWaitTimeS = 1140;
constexpr int kActivityStatusEveryS = 30;
constexpr int kNoWindowsStatusEveryS = 10;
constexpr int kLoopIdleWaitS = 1;
constexpr int kNoWindowsWaitS = 5;
constexpr int kUserInactivityWaitS = 5;
constexpr DWORD kWindowTextTimeoutMs = 100;

using Clock = std::chrono::steady_clock;

std::int64_t mono_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
}

bool contains(std::wstring_view haystack, std::wstring_view needle) {
  return haystack.find(needle) != std::wstring_view::npos;
}

std::string wide_to_utf8(std::wstring_view w) {
  if (w.empty()) {
    return {};
  }
  const int size = ::WideCharToMultiByte(
      CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
  if (size <= 0) {
    return {};
  }
  std::string out;
  out.resize(static_cast<size_t>(size));
  ::WideCharToMultiByte(
      CP_UTF8, 0, w.data(), static_cast<int>(w.size()), out.data(), size, nullptr, nullptr);
  return out;
}

std::wstring window_text(HWND hwnd, bool* timed_out = nullptr) {
  if (timed_out) {
    *timed_out = false;
  }
  if (!hwnd || !::IsWindow(hwnd)) {
    return {};
  }

  const UINT flags = SMTO_ABORTIFHUNG | SMTO_BLOCK;
  bool timeout_hit = false;
  const auto log_timeout = [&](const wchar_t* stage) {
    std::wostringstream oss;
    oss << L"[AntiAFK] window title read timed out (" << stage << L") for hwnd=" << hwnd << L"\n";
    ::OutputDebugStringW(oss.str().c_str());
  };

  DWORD_PTR len = 0;
  const LRESULT len_res =
      ::SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, flags, kWindowTextTimeoutMs, &len);
  if (len_res == 0) {
    timeout_hit = (::GetLastError() == ERROR_TIMEOUT);
    if (timeout_hit) {
      log_timeout(L"WM_GETTEXTLENGTH");
    }
    if (timed_out) {
      *timed_out = timeout_hit;
    }
    return {};
  }
  if (len == 0) {
    return {};
  }

  std::wstring out;
  out.resize(static_cast<size_t>(len) + 1);

  DWORD_PTR written = 0;
  const LRESULT text_res = ::SendMessageTimeoutW(hwnd,
                                                 WM_GETTEXT,
                                                 static_cast<WPARAM>(out.size()),
                                                 reinterpret_cast<LPARAM>(out.data()),
                                                 flags,
                                                 kWindowTextTimeoutMs,
                                                 &written);
  if (text_res == 0) {
    timeout_hit = timeout_hit || (::GetLastError() == ERROR_TIMEOUT);
    if (timeout_hit) {
      log_timeout(L"WM_GETTEXT");
    }
    if (timed_out) {
      *timed_out = timeout_hit;
    }
    return {};
  }

  const size_t actual = std::min(static_cast<size_t>(written), out.size());
  if (actual == 0) {
    return {};
  }

  out.resize(actual);

  if (timeout_hit) {
    log_timeout(L"WM_GETTEXT");
  }
  if (timed_out) {
    *timed_out = timeout_hit;
  }
  return out;
}

std::wstring window_class_name(HWND hwnd) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return {};
  }
  wchar_t buf[256]{};
  const int written = ::GetClassNameW(hwnd, buf, static_cast<int>(sizeof(buf) / sizeof(buf[0])));
  if (written <= 0) {
    return {};
  }
  return std::wstring(buf, static_cast<size_t>(written));
}

std::wstring basename_from_path(std::wstring_view path) {
  const size_t pos = path.find_last_of(L"\\/");
  if (pos == std::wstring_view::npos) {
    return std::wstring(path);
  }
  return std::wstring(path.substr(pos + 1));
}

std::optional<std::wstring> process_basename(DWORD pid) {
  if (pid == 0) {
    return std::nullopt;
  }
  HANDLE h = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
  if (!h) {
    return std::nullopt;
  }

  std::wstring buf;
  buf.resize(32768);
  DWORD size = static_cast<DWORD>(buf.size());
  const BOOL ok = ::QueryFullProcessImageNameW(h, 0, buf.data(), &size);
  ::CloseHandle(h);

  if (!ok || size == 0 || size >= buf.size()) {
    return std::nullopt;
  }
  buf.resize(static_cast<size_t>(size));
  return basename_from_path(buf);
}

bool iequals_ascii(std::wstring_view a, std::wstring_view b) {
  if (a.size() != b.size()) {
    return false;
  }
  for (size_t i = 0; i < a.size(); ++i) {
    wchar_t ca = a[i];
    wchar_t cb = b[i];
    if (ca >= L'A' && ca <= L'Z') {
      ca = static_cast<wchar_t>(ca - L'A' + L'a');
    }
    if (cb >= L'A' && cb <= L'Z') {
      cb = static_cast<wchar_t>(cb - L'A' + L'a');
    }
    if (ca != cb) {
      return false;
    }
  }
  return true;
}

bool is_roblox_process(DWORD pid) {
  const auto base = process_basename(pid);
  if (!base) {
    return false;
  }
  return iequals_ascii(*base, L"RobloxPlayerBeta.exe");
}

bool is_valid_roblox_title_for_action(std::wstring_view title) {
  if (title.empty()) {
    // Fail-open on unknown titles to avoid excluding valid Roblox windows.
    return true;
  }
  if (!contains(title, L"Roblox")) {
    return false;
  }
  if (contains(title, L"MSCTFIME") || contains(title, L"Default IME")) {
    return false;
  }
  return true;
}

bool is_valid_roblox_title_for_find(std::wstring_view title) {
  if (!is_valid_roblox_title_for_action(title)) {
    return false;
  }
  if (contains(title, L"NVIDIA")) {
    return false;
  }
  return true;
}

void clear_notopmost(HWND hwnd) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return;
  }
  ::SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
}

void set_topmost(HWND hwnd) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return;
  }
  ::SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
}

struct ThreadInputAttachGuard {
  DWORD a{0};
  DWORD b{0};
  bool attached{false};

  ThreadInputAttachGuard() = default;
  ThreadInputAttachGuard(DWORD a_, DWORD b_) : a(a_), b(b_) {
    if (a != 0 && b != 0) {
      attached = ::AttachThreadInput(a, b, TRUE) != FALSE;
    }
  }
  ThreadInputAttachGuard(const ThreadInputAttachGuard&) = delete;
  ThreadInputAttachGuard& operator=(const ThreadInputAttachGuard&) = delete;
  ThreadInputAttachGuard(ThreadInputAttachGuard&&) = delete;
  ThreadInputAttachGuard& operator=(ThreadInputAttachGuard&&) = delete;

  ~ThreadInputAttachGuard() {
    if (attached) {
      ::AttachThreadInput(a, b, FALSE);
    }
  }
};

#ifndef LSFW_LOCK
#  define LSFW_LOCK 1
#endif
#ifndef LSFW_UNLOCK
#  define LSFW_UNLOCK 2
#endif

struct ForegroundLockGuard {
  bool locked{false};

  ForegroundLockGuard() { locked = ::LockSetForegroundWindow(LSFW_LOCK) != FALSE; }
  ForegroundLockGuard(const ForegroundLockGuard&) = delete;
  ForegroundLockGuard& operator=(const ForegroundLockGuard&) = delete;
  ForegroundLockGuard(ForegroundLockGuard&&) = delete;
  ForegroundLockGuard& operator=(ForegroundLockGuard&&) = delete;

  ~ForegroundLockGuard() {
    if (locked) {
      (void)::LockSetForegroundWindow(LSFW_UNLOCK);
    }
  }
};

bool should_abort(const std::atomic<bool>* stop_requested, const std::atomic<bool>* shutdown_requested) {
  return (stop_requested && stop_requested->load()) ||
         (shutdown_requested && shutdown_requested->load());
}

void key_event_down(int vk_code) {
  const UINT scan = ::MapVirtualKeyW(static_cast<UINT>(vk_code), MAPVK_VK_TO_VSC);
  ::keybd_event(static_cast<BYTE>(vk_code), static_cast<BYTE>(scan), 0, 0);
}

void key_event_up(int vk_code) {
  const UINT scan = ::MapVirtualKeyW(static_cast<UINT>(vk_code), MAPVK_VK_TO_VSC);
  ::keybd_event(static_cast<BYTE>(vk_code), static_cast<BYTE>(scan), KEYEVENTF_KEYUP, 0);
}

void sleep_abortable(int delay_ms,
                     const std::atomic<bool>* stop_requested,
                     const std::atomic<bool>* shutdown_requested) {
  constexpr int kSliceMs = 25;
  int remaining = delay_ms;
  while (remaining > 0 && !should_abort(stop_requested, shutdown_requested)) {
    const int slice = std::min(kSliceMs, remaining);
    ::Sleep(static_cast<DWORD>(slice));
    remaining -= slice;
  }
}

bool focus_window_best_effort(HWND hwnd, DWORD window_tid) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return false;
  }
  if (::GetForegroundWindow() == hwnd) {
    return true;
  }

  const DWORD current_tid = ::GetCurrentThreadId();
  if (window_tid != 0) {
    ThreadInputAttachGuard attach(current_tid, window_tid);
    (void)::BringWindowToTop(hwnd);
    (void)::SetForegroundWindow(hwnd);
  } else {
    (void)::SetForegroundWindow(hwnd);
  }

  if (::GetForegroundWindow() == hwnd) {
    return true;
  }

  (void)::SetForegroundWindow(hwnd);
  return ::GetForegroundWindow() == hwnd;
}

bool ensure_foreground(HWND hwnd,
                       DWORD window_tid,
                       int attempts,
                       int sleep_ms,
                       const std::atomic<bool>* stop_requested,
                       const std::atomic<bool>* shutdown_requested) {
  for (int i = 0; i < attempts; ++i) {
    if (should_abort(stop_requested, shutdown_requested)) {
      return false;
    }
    if (focus_window_best_effort(hwnd, window_tid)) {
      return true;
    }
    ::Sleep(static_cast<DWORD>(sleep_ms));
  }
  return ::GetForegroundWindow() == hwnd;
}

bool press_key_defensive(HWND hwnd,
                         DWORD window_tid,
                         int vk_code,
                         int delay_ms,
                         const std::atomic<bool>* stop_requested,
                         const std::atomic<bool>* shutdown_requested) {
  constexpr int kFocusAttempts = 8;
  constexpr int kFocusSleepMs = 20;

  if (!ensure_foreground(hwnd, window_tid, kFocusAttempts, kFocusSleepMs, stop_requested, shutdown_requested)) {
    return false;
  }

  key_event_down(vk_code);
  sleep_abortable(delay_ms, stop_requested, shutdown_requested);

  // Best-effort: try to ensure the window is still foreground before releasing.
  (void)ensure_foreground(hwnd, window_tid, 2, kFocusSleepMs, stop_requested, shutdown_requested);
  key_event_up(vk_code);
  return true;
}

struct ActionResult {
  bool ok{false};
  std::string message;
};

ActionResult action_task_impl(HWND hwnd,
                              const std::string& base_action,
                              bool menu_autoreconnect,
                              bool in_menu,
                              std::atomic<bool>* stop_requested = nullptr,
                              std::atomic<bool>* shutdown_requested = nullptr) {
  ActionResult out;
  const std::uint64_t hwnd_int = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(hwnd));
  const std::string effective_action = (menu_autoreconnect && in_menu) ? "AutoReconnect" : base_action;

  const auto aborting = [&]() -> bool {
    return (stop_requested && stop_requested->load()) || (shutdown_requested && shutdown_requested->load());
  };

  HWND old_hwnd = nullptr;
  struct Cleanup {
    HWND hwnd{nullptr};
    HWND old_hwnd{nullptr};
    bool clear_old_hwnd{false};
    ~Cleanup() {
      clear_notopmost(hwnd);
      if (clear_old_hwnd) {
        clear_notopmost(old_hwnd);
      }
    }
  } cleanup{hwnd, nullptr, false};

  if (!hwnd || !::IsWindow(hwnd)) {
    out.ok = false;
    out.message = "Window " + std::to_string(hwnd_int) + " is not a valid window";
    return out;
  }

  DWORD window_pid = 0;
  const DWORD window_tid = ::GetWindowThreadProcessId(hwnd, &window_pid);

  bool title_timed_out = false;
  const std::wstring wtitle = window_text(hwnd, &title_timed_out);
  (void)title_timed_out;
  const std::string utf8_title = wtitle.empty() ? std::string("<unknown>") : wide_to_utf8(wtitle);
  const bool process_ok = is_roblox_process(window_pid);
  if (!process_ok) {
    out.ok = false;
    out.message = "Window '" + utf8_title + "' is not a Roblox process";
    return out;
  }

  if (!is_valid_roblox_title_for_action(wtitle)) {
    out.ok = false;
    out.message = "Window '" + utf8_title + "' is not a valid Roblox window";
    return out;
  }

  old_hwnd = ::GetForegroundWindow();
  const BOOL was_minimized = ::IsIconic(hwnd);

  try {
    ForegroundLockGuard fg_lock;

    if (was_minimized) {
      ::ShowWindow(hwnd, SW_RESTORE);
    }

    if (!ensure_foreground(hwnd, window_tid, /*attempts=*/10, /*sleep_ms=*/20, stop_requested, shutdown_requested)) {
      out.ok = false;
      out.message = "Failed to focus target window for Anti-AFK action";
      return out;
    }

    ::Sleep(static_cast<DWORD>(kActionDelayMs));

    const auto press_key = [&](int vk_code, int delay_ms) {
      return press_key_defensive(hwnd, window_tid, vk_code, delay_ms, stop_requested, shutdown_requested);
    };

    if (effective_action == "space") {
      if (!press_key(VK_SPACE, kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (space)";
        return out;
      }
    } else if (effective_action == "ws") {
      if (!press_key(static_cast<int>('W'), kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (W)";
        return out;
      }
      ::Sleep(static_cast<DWORD>(kAltDelayMs));
      if (!press_key(static_cast<int>('S'), kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (S)";
        return out;
      }
    } else if (effective_action == "zoom") {
      if (!press_key(static_cast<int>('I'), kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (I)";
        return out;
      }
      ::Sleep(static_cast<DWORD>(kAltDelayMs));
      if (!press_key(static_cast<int>('O'), kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (O)";
        return out;
      }
    } else if (effective_action == "AutoReconnect") {
      const int vk_backslash = ::VkKeyScanA('\\') & 0xFF;
      if (!press_key(vk_backslash, kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (\\\\)";
        return out;
      }
      ::Sleep(static_cast<DWORD>(kAltDelayMs));

      const int vk_a = ::VkKeyScanA('a') & 0xFF;
      if (!press_key(vk_a, kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (a)";
        return out;
      }
      ::Sleep(static_cast<DWORD>(kAltDelayMs));

      if (!press_key(static_cast<int>('S'), kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (S)";
        return out;
      }

      if (!press_key(VK_RETURN, kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (Enter)";
        return out;
      }
      ::Sleep(static_cast<DWORD>(kAltDelayMs));

      if (!press_key(vk_backslash, kAltDelayMs)) {
        out.ok = false;
        out.message = "Lost focus before key press (\\\\)";
        return out;
      }
    } else {
      out.ok = false;
      out.message = "Unknown Anti-AFK action: " + effective_action;
      return out;
    }

    ::Sleep(static_cast<DWORD>(kActionDelayMs));

    if (was_minimized) {
      ::ShowWindow(hwnd, SW_MINIMIZE);
    }

    if (!aborting() && old_hwnd && old_hwnd != hwnd && ::IsWindow(old_hwnd)) {
      if (::IsWindowVisible(old_hwnd) && !::IsIconic(old_hwnd)) {
        const std::wstring cls = window_class_name(old_hwnd);
        if (cls != L"AntiAFK-RBX-tray") {
          ::ShowWindow(old_hwnd, SW_SHOW);
          if (!aborting()) {
            set_topmost(old_hwnd);
            cleanup.old_hwnd = old_hwnd;
            cleanup.clear_old_hwnd = true;
            const DWORD current_thread = ::GetCurrentThreadId();
            DWORD fg_pid = 0;
            const DWORD fg_tid = ::GetWindowThreadProcessId(old_hwnd, &fg_pid);
            if (fg_tid != 0) {
              ThreadInputAttachGuard attach(current_thread, fg_tid);
              (void)::BringWindowToTop(old_hwnd);
              (void)::SetForegroundWindow(old_hwnd);
            } else {
              (void)::SetForegroundWindow(old_hwnd);
            }
            clear_notopmost(old_hwnd);
            cleanup.clear_old_hwnd = false;
          }
        }
      }
    }

    out.ok = true;
    out.message = "Performed " + effective_action + " action on '" + utf8_title + "'";
    return out;
  } catch (const std::exception& e) {
    if (was_minimized) {
      ::ShowWindow(hwnd, SW_MINIMIZE);
    }
    if (!aborting() && old_hwnd && ::IsWindow(old_hwnd)) {
      (void)::SetForegroundWindow(old_hwnd);
    }
    out.ok = false;
    out.message =
        "Error performing anti-AFK action on '" + utf8_title + "': " + std::string(e.what());
    return out;
  } catch (...) {
    if (was_minimized) {
      ::ShowWindow(hwnd, SW_MINIMIZE);
    }
    if (!aborting() && old_hwnd && ::IsWindow(old_hwnd)) {
      (void)::SetForegroundWindow(old_hwnd);
    }
    out.ok = false;
    out.message = "Error performing anti-AFK action on '" + utf8_title + "': unknown error";
    return out;
  }
}

py::tuple action_task(std::uint64_t hwnd_int,
                      const std::string& base_action,
                      bool menu_autoreconnect,
                      bool in_menu) {
  ActionResult res;
  {
    py::gil_scoped_release release;
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
    res = action_task_impl(hwnd, base_action, menu_autoreconnect, in_menu);
  }
  return py::make_tuple(res.ok, res.message);
}

struct FindEnumData {
  bool include_hidden{true};
  std::vector<std::uint64_t>* out{nullptr};
};

BOOL CALLBACK enum_find_roblox_windows(HWND hwnd, LPARAM lparam) {
  auto* data = reinterpret_cast<FindEnumData*>(lparam);
  if (!data || !data->out) {
    return TRUE;
  }
  if (!data->include_hidden && !::IsWindowVisible(hwnd)) {
    return TRUE;
  }

  DWORD pid = 0;
  (void)::GetWindowThreadProcessId(hwnd, &pid);
  if (!is_roblox_process(pid)) {
    return TRUE;
  }

  std::wstring title = window_text(hwnd);
  if (!is_valid_roblox_title_for_find(title)) {
    return TRUE;
  }

  data->out->push_back(static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(hwnd)));
  return TRUE;
}

std::vector<std::uint64_t> find_roblox_windows_impl(bool include_hidden) {
  std::vector<std::uint64_t> out;
  FindEnumData data;
  data.include_hidden = include_hidden;
  data.out = &out;
  ::EnumWindows(enum_find_roblox_windows, reinterpret_cast<LPARAM>(&data));
  return out;
}

std::vector<std::uint64_t> find_roblox_windows(bool include_hidden) {
  py::gil_scoped_release release;
  return find_roblox_windows_impl(include_hidden);
}

struct ClearEnumData {
  int count{0};
};

BOOL CALLBACK enum_clear_notopmost(HWND hwnd, LPARAM lparam) {
  auto* data = reinterpret_cast<ClearEnumData*>(lparam);
  if (!data) {
    return TRUE;
  }
  DWORD pid = 0;
  (void)::GetWindowThreadProcessId(hwnd, &pid);
  if (!is_roblox_process(pid)) {
    return TRUE;
  }
  clear_notopmost(hwnd);
  data->count += 1;
  return TRUE;
}

int clear_notopmost_all_roblox_windows_impl() {
  ClearEnumData data;
  ::EnumWindows(enum_clear_notopmost, reinterpret_cast<LPARAM>(&data));
  return data.count;
}

int clear_notopmost_all_roblox_windows() {
  py::gil_scoped_release release;
  return clear_notopmost_all_roblox_windows_impl();
}

class AntiAFK {
 public:
  AntiAFK(py::object parent, py::object config_obj);
  ~AntiAFK();

  AntiAFK(const AntiAFK&) = delete;
  AntiAFK& operator=(const AntiAFK&) = delete;

  py::object status_callback = py::none();
  py::object button_state_callback = py::none();
  py::object is_pid_in_menu_callback = py::none();

  py::dict config;

  bool antiafk_running() const;

  void update_status(const std::string& message);
  void update_button_states();
  void log_error(py::object exception, py::object message_obj = py::none());

  void apply_host_config(std::optional<bool> multi_instance_enabled = std::nullopt,
                         std::optional<int> interval = std::nullopt,
                         std::optional<std::string> action = std::nullopt,
                         std::optional<bool> user_safe = std::nullopt,
                         std::optional<bool> sequential_mode = std::nullopt,
                         std::optional<double> sequential_delay = std::nullopt,
                         std::optional<bool> menu_autoreconnect = std::nullopt);

  void toggle_antiafk(py::object enable_obj = py::none());
  void start_antiafk();
  void stop_antiafk();
  bool pause_antiafk(bool wait = true);
  bool resume_antiafk();

  bool enable_multi_instance();
  bool disable_multi_instance();
  void toggle_multi_instance();

  std::vector<std::uint64_t> find_roblox_windows(bool include_hidden = true) const;
  void show_roblox_windows();
  void hide_roblox_windows();

  bool perform_antiafk_action(std::uint64_t hwnd_int,
                             std::optional<std::string> action_type = std::nullopt);
  void test_action();
  void test_action_with_delay();

  void start_activity_monitor();
  void stop_activity_monitor();
  bool check_user_active();

  bool is_window_fullscreen(std::uint64_t hwnd_int);
  void restore_foreground_window(std::uint64_t hwnd_int);

  void shutdown();

 private:
  static double clamp_delay(double d);
  void ensure_defaults_locked();

  int get_int(const char* key, int fallback) const;
  bool get_bool(const char* key, bool fallback) const;
  double get_double(const char* key, double fallback) const;
  std::string get_str(const char* key, std::string fallback) const;

  void emit_status(const std::string& message);
  void emit_button_state(bool running);

  bool call_is_pid_in_menu(DWORD pid);
  bool wait_for_stop_or(std::chrono::milliseconds d);
  bool wait_for_stop_or_pause(std::chrono::milliseconds d);
  bool wait_for_shutdown_or(std::chrono::milliseconds d);

  void cleanup_topmost_after_error(const std::string& reason, bool emit);
  void restore_foreground_window_impl(HWND hwnd);
  ActionResult run_action(HWND hwnd, const std::string& base_action, bool menu_autoreconnect);

  void antiafk_loop_thread();
  void monitor_user_activity_thread();
  void run_test_thread(std::vector<std::uint64_t> windows);

  void shutdown_impl(bool emit);
  void shutdown_noexcept() noexcept;

  py::object parent_;

  std::atomic<bool> antiafk_running_{false};
  std::atomic<bool> stop_requested_{false};
  std::atomic<bool> pause_requested_{false};
  std::atomic<bool> paused_{false};
  std::atomic<bool> shutdown_requested_{false};

  std::atomic<bool> monitor_running_{false};
  std::atomic<bool> user_active_{false};
  std::atomic<std::int64_t> last_activity_ms_{0};

  std::atomic<bool> test_running_{false};

  mutable std::mutex cv_mutex_;
  std::condition_variable cv_;

  std::thread antiafk_thread_;
  std::thread monitor_thread_;
  std::thread test_thread_;

  HANDLE multi_instance_mutex_{nullptr};

  mutable std::mutex activity_check_mutex_;
  std::optional<POINT> last_check_pos_;
  std::optional<std::uintptr_t> last_check_foreground_;
};

double AntiAFK::clamp_delay(double d) {
  if (d < 0.1) {
    return 0.1;
  }
  if (d > 5.0) {
    return 5.0;
  }
  return d;
}

void AntiAFK::ensure_defaults_locked() {
  py::gil_scoped_acquire acquire;

  auto set_if_missing = [&](const char* key, py::object value) {
    const py::str k(key);
    if (!config.contains(k)) {
      config[k] = std::move(value);
    }
  };

  set_if_missing("antiafk_enabled", py::bool_(false));
  set_if_missing("multi_instance_enabled", py::bool_(true));
  set_if_missing("antiafk_interval", py::int_(120));
  set_if_missing("antiafk_action", py::str("space"));
  set_if_missing("antiafk_user_safe", py::bool_(false));
  set_if_missing("antiafk_dev_mode", py::bool_(false));
  set_if_missing("antiafk_sequential_mode", py::bool_(false));
  set_if_missing("antiafk_sequential_delay", py::float_(0.75));
  set_if_missing("antiafk_menu_autoreconnect", py::bool_(false));
}

int AntiAFK::get_int(const char* key, int fallback) const {
  py::gil_scoped_acquire acquire;
  try {
    py::object v = config.attr("get")(py::str(key), py::int_(fallback));
    return py::cast<int>(v);
  } catch (...) {
    PyErr_Clear();
    return fallback;
  }
}

bool AntiAFK::get_bool(const char* key, bool fallback) const {
  py::gil_scoped_acquire acquire;
  try {
    py::object v = config.attr("get")(py::str(key), py::bool_(fallback));
    return py::cast<bool>(v);
  } catch (...) {
    PyErr_Clear();
    return fallback;
  }
}

double AntiAFK::get_double(const char* key, double fallback) const {
  py::gil_scoped_acquire acquire;
  try {
    py::object v = config.attr("get")(py::str(key), py::float_(fallback));
    return py::cast<double>(v);
  } catch (...) {
    PyErr_Clear();
    return fallback;
  }
}

std::string AntiAFK::get_str(const char* key, std::string fallback) const {
  py::gil_scoped_acquire acquire;
  try {
    py::object v = config.attr("get")(py::str(key), py::str(fallback));
    return py::cast<std::string>(v);
  } catch (...) {
    PyErr_Clear();
    return fallback;
  }
}

void AntiAFK::emit_status(const std::string& message) {
  py::gil_scoped_acquire acquire;
  if (!status_callback.is_none()) {
    try {
      status_callback(py::str(message));
    } catch (...) {
      PyErr_Clear();
    }
  }
}

void AntiAFK::emit_button_state(bool running) {
  py::gil_scoped_acquire acquire;
  if (!button_state_callback.is_none()) {
    try {
      button_state_callback(py::bool_(running));
    } catch (...) {
      PyErr_Clear();
    }
  }
}

bool AntiAFK::call_is_pid_in_menu(DWORD pid) {
  py::gil_scoped_acquire acquire;
  if (is_pid_in_menu_callback.is_none()) {
    return false;
  }
  try {
    py::object out = is_pid_in_menu_callback(py::int_(static_cast<int>(pid)));
    if (out.is_none()) {
      return false;
    }
    return py::cast<bool>(out);
  } catch (...) {
    PyErr_Clear();
    return false;
  }
}

bool AntiAFK::wait_for_stop_or(std::chrono::milliseconds d) {
  std::unique_lock<std::mutex> lk(cv_mutex_);
  return cv_.wait_for(lk, d, [&] { return stop_requested_.load() || shutdown_requested_.load(); });
}

bool AntiAFK::wait_for_stop_or_pause(std::chrono::milliseconds d) {
  std::unique_lock<std::mutex> lk(cv_mutex_);
  (void)cv_.wait_for(lk, d, [&] {
    return stop_requested_.load() || shutdown_requested_.load() || pause_requested_.load();
  });
  return stop_requested_.load() || shutdown_requested_.load();
}

bool AntiAFK::wait_for_shutdown_or(std::chrono::milliseconds d) {
  std::unique_lock<std::mutex> lk(cv_mutex_);
  return cv_.wait_for(lk, d, [&] { return shutdown_requested_.load(); });
}

void AntiAFK::cleanup_topmost_after_error(const std::string& reason, bool emit) {
  const int processed = clear_notopmost_all_roblox_windows_impl();
  if (emit && !reason.empty()) {
    emit_status("Topmost cleanup (" + reason + "): processed " + std::to_string(processed) +
                " Roblox window(s).");
  }
}

void AntiAFK::restore_foreground_window_impl(HWND hwnd) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return;
  }
  const std::wstring cls = window_class_name(hwnd);
  if (cls == L"AntiAFK-RBX-tray") {
    return;
  }
  if (!::IsWindowVisible(hwnd) || ::IsIconic(hwnd)) {
    return;
  }

  ::ShowWindow(hwnd, SW_SHOW);
  set_topmost(hwnd);
  const DWORD current_tid = ::GetCurrentThreadId();
  DWORD pid = 0;
  const DWORD tid = ::GetWindowThreadProcessId(hwnd, &pid);
  if (tid != 0) {
    ThreadInputAttachGuard attach(current_tid, tid);
    (void)::BringWindowToTop(hwnd);
    (void)::SetForegroundWindow(hwnd);
  } else {
    (void)::SetForegroundWindow(hwnd);
  }
  clear_notopmost(hwnd);
}

ActionResult AntiAFK::run_action(HWND hwnd, const std::string& base_action, bool menu_autoreconnect) {
  bool in_menu = false;
  if (menu_autoreconnect && !is_pid_in_menu_callback.is_none()) {
    DWORD pid = 0;
    (void)::GetWindowThreadProcessId(hwnd, &pid);
    if (pid != 0) {
      in_menu = call_is_pid_in_menu(pid);
    }
  }
  return action_task_impl(hwnd, base_action, menu_autoreconnect, in_menu, &stop_requested_, &shutdown_requested_);
}

AntiAFK::AntiAFK(py::object parent, py::object config_obj) : parent_(std::move(parent)) {
  py::gil_scoped_acquire acquire;
  if (config_obj.is_none()) {
    config = py::dict();
  } else {
    config = py::cast<py::dict>(config_obj);
  }
  ensure_defaults_locked();
  if (get_bool("antiafk_user_safe", false)) {
    start_activity_monitor();
  }
}

AntiAFK::~AntiAFK() { shutdown_noexcept(); }

bool AntiAFK::antiafk_running() const { return antiafk_running_.load(); }

void AntiAFK::update_status(const std::string& message) { emit_status(message); }

void AntiAFK::update_button_states() { emit_button_state(antiafk_running_.load()); }

void AntiAFK::log_error(py::object exception, py::object message_obj) {
  py::gil_scoped_acquire acquire;
  try {
    if (!parent_.is_none() && py::hasattr(parent_, "error_logging")) {
      parent_.attr("error_logging")(exception, message_obj);
    }
  } catch (...) {
    PyErr_Clear();
  }

  std::string msg;
  try {
    if (!message_obj.is_none()) {
      msg = py::cast<std::string>(py::str(message_obj));
    } else {
      msg = py::cast<std::string>(py::str(exception));
    }
  } catch (...) {
    PyErr_Clear();
    msg = "Unknown error";
  }
  emit_status("Error: " + msg);
}

void AntiAFK::apply_host_config(std::optional<bool> multi_instance_enabled,
                                std::optional<int> interval,
                                std::optional<std::string> action,
                                std::optional<bool> user_safe,
                                std::optional<bool> sequential_mode,
                                std::optional<double> sequential_delay,
                                std::optional<bool> menu_autoreconnect) {
  py::gil_scoped_acquire acquire;
  if (multi_instance_enabled.has_value()) {
    config[py::str("multi_instance_enabled")] = py::bool_(*multi_instance_enabled);
  }
  if (interval.has_value()) {
    config[py::str("antiafk_interval")] = py::int_(*interval);
  }
  if (action.has_value()) {
    config[py::str("antiafk_action")] = py::str(*action);
  }
  if (user_safe.has_value()) {
    config[py::str("antiafk_user_safe")] = py::bool_(*user_safe);
  }
  if (sequential_mode.has_value()) {
    config[py::str("antiafk_sequential_mode")] = py::bool_(*sequential_mode);
  }
  if (sequential_delay.has_value()) {
    config[py::str("antiafk_sequential_delay")] = py::float_(clamp_delay(*sequential_delay));
  }
  if (menu_autoreconnect.has_value()) {
    config[py::str("antiafk_menu_autoreconnect")] = py::bool_(*menu_autoreconnect);
  }

  if (get_bool("antiafk_user_safe", false)) {
    start_activity_monitor();
  } else {
    stop_activity_monitor();
  }

  std::ostringstream oss;
  oss << "Configuration updated: Interval=" << get_int("antiafk_interval", 120)
      << "s, Action=" << get_str("antiafk_action", "space");
  emit_status(oss.str());
}

void AntiAFK::toggle_antiafk(py::object enable_obj) {
  py::gil_scoped_acquire acquire;
  if (enable_obj.is_none()) {
    emit_status("Error: toggle_antiafk called without explicit enable state");
    return;
  }

  bool enable = false;
  try {
    enable = py::cast<bool>(enable_obj);
  } catch (...) {
    PyErr_Clear();
    emit_status("Error: toggle_antiafk received invalid enable state");
    return;
  }

  config[py::str("antiafk_enabled")] = py::bool_(enable);

  try {
    if (!parent_.is_none() && py::hasattr(parent_, "save_state")) {
      parent_.attr("save_state")();
    }
  } catch (...) {
    PyErr_Clear();
  }

  if (enable) {
    if (!antiafk_running_.load()) {
      start_antiafk();
    }
  } else {
    if (antiafk_running_.load()) {
      stop_antiafk();
    }
  }

  emit_button_state(antiafk_running_.load());
}

void AntiAFK::start_antiafk() {
  if (shutdown_requested_.load()) {
    emit_status("Anti-AFK is shut down");
    return;
  }
  if (antiafk_running_.load()) {
    emit_status("Anti-AFK is already running");
    return;
  }

  stop_requested_.store(false);
  pause_requested_.store(false);
  paused_.store(false);
  antiafk_running_.store(true);

  antiafk_thread_ = std::thread([this] { antiafk_loop_thread(); });

  if (get_bool("antiafk_user_safe", false)) {
    start_activity_monitor();
  }

  emit_status("Anti-AFK started");
  emit_button_state(true);
}

void AntiAFK::stop_antiafk() {
  if (!antiafk_running_.load()) {
    emit_status("Anti-AFK is not running");
    return;
  }

  stop_requested_.store(true);
  pause_requested_.store(false);
  paused_.store(false);
  cv_.notify_all();

  std::thread t = std::move(antiafk_thread_);
  {
    py::gil_scoped_release release;
    if (t.joinable()) {
      t.join();
    }
  }

  antiafk_running_.store(false);
  stop_activity_monitor();
  cleanup_topmost_after_error("stop", /*emit=*/false);

  emit_status("Anti-AFK stopped");
  emit_button_state(false);
}

bool AntiAFK::pause_antiafk(bool wait) {
  if (!antiafk_running_.load()) {
    return false;
  }
  pause_requested_.store(true);
  cv_.notify_all();

  if (wait) {
    py::gil_scoped_release release;
    std::unique_lock<std::mutex> lk(cv_mutex_);
    (void)cv_.wait_for(lk, std::chrono::seconds(30), [&] {
      return paused_.load() || !antiafk_running_.load() || shutdown_requested_.load();
    });
    return paused_.load();
  }
  return true;
}

bool AntiAFK::resume_antiafk() {
  if (!antiafk_running_.load()) {
    return false;
  }
  pause_requested_.store(false);
  cv_.notify_all();
  return true;
}

bool AntiAFK::enable_multi_instance() {
  py::gil_scoped_release release;
  if (multi_instance_mutex_ != nullptr) {
    return true;
  }

  HANDLE h = ::CreateMutexW(nullptr, TRUE, L"ROBLOX_singletonEvent");
  if (!h) {
    const DWORD err = ::GetLastError();
    if (err == 6) {
      SECURITY_ATTRIBUTES sa{};
      sa.nLength = sizeof(sa);
      sa.lpSecurityDescriptor = nullptr;
      sa.bInheritHandle = TRUE;
      h = ::CreateMutexW(&sa, TRUE, L"ROBLOX_singletonEvent");
      if (!h) {
        h = ::CreateMutexW(nullptr, TRUE, L"ROBLOX_singletonMutex");
      }
    } else if (err == 183) {
      h = ::OpenMutexW(SYNCHRONIZE, FALSE, L"ROBLOX_singletonEvent");
    }
  }

  multi_instance_mutex_ = h;
  return multi_instance_mutex_ != nullptr;
}

bool AntiAFK::disable_multi_instance() {
  py::gil_scoped_release release;
  if (multi_instance_mutex_ != nullptr) {
    ::CloseHandle(multi_instance_mutex_);
    multi_instance_mutex_ = nullptr;
  }
  return true;
}

void AntiAFK::toggle_multi_instance() {
  {
    py::gil_scoped_acquire acquire;
    config[py::str("multi_instance_enabled")] = py::bool_(true);
  }
  const bool ok = enable_multi_instance();
  emit_status(ok ? "Multi-instance support enabled" : "Failed to enable multi-instance mutex");
}

std::vector<std::uint64_t> AntiAFK::find_roblox_windows(bool include_hidden) const {
  py::gil_scoped_release release;
  return find_roblox_windows_impl(include_hidden);
}

void AntiAFK::show_roblox_windows() {
  std::vector<std::uint64_t> windows;
  int visible_count = 0;
  {
    py::gil_scoped_release release;
    windows = find_roblox_windows_impl(true);
    for (std::uint64_t w : windows) {
      const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(w));
      if (!::IsWindow(hwnd)) {
        continue;
      }
      if (!::IsWindowVisible(hwnd) || ::IsIconic(hwnd)) {
        if (::IsIconic(hwnd)) {
          ::ShowWindow(hwnd, SW_RESTORE);
        }
        ::ShowWindow(hwnd, SW_SHOW);
        ::SetForegroundWindow(hwnd);
        visible_count += 1;
      }
    }
  }

  if (windows.empty()) {
    emit_status("No Roblox windows found");
    return;
  }
  emit_status("Showed " + std::to_string(visible_count) + " Roblox window(s)");
}

void AntiAFK::hide_roblox_windows() {
  std::vector<std::uint64_t> windows;
  {
    py::gil_scoped_release release;
    windows = find_roblox_windows_impl(false);
    for (std::uint64_t w : windows) {
      const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(w));
      if (!::IsWindow(hwnd)) {
        continue;
      }
      ::ShowWindow(hwnd, SW_HIDE);
    }
  }

  if (windows.empty()) {
    emit_status("No visible Roblox windows found");
    return;
  }
  emit_status("Hid " + std::to_string(windows.size()) + " Roblox window(s)");
}

bool AntiAFK::perform_antiafk_action(std::uint64_t hwnd_int, std::optional<std::string> action_type) {
  const std::string base_action = action_type.value_or(get_str("antiafk_action", "space"));
  const bool menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", false);

  ActionResult res;
  {
    py::gil_scoped_release release;
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
    res = run_action(hwnd, base_action, menu_autoreconnect);
  }

  if (!res.ok && !res.message.empty()) {
    emit_status(res.message);
  }
  return res.ok;
}

void AntiAFK::test_action() {
  const std::string action = get_str("antiafk_action", "space");
  std::vector<std::uint64_t> windows = find_roblox_windows(true);
  if (windows.empty()) {
    emit_status("No Roblox windows found for testing");
    return;
  }

  emit_status("Testing " + action + " action on " + std::to_string(windows.size()) +
              " Roblox window(s)...");

  const HWND old_hwnd = ::GetForegroundWindow();
  for (std::uint64_t w : windows) {
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(w));
    emit_status("Testing on window: '" + wide_to_utf8(window_text(hwnd)) + "' (handle: " +
                std::to_string(w) + ")");
    for (int j = 0; j < 3; ++j) {
      emit_status("Action attempt " + std::to_string(j + 1) + "/3...");
      if (!perform_antiafk_action(w, action)) {
        emit_status("Action failed, aborting remaining attempts");
        break;
      }
      (void)wait_for_shutdown_or(std::chrono::milliseconds(500));
    }
  }

  if (old_hwnd && ::IsWindow(old_hwnd)) {
    ::SetForegroundWindow(old_hwnd);
  }
  emit_status("Completed testing " + action + " action on all windows");
}

void AntiAFK::test_action_with_delay() {
  if (test_running_.load()) {
    emit_status("Anti-AFK test already running");
    return;
  }

  emit_status("Starting Anti-AFK test with detailed diagnostics...");

  struct WinInfo {
    HWND hwnd{nullptr};
    std::wstring title;
    DWORD pid{0};
    std::wstring pname;
  };

  std::vector<WinInfo> all;
  {
    py::gil_scoped_release release;
    ::EnumWindows(
        [](HWND hwnd, LPARAM lparam) -> BOOL {
          auto* vec = reinterpret_cast<std::vector<WinInfo>*>(lparam);
          if (!vec || !::IsWindow(hwnd)) {
            return TRUE;
          }
          DWORD pid = 0;
          (void)::GetWindowThreadProcessId(hwnd, &pid);
          std::wstring pname = process_basename(pid).value_or(L"unknown");

          bool title_timeout = false;
          std::wstring title = window_text(hwnd, &title_timeout);

          const bool is_roblox_proc = iequals_ascii(pname, L"RobloxPlayerBeta.exe");
          const bool title_mentions_roblox =
              (!title.empty() && title.find(L"Roblox") != std::wstring::npos);

          if (!is_roblox_proc && !title_mentions_roblox) {
            return TRUE;
          }

          vec->push_back(WinInfo{hwnd, std::move(title), pid, std::move(pname)});
          return TRUE;
        },
        reinterpret_cast<LPARAM>(&all));
  }

  if (all.empty()) {
    emit_status("No Roblox windows found for testing!");
    return;
  }

  emit_status("Found " + std::to_string(all.size()) + " Roblox window(s) (title read best-effort):");
  for (const auto& w : all) {
    const std::string utf8_title = w.title.empty() ? std::string("<unknown>") : wide_to_utf8(w.title);
    std::ostringstream oss;
    oss << "Window: '" << utf8_title << "' (handle: "
        << reinterpret_cast<std::uintptr_t>(w.hwnd) << ", process: " << wide_to_utf8(w.pname)
        << ", PID: " << w.pid << ")";
    emit_status(oss.str());
  }

  std::vector<std::uint64_t> roblox_windows;
  for (const auto& w : all) {
    if (!iequals_ascii(w.pname, L"RobloxPlayerBeta.exe")) {
      continue;
    }
    if (contains(w.title, L"MSCTFIME") || contains(w.title, L"Default IME")) {
      continue;
    }
    roblox_windows.push_back(static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(w.hwnd)));
  }

  if (roblox_windows.empty()) {
    emit_status("No main Roblox windows found after filtering!");
    return;
  }

  emit_status("Testing with " + std::to_string(roblox_windows.size()) +
              " main Roblox window(s) in 3 seconds...");

  if (test_thread_.joinable()) {
    std::thread old = std::move(test_thread_);
    py::gil_scoped_release release;
    old.join();
  }

  test_running_.store(true);
  test_thread_ = std::thread([this, windows = std::move(roblox_windows)]() mutable {
    run_test_thread(std::move(windows));
    test_running_.store(false);
  });
}

void AntiAFK::run_test_thread(std::vector<std::uint64_t> windows) {
  (void)wait_for_shutdown_or(std::chrono::seconds(3));
  if (shutdown_requested_.load()) {
    return;
  }

  const std::string action_type = get_str("antiafk_action", "space");
  const bool menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", false);

  const HWND old_hwnd = ::GetForegroundWindow();

  for (size_t i = 0; i < windows.size(); ++i) {
    if (shutdown_requested_.load()) {
      break;
    }
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(windows[i]));
    emit_status("Testing on window " + std::to_string(i + 1) + "/" + std::to_string(windows.size()) +
                ": '" + wide_to_utf8(window_text(hwnd)) + "'");
    emit_status("===== DIRECT METHOD WITH MapVirtualKey =====");
    const ActionResult res = run_action(hwnd, action_type, menu_autoreconnect);
    if (!res.ok && !res.message.empty()) {
      emit_status(res.message);
    }
    (void)wait_for_shutdown_or(std::chrono::milliseconds(500));
    emit_status("Test complete. Check if action was performed correctly.");
  }

  if (old_hwnd && ::IsWindow(old_hwnd)) {
    ::SetForegroundWindow(old_hwnd);
  }
}

void AntiAFK::start_activity_monitor() {
  if (monitor_running_.load() || shutdown_requested_.load()) {
    return;
  }
  monitor_running_.store(true);
  user_active_.store(false);
  last_activity_ms_.store(mono_ms());
  monitor_thread_ = std::thread([this] { monitor_user_activity_thread(); });
  emit_status("User activity monitoring started");
}

void AntiAFK::stop_activity_monitor() {
  if (!monitor_running_.load()) {
    return;
  }
  monitor_running_.store(false);
  cv_.notify_all();

  std::thread t = std::move(monitor_thread_);
  {
    py::gil_scoped_release release;
    if (t.joinable()) {
      t.join();
    }
  }
  emit_status("User activity monitoring stopped");
}

bool AntiAFK::check_user_active() {
  std::lock_guard<std::mutex> g(activity_check_mutex_);

  POINT current_pos{};
  if (::GetCursorPos(&current_pos)) {
    if (last_check_pos_.has_value() &&
        (current_pos.x != last_check_pos_->x || current_pos.y != last_check_pos_->y)) {
      last_check_pos_ = current_pos;
      return true;
    }
    last_check_pos_ = current_pos;
  }

  const int game_keys[] = {
      0x20,  // SPACE
      0x57,  // W
      0x41,  // A
      0x53,  // S
      0x44,  // D
      0x45,  // E
      0x52,  // R
      0x51,  // Q
      0x46,  // F
      0x51,  // Q
      0x31, 0x32, 0x33, 0x34, 0x35, 0x36,  // 1-6
      0x10,  // SHIFT
      0x11,  // CTRL
  };
  for (int key : game_keys) {
    if ((::GetAsyncKeyState(key) & 0x8000) != 0) {
      return true;
    }
  }

  const int mouse_buttons[] = {0x01, 0x02, 0x04};
  for (int button : mouse_buttons) {
    if ((::GetKeyState(button) & 0x8000) != 0) {
      return true;
    }
  }

  const HWND fg = ::GetForegroundWindow();
  const std::uintptr_t fg_int = reinterpret_cast<std::uintptr_t>(fg);
  if (last_check_foreground_.has_value() && fg_int != *last_check_foreground_) {
    last_check_foreground_ = fg_int;
    return true;
  }
  last_check_foreground_ = fg_int;
  return false;
}

bool AntiAFK::is_window_fullscreen(std::uint64_t hwnd_int) {
  const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
  py::gil_scoped_release release;
  if (!hwnd || !::IsWindow(hwnd)) {
    return false;
  }
  RECT window_rect{};
  if (!::GetWindowRect(hwnd, &window_rect)) {
    return false;
  }
  const HMONITOR mon = ::MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
  MONITORINFO mi{};
  mi.cbSize = sizeof(mi);
  if (!::GetMonitorInfoW(mon, &mi)) {
    return false;
  }
  const bool is_monitor = ::EqualRect(&window_rect, &mi.rcMonitor) != FALSE;
  const bool is_work = ::EqualRect(&window_rect, &mi.rcWork) != FALSE;
  return is_monitor && !is_work;
}

void AntiAFK::restore_foreground_window(std::uint64_t hwnd_int) {
  const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
  py::gil_scoped_release release;
  restore_foreground_window_impl(hwnd);
}

void AntiAFK::antiafk_loop_thread() {
  std::string crash_message;
  try {
    emit_status("Anti-AFK loop started");

    int interval = get_int("antiafk_interval", 120);
    std::string action_type = get_str("antiafk_action", "space");
    bool user_safe = get_bool("antiafk_user_safe", false);
    bool sequential_mode = get_bool("antiafk_sequential_mode", false);
    double sequential_delay = clamp_delay(get_double("antiafk_sequential_delay", 0.75));

    emit_status("Settings: Interval=" + std::to_string(interval) + "s, Action=" + action_type +
                ", True-AFK=" + std::string(user_safe ? "True" : "False") +
                ", Sequential=" + std::string(sequential_mode ? "True" : "False"));

    if (user_safe) {
      emit_status("True-AFK mode: Will wait for inactivity or max " +
                  std::to_string(kMaxWaitTimeS / 60) + " minutes since last action");
      if (last_activity_ms_.load() == 0) {
        last_activity_ms_.store(mono_ms());
      }
    }

    auto last_action_time = Clock::now() - std::chrono::seconds(std::max(0, interval - 10));
    auto last_status_update = Clock::now() - std::chrono::seconds(kActivityStatusEveryS + 1);
    std::optional<Clock::time_point> pause_started;

    while (!stop_requested_.load() && !shutdown_requested_.load()) {
      if (pause_requested_.load()) {
        if (!pause_started.has_value()) {
          pause_started = Clock::now();
          paused_.store(true);
          cv_.notify_all();
          emit_status("Anti-AFK paused");
        }

        {
          std::unique_lock<std::mutex> lk(cv_mutex_);
          cv_.wait(lk, [&] {
            return !pause_requested_.load() || stop_requested_.load() || shutdown_requested_.load();
          });
        }
        if (stop_requested_.load() || shutdown_requested_.load()) {
          break;
        }

        const auto paused_for = Clock::now() - *pause_started;
        last_action_time += paused_for;
        last_status_update += paused_for;
        pause_started.reset();
        paused_.store(false);
        emit_status("Anti-AFK resumed");
      }

      interval = get_int("antiafk_interval", interval);
      action_type = get_str("antiafk_action", action_type);
      user_safe = get_bool("antiafk_user_safe", user_safe);
      sequential_mode = get_bool("antiafk_sequential_mode", sequential_mode);
      sequential_delay = clamp_delay(get_double("antiafk_sequential_delay", sequential_delay));

      const auto now = Clock::now();
      std::vector<std::uint64_t> windows = find_roblox_windows_impl(true);

      if (windows.empty()) {
        if (std::chrono::duration_cast<std::chrono::seconds>(now - last_status_update).count() >
            kNoWindowsStatusEveryS) {
          emit_status("No Roblox windows found, waiting...");
          last_status_update = now;
        }
        if (wait_for_stop_or_pause(std::chrono::seconds(kNoWindowsWaitS))) {
          break;
        }
        continue;
      }

      const double elapsed_s =
          std::chrono::duration_cast<std::chrono::duration<double>>(now - last_action_time).count();

      const HWND foreground = ::GetForegroundWindow();

      if (user_safe &&
          std::chrono::duration_cast<std::chrono::seconds>(now - last_status_update).count() >
              kActivityStatusEveryS) {
        const bool active_now = check_user_active();
        if (active_now) {
          const int wait_time = std::max(0, kMaxWaitTimeS - static_cast<int>(elapsed_s));
          emit_status("User is active. Next action in " + std::to_string(wait_time) +
                      "s max or when inactive.");
        } else {
          const int inactivity = static_cast<int>((mono_ms() - last_activity_ms_.load()) / 1000);
          const int due_in = std::max(0, interval - static_cast<int>(elapsed_s));
          emit_status("User inactive for " + std::to_string(inactivity) +
                      "s. Action due in " + std::to_string(due_in) + "s.");
        }
        last_status_update = now;
      }

      bool perform_action = false;
      if (user_safe) {
        const int inactivity = static_cast<int>((mono_ms() - last_activity_ms_.load()) / 1000);
        if (inactivity >= kUserInactivityWaitS && elapsed_s >= static_cast<double>(interval)) {
          emit_status("User inactive for " + std::to_string(inactivity) +
                      "s and interval elapsed - performing action");
          perform_action = true;
        }
        if (elapsed_s >= static_cast<double>(kMaxWaitTimeS)) {
          emit_status("Maximum wait time reached (" + std::to_string(kMaxWaitTimeS / 60) +
                      " min since last action) - performing action");
          perform_action = true;
        }
      } else {
        if (elapsed_s >= static_cast<double>(interval)) {
          perform_action = true;
        }
      }

      if (perform_action) {
        const bool use_sequential = sequential_mode && windows.size() >= 5;
        if (use_sequential) {
          emit_status("Performing SEQUENTIAL Anti-AFK actions on " + std::to_string(windows.size()) +
                      " Roblox window(s)");
        } else {
          emit_status("Performing Anti-AFK action on " + std::to_string(windows.size()) +
                      " Roblox window(s)");
        }

        const bool menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", false);
        bool action_success = true;

        for (size_t i = 0; i < windows.size(); ++i) {
          if (stop_requested_.load() || shutdown_requested_.load()) {
            break;
          }
          if (pause_requested_.load()) {
            break;
          }
          const std::uint64_t hwnd_int = windows[i];
          const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
          const ActionResult res = run_action(hwnd, action_type, menu_autoreconnect);
          if (!res.ok) {
            action_success = false;
            if (!res.message.empty()) {
              emit_status(res.message);
            }
          }

          if (use_sequential && i + 1 < windows.size()) {
            const int delay_ms = static_cast<int>(clamp_delay(sequential_delay) * 1000.0);
            if (wait_for_stop_or_pause(std::chrono::milliseconds(delay_ms))) {
              break;
            }
          }
        }

        if (!stop_requested_.load() && !shutdown_requested_.load() && !pause_requested_.load()) {
          restore_foreground_window_impl(foreground);
        }

        if (pause_requested_.load()) {
          continue;
        }

        if (action_success) {
          last_action_time = Clock::now();
          emit_status("Anti-AFK action completed successfully");
          (void)wait_for_stop_or_pause(std::chrono::milliseconds(500));
        } else {
          cleanup_topmost_after_error("action failed", /*emit=*/false);
          emit_status("Anti-AFK action failed, will retry on next cycle");
        }
      }

      if (wait_for_stop_or_pause(std::chrono::seconds(kLoopIdleWaitS))) {
        break;
      }
    }
  } catch (const std::exception& e) {
    crash_message = e.what();
  } catch (...) {
    crash_message = "unknown error";
  }

  const bool ended_by_request = stop_requested_.load() || shutdown_requested_.load();
  if (!crash_message.empty() && !ended_by_request) {
    emit_status("Anti-AFK loop crashed: " + crash_message);
  } else if (!ended_by_request) {
    emit_status("Anti-AFK loop ended unexpectedly");
  } else {
    emit_status("Anti-AFK loop ended");
  }

  antiafk_running_.store(false);
  paused_.store(false);
  cv_.notify_all();
  emit_button_state(false);
}

void AntiAFK::monitor_user_activity_thread() {
  try {
    emit_status("User activity monitoring started - True-AFK mode active");

    constexpr int kMouseCheckIntervalMs = 500;
    const int mouse_buttons[] = {0x01, 0x02, 0x04};
    bool last_button_states[3] = {false, false, false};

    POINT last_mouse_pos{};
    (void)::GetCursorPos(&last_mouse_pos);
    std::int64_t last_pos_check = mono_ms();
    std::int64_t last_activity_logged = 0;
    std::int64_t last_inactivity_logged = 0;
    std::optional<std::uintptr_t> last_fg;

    while (monitor_running_.load() && !shutdown_requested_.load()) {
      bool activity = false;
      const std::int64_t now = mono_ms();

      const int quick_keys[] = {0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x20, 0x25, 0x26, 0x27, 0x28};
      for (int key : quick_keys) {
        if ((::GetAsyncKeyState(key) & 0x8000) != 0) {
          activity = true;
          break;
        }
      }

      if (!activity) {
        for (int i = 65; i <= 90; ++i) {
          if ((::GetAsyncKeyState(i) & 0x8000) != 0) {
            activity = true;
            break;
          }
        }
      }

      if (!activity) {
        for (int i = 48; i <= 57; ++i) {
          if ((::GetAsyncKeyState(i) & 0x8000) != 0) {
            activity = true;
            break;
          }
        }
      }

      if (!activity) {
        for (size_t i = 0; i < 3; ++i) {
          const int button = mouse_buttons[i];
          const bool down = (::GetKeyState(button) & 0x8000) != 0;
          if (down) {
            activity = true;
            last_button_states[i] = true;
            break;
          }
          if (last_button_states[i]) {
            last_button_states[i] = false;
            activity = true;
            break;
          }
        }
      }

      if (!activity && (now - last_pos_check) >= kMouseCheckIntervalMs) {
        POINT cur{};
        if (::GetCursorPos(&cur)) {
          if (cur.x != last_mouse_pos.x || cur.y != last_mouse_pos.y) {
            activity = true;
          }
          last_mouse_pos = cur;
        }
        last_pos_check = now;
      }

      if (!activity) {
        const std::uintptr_t fg = reinterpret_cast<std::uintptr_t>(::GetForegroundWindow());
        if (last_fg.has_value() && fg != *last_fg) {
          activity = true;
        }
        last_fg = fg;
      }

      if (activity) {
        last_activity_ms_.store(now);
        if (!user_active_.load() && (now - last_activity_logged) > 10000) {
          emit_status("User activity detected");
          last_activity_logged = now;
        }
        user_active_.store(true);
      } else {
        const int inactivity = static_cast<int>((now - last_activity_ms_.load()) / 1000);
        if (inactivity >= kUserInactivityWaitS) {
          if (user_active_.load() && (now - last_inactivity_logged) > 10000) {
            emit_status("User inactive for " + std::to_string(inactivity) + " seconds");
            last_inactivity_logged = now;
          }
          user_active_.store(false);
        }
      }

      (void)wait_for_shutdown_or(std::chrono::milliseconds(20));
    }
  } catch (...) {
  }
}

void AntiAFK::shutdown() {
  shutdown_impl(/*emit=*/false);
}

void AntiAFK::shutdown_impl(bool emit) {
  shutdown_requested_.store(true);
  stop_requested_.store(true);
  pause_requested_.store(false);
  paused_.store(false);
  monitor_running_.store(false);
  cv_.notify_all();

  std::thread t1 = std::move(antiafk_thread_);
  std::thread t2 = std::move(monitor_thread_);
  std::thread t3 = std::move(test_thread_);

  {
    py::gil_scoped_release release;
    if (t1.joinable()) {
      t1.join();
    }
    if (t2.joinable()) {
      t2.join();
    }
    if (t3.joinable()) {
      t3.join();
    }
  }

  antiafk_running_.store(false);
  test_running_.store(false);

  {
    py::gil_scoped_release release;
    if (multi_instance_mutex_ != nullptr) {
      ::CloseHandle(multi_instance_mutex_);
      multi_instance_mutex_ = nullptr;
    }
    cleanup_topmost_after_error("shutdown", /*emit=*/false);
  }

  if (emit) {
    emit_status("Anti-AFK shut down");
  }
}

void AntiAFK::shutdown_noexcept() noexcept {
  try {
    shutdown_impl(/*emit=*/false);
  } catch (...) {
  }
}

}  // namespace

PYBIND11_MODULE(antiafk_native, m) {
  m.doc() = "Native Win32 helpers for Roblox Anti-AFK (pybind11).";

  m.def(
      "action_task",
      &action_task,
      py::arg("hwnd"),
      py::arg("base_action"),
      py::arg("menu_autoreconnect") = false,
      py::arg("in_menu") = false,
      "Perform one Anti-AFK action for the given window handle.\n\n"
      "Returns (success: bool, message: str).");

  m.def(
      "find_roblox_windows",
      &find_roblox_windows,
      py::arg("include_hidden") = true,
      "Enumerate RobloxPlayerBeta.exe top-level windows.\n\n"
      "Returns list[int] of HWNDs.");

  m.def(
      "clear_notopmost",
      [](std::uint64_t hwnd_int) {
        const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
        py::gil_scoped_release release;
        clear_notopmost(hwnd);
      },
      py::arg("hwnd"),
      "Best-effort: clear TOPMOST flag for a window.");

  m.def(
      "clear_notopmost_all_roblox_windows",
      &clear_notopmost_all_roblox_windows,
      "Clear TOPMOST from all RobloxPlayerBeta.exe windows; returns count processed.");

  py::class_<AntiAFK>(m, "AntiAFK")
      .def(py::init<py::object, py::object>(), py::arg("parent"), py::arg("config") = py::none())
      .def_readwrite("config", &AntiAFK::config)
      .def_property_readonly("antiafk_running", &AntiAFK::antiafk_running)
      .def_readwrite("status_callback", &AntiAFK::status_callback)
      .def_readwrite("button_state_callback", &AntiAFK::button_state_callback)
      .def_readwrite("is_pid_in_menu_callback", &AntiAFK::is_pid_in_menu_callback)
      .def("apply_host_config",
           &AntiAFK::apply_host_config,
           py::kw_only(),
           py::arg("multi_instance_enabled") = std::nullopt,
           py::arg("interval") = std::nullopt,
           py::arg("action") = std::nullopt,
           py::arg("user_safe") = std::nullopt,
           py::arg("sequential_mode") = std::nullopt,
           py::arg("sequential_delay") = std::nullopt,
           py::arg("menu_autoreconnect") = std::nullopt)
      .def("toggle_antiafk", &AntiAFK::toggle_antiafk, py::arg("enable") = py::none())
      .def("start_antiafk", &AntiAFK::start_antiafk)
      .def("stop_antiafk", &AntiAFK::stop_antiafk)
      .def("pause_antiafk", &AntiAFK::pause_antiafk, py::arg("wait") = true)
      .def("resume_antiafk", &AntiAFK::resume_antiafk)
      .def("shutdown", &AntiAFK::shutdown)
      .def("find_roblox_windows", &AntiAFK::find_roblox_windows, py::arg("include_hidden") = true)
      .def("show_roblox_windows", &AntiAFK::show_roblox_windows)
      .def("hide_roblox_windows", &AntiAFK::hide_roblox_windows)
      .def("perform_antiafk_action",
           &AntiAFK::perform_antiafk_action,
           py::arg("hwnd"),
           py::arg("action_type") = std::nullopt)
      .def("test_action", &AntiAFK::test_action)
      .def("test_action_with_delay", &AntiAFK::test_action_with_delay)
      .def("start_activity_monitor", &AntiAFK::start_activity_monitor)
      .def("stop_activity_monitor", &AntiAFK::stop_activity_monitor)
      .def("check_user_active", &AntiAFK::check_user_active)
      .def("is_window_fullscreen", &AntiAFK::is_window_fullscreen, py::arg("hwnd"))
      .def("restore_foreground_window", &AntiAFK::restore_foreground_window, py::arg("hwnd"))
      .def("enable_multi_instance", &AntiAFK::enable_multi_instance)
      .def("disable_multi_instance", &AntiAFK::disable_multi_instance)
      .def("toggle_multi_instance", &AntiAFK::toggle_multi_instance)
      .def("update_status", &AntiAFK::update_status, py::arg("message"))
      .def("update_button_states", &AntiAFK::update_button_states)
      .def("log_error", &AntiAFK::log_error, py::arg("exception"), py::arg("message") = py::none());
}
