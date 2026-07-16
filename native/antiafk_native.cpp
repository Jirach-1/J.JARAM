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
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

constexpr int kActionDelayMs = 5;
constexpr int kWindowTextTimeoutMsDefault = 150;
constexpr int kWindowTextTimeoutMsFast = 40;
constexpr size_t kFastSweepWindowThreshold = 40;
constexpr int kFocusAttemptsDefault = 40;
constexpr int kFocusAttemptsFast = 12;
constexpr int kFocusSleepMsDefault = 25;
constexpr int kFocusSleepMsFast = 8;
constexpr int kPressFocusAttemptsDefault = 20;
constexpr int kPressFocusAttemptsFast = 6;
constexpr int kDefaultAltDelayMs = 400;
constexpr int kMinAltDelayMs = 10;
constexpr int kMaxAltDelayMs = 5000;
constexpr int kMinIntervalS = 5;
constexpr int kMaxIntervalS = 3600;
constexpr int kMinBatchSize = 1;
constexpr int kMaxBatchSize = 50;
constexpr double kMaxAlertLeadS = 60.0;
constexpr double kMaxUnthrottleLeadS = 15.0;
constexpr double kMaxUnthrottleHoldS = 300.0;
constexpr double kLargeSweepUnthrottleHoldS = 180.0;
constexpr int kFailedSweepRetryBackoffS = 10;
constexpr int kNoWindowsStatusEveryS = 10;
constexpr int kLoopIdleWaitS = 1;
constexpr int kNoWindowsWaitS = 5;
constexpr int kFailureTopmostCleanupBudgetMs = 1500;
constexpr int kGlobalTopmostCleanupBudgetMs = 3000;

using Clock = std::chrono::steady_clock;

int clamp_alt_delay_ms(int ms) {
  return std::clamp(ms, kMinAltDelayMs, kMaxAltDelayMs);
}

int clamp_interval_s(int seconds) {
  return std::clamp(seconds, kMinIntervalS, kMaxIntervalS);
}

int clamp_batch_size(int batch_size) {
  return std::clamp(batch_size, kMinBatchSize, kMaxBatchSize);
}

double clamp_alert_lead_s(double seconds) {
  return std::clamp(seconds, 0.0, kMaxAlertLeadS);
}

double clamp_unthrottle_lead_s(double seconds) {
  return std::clamp(seconds, 0.0, kMaxUnthrottleLeadS);
}

std::string ascii_lower(std::string s) {
  for (char& ch : s) {
    if (ch >= 'A' && ch <= 'Z') {
      ch = static_cast<char>(ch - 'A' + 'a');
    }
  }
  return s;
}

std::string normalize_action_name(const std::string& action) {
  const std::string lower = ascii_lower(action);
  if (lower == "space") {
    return "space";
  }
  if (lower == "ws") {
    return "ws";
  }
  if (lower == "zoom") {
    return "zoom";
  }
  if (lower == "autoreconnect" || lower == "auto_reconnect" || lower == "auto-reconnect") {
    return "AutoReconnect";
  }
  return "space";
}

double estimated_action_hold_s(const std::string& base_action,
                               bool menu_autoreconnect,
                               int alt_delay_ms,
                               int batch_size,
                               double lead_s,
                               bool large_sweep = false) {
  const std::string action = normalize_action_name(base_action);
  int delay_units = 1;
  if (action == "ws" || action == "zoom") {
    delay_units = 3;
  } else if (action == "AutoReconnect") {
    delay_units = 8;
  }
  if (menu_autoreconnect) {
    delay_units = std::max(delay_units, 8);
  }

  const double per_window_s = 0.5 + (static_cast<double>(delay_units) * clamp_alt_delay_ms(alt_delay_ms) / 1000.0);
  const double requested =
      clamp_unthrottle_lead_s(lead_s) + (per_window_s * static_cast<double>(clamp_batch_size(batch_size))) + 2.0;
  const double minimum = large_sweep ? kLargeSweepUnthrottleHoldS : 10.0;
  return std::clamp(std::max(requested, minimum), 2.0, kMaxUnthrottleHoldS);
}

std::int64_t mono_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
}

int elapsed_ms_since(Clock::time_point start) {
  return static_cast<int>(
      std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count());
}

bool contains(std::wstring_view haystack, std::wstring_view needle) {
  return haystack.find(needle) != std::wstring_view::npos;
}

inline wchar_t lower_ascii(wchar_t c) {
  if (c >= L'A' && c <= L'Z') {
    return static_cast<wchar_t>(c - L'A' + L'a');
  }
  return c;
}

bool icontains_ascii(std::wstring_view haystack, std::wstring_view needle) {
  if (needle.empty()) {
    return true;
  }
  if (haystack.size() < needle.size()) {
    return false;
  }
  for (size_t i = 0; i + needle.size() <= haystack.size(); ++i) {
    bool match = true;
    for (size_t j = 0; j < needle.size(); ++j) {
      if (lower_ascii(haystack[i + j]) != lower_ascii(needle[j])) {
        match = false;
        break;
      }
    }
    if (match) {
      return true;
    }
  }
  return false;
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

std::wstring window_text(HWND hwnd, bool* timed_out = nullptr, int timeout_ms = kWindowTextTimeoutMsDefault) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return {};
  }
  if (timed_out) {
    *timed_out = false;
  }
  // NOTE: Avoid GetWindowTextLengthW/GetWindowTextW for out-of-process windows; they can hang when
  // the target is throttled/unresponsive. Use a bounded SendMessageTimeout instead.
  const int kTimeoutMs = std::max(1, timeout_ms);
  constexpr size_t kMaxChars = 1024;
  std::wstring out;
  out.resize(kMaxChars);

  DWORD_PTR message_result = 0;
  ::SetLastError(ERROR_SUCCESS);
  const LRESULT sent = ::SendMessageTimeoutW(
      hwnd,
      WM_GETTEXT,
      static_cast<WPARAM>(out.size()),
      reinterpret_cast<LPARAM>(out.data()),
      SMTO_ABORTIFHUNG | SMTO_BLOCK,
      kTimeoutMs,
      &message_result);
  if (sent == 0) {
    if (timed_out && ::GetLastError() == ERROR_TIMEOUT) {
      *timed_out = true;
    }
    return {};
  }
  const int copied = static_cast<int>(message_result);
  if (copied <= 0) {
    return {};
  }
  out.resize(static_cast<size_t>(copied));
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
  // Title checks are best-effort only. IME windows can appear as top-level windows
  // inside the Roblox process and should be excluded.
  if (title.empty()) {
    return false;
  }

  // Case-insensitive match: users have reported "default ime" / "msctfime" in lowercase.
  if (!icontains_ascii(title, L"roblox")) {
    return false;
  }
  if (icontains_ascii(title, L"msctfime") || icontains_ascii(title, L"default ime")) {
    return false;
  }
  return true;
}

bool is_valid_roblox_title_for_find(std::wstring_view title) {
  if (!is_valid_roblox_title_for_action(title)) {
    return false;
  }
  if (icontains_ascii(title, L"nvidia")) {
    return false;
  }
  return true;
}

// Forward-declared: implementation near the window enumeration helpers.
bool is_probably_ime_window(std::wstring_view title, std::wstring_view cls);

