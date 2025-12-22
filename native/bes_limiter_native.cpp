#ifdef _WIN32
#  define NOMINMAX
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <tlhelp32.h>
#  include <mmsystem.h>
#  pragma comment(lib, "winmm.lib")
#else
#  error "bes_limiter_native is Windows-only."
#endif

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <ctime>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

constexpr DWORD kInvalidSuspendCount = 0xFFFFFFFFu;

[[noreturn]] void throw_win_error(const char* prefix) {
  const DWORD err = ::GetLastError();
  std::ostringstream oss;
  oss << prefix << " (WinError " << err << ")";
  PyErr_SetString(PyExc_OSError, oss.str().c_str());
  throw py::error_already_set();
}

std::string now_hhmmss() {
  std::time_t t = std::time(nullptr);
  std::tm tm{};
  localtime_s(&tm, &t);
  char buf[16]{};
  std::strftime(buf, sizeof(buf), "%H:%M:%S", &tm);
  return std::string(buf);
}

double wall_time_s() {
  using clock = std::chrono::system_clock;
  return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

double mono_time_s() {
  using clock = std::chrono::steady_clock;
  return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

int clamp_pct(int pct) {
  if (pct < 0) {
    return 0;
  }
  if (pct > 99) {
    return 99;
  }
  return pct;
}

std::pair<int, int> compute_red_green_ms(int cycle_ms, int pct) {
  const int cycle = std::max(10, cycle_ms);
  const int p = clamp_pct(pct);
  if (p <= 0) {
    return {0, cycle};
  }
  int red_ms = static_cast<int>((static_cast<long long>(cycle) * p) / 100);
  int green_ms = std::max(1, cycle - red_ms);
  if (p >= 99) {
    red_ms = cycle - 1;
    green_ms = 1;
  }
  return {red_ms, green_ms};
}

std::vector<int> list_thread_ids_win(int pid) {
  HANDLE snap = ::CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
  if (snap == INVALID_HANDLE_VALUE) {
    throw_win_error("CreateToolhelp32Snapshot(THREAD) failed");
  }

  std::vector<int> tids;
  THREADENTRY32 te{};
  te.dwSize = sizeof(THREADENTRY32);
  BOOL ok = ::Thread32First(snap, &te);
  while (ok) {
    if (static_cast<int>(te.th32OwnerProcessID) == pid) {
      tids.push_back(static_cast<int>(te.th32ThreadID));
    }
    ok = ::Thread32Next(snap, &te);
  }

  ::CloseHandle(snap);
  return tids;
}

std::unordered_map<int, std::vector<int>> list_thread_ids_for_pids_win(const std::vector<int>& pids) {
  std::unordered_set<int> pidset;
  pidset.reserve(pids.size());
  for (int p : pids) {
    pidset.insert(static_cast<int>(p));
  }

  std::unordered_map<int, std::vector<int>> out;
  out.reserve(pidset.size());
  for (int p : pidset) {
    out.emplace(p, std::vector<int>{});
  }
  if (pidset.empty()) {
    return out;
  }

  HANDLE snap = ::CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
  if (snap == INVALID_HANDLE_VALUE) {
    throw_win_error("CreateToolhelp32Snapshot(THREAD) failed");
  }

  THREADENTRY32 te{};
  te.dwSize = sizeof(THREADENTRY32);
  BOOL ok = ::Thread32First(snap, &te);
  while (ok) {
    const int owner = static_cast<int>(te.th32OwnerProcessID);
    if (pidset.find(owner) != pidset.end()) {
      out[owner].push_back(static_cast<int>(te.th32ThreadID));
    }
    ok = ::Thread32Next(snap, &te);
  }

  ::CloseHandle(snap);
  return out;
}

HANDLE open_thread_handle_win(int tid) {
  HANDLE h = ::OpenThread(THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION, FALSE, static_cast<DWORD>(tid));
  return h ? h : nullptr;
}

class PythonLogQueue {
 public:
  void push(std::string msg) {
    std::lock_guard<std::mutex> g(mutex_);
    if (closed_) {
      return;
    }
    queue_.push_back(std::move(msg));
    cv_.notify_one();
  }

  bool pop(std::string& out) {
    std::unique_lock<std::mutex> lk(mutex_);
    cv_.wait(lk, [&] { return closed_ || !queue_.empty(); });
    if (queue_.empty()) {
      return false;
    }
    out = std::move(queue_.front());
    queue_.pop_front();
    return true;
  }

  void close() {
    std::lock_guard<std::mutex> g(mutex_);
    closed_ = true;
    cv_.notify_all();
  }

 private:
  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<std::string> queue_;
  bool closed_{false};
};

struct PidState {
  int pid{0};
  std::string name;
  int pct{0};

  std::unordered_map<int, HANDLE> handles;  // tid -> HANDLE
  std::unordered_map<int, int> depth;       // tid -> suspend depth created by us
  int total_depth{0};

  bool is_suspended{false};
  double last_refresh_monotonic{0.0};
  int gen{0};

  double next_event_at{0.0};
  int scheduled_gen{-1};

  std::uint32_t phase_seed{0};
};

class PidCleanupQueue {
 public:
  void push(std::unique_ptr<PidState> st) {
    std::lock_guard<std::mutex> g(mutex_);
    if (closed_) {
      return;
    }
    queue_.push_back(std::move(st));
    cv_.notify_one();
  }

  bool pop(std::unique_ptr<PidState>& out) {
    std::unique_lock<std::mutex> lk(mutex_);
    cv_.wait(lk, [&] { return closed_ || !queue_.empty(); });
    if (queue_.empty()) {
      return false;
    }
    out = std::move(queue_.front());
    queue_.pop_front();
    return true;
  }

  void close() {
    std::lock_guard<std::mutex> g(mutex_);
    closed_ = true;
    cv_.notify_all();
  }

 private:
  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<std::unique_ptr<PidState>> queue_;
  bool closed_{false};
};

class BESLimiterWorker {
 public:
  BESLimiterWorker(
      int pid,
      int reduce_percent,
      int cycle_ms,
      py::object logq = py::none(),
      std::string name = "")
      : pid_(pid),
        reduce_percent_(clamp_pct(reduce_percent)),
        cycle_ms_(std::max(10, cycle_ms)),
        logq_(std::move(logq)),
        name_(name.empty() ? ("PID " + std::to_string(pid)) : std::move(name)) {}

  ~BESLimiterWorker() {
    py::gil_scoped_release release;
    stop(2.0);
  }

  int pid() const { return pid_; }

  int reduce_percent() const { return reduce_percent_.load(); }
  void set_reduce_percent(int pct) { reduce_percent_.store(clamp_pct(pct)); }

  int cycle_ms() const { return cycle_ms_.load(); }
  void set_cycle_ms(int ms) { cycle_ms_.store(std::max(10, ms)); }

  std::string name() const {
    std::lock_guard<std::mutex> g(name_mutex_);
    return name_;
  }
  void set_name(const std::string& name) {
    if (name.empty()) {
      return;
    }
    std::lock_guard<std::mutex> g(name_mutex_);
    name_ = name;
  }

  void request_stop() { stop_.store(true); }

  void start() {
    std::lock_guard<std::mutex> g(thread_mutex_);
    if (thread_.joinable()) {
      return;
    }
    stop_.store(false);
    thread_ = std::thread([this] { run(); });
  }

  void stop(double /*join_timeout*/ = 2.0) {
    request_stop();
    std::thread t;
    {
      std::lock_guard<std::mutex> g(thread_mutex_);
      t = std::move(thread_);
    }
    if (t.joinable()) {
      t.join();
    }
    resume_balanced();
    close_all_handles();
  }

  bool is_running() const {
    std::lock_guard<std::mutex> g(thread_mutex_);
    return thread_.joinable() && !stop_.load();
  }

  void resume_balanced() { balanced_resume_all(); }

 private:
  void log(const std::string& msg) {
    std::string prefix;
    {
      std::lock_guard<std::mutex> g(name_mutex_);
      prefix = name_;
    }
    const std::string full = "[" + now_hhmmss() + "] [" + prefix + "] " + msg;
    try {
      py::gil_scoped_acquire gil;
      if (logq_.is_none()) {
        return;
      }
      logq_.attr("put_nowait")(full);
    } catch (...) {
    }
  }

  void refresh_threads() {
    std::unordered_set<int> tids;
    try {
      for (int tid : list_thread_ids_win(pid_)) {
        tids.insert(tid);
      }
    } catch (...) {
      tids.clear();
    }

    std::lock_guard<std::mutex> g(handles_mutex_);
    for (auto it = handles_.begin(); it != handles_.end();) {
      const int tid = it->first;
      if (tids.find(tid) == tids.end()) {
        ::CloseHandle(it->second);
        const int depth = depth_.count(tid) ? depth_[tid] : 0;
        if (depth > 0) {
          total_depth_ = std::max(0, total_depth_ - depth);
        }
        depth_.erase(tid);
        it = handles_.erase(it);
      } else {
        ++it;
      }
    }

    for (int tid : tids) {
      if (handles_.find(tid) != handles_.end()) {
        continue;
      }
      HANDLE h = open_thread_handle_win(tid);
      if (!h) {
        continue;
      }
      handles_[tid] = h;
      depth_.try_emplace(tid, 0);
    }
  }

  void balanced_resume_all() {
    std::lock_guard<std::mutex> g(handles_mutex_);
    if (total_depth_ <= 0) {
      return;
    }
    for (const auto& kv : handles_) {
      const int tid = kv.first;
      HANDLE h = kv.second;
      const int depth = depth_.count(tid) ? depth_[tid] : 0;
      if (depth <= 0) {
        continue;
      }
      int resumed = 0;
      for (int i = 0; i < depth; ++i) {
        const DWORD prev = ::ResumeThread(h);
        if (prev == kInvalidSuspendCount) {
          break;
        }
        resumed += 1;
      }
      if (resumed) {
        depth_[tid] = std::max(0, depth - resumed);
        total_depth_ = std::max(0, total_depth_ - resumed);
      }
    }
  }

  void close_all_handles() {
    std::lock_guard<std::mutex> g(handles_mutex_);
    for (const auto& kv : handles_) {
      ::CloseHandle(kv.second);
    }
    handles_.clear();
    depth_.clear();
    total_depth_ = 0;
  }

  void run() {
    log("Starting limiter: reduce=" + std::to_string(reduce_percent()) +
        "% cycle=" + std::to_string(cycle_ms()) + "ms");

    double last_refresh = 0.0;
    while (!stop_.load()) {
      const double now = wall_time_s();
      if ((now - last_refresh) > 2.0) {
        refresh_threads();
        last_refresh = now;
      }

      const int pct = clamp_pct(reduce_percent());
      const int cycle = std::max(10, cycle_ms());

      if (pct <= 0) {
        balanced_resume_all();
        std::this_thread::sleep_for(std::chrono::milliseconds(cycle));
        continue;
      }

      const auto [red_ms, green_ms] = compute_red_green_ms(cycle, pct);

      {
        std::lock_guard<std::mutex> g(handles_mutex_);
        for (const auto& kv : handles_) {
          const int tid = kv.first;
          HANDLE h = kv.second;
          const DWORD prev = ::SuspendThread(h);
          if (prev != kInvalidSuspendCount) {
            depth_[tid] = (depth_.count(tid) ? depth_[tid] : 0) + 1;
            total_depth_ += 1;
          }
        }
      }

      std::this_thread::sleep_for(std::chrono::milliseconds(red_ms));

      {
        std::lock_guard<std::mutex> g(handles_mutex_);
        if (total_depth_ > 0) {
          for (const auto& kv : handles_) {
            const int tid = kv.first;
            HANDLE h = kv.second;
            const int depth = depth_.count(tid) ? depth_[tid] : 0;
            if (depth <= 0) {
              continue;
            }
            const DWORD prev = ::ResumeThread(h);
            if (prev != kInvalidSuspendCount) {
              depth_[tid] = std::max(0, depth - 1);
              total_depth_ = std::max(0, total_depth_ - 1);
            }
          }
        }
      }

      std::this_thread::sleep_for(std::chrono::milliseconds(green_ms));
    }

    log("Limiter thread exiting.");
  }

  int pid_{0};
  std::atomic<int> reduce_percent_{0};
  std::atomic<int> cycle_ms_{50};

  py::object logq_;

  mutable std::mutex name_mutex_;
  std::string name_;

  std::atomic<bool> stop_{false};

  mutable std::mutex thread_mutex_;
  std::thread thread_;

  mutable std::mutex handles_mutex_;
  std::unordered_map<int, HANDLE> handles_;
  std::unordered_map<int, int> depth_;
  int total_depth_{0};
};

class BESMultiProcessController {
 public:
  BESMultiProcessController(
      int cycle_ms = 50,
      py::object log = py::none(),
      bool auto_scale_cycle = true,
      bool stagger_phases = true,
      double refresh_interval_s = 1.0,
      int max_cycle_ms = 400,
      int min_cycle_ms_per_pid = 2)
      : cycle_ms_(std::max(10, cycle_ms)),
        effective_cycle_ms_(std::max(10, cycle_ms)),
        auto_scale_cycle_(auto_scale_cycle),
        stagger_phases_(stagger_phases),
        refresh_interval_s_(std::max(0.25, refresh_interval_s)),
        max_cycle_ms_(std::max(20, max_cycle_ms)),
        min_cycle_ms_per_pid_(std::max(0, min_cycle_ms_per_pid)),
        log_cb_(std::move(log)) {
    cleaner_thread_ = std::thread([this] { cleaner_loop(); });
    log_thread_ = std::thread([this] { pump_log_loop(); });
  }

  ~BESMultiProcessController() {
    py::gil_scoped_release release;
    shutdown();
  }

  void set_enabled(bool enabled) {
    if (shutdown_.load()) {
      return;
    }
    enabled = static_cast<bool>(enabled);
    const bool was_enabled = enabled_.load();
    if (enabled == was_enabled) {
      return;
    }

    if (!enabled) {
      stop_scheduler();
      disable_timer_resolution();
      {
        std::lock_guard<std::mutex> g(config_mutex_);
        desired_pcts_.clear();
        desired_names_.clear();
        hold_until_.clear();
        force_resume_.clear();
      }
      return;
    }

    enable_timer_resolution();
    start_scheduler();
  }

  void set_cycle_ms(int ms) {
    cycle_ms_.store(std::max(10, ms));
    wake_scheduler();
  }

  void hold_unthrottled(int pid, double seconds) {
    pid = static_cast<int>(pid);
    const double until = wall_time_s() + std::max(0.0, seconds);
    {
      std::lock_guard<std::mutex> g(config_mutex_);
      const double cur = hold_until_.count(pid) ? hold_until_[pid] : 0.0;
      if (until > cur) {
        hold_until_[pid] = until;
      }
      force_resume_.insert(pid);
    }
    wake_scheduler();
  }

  void release_hold(int pid) {
    pid = static_cast<int>(pid);
    {
      std::lock_guard<std::mutex> g(config_mutex_);
      hold_until_.erase(pid);
    }
    wake_scheduler();
  }

  void apply(const std::unordered_map<int, int>& target_pcts, py::object names_obj = py::none()) {
    if (!enabled_.load() || shutdown_.load()) {
      return;
    }
    std::unordered_map<int, std::string> names;
    if (!names_obj.is_none()) {
      names = names_obj.cast<std::unordered_map<int, std::string>>();
    }

    std::unordered_map<int, int> clamped;
    clamped.reserve(target_pcts.size());
    for (const auto& kv : target_pcts) {
      clamped.emplace(static_cast<int>(kv.first), clamp_pct(static_cast<int>(kv.second)));
    }

    {
      std::lock_guard<std::mutex> g(config_mutex_);
      desired_pcts_ = std::move(clamped);
      desired_names_ = std::move(names);
    }
    wake_scheduler();
  }

  py::dict snapshot() {
    const double now = wall_time_s();
    int holds_count = 0;
    int active = 0;
    int desired_pids = 0;
    const int cycle_ms = cycle_ms_.load();
    const int effective_cycle_ms = effective_cycle_ms_.load();
    const bool enabled = enabled_.load();

    {
      std::lock_guard<std::mutex> g(config_mutex_);
      desired_pids = static_cast<int>(desired_pcts_.size());
      for (const auto& kv : hold_until_) {
        if (kv.second > now) {
          holds_count += 1;
        }
      }
      for (const auto& kv : desired_pcts_) {
        const int pid = kv.first;
        const int pct = kv.second;
        const double exp = hold_until_.count(pid) ? hold_until_[pid] : 0.0;
        if (exp > now) {
          continue;
        }
        if (pct > 0) {
          active += 1;
        }
      }
    }

    py::dict out;
    out["enabled"] = enabled;
    out["cycle_ms"] = cycle_ms;
    out["effective_cycle_ms"] = effective_cycle_ms;
    out["pids"] = desired_pids;
    out["active"] = active;
    out["holds"] = holds_count;
    return out;
  }

  void shutdown() {
    bool expected = false;
    if (!shutdown_.compare_exchange_strong(expected, true)) {
      return;
    }

    stop_scheduler();
    disable_timer_resolution();

    cleanup_q_.close();
    log_q_.close();

    if (cleaner_thread_.joinable()) {
      cleaner_thread_.join();
    }
    if (log_thread_.joinable()) {
      log_thread_.join();
    }
  }

 private:
  struct Event {
    double when{0.0};
    int pid{0};
    int action{0};  // 0=suspend, 1=resume
    int gen{0};
  };

  struct EventCompare {
    bool operator()(const Event& a, const Event& b) const { return a.when > b.when; }
  };

  void log_line(const std::string& msg) {
    log_q_.push("[" + now_hhmmss() + "] [BES] " + msg);
  }

  void pump_log_loop() {
    while (true) {
      std::string msg;
      if (!log_q_.pop(msg)) {
        break;
      }
      try {
        py::gil_scoped_acquire gil;
        if (log_cb_.is_none()) {
          continue;
        }
        log_cb_(msg);
      } catch (...) {
      }
    }
  }

  void enable_timer_resolution() {
    if (timer_res_enabled_.load()) {
      return;
    }
    const MMRESULT r = ::timeBeginPeriod(1);
    timer_res_enabled_.store(r == TIMERR_NOERROR);
  }

  void disable_timer_resolution() {
    if (!timer_res_enabled_.load()) {
      return;
    }
    ::timeEndPeriod(1);
    timer_res_enabled_.store(false);
  }

  void balanced_resume_all(PidState& st) {
    if (st.total_depth <= 0) {
      st.is_suspended = false;
      return;
    }
    for (const auto& kv : st.handles) {
      const int tid = kv.first;
      HANDLE h = kv.second;
      const int depth = st.depth.count(tid) ? st.depth[tid] : 0;
      if (depth <= 0) {
        continue;
      }
      int resumed = 0;
      for (int i = 0; i < depth; ++i) {
        const DWORD prev = ::ResumeThread(h);
        if (prev == kInvalidSuspendCount) {
          break;
        }
        resumed += 1;
      }
      if (resumed) {
        st.depth[tid] = std::max(0, depth - resumed);
        st.total_depth = std::max(0, st.total_depth - resumed);
      }
    }
    st.is_suspended = false;
  }

  void close_all_handles(PidState& st) {
    for (const auto& kv : st.handles) {
      ::CloseHandle(kv.second);
    }
    st.handles.clear();
    st.depth.clear();
    st.total_depth = 0;
  }

  void sync_handles(PidState& st, const std::vector<int>& tids) {
    std::unordered_set<int> tids_set;
    tids_set.reserve(tids.size());
    for (int t : tids) {
      tids_set.insert(t);
    }

    for (auto it = st.handles.begin(); it != st.handles.end();) {
      const int tid = it->first;
      if (tids_set.find(tid) == tids_set.end()) {
        ::CloseHandle(it->second);
        const int depth = st.depth.count(tid) ? st.depth[tid] : 0;
        if (depth > 0) {
          st.total_depth = std::max(0, st.total_depth - depth);
        }
        st.depth.erase(tid);
        it = st.handles.erase(it);
      } else {
        ++it;
      }
    }

    for (int tid : tids_set) {
      if (st.handles.find(tid) != st.handles.end()) {
        continue;
      }
      HANDLE h = open_thread_handle_win(tid);
      if (!h) {
        continue;
      }
      st.handles[tid] = h;
      st.depth.try_emplace(tid, 0);
    }
  }

  int auto_scaled_cycle_ms(int active_pids) const {
    const int base = cycle_ms_.load();
    if (!auto_scale_cycle_) {
      return base;
    }
    const int scaled = std::max(base, active_pids * min_cycle_ms_per_pid_);
    return std::min(max_cycle_ms_, std::max(10, scaled));
  }

  double phase_offset_s(const PidState& st, int cycle_ms) const {
    if (!stagger_phases_) {
      return 0.0;
    }
    const double frac = static_cast<double>((st.phase_seed & 0xFFFFFFFFu) % 1000003u) / 1000003.0;
    return frac * (static_cast<double>(cycle_ms) / 1000.0);
  }

  void cleaner_loop() {
    while (true) {
      std::unique_ptr<PidState> st;
      if (!cleanup_q_.pop(st)) {
        break;
      }
      if (!st) {
        continue;
      }
      try {
        balanced_resume_all(*st);
      } catch (...) {
      }
      try {
        close_all_handles(*st);
      } catch (...) {
      }
    }
  }

  void wake_scheduler() {
    {
      std::lock_guard<std::mutex> g(wake_mutex_);
      wake_flag_ = true;
    }
    wake_cv_.notify_one();
  }

  void start_scheduler() {
    enabled_.store(true);
    stop_.store(false);
    {
      std::lock_guard<std::mutex> g(sched_mutex_);
      if (sched_thread_.joinable()) {
        return;
      }
      sched_thread_ = std::thread([this] { scheduler_loop(); });
    }
    wake_scheduler();
  }

  void stop_scheduler() {
    enabled_.store(false);
    stop_.store(true);
    wake_scheduler();

    std::thread t;
    {
      std::lock_guard<std::mutex> g(sched_mutex_);
      t = std::move(sched_thread_);
    }
    if (t.joinable()) {
      t.join();
    }

    for (auto& kv : states_) {
      cleanup_q_.push(std::move(kv.second));
    }
    states_.clear();
    effective_cycle_ms_.store(cycle_ms_.load());
  }

  void scheduler_loop() {
    ::SetThreadPriority(::GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);

    std::priority_queue<Event, std::vector<Event>, EventCompare> events;
    double next_refresh = 0.0;

    while (!stop_.load()) {
      const double now_wall = wall_time_s();
      const double now_mono = mono_time_s();

      std::unordered_map<int, int> desired_pcts;
      std::unordered_map<int, std::string> desired_names;
      std::unordered_map<int, double> hold_until;
      std::unordered_set<int> force_resume;

      {
        std::lock_guard<std::mutex> g(config_mutex_);
        if (!enabled_.load()) {
          break;
        }

        for (auto it = hold_until_.begin(); it != hold_until_.end();) {
          if (it->second <= now_wall) {
            it = hold_until_.erase(it);
          } else {
            ++it;
          }
        }

        desired_pcts = desired_pcts_;
        desired_names = desired_names_;
        hold_until = hold_until_;
        force_resume = force_resume_;
        force_resume_.clear();
      }

      std::unordered_set<int> desired_pids;
      desired_pids.reserve(desired_pcts.size());
      for (const auto& kv : desired_pcts) {
        desired_pids.insert(kv.first);
      }

      std::unordered_set<int> current_pids;
      current_pids.reserve(states_.size());
      for (const auto& kv : states_) {
        current_pids.insert(kv.first);
      }

      for (int pid : current_pids) {
        if (desired_pids.find(pid) != desired_pids.end()) {
          continue;
        }
        auto it = states_.find(pid);
        if (it == states_.end()) {
          continue;
        }
        if (it->second) {
          it->second->gen += 1;
          cleanup_q_.push(std::move(it->second));
        }
        states_.erase(it);
      }

      for (int pid : desired_pids) {
        const int pct = clamp_pct(desired_pcts.count(pid) ? desired_pcts[pid] : 0);
        const std::string name = desired_names.count(pid) && !desired_names[pid].empty()
                                     ? desired_names[pid]
                                     : ("PID " + std::to_string(pid));

        PidState* st = nullptr;
        auto it = states_.find(pid);
        if (it == states_.end()) {
          auto ptr = std::make_unique<PidState>();
          ptr->pid = pid;
          ptr->name = name;
          ptr->pct = pct;
          ptr->phase_seed = (static_cast<std::uint32_t>(pid) * 2654435761u) & 0xFFFFFFFFu;
          ptr->gen += 1;
          st = ptr.get();
          states_.emplace(pid, std::move(ptr));
        } else {
          st = it->second.get();
          if (!st) {
            continue;
          }
          if (st->name != name) {
            st->name = name;
          }
          if (st->pct != pct) {
            st->pct = pct;
            st->gen += 1;
          }
        }

        const double exp = hold_until.count(pid) ? hold_until[pid] : 0.0;
        if (exp > now_wall) {
          if (st->pct != 0) {
            st->gen += 1;
          }
          st->pct = 0;
        }
      }

      for (int pid : force_resume) {
        auto it = states_.find(pid);
        if (it == states_.end() || !it->second) {
          continue;
        }
        PidState& st = *it->second;
        st.pct = 0;
        st.gen += 1;
        balanced_resume_all(st);
      }

      int active_count = 0;
      for (const auto& kv : states_) {
        if (kv.second && kv.second->pct > 0) {
          active_count += 1;
        }
      }

      const int effective_cycle = auto_scaled_cycle_ms(active_count);
      if (effective_cycle != effective_cycle_ms_.load()) {
        effective_cycle_ms_.store(effective_cycle);
        events = {};
        for (auto& kv : states_) {
          if (!kv.second) {
            continue;
          }
          PidState& st = *kv.second;
          st.gen += 1;
          balanced_resume_all(st);
          if (st.pct > 0) {
            const double offset = phase_offset_s(st, effective_cycle);
            const double when = now_mono + offset;
            events.push(Event{when, st.pid, 0, st.gen});
            st.next_event_at = when;
            st.scheduled_gen = st.gen;
          }
        }
      }

      for (auto& kv : states_) {
        if (!kv.second) {
          continue;
        }
        PidState& st = *kv.second;
        if (st.pct <= 0) {
          if (st.is_suspended || st.total_depth > 0) {
            balanced_resume_all(st);
          }
          st.next_event_at = 0.0;
          st.scheduled_gen = st.gen;
          continue;
        }

        if (st.scheduled_gen != st.gen) {
          balanced_resume_all(st);
          st.is_suspended = false;
          const double offset = phase_offset_s(st, effective_cycle_ms_.load());
          const double when = now_mono + offset;
          events.push(Event{when, st.pid, 0, st.gen});
          st.next_event_at = when;
          st.scheduled_gen = st.gen;
          continue;
        }

        const double stall_s =
            std::max(1.0, (static_cast<double>(effective_cycle_ms_.load()) / 1000.0) * 3.0);
        if (st.next_event_at > 0.0 && (now_mono - st.next_event_at) > stall_s) {
          st.gen += 1;
          balanced_resume_all(st);
          st.is_suspended = false;
          const double offset = phase_offset_s(st, effective_cycle_ms_.load());
          const double when = now_mono + offset;
          events.push(Event{when, st.pid, 0, st.gen});
          st.next_event_at = when;
          st.scheduled_gen = st.gen;
          continue;
        }
      }

      const double now_mono2 = mono_time_s();
      if (now_mono2 >= next_refresh) {
        try {
          std::vector<int> pids_to_refresh;
          for (const auto& kv : states_) {
            if (kv.second && (kv.second->pct > 0 || kv.second->total_depth > 0)) {
              pids_to_refresh.push_back(kv.first);
            }
          }
          const auto tids_map = list_thread_ids_for_pids_win(pids_to_refresh);
          for (const auto& kv : tids_map) {
            const int pid = kv.first;
            auto it = states_.find(pid);
            if (it == states_.end() || !it->second) {
              continue;
            }
            sync_handles(*it->second, kv.second);
            it->second->last_refresh_monotonic = now_mono2;
          }
        } catch (const std::exception& e) {
          log_line(std::string("[THREADS] Refresh sweep failed: ") + e.what());
        } catch (...) {
          log_line("[THREADS] Refresh sweep failed: unknown error");
        }
        next_refresh = now_mono2 + refresh_interval_s_;
      }

      double now_mono3 = mono_time_s();
      while (!events.empty() && events.top().when <= now_mono3) {
        const Event ev = events.top();
        events.pop();

        auto it = states_.find(ev.pid);
        if (it == states_.end() || !it->second) {
          continue;
        }
        PidState& st = *it->second;
        if (ev.gen != st.gen) {
          continue;
        }

        if (st.pct <= 0) {
          balanced_resume_all(st);
          continue;
        }

        const auto [red_ms, green_ms] = compute_red_green_ms(effective_cycle_ms_.load(), st.pct);
        if (ev.action == 0) {
          int suspended = 0;
          for (const auto& kvh : st.handles) {
            const int tid = kvh.first;
            HANDLE h = kvh.second;
            const DWORD prev = ::SuspendThread(h);
            if (prev != kInvalidSuspendCount) {
              st.depth[tid] = (st.depth.count(tid) ? st.depth[tid] : 0) + 1;
              st.total_depth += 1;
              suspended += 1;
            }
          }
          st.is_suspended = true;
          if (suspended == 0 && st.handles.empty()) {
            st.gen += 1;
            continue;
          }
          const double when2 = now_mono3 + (static_cast<double>(red_ms) / 1000.0);
          events.push(Event{when2, st.pid, 1, st.gen});
          st.next_event_at = when2;
        } else {
          if (st.total_depth > 0) {
            for (const auto& kvh : st.handles) {
              const int tid = kvh.first;
              HANDLE h = kvh.second;
              const int depth = st.depth.count(tid) ? st.depth[tid] : 0;
              if (depth <= 0) {
                continue;
              }
              const DWORD prev = ::ResumeThread(h);
              if (prev != kInvalidSuspendCount) {
                st.depth[tid] = std::max(0, depth - 1);
                st.total_depth = std::max(0, st.total_depth - 1);
              }
            }
          }
          st.is_suspended = false;
          const double when2 = now_mono3 + (static_cast<double>(green_ms) / 1000.0);
          events.push(Event{when2, st.pid, 0, st.gen});
          st.next_event_at = when2;
        }

        now_mono3 = mono_time_s();
      }

      double timeout_s = 0.25;
      if (!events.empty()) {
        timeout_s = std::max(0.0, events.top().when - mono_time_s());
        timeout_s = std::min(timeout_s, 0.25);
      }

      std::unique_lock<std::mutex> lk(wake_mutex_);
      wake_cv_.wait_for(lk, std::chrono::duration<double>(timeout_s),
                        [&] { return stop_.load() || wake_flag_; });
      wake_flag_ = false;
    }

    for (auto& kv : states_) {
      cleanup_q_.push(std::move(kv.second));
    }
    states_.clear();
  }

  std::atomic<int> cycle_ms_{50};
  std::atomic<int> effective_cycle_ms_{50};
  const bool auto_scale_cycle_{true};
  const bool stagger_phases_{true};
  const double refresh_interval_s_{1.0};
  const int max_cycle_ms_{400};
  const int min_cycle_ms_per_pid_{2};

  py::object log_cb_;
  PythonLogQueue log_q_;
  std::thread log_thread_;

  PidCleanupQueue cleanup_q_;
  std::thread cleaner_thread_;

  std::mutex config_mutex_;
  std::unordered_map<int, int> desired_pcts_;
  std::unordered_map<int, std::string> desired_names_;
  std::unordered_map<int, double> hold_until_;
  std::unordered_set<int> force_resume_;

  std::atomic<bool> enabled_{false};
  std::atomic<bool> stop_{false};
  std::atomic<bool> shutdown_{false};
  std::atomic<bool> timer_res_enabled_{false};

  std::mutex sched_mutex_;
  std::thread sched_thread_;

  std::mutex wake_mutex_;
  std::condition_variable wake_cv_;
  bool wake_flag_{false};

  std::unordered_map<int, std::unique_ptr<PidState>> states_;
};

}  // namespace

PYBIND11_MODULE(bes_limiter_native, m) {
  m.doc() = "Windows-only BES-style CPU throttling (SuspendThread/ResumeThread duty-cycling)";

  m.def("list_thread_ids", &list_thread_ids_win, py::arg("pid"));
  m.def(
      "list_thread_ids_for_pids",
      [](py::iterable pids) {
        std::vector<int> pidvec;
        for (py::handle h : pids) {
          pidvec.push_back(py::cast<int>(h));
        }
        const auto tids_map = list_thread_ids_for_pids_win(pidvec);
        py::dict out;
        for (const auto& kv : tids_map) {
          out[py::int_(kv.first)] = py::cast(kv.second);
        }
        return out;
      },
      py::arg("pids"));

  py::class_<BESLimiterWorker>(m, "BESLimiterWorker")
      .def(py::init<int, int, int, py::object, std::string>(),
           py::arg("pid"),
           py::kw_only(),
           py::arg("reduce_percent"),
           py::arg("cycle_ms"),
           py::arg("logq") = py::none(),
           py::arg("name") = "")
      .def("request_stop", &BESLimiterWorker::request_stop, py::call_guard<py::gil_scoped_release>())
      .def("start", &BESLimiterWorker::start, py::call_guard<py::gil_scoped_release>())
      .def("stop", &BESLimiterWorker::stop, py::arg("join_timeout") = 2.0, py::call_guard<py::gil_scoped_release>())
      .def("is_running", &BESLimiterWorker::is_running)
      .def("set_reduce_percent", &BESLimiterWorker::set_reduce_percent)
      .def("set_cycle_ms", &BESLimiterWorker::set_cycle_ms)
      .def("set_name", &BESLimiterWorker::set_name)
      .def("resume_balanced", &BESLimiterWorker::resume_balanced, py::call_guard<py::gil_scoped_release>())
      .def_property("reduce_percent", &BESLimiterWorker::reduce_percent, &BESLimiterWorker::set_reduce_percent)
      .def_property("cycle_ms", &BESLimiterWorker::cycle_ms, &BESLimiterWorker::set_cycle_ms)
      .def_property("name", &BESLimiterWorker::name, &BESLimiterWorker::set_name)
      .def_property_readonly("pid", &BESLimiterWorker::pid);

  py::class_<BESMultiProcessController>(m, "BESMultiProcessController")
      .def(py::init<int, py::object, bool, bool, double, int, int>(),
           py::arg("cycle_ms") = 50,
           py::arg("log") = py::none(),
           py::arg("auto_scale_cycle") = true,
           py::arg("stagger_phases") = true,
           py::arg("refresh_interval_s") = 1.0,
           py::arg("max_cycle_ms") = 400,
           py::arg("min_cycle_ms_per_pid") = 2)
      .def("set_enabled", &BESMultiProcessController::set_enabled, py::call_guard<py::gil_scoped_release>())
      .def("set_cycle_ms", &BESMultiProcessController::set_cycle_ms)
      .def("hold_unthrottled", &BESMultiProcessController::hold_unthrottled)
      .def("release_hold", &BESMultiProcessController::release_hold)
      .def("apply",
           &BESMultiProcessController::apply,
           py::arg("target_pcts"),
           py::kw_only(),
           py::arg("names") = py::none())
      .def("snapshot", &BESMultiProcessController::snapshot)
      .def("shutdown", &BESMultiProcessController::shutdown, py::call_guard<py::gil_scoped_release>());
}