void clear_notopmost(HWND hwnd) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return;
  }
  ::SetWindowPos(
      hwnd,
      HWND_NOTOPMOST,
      0,
      0,
      0,
      0,
      SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOSENDCHANGING | SWP_ASYNCWINDOWPOS);
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
    if (a != 0 && b != 0 && a != b) {
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

bool should_abort(const std::atomic<bool>* stop_requested,
                  const std::atomic<bool>* shutdown_requested,
                  const std::atomic<bool>* pause_requested = nullptr) {
  return (stop_requested && stop_requested->load()) || (shutdown_requested && shutdown_requested->load()) ||
         (pause_requested && pause_requested->load());
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
                     const std::atomic<bool>* shutdown_requested,
                     const std::atomic<bool>* pause_requested = nullptr) {
  constexpr int kSliceMs = 25;
  int remaining = delay_ms;
  while (remaining > 0 && !should_abort(stop_requested, shutdown_requested, pause_requested)) {
    const int slice = std::min(kSliceMs, remaining);
    ::Sleep(static_cast<DWORD>(slice));
    remaining -= slice;
  }
}

bool focus_window_best_effort(HWND hwnd, DWORD window_tid) {
  if (!hwnd || !::IsWindow(hwnd)) {
    return false;
  }
  const HWND current_foreground = ::GetForegroundWindow();
  if (current_foreground == hwnd) {
    return true;
  }

  const DWORD current_tid = ::GetCurrentThreadId();
  const DWORD foreground_tid =
      current_foreground ? ::GetWindowThreadProcessId(current_foreground, nullptr) : 0;

  auto attempt = [&](bool make_topmost) -> bool {
    if (make_topmost) {
      set_topmost(hwnd);
    }

    // Attach to both the target thread and the current foreground thread where possible.
    // This improves SetForegroundWindow reliability when running out-of-process.
    ThreadInputAttachGuard attach_target(current_tid, window_tid);
    ThreadInputAttachGuard attach_foreground(current_tid, foreground_tid);

    (void)::ShowWindow(hwnd, SW_SHOW);
    (void)::BringWindowToTop(hwnd);
    (void)::SetForegroundWindow(hwnd);
    (void)::SetActiveWindow(hwnd);
    (void)::SetFocus(hwnd);

    if (::GetForegroundWindow() == hwnd) {
      return true;
    }

    // Retry: Windows sometimes ignores the first activation attempt.
    (void)::BringWindowToTop(hwnd);
    (void)::SetForegroundWindow(hwnd);
    (void)::SetActiveWindow(hwnd);
    (void)::SetFocus(hwnd);
    if (::GetForegroundWindow() == hwnd) {
      return true;
    }

    // Deprecated but still effective in some focus-lock scenarios.
    (void)::SwitchToThisWindow(hwnd, TRUE);
    (void)::BringWindowToTop(hwnd);
    (void)::SetForegroundWindow(hwnd);
    (void)::SetActiveWindow(hwnd);
    (void)::SetFocus(hwnd);
    return ::GetForegroundWindow() == hwnd;
  };

  if (attempt(false)) {
    return true;
  }
  return attempt(true);
}

bool foreground_matches_target(HWND target_hwnd, DWORD target_pid) {
  const HWND fg = ::GetForegroundWindow();
  if (fg == target_hwnd) {
    return true;
  }
  if (!fg || !::IsWindow(fg) || target_pid == 0) {
    return false;
  }
  DWORD fg_pid = 0;
  (void)::GetWindowThreadProcessId(fg, &fg_pid);
  if (fg_pid != target_pid) {
    return false;
  }
  const LONG_PTR exstyle = ::GetWindowLongPtrW(fg, GWL_EXSTYLE);
  if (exstyle & WS_EX_NOACTIVATE) {
    return false;
  }
  if (exstyle & WS_EX_TOOLWINDOW) {
    return false;
  }
  if (::GetWindow(fg, GW_OWNER) != nullptr) {
    return false;
  }
  const std::wstring fg_cls = window_class_name(fg);
  if (is_probably_ime_window(L"", fg_cls)) {
    return false;
  }
  return true;
}

bool ensure_foreground(HWND hwnd,
                       DWORD window_tid,
                       DWORD window_pid,
                       int attempts,
                       int sleep_ms,
                       const std::atomic<bool>* stop_requested,
                       const std::atomic<bool>* shutdown_requested,
                       const std::atomic<bool>* pause_requested = nullptr,
                       int* out_attempts_used = nullptr) {
  int attempts_used = 0;
  for (int i = 0; i < attempts; ++i) {
    attempts_used = i + 1;
    if (should_abort(stop_requested, shutdown_requested, pause_requested)) {
      if (out_attempts_used) {
        *out_attempts_used = attempts_used;
      }
      return false;
    }
    if (foreground_matches_target(hwnd, window_pid)) {
      if (out_attempts_used) {
        *out_attempts_used = attempts_used;
      }
      return true;
    }
    if (focus_window_best_effort(hwnd, window_tid)) {
      if (out_attempts_used) {
        *out_attempts_used = attempts_used;
      }
      if (foreground_matches_target(hwnd, window_pid)) {
        return true;
      }
      return true;
    }
    sleep_abortable(sleep_ms, stop_requested, shutdown_requested, pause_requested);
  }
  if (out_attempts_used) {
    *out_attempts_used = attempts_used;
  }
  return foreground_matches_target(hwnd, window_pid);
}

bool press_key_defensive(HWND hwnd,
                          DWORD window_tid,
                          DWORD window_pid,
                          int vk_code,
                          int delay_ms,
                          bool fast_focus,
                          const std::atomic<bool>* stop_requested,
                          const std::atomic<bool>* shutdown_requested,
                          const std::atomic<bool>* pause_requested = nullptr) {
  const int focus_attempts = fast_focus ? kPressFocusAttemptsFast : kPressFocusAttemptsDefault;
  const int focus_sleep_ms = fast_focus ? kFocusSleepMsFast : kFocusSleepMsDefault;

  if (!ensure_foreground(hwnd,
                         window_tid,
                         window_pid,
                         focus_attempts,
                         focus_sleep_ms,
                         stop_requested,
                         shutdown_requested,
                         pause_requested)) {
    return false;
  }

  key_event_down(vk_code);
  sleep_abortable(delay_ms, stop_requested, shutdown_requested, pause_requested);

  // Best-effort: try to ensure the window is still foreground before releasing.
  if (!fast_focus) {
    (void)ensure_foreground(
        hwnd, window_tid, window_pid, 2, focus_sleep_ms, stop_requested, shutdown_requested, pause_requested);
  }
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
                              int alt_delay_ms,
                              const std::atomic<bool>* stop_requested = nullptr,
                              const std::atomic<bool>* shutdown_requested = nullptr,
                              const std::atomic<bool>* pause_requested = nullptr,
                              bool restore_foreground = true,
                              bool fast_focus = false,
                              int* out_focus_ms = nullptr,
                              int* out_focus_attempts = nullptr,
                              int* out_total_ms = nullptr) {
  ActionResult out;
  const auto start_ts = Clock::now();
  const std::uint64_t hwnd_int = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(hwnd));
  const std::string normalized_base_action = normalize_action_name(base_action);
  const std::string effective_action = (menu_autoreconnect && in_menu) ? "AutoReconnect" : normalized_base_action;
  const int alt_delay = clamp_alt_delay_ms(alt_delay_ms);
  const int focus_attempts = fast_focus ? kFocusAttemptsFast : kFocusAttemptsDefault;
  const int focus_sleep_ms = fast_focus ? kFocusSleepMsFast : kFocusSleepMsDefault;
  const int action_delay_ms = fast_focus ? 0 : kActionDelayMs;

  const auto aborting = [&]() -> bool {
    return should_abort(stop_requested, shutdown_requested, pause_requested);
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
  if (window_pid == 0 || !is_roblox_process(window_pid)) {
    out.ok = false;
    out.message = "Window '" + wide_to_utf8(window_text(hwnd)) + "' is not a Roblox window";
    return out;
  }

  const std::wstring cls = window_class_name(hwnd);
  if (is_probably_ime_window(L"", cls)) {
    out.ok = false;
    out.message = "Window (handle: " + std::to_string(hwnd_int) + ", class: '" + wide_to_utf8(cls) +
                  "') is not a valid Roblox game window";
    return out;
  }

  if (restore_foreground) {
    old_hwnd = ::GetForegroundWindow();
  }
  const BOOL was_minimized = ::IsIconic(hwnd);

  try {
    if (was_minimized) {
      ::ShowWindow(hwnd, SW_RESTORE);
    }

    const auto focus_start_ts = Clock::now();
    int focus_attempts_used = 0;
    if (!ensure_foreground(hwnd,
                           window_tid,
                           window_pid,
                           focus_attempts,
                           focus_sleep_ms,
                           stop_requested,
                           shutdown_requested,
                           pause_requested,
                           &focus_attempts_used)) {
      out.ok = false;
      if (aborting()) {
        if (out_focus_ms) {
          *out_focus_ms = static_cast<int>(
              std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - focus_start_ts).count());
        }
        if (out_focus_attempts) {
          *out_focus_attempts = focus_attempts_used;
        }
        if (out_total_ms) {
          *out_total_ms = static_cast<int>(
              std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start_ts).count());
        }
        return out;
      }
      const HWND fg = ::GetForegroundWindow();
      if (fg && ::IsWindow(fg) && fg != hwnd) {
        DWORD fg_pid = 0;
        (void)::GetWindowThreadProcessId(fg, &fg_pid);
        bool fg_title_timed_out = false;
        const std::wstring fg_title = window_text(fg, &fg_title_timed_out);
        const std::wstring fg_cls = window_class_name(fg);
        std::string fg_exe;
        if (fg_pid != 0) {
          fg_exe = wide_to_utf8(process_basename(fg_pid).value_or(L""));
        }
        out.message =
            "Failed to focus target window for Anti-AFK action (foreground: '" + wide_to_utf8(fg_title) +
            "'" + (fg_title_timed_out ? ", title_timeout" : "") + ", class: '" + wide_to_utf8(fg_cls) +
            "', pid: " + std::to_string(fg_pid) + (fg_exe.empty() ? "" : ", exe: " + fg_exe) + ")";
      } else {
        out.message = "Failed to focus target window for Anti-AFK action";
      }
      if (out_focus_ms) {
        *out_focus_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - focus_start_ts).count());
      }
      if (out_focus_attempts) {
        *out_focus_attempts = focus_attempts_used;
      }
      if (out_total_ms) {
        *out_total_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start_ts).count());
      }
      return out;
    }
    if (out_focus_ms) {
      *out_focus_ms = static_cast<int>(
          std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - focus_start_ts).count());
    }
    if (out_focus_attempts) {
      *out_focus_attempts = focus_attempts_used;
    }

    if (action_delay_ms > 0) {
      sleep_abortable(action_delay_ms, stop_requested, shutdown_requested, pause_requested);
    }

    const auto press_key = [&](int vk_code, int delay_ms) {
      return press_key_defensive(
          hwnd,
          window_tid,
          window_pid,
          vk_code,
          delay_ms,
          fast_focus,
          stop_requested,
          shutdown_requested,
          pause_requested);
    };

    if (effective_action == "space") {
      if (!press_key(VK_SPACE, alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (space)";
        }
        return out;
      }
    } else if (effective_action == "ws") {
      if (!press_key(static_cast<int>('W'), alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (W)";
        }
        return out;
      }
      sleep_abortable(alt_delay, stop_requested, shutdown_requested, pause_requested);
      if (!press_key(static_cast<int>('S'), alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (S)";
        }
        return out;
      }
    } else if (effective_action == "zoom") {
      if (!press_key(static_cast<int>('I'), alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (I)";
        }
        return out;
      }
      sleep_abortable(alt_delay, stop_requested, shutdown_requested, pause_requested);
      if (!press_key(static_cast<int>('O'), alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (O)";
        }
        return out;
      }
    } else if (effective_action == "AutoReconnect") {
      const int vk_backslash = ::VkKeyScanA('\\') & 0xFF;
      if (!press_key(vk_backslash, alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (\\\\)";
        }
        return out;
      }
      sleep_abortable(alt_delay, stop_requested, shutdown_requested, pause_requested);

      const int vk_a = ::VkKeyScanA('a') & 0xFF;
      if (!press_key(vk_a, alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (a)";
        }
        return out;
      }
      sleep_abortable(alt_delay, stop_requested, shutdown_requested, pause_requested);

      if (!press_key(static_cast<int>('S'), alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (S)";
        }
        return out;
      }

      if (!press_key(VK_RETURN, alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (Enter)";
        }
        return out;
      }
      sleep_abortable(alt_delay, stop_requested, shutdown_requested, pause_requested);

      if (!press_key(vk_backslash, alt_delay)) {
        out.ok = false;
        if (!aborting()) {
          out.message = "Lost focus before key press (\\\\)";
        }
        return out;
      }
    } else {
      out.ok = false;
      out.message = "Unknown Anti-AFK action: " + effective_action;
      return out;
    }

    if (action_delay_ms > 0) {
      sleep_abortable(action_delay_ms, stop_requested, shutdown_requested, pause_requested);
    }

    if (was_minimized) {
      ::ShowWindow(hwnd, SW_MINIMIZE);
    }

    if (restore_foreground && !aborting() && old_hwnd && old_hwnd != hwnd && ::IsWindow(old_hwnd)) {
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
     out.message = "Performed " + effective_action + " action";
     if (out_total_ms) {
       *out_total_ms = static_cast<int>(
           std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start_ts).count());
     }
     return out;
  } catch (const std::exception& e) {
    if (was_minimized) {
      ::ShowWindow(hwnd, SW_MINIMIZE);
    }
    if (restore_foreground && !aborting() && old_hwnd && ::IsWindow(old_hwnd)) {
      (void)::SetForegroundWindow(old_hwnd);
    }
    const std::string wtitle = wide_to_utf8(window_text(hwnd));
    out.ok = false;
    out.message =
        "Error performing anti-AFK action on window " + std::to_string(hwnd_int) +
        (wtitle.empty() ? "" : " ('" + wtitle + "')") + ": " + std::string(e.what());
    if (out_total_ms) {
      *out_total_ms = static_cast<int>(
          std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start_ts).count());
    }
    return out;
  } catch (...) {
    if (was_minimized) {
      ::ShowWindow(hwnd, SW_MINIMIZE);
    }
    if (restore_foreground && !aborting() && old_hwnd && ::IsWindow(old_hwnd)) {
      (void)::SetForegroundWindow(old_hwnd);
    }
    const std::string wtitle = wide_to_utf8(window_text(hwnd));
    out.ok = false;
    out.message = "Error performing anti-AFK action on window " + std::to_string(hwnd_int) +
                  (wtitle.empty() ? "" : " ('" + wtitle + "')") + ": unknown error";
    if (out_total_ms) {
      *out_total_ms = static_cast<int>(
          std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start_ts).count());
    }
    return out;
  }
}

py::tuple action_task(std::uint64_t hwnd_int,
                      const std::string& base_action,
                      bool menu_autoreconnect,
                      bool in_menu,
                      std::optional<int> alt_delay_ms = std::nullopt) {
  ActionResult res;
  {
    py::gil_scoped_release release;
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
    res = action_task_impl(
        hwnd, base_action, menu_autoreconnect, in_menu, alt_delay_ms.value_or(kDefaultAltDelayMs));
  }
  return py::make_tuple(res.ok, res.message);
}

struct FindEnumData {
  struct Best {
    HWND hwnd{nullptr};
    int score{0};
  };

  bool include_hidden{true};
  int text_timeout_ms{kWindowTextTimeoutMsDefault};
  std::unordered_map<DWORD, bool> roblox_pid_cache;
  std::unordered_map<DWORD, Best> best_by_pid;
};

bool is_probably_ime_window(std::wstring_view title, std::wstring_view cls) {
  // Filter IME helper windows that sometimes live in the Roblox process and show up as
  // top-level windows in EnumWindows().
  if (icontains_ascii(title, L"msctfime") || icontains_ascii(cls, L"msctfime")) {
    return true;
  }
  if (icontains_ascii(title, L"default ime") || icontains_ascii(cls, L"default ime")) {
    return true;
  }
  // Some IME windows use a very short class name.
  if (iequals_ascii(cls, L"IME")) {
    return true;
  }
  return false;
}

int score_roblox_window_candidate(HWND hwnd,
                                 bool include_hidden,
                                 std::wstring_view title,
                                 std::wstring_view cls,
                                 bool title_timed_out) {
  // Higher score = more likely the main Roblox game window.
  int score = 0;
  if (::IsWindowVisible(hwnd)) {
    score += 200;
  } else if (!include_hidden) {
    // Should have been filtered out, but keep it low just in case.
    score -= 200;
  }
  if (!::IsIconic(hwnd)) {
    score += 50;
  }

  LONG_PTR exstyle = ::GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
  if (exstyle & WS_EX_APPWINDOW) {
    score += 25;
  }
  if (exstyle & WS_EX_TOOLWINDOW) {
    score -= 250;
  }
  if (exstyle & WS_EX_NOACTIVATE) {
    score -= 400;
  }

  RECT r{};
  if (::GetWindowRect(hwnd, &r)) {
    const int w = std::max(0, static_cast<int>(r.right - r.left));
    const int h = std::max(0, static_cast<int>(r.bottom - r.top));
    const int area = w * h;
    // Scale area influence but cap it.
    score += std::min(area / 10000, 350);
  }

  if (!title.empty()) {
    if (is_valid_roblox_title_for_find(title)) {
      score += 500;
    } else {
      score -= 30;
    }
  }
  if (title_timed_out) {
    score -= 100;
  }
  if (!cls.empty() && icontains_ascii(cls, L"windowsclient")) {
    // Common Roblox player class name on Windows.
    score += 150;
  }
  return score;
}

BOOL CALLBACK enum_find_roblox_windows(HWND hwnd, LPARAM lparam) {
  auto* data = reinterpret_cast<FindEnumData*>(lparam);
  if (!data) {
    return TRUE;
  }
  if (!hwnd || !::IsWindow(hwnd)) {
    return TRUE;
  }
  if (!data->include_hidden && !::IsWindowVisible(hwnd)) {
    return TRUE;
  }

  DWORD pid = 0;
  (void)::GetWindowThreadProcessId(hwnd, &pid);
  if (pid == 0) {
    return TRUE;
  }
  auto cache_it = data->roblox_pid_cache.find(pid);
  bool is_rbx = false;
  if (cache_it != data->roblox_pid_cache.end()) {
    is_rbx = cache_it->second;
  } else {
    is_rbx = is_roblox_process(pid);
    data->roblox_pid_cache.emplace(pid, is_rbx);
  }
  if (!is_rbx) {
    return TRUE;
  }

  // Skip obvious non-game windows that can live in the Roblox process.
  const LONG_PTR exstyle = ::GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
  if (exstyle & WS_EX_NOACTIVATE) {
    return TRUE;
  }
  if (exstyle & WS_EX_TOOLWINDOW) {
    return TRUE;
  }
  if (::GetWindow(hwnd, GW_OWNER) != nullptr) {
    // Owned windows are often helper/overlay windows.
    return TRUE;
  }

  const std::wstring cls = window_class_name(hwnd);
  bool title_timed_out = false;
  const std::wstring title = window_text(hwnd, &title_timed_out, data->text_timeout_ms);

  if (is_probably_ime_window(title, cls)) {
    return TRUE;
  }

  // Filter out tiny helper windows (IME, overlays, etc.). Keep minimized windows.
  RECT r{};
  if (::GetWindowRect(hwnd, &r) && !::IsIconic(hwnd)) {
    const int w = std::max(0, static_cast<int>(r.right - r.left));
    const int h = std::max(0, static_cast<int>(r.bottom - r.top));
    // Very small windows are almost never the actual game client window.
    // Keep the threshold low because many users run Roblox in tiny tiled windows.
    if (w < 120 || h < 80) {
      return TRUE;
    }
  }

  // If title matches the expected pattern we heavily prefer it, but don't require it.
  const int score = score_roblox_window_candidate(hwnd, data->include_hidden, title, cls, title_timed_out);
  auto it = data->best_by_pid.find(pid);
  if (it == data->best_by_pid.end() || score > it->second.score) {
    data->best_by_pid[pid] = FindEnumData::Best{hwnd, score};
  }
  return TRUE;
}

std::vector<std::uint64_t> find_roblox_windows_impl(bool include_hidden, int text_timeout_ms) {
  FindEnumData data;
  data.include_hidden = include_hidden;
  data.text_timeout_ms = std::max(1, text_timeout_ms);
  ::EnumWindows(enum_find_roblox_windows, reinterpret_cast<LPARAM>(&data));

  // Return at most one hwnd per Roblox PID (the best-scoring candidate).
  std::vector<std::pair<DWORD, FindEnumData::Best>> entries;
  entries.reserve(data.best_by_pid.size());
  for (const auto& kv : data.best_by_pid) {
    entries.emplace_back(kv.first, kv.second);
  }
  std::sort(entries.begin(), entries.end(), [](const auto& a, const auto& b) { return a.first < b.first; });

  std::vector<std::uint64_t> out;
  out.reserve(entries.size());
  for (const auto& kv : entries) {
    const HWND hwnd = kv.second.hwnd;
    if (hwnd && ::IsWindow(hwnd)) {
      out.push_back(static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(hwnd)));
    }
  }
  return out;
}

std::vector<std::uint64_t> find_roblox_windows(bool include_hidden) {
  py::gil_scoped_release release;
  return find_roblox_windows_impl(include_hidden, kWindowTextTimeoutMsDefault);
}

int clear_notopmost_windows_impl(const std::vector<std::uint64_t>& windows, int budget_ms = 0) {
  const auto started = Clock::now();
  std::unordered_set<std::uint64_t> seen;
  seen.reserve(windows.size());
  int count = 0;
  for (std::uint64_t w : windows) {
    if (budget_ms > 0 && elapsed_ms_since(started) >= budget_ms) {
      break;
    }
    if (!seen.insert(w).second) {
      continue;
    }
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(w));
    if (!hwnd || !::IsWindow(hwnd)) {
      continue;
    }
    clear_notopmost(hwnd);
    count += 1;
  }
  return count;
}

int clear_notopmost_all_roblox_windows_impl(int budget_ms = kGlobalTopmostCleanupBudgetMs) {
  // Only operate on the main/primary Roblox window per PID. Use the fast title timeout because
  // cleanup must never spend minutes waiting on throttled or unresponsive Roblox windows.
  const std::vector<std::uint64_t> windows = find_roblox_windows_impl(true, kWindowTextTimeoutMsFast);
  return clear_notopmost_windows_impl(windows, budget_ms);
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
  py::object touch_callback = py::none();
  py::object pre_action_callback = py::none();
  py::object is_pid_in_menu_callback = py::none();
  py::object bes_hold_unthrottled_callback = py::none();
  py::object bes_release_hold_callback = py::none();

  py::dict config;

  bool antiafk_running() const;

  void update_status(const std::string& message);
  void update_button_states();
  void log_error(py::object exception, py::object message_obj = py::none());

  void touch_pids(const std::vector<int>& pids);
  void set_pids_disconnected(const std::vector<int>& pids, bool disconnected = true);

  void apply_host_config(std::optional<bool> multi_instance_enabled = std::nullopt,
                         std::optional<int> interval = std::nullopt,
                         std::optional<std::string> action = std::nullopt,
                         std::optional<int> alt_delay_ms = std::nullopt,
                         std::optional<bool> menu_autoreconnect = std::nullopt,
                         std::optional<bool> alert_sound_enabled = std::nullopt,
                         std::optional<bool> alert_tooltip_enabled = std::nullopt,
                         std::optional<double> alert_lead_s = std::nullopt,
                         std::optional<bool> unthrottle_enabled = std::nullopt,
                         std::optional<int> unthrottle_batch_size = std::nullopt,
                         std::optional<double> unthrottle_lead_s = std::nullopt,
                         std::optional<bool> dev_mode = std::nullopt);

  void toggle_antiafk(py::object enable_obj = py::none());
  void start_antiafk();
  void stop_antiafk();
  bool pause_antiafk(bool wait = true);
  bool resume_antiafk();

  bool enable_multi_instance();
  bool disable_multi_instance();
  bool refresh_multi_instance();
  bool multi_instance_guard_active() const;
  unsigned long multi_instance_last_error() const;
  void toggle_multi_instance();

  std::vector<std::uint64_t> find_roblox_windows(bool include_hidden = true) const;
  void show_roblox_windows();
  void hide_roblox_windows();

  bool perform_antiafk_action(std::uint64_t hwnd_int,
                             std::optional<std::string> action_type = std::nullopt,
                             bool restore_foreground = true);
  void test_action();
  void test_action_with_delay();

  bool is_window_fullscreen(std::uint64_t hwnd_int);
  void restore_foreground_window(std::uint64_t hwnd_int);

  void shutdown();

 private:
  void ensure_defaults_locked();

  int get_int(const char* key, int fallback) const;
  bool get_bool(const char* key, bool fallback) const;
  double get_double(const char* key, double fallback) const;
  std::string get_str(const char* key, std::string fallback) const;
  bool debug_timings_enabled() const;

  void emit_status(const std::string& message);
  void emit_debug_timing(const std::string& message);
  void emit_button_state(bool running);
  void emit_touch(DWORD pid);
  void emit_pre_action(double seconds_until_action);
  void sort_windows_by_touch(std::vector<std::uint64_t>& windows);

  bool call_is_pid_in_menu(DWORD pid);
  bool call_bes_hold_unthrottled(const std::vector<int>& pids, double seconds);
  void call_bes_release_hold(const std::vector<int>& pids);
  bool wait_for_stop_or(std::chrono::milliseconds d);
  bool wait_for_stop_or_pause(std::chrono::milliseconds d);
  bool wait_for_shutdown_or(std::chrono::milliseconds d);

  void cleanup_topmost_after_error(const std::string& reason,
                                   bool emit,
                                   const std::vector<std::uint64_t>* windows = nullptr);
  void restore_foreground_window_impl(HWND hwnd);
  ActionResult run_action(HWND hwnd,
                          const std::string& base_action,
                          bool menu_autoreconnect,
                          bool restore_foreground = true,
                          bool fast_focus = false);

  void antiafk_loop_thread();
  void run_test_thread(std::vector<std::uint64_t> windows);

  void shutdown_impl(bool emit);
  void shutdown_noexcept() noexcept;

  py::object parent_;

  std::atomic<bool> antiafk_running_{false};
  std::atomic<bool> stop_requested_{false};
  std::atomic<bool> pause_requested_{false};
  std::atomic<bool> paused_{false};
  std::atomic<bool> shutdown_requested_{false};

  std::atomic<bool> test_running_{false};

  mutable std::mutex cv_mutex_;
  std::condition_variable cv_;

  std::thread antiafk_thread_;
  std::thread test_thread_;

  HANDLE multi_instance_mutex_{nullptr};
  std::atomic<DWORD> multi_instance_last_error_{ERROR_SUCCESS};

  std::mutex touch_mutex_;
  std::unordered_map<DWORD, std::int64_t> last_touch_ms_by_pid_;
  std::unordered_set<DWORD> disconnected_pids_;
};

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
  set_if_missing("antiafk_alt_delay_ms", py::int_(kDefaultAltDelayMs));
  set_if_missing("antiafk_dev_mode", py::bool_(false));
  set_if_missing("antiafk_menu_autoreconnect", py::bool_(false));
  set_if_missing("antiafk_alert_sound", py::bool_(false));
  set_if_missing("antiafk_alert_tooltip", py::bool_(false));
  set_if_missing("antiafk_alert_lead_s", py::float_(3.0));
  set_if_missing("antiafk_unthrottle_enabled", py::bool_(false));
  set_if_missing("antiafk_unthrottle_batch_size", py::int_(5));
  set_if_missing("antiafk_unthrottle_lead_s", py::float_(3.0));

  // Feature removals: purge stale keys if present so they don't get re-saved.
  try {
    config.attr("pop")(py::str("antiafk_user_safe"), py::none());
    config.attr("pop")(py::str("antiafk_sequential_mode"), py::none());
    config.attr("pop")(py::str("antiafk_sequential_delay"), py::none());
  } catch (...) {
    PyErr_Clear();
  }
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

bool AntiAFK::debug_timings_enabled() const { return get_bool("antiafk_dev_mode", false); }

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

void AntiAFK::emit_debug_timing(const std::string& message) {
  if (!debug_timings_enabled()) {
    return;
  }
  emit_status("[Anti-AFK timing] " + message);
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

void AntiAFK::emit_touch(DWORD pid) {
  py::gil_scoped_acquire acquire;
  if (touch_callback.is_none()) {
    return;
  }
  try {
    touch_callback(py::int_(static_cast<int>(pid)));
  } catch (...) {
    PyErr_Clear();
  }
}

void AntiAFK::emit_pre_action(double seconds_until_action) {
  py::gil_scoped_acquire acquire;
  if (pre_action_callback.is_none()) {
    return;
  }
  try {
    pre_action_callback(py::float_(seconds_until_action));
  } catch (...) {
    PyErr_Clear();
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

void AntiAFK::touch_pids(const std::vector<int>& pids) {
  const std::int64_t now_ms = mono_ms();
  std::lock_guard<std::mutex> lk(touch_mutex_);
  for (int pid_i : pids) {
    if (pid_i <= 0) {
      continue;
    }
    last_touch_ms_by_pid_[static_cast<DWORD>(pid_i)] = now_ms;
  }
}

void AntiAFK::set_pids_disconnected(const std::vector<int>& pids, bool disconnected) {
  std::lock_guard<std::mutex> lk(touch_mutex_);
  for (int pid_i : pids) {
    if (pid_i <= 0) {
      continue;
    }
    const DWORD pid = static_cast<DWORD>(pid_i);
    if (disconnected) {
      disconnected_pids_.insert(pid);
    } else {
      disconnected_pids_.erase(pid);
    }
  }
}

void AntiAFK::sort_windows_by_touch(std::vector<std::uint64_t>& windows) {
  if (windows.size() < 2) {
    return;
  }

  struct SweepEntry {
    std::uint64_t hwnd = 0;
    DWORD pid = 0;
    std::int64_t touched_ms = 0;
    bool disconnected = false;
  };

  std::vector<SweepEntry> entries;
  entries.reserve(windows.size());

  for (std::uint64_t w : windows) {
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(w));
    if (!hwnd || !::IsWindow(hwnd)) {
      continue;
    }
    DWORD pid = 0;
    (void)::GetWindowThreadProcessId(hwnd, &pid);
    if (pid == 0) {
      continue;
    }
    SweepEntry e;
    e.hwnd = w;
    e.pid = pid;
    entries.push_back(e);
  }

  {
    std::lock_guard<std::mutex> lk(touch_mutex_);
    for (auto& e : entries) {
      e.disconnected = disconnected_pids_.find(e.pid) != disconnected_pids_.end();
      const auto it = last_touch_ms_by_pid_.find(e.pid);
      e.touched_ms = (it != last_touch_ms_by_pid_.end()) ? it->second : 0;
    }
  }

  std::sort(entries.begin(), entries.end(), [](const SweepEntry& a, const SweepEntry& b) {
    if (a.disconnected != b.disconnected) {
      return a.disconnected < b.disconnected;  // connected first
    }
    if (a.touched_ms != b.touched_ms) {
      return a.touched_ms < b.touched_ms;  // oldest first
    }
    return a.pid < b.pid;
  });

  windows.clear();
  windows.reserve(entries.size());
  for (const auto& e : entries) {
    windows.push_back(e.hwnd);
  }
}

bool AntiAFK::call_bes_hold_unthrottled(const std::vector<int>& pids, double seconds) {
  py::gil_scoped_acquire acquire;
  if (bes_hold_unthrottled_callback.is_none()) {
    return false;
  }
  try {
    py::object out = bes_hold_unthrottled_callback(py::cast(pids), py::float_(seconds));
    if (out.is_none()) {
      return false;
    }
    return py::cast<bool>(out);
  } catch (...) {
    PyErr_Clear();
    return false;
  }
}

void AntiAFK::call_bes_release_hold(const std::vector<int>& pids) {
  py::gil_scoped_acquire acquire;
  if (bes_release_hold_callback.is_none()) {
    return;
  }
  try {
    (void)bes_release_hold_callback(py::cast(pids));
  } catch (...) {
    PyErr_Clear();
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

void AntiAFK::cleanup_topmost_after_error(const std::string& reason,
                                          bool emit,
                                          const std::vector<std::uint64_t>* windows) {
  const int processed = windows ? clear_notopmost_windows_impl(*windows, kFailureTopmostCleanupBudgetMs)
                                : clear_notopmost_all_roblox_windows_impl(kGlobalTopmostCleanupBudgetMs);
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

ActionResult AntiAFK::run_action(HWND hwnd,
                                 const std::string& base_action,
                                 bool menu_autoreconnect,
                                 bool restore_foreground,
                                 bool fast_focus) {
  const bool debug_timings = debug_timings_enabled();

  DWORD pid = 0;
  (void)::GetWindowThreadProcessId(hwnd, &pid);

  bool in_menu = false;
  int in_menu_ms = 0;
  if (menu_autoreconnect && pid != 0 && !is_pid_in_menu_callback.is_none()) {
    const auto start_ts = Clock::now();
    if (pid != 0) {
      in_menu = call_is_pid_in_menu(pid);
    }
    in_menu_ms = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start_ts).count());
  }
  const int alt_delay_ms =
      clamp_alt_delay_ms(get_int("antiafk_alt_delay_ms", kDefaultAltDelayMs));
  int focus_ms = 0;
  int focus_attempts = 0;
  int total_ms = 0;
  ActionResult res = action_task_impl(
      hwnd,
      base_action,
      menu_autoreconnect,
      in_menu,
      alt_delay_ms,
      &stop_requested_,
      &shutdown_requested_,
      &pause_requested_,
      restore_foreground,
      fast_focus,
      debug_timings ? &focus_ms : nullptr,
      debug_timings ? &focus_attempts : nullptr,
      debug_timings ? &total_ms : nullptr);
  if (debug_timings) {
    emit_debug_timing(
        "Window action: pid=" + std::to_string(pid) + ", action=" + base_action +
        ", result=" + std::string(res.ok ? "ok" : "failed") +
        ", menu_autoreconnect=" + std::string(menu_autoreconnect ? "true" : "false") +
        ", in_menu=" + std::string(in_menu ? "true" : "false") + ", menu_ms=" +
        std::to_string(in_menu_ms) + ", focus_ms=" + std::to_string(focus_ms) +
        ", focus_attempts=" + std::to_string(focus_attempts) + ", total_ms=" +
        std::to_string(total_ms));
  }
  if (res.ok && pid != 0) {
    {
      std::lock_guard<std::mutex> lk(touch_mutex_);
      last_touch_ms_by_pid_[pid] = mono_ms();
    }
    emit_touch(pid);
  }
  return res;
}

AntiAFK::AntiAFK(py::object parent, py::object config_obj) : parent_(std::move(parent)) {
  py::gil_scoped_acquire acquire;
  if (config_obj.is_none()) {
    config = py::dict();
  } else {
    config = py::cast<py::dict>(config_obj);
  }
  ensure_defaults_locked();
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
                                std::optional<int> alt_delay_ms,
                                std::optional<bool> menu_autoreconnect,
                                std::optional<bool> alert_sound_enabled,
                                std::optional<bool> alert_tooltip_enabled,
                                std::optional<double> alert_lead_s,
                                std::optional<bool> unthrottle_enabled,
                                std::optional<int> unthrottle_batch_size,
                                std::optional<double> unthrottle_lead_s,
                                std::optional<bool> dev_mode) {
  py::gil_scoped_acquire acquire;
  if (multi_instance_enabled.has_value()) {
    config[py::str("multi_instance_enabled")] = py::bool_(*multi_instance_enabled);
  }
  if (interval.has_value()) {
    config[py::str("antiafk_interval")] = py::int_(clamp_interval_s(*interval));
  }
  if (action.has_value()) {
    config[py::str("antiafk_action")] = py::str(normalize_action_name(*action));
  }
  if (alt_delay_ms.has_value()) {
    config[py::str("antiafk_alt_delay_ms")] = py::int_(clamp_alt_delay_ms(*alt_delay_ms));
  }
  if (menu_autoreconnect.has_value()) {
    config[py::str("antiafk_menu_autoreconnect")] = py::bool_(*menu_autoreconnect);
  }
  if (alert_sound_enabled.has_value()) {
    config[py::str("antiafk_alert_sound")] = py::bool_(*alert_sound_enabled);
  }
  if (alert_tooltip_enabled.has_value()) {
    config[py::str("antiafk_alert_tooltip")] = py::bool_(*alert_tooltip_enabled);
  }
  if (alert_lead_s.has_value()) {
    config[py::str("antiafk_alert_lead_s")] = py::float_(clamp_alert_lead_s(*alert_lead_s));
  }
  if (unthrottle_enabled.has_value()) {
    config[py::str("antiafk_unthrottle_enabled")] = py::bool_(*unthrottle_enabled);
  }
  if (unthrottle_batch_size.has_value()) {
    config[py::str("antiafk_unthrottle_batch_size")] = py::int_(clamp_batch_size(*unthrottle_batch_size));
  }
  if (unthrottle_lead_s.has_value()) {
    config[py::str("antiafk_unthrottle_lead_s")] = py::float_(clamp_unthrottle_lead_s(*unthrottle_lead_s));
  }
  if (dev_mode.has_value()) {
    config[py::str("antiafk_dev_mode")] = py::bool_(*dev_mode);
  }

  // Feature removals: purge stale keys if present so they don't get re-saved.
  try {
    config.attr("pop")(py::str("antiafk_user_safe"), py::none());
    config.attr("pop")(py::str("antiafk_sequential_mode"), py::none());
    config.attr("pop")(py::str("antiafk_sequential_delay"), py::none());
  } catch (...) {
    PyErr_Clear();
  }

  std::ostringstream oss;
  oss << "Configuration updated: Interval=" << clamp_interval_s(get_int("antiafk_interval", 120))
      << "s, Action=" << normalize_action_name(get_str("antiafk_action", "space"))
      << ", AltDelayMs=" << clamp_alt_delay_ms(get_int("antiafk_alt_delay_ms", kDefaultAltDelayMs))
      << ", Pacify=" << (get_bool("antiafk_unthrottle_enabled", false) ? "True" : "False")
      << ", DebugTimings=" << (debug_timings_enabled() ? "True" : "False");
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
  stop_requested_.store(false);
  pause_requested_.store(false);
  paused_.store(false);
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
    const bool signaled = cv_.wait_for(lk, std::chrono::seconds(30), [&] {
      return paused_.load() || !antiafk_running_.load() || shutdown_requested_.load();
    });
    if (!signaled && !paused_.load() && antiafk_running_.load() && !shutdown_requested_.load()) {
      pause_requested_.store(false);
      cv_.notify_all();
    }
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
    multi_instance_last_error_.store(ERROR_SUCCESS);
    return true;
  }

  ::SetLastError(ERROR_SUCCESS);
  HANDLE h = ::CreateMutexW(nullptr, TRUE, L"ROBLOX_singletonEvent");
  if (!h) {
    // ERROR_INVALID_HANDLE means Roblox already created an Event with this
    // name. A differently named fallback does not provide multi-instancing and
    // must not be reported as success.
    multi_instance_last_error_.store(::GetLastError());
    return false;
  }

  multi_instance_mutex_ = h;
  multi_instance_last_error_.store(ERROR_SUCCESS);
  return true;
}

bool AntiAFK::disable_multi_instance() {
  py::gil_scoped_release release;
  if (multi_instance_mutex_ != nullptr) {
    ::CloseHandle(multi_instance_mutex_);
    multi_instance_mutex_ = nullptr;
  }
  multi_instance_last_error_.store(ERROR_SUCCESS);
  return true;
}

bool AntiAFK::refresh_multi_instance() {
  {
    py::gil_scoped_release release;
    if (multi_instance_mutex_ != nullptr) {
      ::CloseHandle(multi_instance_mutex_);
      multi_instance_mutex_ = nullptr;
    }
    multi_instance_last_error_.store(ERROR_SUCCESS);
  }
  return enable_multi_instance();
}

bool AntiAFK::multi_instance_guard_active() const {
  return multi_instance_mutex_ != nullptr;
}

unsigned long AntiAFK::multi_instance_last_error() const {
  return static_cast<unsigned long>(multi_instance_last_error_.load());
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
  return find_roblox_windows_impl(include_hidden, kWindowTextTimeoutMsDefault);
}

void AntiAFK::show_roblox_windows() {
  std::vector<std::uint64_t> windows;
  int visible_count = 0;
  {
    py::gil_scoped_release release;
    windows = find_roblox_windows_impl(true, kWindowTextTimeoutMsDefault);
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
    windows = find_roblox_windows_impl(false, kWindowTextTimeoutMsDefault);
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

bool AntiAFK::perform_antiafk_action(std::uint64_t hwnd_int,
                                     std::optional<std::string> action_type,
                                     bool restore_foreground) {
  const std::string base_action = normalize_action_name(action_type.value_or(get_str("antiafk_action", "space")));
  const bool menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", false);

  ActionResult res;
  {
    py::gil_scoped_release release;
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(hwnd_int));
    res = run_action(hwnd, base_action, menu_autoreconnect, restore_foreground, /*fast_focus=*/false);
  }

  if (!res.ok && !res.message.empty()) {
    emit_status(res.message);
  }
  return res.ok;
}

void AntiAFK::test_action() {
  const std::string action = normalize_action_name(get_str("antiafk_action", "space"));
  const bool menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", false);
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
      const ActionResult res =
          run_action(hwnd, action, menu_autoreconnect, /*restore_foreground=*/false, /*fast_focus=*/false);
      if (!res.ok) {
        if (!res.message.empty()) {
          emit_status(res.message);
        }
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

  std::vector<std::uint64_t> roblox_windows;
  {
    py::gil_scoped_release release;
    roblox_windows = find_roblox_windows_impl(true, kWindowTextTimeoutMsDefault);
  }

  if (roblox_windows.empty()) {
    emit_status("No main Roblox windows found by Anti-AFK discovery!");
    return;
  }

  emit_status("Found " + std::to_string(roblox_windows.size()) +
              " main Roblox window(s) by Anti-AFK discovery:");
  for (const auto w : roblox_windows) {
    const HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(w));
    DWORD pid = 0;
    (void)::GetWindowThreadProcessId(hwnd, &pid);
    const std::wstring pname = process_basename(pid).value_or(L"unknown");
    std::ostringstream oss;
    oss << "Window: '" << wide_to_utf8(window_text(hwnd)) << "' (handle: " << w
        << ", process: " << wide_to_utf8(pname) << ", PID: " << pid << ")";
    emit_status(oss.str());
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

  const std::string action_type = normalize_action_name(get_str("antiafk_action", "space"));
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
    const ActionResult res =
        run_action(hwnd, action_type, menu_autoreconnect, /*restore_foreground=*/false, /*fast_focus=*/false);
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

    int interval = clamp_interval_s(get_int("antiafk_interval", 120));
    std::string action_type = normalize_action_name(get_str("antiafk_action", "space"));
    bool menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", false);
    const int alt_delay_ms =
        clamp_alt_delay_ms(get_int("antiafk_alt_delay_ms", kDefaultAltDelayMs));

    emit_status(
        "Settings: Interval=" + std::to_string(interval) + "s, Action=" + action_type +
        ", AltDelayMs=" + std::to_string(alt_delay_ms) +
        ", MenuAutoReconnect=" + std::string(menu_autoreconnect ? "True" : "False"));

    auto last_action_time = Clock::now() - std::chrono::seconds(std::max(0, interval - 10));
    auto last_status_update = Clock::now() - std::chrono::seconds(kNoWindowsStatusEveryS + 1);
    std::optional<Clock::time_point> pause_started;
    std::vector<std::uint64_t> sweep_windows;
    size_t sweep_index = 0;
    bool sweep_action_success = true;
    HWND sweep_foreground = nullptr;
    bool any_pacify_fail = false;
    auto last_pacify_fail_log = Clock::now() - std::chrono::seconds(31);
    size_t last_window_count = 0;
    bool sweep_fast_focus = false;
    bool last_fast_focus = false;
    bool pre_action_alert_sent = false;
    std::optional<Clock::time_point> sweep_started;
    size_t sweep_total_windows = 0;

    while (!stop_requested_.load() && !shutdown_requested_.load()) {
      if (pause_requested_.load()) {
        if (!pause_started.has_value()) {
          pause_started = Clock::now();
          paused_.store(true);
          cv_.notify_all();
          emit_status("Anti-AFK paused");
          pre_action_alert_sent = false;
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
        if (sweep_started.has_value()) {
          *sweep_started += paused_for;
        }
        pause_started.reset();
        paused_.store(false);
        emit_status("Anti-AFK resumed");
      }

      interval = clamp_interval_s(get_int("antiafk_interval", interval));
      action_type = normalize_action_name(get_str("antiafk_action", action_type));
      menu_autoreconnect = get_bool("antiafk_menu_autoreconnect", menu_autoreconnect);

      const auto now = Clock::now();

      const double elapsed_s =
          std::chrono::duration_cast<std::chrono::duration<double>>(now - last_action_time).count();

      const bool in_sweep = !sweep_windows.empty() && sweep_index < sweep_windows.size();

      const bool alert_enabled =
          get_bool("antiafk_alert_sound", false) || get_bool("antiafk_alert_tooltip", false);
      if (alert_enabled && !in_sweep && !pre_action_alert_sent) {
        double lead_s = clamp_alert_lead_s(get_double("antiafk_alert_lead_s", 0.0));
        const double max_lead = std::max(0.0, static_cast<double>(std::max(0, interval)));
        if (lead_s > max_lead) {
          lead_s = max_lead;
        }

        const auto next_action_time = last_action_time + std::chrono::seconds(std::max(0, interval));
        const double time_to_action_s =
            std::chrono::duration_cast<std::chrono::duration<double>>(next_action_time - now).count();
        if (time_to_action_s > 0.0 && time_to_action_s <= lead_s) {
          bool has_windows = last_window_count > 0;
          if (!has_windows) {
            const std::vector<std::uint64_t> probe =
                find_roblox_windows_impl(true, kWindowTextTimeoutMsFast);
            has_windows = !probe.empty();
          }
          if (has_windows) {
            emit_pre_action(time_to_action_s);
            pre_action_alert_sent = true;
          }
        }
      }

      if (!in_sweep && elapsed_s >= static_cast<double>(interval)) {
        const bool fast_scan = last_window_count >= kFastSweepWindowThreshold;
        const int text_timeout_ms =
            fast_scan ? kWindowTextTimeoutMsFast : kWindowTextTimeoutMsDefault;
        sweep_started = Clock::now();
        const auto scan_started = Clock::now();
        sweep_windows = find_roblox_windows_impl(true, text_timeout_ms);
        const int scan_ms = elapsed_ms_since(scan_started);
        const auto sort_started = Clock::now();
        sort_windows_by_touch(sweep_windows);
        const int sort_ms = elapsed_ms_since(sort_started);
        sweep_index = 0;
        sweep_action_success = true;
        any_pacify_fail = false;
        const auto foreground_started = Clock::now();
        sweep_foreground = ::GetForegroundWindow();
        const int foreground_ms = elapsed_ms_since(foreground_started);
        sweep_fast_focus = sweep_windows.size() >= kFastSweepWindowThreshold;
        last_window_count = sweep_windows.size();
        sweep_total_windows = sweep_windows.size();
        if (sweep_fast_focus != last_fast_focus) {
          last_fast_focus = sweep_fast_focus;
          emit_status(sweep_fast_focus ? "Anti-AFK fast focus mode enabled (large window count)"
                                       : "Anti-AFK fast focus mode disabled");
        }
        {
          std::ostringstream timing;
          timing << "Sweep scan: windows=" << sweep_windows.size()
                 << ", scan_ms=" << scan_ms
                 << ", sort_ms=" << sort_ms
                 << ", foreground_ms=" << foreground_ms
                 << ", text_timeout_ms=" << text_timeout_ms
                 << ", fast_focus=" << (sweep_fast_focus ? "true" : "false");
          emit_debug_timing(timing.str());
        }

        if (sweep_windows.empty()) {
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

        if (alert_enabled && !pre_action_alert_sent) {
          emit_pre_action(0.0);
          pre_action_alert_sent = true;
        }

        emit_status("Performing Anti-AFK action on " + std::to_string(sweep_windows.size()) +
                    " Roblox window(s)");
      }

      // Continue an in-progress sweep even if the interval hasn't elapsed yet.
      if (!sweep_windows.empty() && sweep_index < sweep_windows.size()) {
        while (sweep_index < sweep_windows.size()) {
          if (stop_requested_.load() || shutdown_requested_.load() || pause_requested_.load()) {
            break;
          }

          const bool unthrottle_enabled = get_bool("antiafk_unthrottle_enabled", false);
          const int batch_size = clamp_batch_size(get_int("antiafk_unthrottle_batch_size", 5));
          const double lead_s = clamp_unthrottle_lead_s(get_double("antiafk_unthrottle_lead_s", 0.0));
          const int current_alt_delay_ms =
              clamp_alt_delay_ms(get_int("antiafk_alt_delay_ms", kDefaultAltDelayMs));

          const size_t batch_end =
              std::min(sweep_index + static_cast<size_t>(batch_size), sweep_windows.size());
          const size_t batch_start_index = sweep_index;
          const auto batch_started = Clock::now();
          int collect_pids_ms = 0;
          int hold_request_ms = 0;
          int hold_wait_ms = 0;
          int actions_ms = 0;
          int release_ms = 0;
          int hold_extend_ms = 0;
          int hold_extend_count = 0;
          double hold_seconds = 0.0;
          size_t action_windows = 0;
          size_t action_failures = 0;
          bool hold_attempted = false;
          bool hold_ok = false;
          size_t held_pid_count = 0;

          bool can_unthrottle = false;
          if (unthrottle_enabled) {
            py::gil_scoped_acquire acquire;
            can_unthrottle =
                !bes_hold_unthrottled_callback.is_none() && !bes_release_hold_callback.is_none();
          }

          std::vector<int> held_pids;
          bool release_needed = false;
          auto last_hold_extend = Clock::now();
          auto release_held = [&]() {
            if (!release_needed || held_pids.empty()) {
              return;
            }
            const auto release_started = Clock::now();
            call_bes_release_hold(held_pids);
            release_ms += elapsed_ms_since(release_started);
            release_needed = false;
          };

          try {
            if (can_unthrottle) {
              const auto collect_started = Clock::now();
              std::unordered_set<DWORD> seen;
              seen.reserve(batch_end - sweep_index);
              for (size_t i = sweep_index; i < batch_end; ++i) {
                const HWND hwnd =
                    reinterpret_cast<HWND>(static_cast<std::uintptr_t>(sweep_windows[i]));
                if (!hwnd || !::IsWindow(hwnd)) {
                  continue;
                }
                DWORD pid = 0;
                (void)::GetWindowThreadProcessId(hwnd, &pid);
                if (pid == 0) {
                  continue;
                }
                if (seen.insert(pid).second) {
                  held_pids.push_back(static_cast<int>(pid));
                }
              }
              collect_pids_ms = elapsed_ms_since(collect_started);
              held_pid_count = held_pids.size();

              if (!held_pids.empty()) {
                hold_seconds = estimated_action_hold_s(
                    action_type, menu_autoreconnect, current_alt_delay_ms, batch_size, lead_s, sweep_fast_focus);
                hold_attempted = true;
                release_needed = true;
                const auto hold_started = Clock::now();
                hold_ok = call_bes_hold_unthrottled(held_pids, hold_seconds);
                hold_request_ms = elapsed_ms_since(hold_started);
                last_hold_extend = Clock::now();
                if (!hold_ok) {
                  any_pacify_fail = true;
                  if (std::chrono::duration_cast<std::chrono::seconds>(Clock::now() - last_pacify_fail_log)
                          .count() >= 30) {
                    last_pacify_fail_log = Clock::now();
                    emit_status(
                        "BES pacify: failed to request unthrottle for this batch; inputs may be throttled.");
                  }
                } else {
                  const double apply_wait_s = std::max(lead_s, 0.25);
                  if (apply_wait_s > 0.0) {
                    const auto apply_wait_ms = std::chrono::milliseconds(
                        static_cast<long long>(std::clamp(apply_wait_s, 0.0, kMaxUnthrottleLeadS) * 1000.0));
                    const auto wait_started = Clock::now();
                    (void)wait_for_stop_or_pause(apply_wait_ms);
                    hold_wait_ms = elapsed_ms_since(wait_started);
                  }
                }
              }
            }

            if (hold_attempted && !hold_ok) {
              sweep_action_success = false;
              action_failures += (batch_end > sweep_index) ? (batch_end - sweep_index) : 0;
              sweep_index = batch_end;
            } else {
              // Process the batch window-by-window so we can resume mid-batch after a pause.
              const auto actions_started = Clock::now();
              auto refresh_hold = [&]() {
                if (!hold_ok || held_pids.empty() || hold_seconds <= 0.0) {
                  return;
                }
                const double elapsed_s =
                    std::chrono::duration_cast<std::chrono::duration<double>>(Clock::now() - last_hold_extend).count();
                if (elapsed_s < std::max(5.0, hold_seconds * 0.45)) {
                  return;
                }
                const auto extend_started = Clock::now();
                if (call_bes_hold_unthrottled(held_pids, hold_seconds)) {
                  hold_extend_count += 1;
                }
                hold_extend_ms += elapsed_ms_since(extend_started);
                last_hold_extend = Clock::now();
              };
              while (sweep_index < batch_end) {
                if (stop_requested_.load() || shutdown_requested_.load() || pause_requested_.load()) {
                  break;
                }

                refresh_hold();
                const HWND hwnd =
                    reinterpret_cast<HWND>(static_cast<std::uintptr_t>(sweep_windows[sweep_index]));
                ++action_windows;
                const ActionResult res =
                    run_action(hwnd, action_type, menu_autoreconnect, /*restore_foreground=*/false, sweep_fast_focus);

                if (!res.ok) {
                  // If we were interrupted (pause/stop/shutdown) mid-action, retry this same window after resume.
                  if (stop_requested_.load() || shutdown_requested_.load() || pause_requested_.load()) {
                    break;
                  }

                  sweep_action_success = false;
                  ++action_failures;
                  if (!res.message.empty()) {
                    emit_status(res.message);
                  }
                }

                ++sweep_index;
                refresh_hold();
              }
              actions_ms = elapsed_ms_since(actions_started);
            }
            release_held();
          } catch (...) {
            release_held();
            throw;
          }

          {
            std::ostringstream timing;
            const size_t processed = (sweep_index > batch_start_index)
                                         ? (sweep_index - batch_start_index)
                                         : 0;
            timing << "Batch: start=" << (batch_start_index + 1)
                   << ", processed=" << processed
                   << ", total_windows=" << sweep_windows.size()
                   << ", total_ms=" << elapsed_ms_since(batch_started)
                   << ", actions_ms=" << actions_ms
                   << ", action_windows=" << action_windows
                   << ", action_failures=" << action_failures
                   << ", unthrottle="
                   << (can_unthrottle ? "enabled" : (unthrottle_enabled ? "unavailable" : "disabled"))
                   << ", held_pids=" << held_pid_count
                   << ", collect_pids_ms=" << collect_pids_ms
                   << ", hold_attempted=" << (hold_attempted ? "true" : "false")
                   << ", hold_ok=" << (hold_ok ? "true" : "false")
                   << ", hold_s=" << hold_seconds
                   << ", hold_request_ms=" << hold_request_ms
                   << ", hold_wait_ms=" << hold_wait_ms
                   << ", hold_extend_count=" << hold_extend_count
                   << ", hold_extend_ms=" << hold_extend_ms
                   << ", release_ms=" << release_ms;
            if (stop_requested_.load() || shutdown_requested_.load() || pause_requested_.load()) {
              timing << ", interrupted=true";
            }
            emit_debug_timing(timing.str());
          }

          if (stop_requested_.load() || shutdown_requested_.load() || pause_requested_.load()) {
            break;
          }
        }

        if (pause_requested_.load()) {
          continue;
        }
        if (stop_requested_.load() || shutdown_requested_.load()) {
          break;
        }

        // Sweep complete.
        int restore_ms = 0;
        if (sweep_foreground && ::IsWindow(sweep_foreground)) {
          const auto restore_started = Clock::now();
          restore_foreground_window_impl(sweep_foreground);
          restore_ms = elapsed_ms_since(restore_started);
        }
        int cleanup_ms = 0;
        if (!sweep_action_success) {
          const auto cleanup_started = Clock::now();
          cleanup_topmost_after_error("action failed", /*emit=*/false, &sweep_windows);
          cleanup_ms = elapsed_ms_since(cleanup_started);
        }
        if (sweep_started.has_value()) {
          std::ostringstream timing;
          timing << "Sweep complete: windows=" << sweep_total_windows
                 << ", success=" << (sweep_action_success ? "true" : "false")
                 << ", pacify_fail=" << (any_pacify_fail ? "true" : "false")
                 << ", total_ms=" << elapsed_ms_since(*sweep_started)
                 << ", restore_foreground_ms=" << restore_ms
                 << ", cleanup_ms=" << cleanup_ms;
          emit_debug_timing(timing.str());
        }
        sweep_windows.clear();
        sweep_index = 0;
        sweep_started.reset();
        sweep_total_windows = 0;

        if (sweep_action_success) {
          last_action_time = Clock::now();
          pre_action_alert_sent = false;
          emit_status("Anti-AFK action completed successfully");
          (void)wait_for_stop_or_pause(std::chrono::milliseconds(500));
        } else {
          const int retry_backoff_s = std::min(interval, kFailedSweepRetryBackoffS);
          last_action_time = Clock::now() - std::chrono::seconds(std::max(0, interval - retry_backoff_s));
          pre_action_alert_sent = false;
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

void AntiAFK::shutdown() {
  shutdown_impl(/*emit=*/false);
}

void AntiAFK::shutdown_impl(bool emit) {
  shutdown_requested_.store(true);
  stop_requested_.store(true);
  pause_requested_.store(false);
  paused_.store(false);
  cv_.notify_all();

  std::thread t1 = std::move(antiafk_thread_);
  std::thread t3 = std::move(test_thread_);

  {
    py::gil_scoped_release release;
    if (t1.joinable()) {
      t1.join();
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
      py::arg("alt_delay_ms") = std::nullopt,
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
      .def_readwrite("touch_callback", &AntiAFK::touch_callback)
      .def_readwrite("pre_action_callback", &AntiAFK::pre_action_callback)
      .def_readwrite("is_pid_in_menu_callback", &AntiAFK::is_pid_in_menu_callback)
      .def_readwrite("bes_hold_unthrottled_callback", &AntiAFK::bes_hold_unthrottled_callback)
      .def_readwrite("bes_release_hold_callback", &AntiAFK::bes_release_hold_callback)
      .def("touch_pids", &AntiAFK::touch_pids, py::arg("pids"))
      .def("set_pids_disconnected",
           &AntiAFK::set_pids_disconnected,
           py::arg("pids"),
           py::arg("disconnected") = true)
      .def("apply_host_config",
           &AntiAFK::apply_host_config,
           py::kw_only(),
           py::arg("multi_instance_enabled") = std::nullopt,
           py::arg("interval") = std::nullopt,
           py::arg("action") = std::nullopt,
           py::arg("alt_delay_ms") = std::nullopt,
           py::arg("menu_autoreconnect") = std::nullopt,
           py::arg("alert_sound_enabled") = std::nullopt,
           py::arg("alert_tooltip_enabled") = std::nullopt,
           py::arg("alert_lead_s") = std::nullopt,
           py::arg("unthrottle_enabled") = std::nullopt,
           py::arg("unthrottle_batch_size") = std::nullopt,
           py::arg("unthrottle_lead_s") = std::nullopt,
           py::arg("dev_mode") = std::nullopt)
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
           py::arg("action_type") = std::nullopt,
           py::arg("restore_foreground") = true)
      .def("test_action", &AntiAFK::test_action)
      .def("test_action_with_delay", &AntiAFK::test_action_with_delay)
      .def("is_window_fullscreen", &AntiAFK::is_window_fullscreen, py::arg("hwnd"))
      .def("restore_foreground_window", &AntiAFK::restore_foreground_window, py::arg("hwnd"))
      .def("enable_multi_instance", &AntiAFK::enable_multi_instance)
      .def("disable_multi_instance", &AntiAFK::disable_multi_instance)
      .def("refresh_multi_instance", &AntiAFK::refresh_multi_instance)
      .def("multi_instance_guard_active", &AntiAFK::multi_instance_guard_active)
      .def("multi_instance_last_error", &AntiAFK::multi_instance_last_error)
      .def("toggle_multi_instance", &AntiAFK::toggle_multi_instance)
      .def("update_status", &AntiAFK::update_status, py::arg("message"))
      .def("update_button_states", &AntiAFK::update_button_states)
      .def("log_error", &AntiAFK::log_error, py::arg("exception"), py::arg("message") = py::none());
}
