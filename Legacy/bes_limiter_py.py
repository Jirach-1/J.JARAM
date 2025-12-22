"""
bes_limiter.py
Windows-only BES-style CPU throttling via SuspendThread/ResumeThread duty-cycling.

This is extracted from `bes_style_tk.py` so it can be reused by the main PyQt GUI
without requiring Tkinter.
"""

from __future__ import annotations

import ctypes
import heapq
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Optional, Tuple


if ctypes.sizeof(ctypes.c_void_p) == 8:
    ULONG_PTR = ctypes.c_uint64
else:
    ULONG_PTR = ctypes.c_uint32


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)

MAX_PATH = 260

TH32CS_SNAPTHREAD = 0x00000004
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_INFORMATION = 0x0040


winmm.timeBeginPeriod.argtypes = [wintypes.UINT]
winmm.timeBeginPeriod.restype = wintypes.UINT
winmm.timeEndPeriod.argtypes = [wintypes.UINT]
winmm.timeEndPeriod.restype = wintypes.UINT

kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE

kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
kernel32.SuspendThread.restype = wintypes.DWORD

kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD

# Thread priority helpers (optional, but improves accuracy under load)
kernel32.GetCurrentThread.argtypes = []
kernel32.GetCurrentThread.restype = wintypes.HANDLE

kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, wintypes.INT]
kernel32.SetThreadPriority.restype = wintypes.BOOL

THREAD_PRIORITY_ABOVE_NORMAL = 1


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.restype = wintypes.BOOL


def _raise_last_error(prefix: str) -> None:
    err = ctypes.get_last_error()
    raise OSError(err, f"{prefix} (WinError {err})")


def list_thread_ids(pid: int) -> list[int]:
    """Return list of thread IDs for a given PID."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == INVALID_HANDLE_VALUE:
        _raise_last_error("CreateToolhelp32Snapshot(THREAD) failed")

    tids: list[int] = []
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = kernel32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if int(te.th32OwnerProcessID) == int(pid):
                tids.append(int(te.th32ThreadID))
            ok = kernel32.Thread32Next(snap, ctypes.byref(te))
    finally:
        kernel32.CloseHandle(snap)

    return tids


def list_thread_ids_for_pids(pids: Iterable[int]) -> Dict[int, list[int]]:
    """Return mapping of pid -> [tid, ...] using a *single* system thread snapshot.

    This scales far better than taking one snapshot per PID when you are throttling
    dozens (or hundreds) of processes.
    """
    pidset = {int(p) for p in pids}
    out: Dict[int, list[int]] = {p: [] for p in pidset}
    if not pidset:
        return out

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == INVALID_HANDLE_VALUE:
        _raise_last_error("CreateToolhelp32Snapshot(THREAD) failed")

    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = kernel32.Thread32First(snap, ctypes.byref(te))
        while ok:
            owner = int(te.th32OwnerProcessID)
            if owner in pidset:
                out.setdefault(owner, []).append(int(te.th32ThreadID))
            ok = kernel32.Thread32Next(snap, ctypes.byref(te))
    finally:
        kernel32.CloseHandle(snap)

    return out


def open_thread_handle(tid: int) -> Optional[wintypes.HANDLE]:
    h = kernel32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION, False, int(tid))
    return h if h else None


class BESLimiterWorker:
    """
    Suspend/resume duty-cycle limiter for one PID.

    Notes:
      - "pct" is a reduction percent (0..99). 0 means no throttling.
      - Balanced resume: only undoes suspend depth created by this worker.
    """

    def __init__(
        self,
        pid: int,
        *,
        reduce_percent: int,
        cycle_ms: int,
        logq: Optional["queue.Queue[str]"] = None,
        name: str = "",
    ) -> None:
        self.pid = int(pid)
        self.reduce_percent = int(reduce_percent)
        self.cycle_ms = max(10, int(cycle_ms))
        self.logq: "queue.Queue[str]" = logq or queue.Queue()
        self.name = name or f"PID {self.pid}"

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._handles: Dict[int, wintypes.HANDLE] = {}  # tid -> HANDLE
        self._depth: Dict[int, int] = {}  # tid -> suspend count created by us
        self._total_depth: int = 0
        self._lock = threading.Lock()

        self._last_refresh = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"Limiter-{self.pid}", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=float(join_timeout))
        self.resume_balanced()
        self._close_all_handles()

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and not self._stop.is_set())

    def set_reduce_percent(self, pct: int) -> None:
        self.reduce_percent = max(0, min(99, int(pct)))

    def set_cycle_ms(self, ms: int) -> None:
        self.cycle_ms = max(10, int(ms))

    def set_name(self, name: str) -> None:
        if str(name or "").strip():
            self.name = str(name).strip()

    def resume_balanced(self) -> None:
        self._balanced_resume_all()

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        try:
            self.logq.put_nowait(f"[{ts}] [{self.name}] {msg}")
        except Exception:
            pass

    def _refresh_threads(self) -> None:
        """Rebuild handle map periodically; close handles for dead threads."""
        try:
            tids = set(list_thread_ids(self.pid))
        except Exception:
            tids = set()

        with self._lock:
            for tid in list(self._handles.keys()):
                if tid not in tids:
                    try:
                        kernel32.CloseHandle(self._handles[tid])
                    except Exception:
                        pass
                    depth = int(self._depth.get(tid, 0))
                    if depth > 0:
                        self._total_depth = max(0, self._total_depth - depth)
                    self._handles.pop(tid, None)
                    self._depth.pop(tid, None)

            for tid in tids:
                if tid in self._handles:
                    continue
                h = open_thread_handle(tid)
                if h:
                    self._handles[tid] = h
                    self._depth.setdefault(tid, 0)

    def _balanced_resume_all(self) -> None:
        """Resume only suspend depth accumulated by this worker."""
        with self._lock:
            if self._total_depth <= 0:
                return
            for tid, h in list(self._handles.items()):
                depth = int(self._depth.get(tid, 0))
                if depth <= 0:
                    continue
                resumed = 0
                for _ in range(depth):
                    prev = kernel32.ResumeThread(h)
                    if prev == 0xFFFFFFFF:
                        break
                    resumed += 1
                new_depth = max(0, depth - resumed)
                self._depth[tid] = new_depth
                self._total_depth = max(0, self._total_depth - resumed)

    def _close_all_handles(self) -> None:
        with self._lock:
            for h in self._handles.values():
                try:
                    kernel32.CloseHandle(h)
                except Exception:
                    pass
            self._handles.clear()
            self._depth.clear()
            self._total_depth = 0

    def _run(self) -> None:
        self._log(f"Starting limiter: reduce={self.reduce_percent}% cycle={self.cycle_ms}ms")
        while not self._stop.is_set():
            now = time.time()
            if now - self._last_refresh > 2.0:
                self._refresh_threads()
                self._last_refresh = now

            pct = max(0, min(99, int(self.reduce_percent)))
            cycle = max(10, int(self.cycle_ms))

            # pct==0 should behave as "no throttling": ensure fully resumed and just sleep.
            if pct <= 0:
                self._balanced_resume_all()
                time.sleep(cycle / 1000.0)
                continue

            red_ms = int((cycle * pct) / 100)
            green_ms = max(1, cycle - red_ms)
            if pct >= 99:
                red_ms = cycle - 1
                green_ms = 1

            # Suspend
            with self._lock:
                for tid, h in list(self._handles.items()):
                    prev = kernel32.SuspendThread(h)
                    if prev != 0xFFFFFFFF:
                        self._depth[tid] = int(self._depth.get(tid, 0)) + 1
                        self._total_depth += 1

            time.sleep(red_ms / 1000.0)

            # Resume (balanced: resume once per cycle if we suspended)
            with self._lock:
                if self._total_depth > 0:
                    for tid, h in list(self._handles.items()):
                        if int(self._depth.get(tid, 0)) <= 0:
                            continue
                        prev = kernel32.ResumeThread(h)
                        if prev != 0xFFFFFFFF:
                            self._depth[tid] = int(self._depth.get(tid, 0)) - 1
                            self._total_depth = max(0, self._total_depth - 1)

            time.sleep(green_ms / 1000.0)

        self._log("Limiter thread exiting.")


def _clamp_pct(pct: int) -> int:
    return max(0, min(99, int(pct)))


def _compute_red_green_ms(*, cycle_ms: int, pct: int) -> Tuple[int, int]:
    """Return (red_ms, green_ms) for a given cycle and reduction percent."""
    cycle = max(10, int(cycle_ms))
    p = _clamp_pct(pct)
    if p <= 0:
        return 0, cycle
    red_ms = int((cycle * p) / 100)
    green_ms = max(1, cycle - red_ms)
    if p >= 99:
        red_ms = cycle - 1
        green_ms = 1
    return red_ms, green_ms


@dataclass
class _PidState:
    pid: int
    name: str = ""
    pct: int = 0

    handles: Dict[int, wintypes.HANDLE] = field(default_factory=dict)  # tid -> HANDLE
    depth: Dict[int, int] = field(default_factory=dict)  # tid -> suspend depth created by us
    total_depth: int = 0

    is_suspended: bool = False
    last_refresh_monotonic: float = 0.0
    gen: int = 0

    # Scheduler bookkeeping (monotonic seconds)
    next_event_at: float = 0.0
    scheduled_gen: int = -1

    # Stable per-pid phase seed (used for staggering)
    phase_seed: int = 0


class BESMultiProcessController:
    """Scalable multi-process BES-style throttling.

    Compared to the naive “one Python thread per PID” approach, this controller uses:
      - **one high-priority scheduler thread**
      - **a single Toolhelp snapshot** per refresh sweep (not per PID)
      - **phase staggering** so 100 PIDs don't all suspend/resume at the same instant
      - optional **auto cycle scaling** so the throttle remains effective as PID count grows

    Public API stays compatible with your previous controller:
      - set_enabled, set_cycle_ms, apply, hold_unthrottled, release_hold, snapshot, shutdown
    """

    def __init__(
        self,
        *,
        cycle_ms: int = 50,
        log: Optional[Callable[[str], None]] = None,
        auto_scale_cycle: bool = True,
        stagger_phases: bool = True,
        refresh_interval_s: float = 1.0,
        max_cycle_ms: int = 400,
        min_cycle_ms_per_pid: int = 2,
    ) -> None:
        self._cycle_ms = max(10, int(cycle_ms))
        self._effective_cycle_ms = self._cycle_ms

        self._auto_scale_cycle = bool(auto_scale_cycle)
        self._stagger_phases = bool(stagger_phases)
        self._refresh_interval_s = max(0.25, float(refresh_interval_s))
        self._max_cycle_ms = max(20, int(max_cycle_ms))
        self._min_cycle_ms_per_pid = max(0, int(min_cycle_ms_per_pid))

        self._log_cb = log
        self._logq: "queue.Queue[str]" = queue.Queue()

        self._enabled = False

        # Desired configuration (updated by apply)
        self._desired_pcts: Dict[int, int] = {}
        self._desired_names: Dict[int, str] = {}

        # Active internal state
        self._states: Dict[int, _PidState] = {}
        self._hold_until: Dict[int, float] = {}
        self._force_resume: set[int] = set()

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()

        self._timer_res_enabled = False

        # Background cleaner (does balanced resume + CloseHandle out of band)
        self._cleanup_q: "queue.Queue[Optional[_PidState]]" = queue.Queue()
        self._cleaner = threading.Thread(target=self._cleaner_loop, name="BES-Cleaner", daemon=True)
        self._cleaner.start()

        # Log pump
        self._pump_thread = threading.Thread(target=self._pump_log_loop, name="BES-LogPump", daemon=True)
        self._pump_thread.start()

        # Scheduler (only created when enabled)
        self._sched_thread: Optional[threading.Thread] = None

    # -------------------- Public API --------------------

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._lock:
            if enabled == self._enabled:
                return
            self._enabled = enabled

            if not enabled:
                # Stop scheduler quickly and let cleaner perform resumes/handle close.
                self._stop.set()
                self._wake.set()

                for st in list(self._states.values()):
                    self._cleanup_q.put(st)
                self._states.clear()
                self._desired_pcts.clear()
                self._desired_names.clear()
                self._hold_until.clear()
                self._force_resume.clear()

                self._disable_timer_resolution()
                return

            # Enable
            self._stop.clear()
            self._enable_timer_resolution()
            if not (self._sched_thread and self._sched_thread.is_alive()):
                self._sched_thread = threading.Thread(
                    target=self._scheduler_loop,
                    name="BES-Scheduler",
                    daemon=True,
                )
                self._sched_thread.start()
            self._wake.set()

    def set_cycle_ms(self, ms: int) -> None:
        ms = max(10, int(ms))
        with self._lock:
            self._cycle_ms = ms
        self._wake.set()

    def hold_unthrottled(self, pid: int, seconds: float) -> None:
        """Keep a PID unthrottled until now+seconds (extends existing hold)."""
        pid = int(pid)
        seconds = float(seconds or 0.0)
        until = time.time() + max(0.0, seconds)
        with self._lock:
            cur = float(self._hold_until.get(pid, 0.0))
            if until > cur:
                self._hold_until[pid] = until
            # ensure we resume ASAP
            self._force_resume.add(pid)
        self._wake.set()

    def release_hold(self, pid: int) -> None:
        pid = int(pid)
        with self._lock:
            self._hold_until.pop(pid, None)
        self._wake.set()

    def apply(self, target_pcts: Dict[int, int], *, names: Optional[Dict[int, str]] = None) -> None:
        """Update target PID -> percent map.

        This is designed to be fast and non-blocking; all expensive work happens
        in the scheduler/cleaner threads.
        """
        with self._lock:
            if not self._enabled:
                return
            self._desired_pcts = {int(pid): _clamp_pct(pct) for pid, pct in (target_pcts or {}).items()}
            self._desired_names = {int(pid): str(n) for pid, n in (names or {}).items()}
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            active = 0
            for pid, st in self._states.items():
                if float(self._hold_until.get(pid, 0.0)) > now:
                    continue
                if st.pct > 0:
                    active += 1
            return {
                "enabled": bool(self._enabled),
                "cycle_ms": int(self._cycle_ms),
                "effective_cycle_ms": int(self._effective_cycle_ms),
                "pids": len(self._states),
                "active": int(active),
                "holds": len([1 for _pid, exp in self._hold_until.items() if float(exp) > now]),
            }

    def shutdown(self) -> None:
        self.set_enabled(False)
        try:
            self._cleanup_q.put(None)
        except Exception:
            pass

    # -------------------- Internals --------------------

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        try:
            self._logq.put_nowait(f"[{ts}] [BES] {msg}")
        except Exception:
            pass

    def _pump_log_loop(self) -> None:
        while True:
            try:
                msg = self._logq.get()
            except Exception:
                msg = None
            if not msg:
                continue
            cb = self._log_cb
            if cb is not None:
                try:
                    cb(str(msg))
                except Exception:
                    pass

    def _enable_timer_resolution(self) -> None:
        if self._timer_res_enabled:
            return
        try:
            winmm.timeBeginPeriod(1)
            self._timer_res_enabled = True
        except Exception:
            self._timer_res_enabled = False

    def _disable_timer_resolution(self) -> None:
        if not self._timer_res_enabled:
            return
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass
        self._timer_res_enabled = False

    def _cleaner_loop(self) -> None:
        while True:
            st = self._cleanup_q.get()
            if st is None:
                return
            try:
                self._balanced_resume_all(st)
            except Exception:
                pass
            try:
                self._close_all_handles(st)
            except Exception:
                pass

    def _balanced_resume_all(self, st: _PidState) -> None:
        """Resume only suspend depth accumulated by *us* for this PID."""
        if st.total_depth <= 0:
            st.is_suspended = False
            return
        for tid, h in list(st.handles.items()):
            depth = int(st.depth.get(tid, 0))
            if depth <= 0:
                continue
            resumed = 0
            for _ in range(depth):
                prev = kernel32.ResumeThread(h)
                if prev == 0xFFFFFFFF:
                    break
                resumed += 1
            if resumed:
                st.depth[tid] = max(0, depth - resumed)
                st.total_depth = max(0, st.total_depth - resumed)
        st.is_suspended = False

    def _close_all_handles(self, st: _PidState) -> None:
        for h in list(st.handles.values()):
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass
        st.handles.clear()
        st.depth.clear()
        st.total_depth = 0

    def _sync_handles(self, st: _PidState, tids: Iterable[int]) -> None:
        tids_set = set(int(t) for t in tids)

        # Close handles that disappeared
        for tid in list(st.handles.keys()):
            if tid not in tids_set:
                try:
                    kernel32.CloseHandle(st.handles[tid])
                except Exception:
                    pass
                depth = int(st.depth.get(tid, 0))
                if depth > 0:
                    st.total_depth = max(0, st.total_depth - depth)
                st.handles.pop(tid, None)
                st.depth.pop(tid, None)

        # Open handles for new tids
        for tid in tids_set:
            if tid in st.handles:
                continue
            h = open_thread_handle(tid)
            if h:
                st.handles[tid] = h
                st.depth.setdefault(tid, 0)

    def _auto_scaled_cycle_ms(self, active_pids: int) -> int:
        base = int(self._cycle_ms)
        if not self._auto_scale_cycle:
            return base
        # Simple scale rule: ensure there's at least (N * per_pid_ms) ms of cycle budget.
        scaled = max(base, active_pids * int(self._min_cycle_ms_per_pid))
        return min(int(self._max_cycle_ms), max(10, int(scaled)))

    def _phase_offset_s(self, st: _PidState, cycle_ms: int) -> float:
        if not self._stagger_phases:
            return 0.0
        # Stable pseudo-random fraction in [0,1)
        frac = ((st.phase_seed & 0xFFFFFFFF) % 1000003) / 1000003.0
        return frac * (float(cycle_ms) / 1000.0)

    def _scheduler_loop(self) -> None:
        # Improve accuracy under load (mirrors what BES does)
        try:
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL)
        except Exception:
            pass

        # heap events: (when_monotonic, pid, action, gen)
        # action: 0=suspend, 1=resume
        events: list[Tuple[float, int, int, int]] = []

        next_refresh = 0.0

        while not self._stop.is_set():
            now_wall = time.time()
            now_mono = time.perf_counter()

            # Snapshot desired state / apply holds / add/remove PIDs
            with self._lock:
                if not self._enabled:
                    break

                # expire holds
                for pid, exp in list(self._hold_until.items()):
                    if float(exp) <= now_wall:
                        self._hold_until.pop(pid, None)

                desired_pcts = dict(self._desired_pcts)
                desired_names = dict(self._desired_names)
                hold_until = dict(self._hold_until)
                force_resume = set(self._force_resume)
                self._force_resume.clear()

            desired_pids = set(desired_pcts.keys())
            current_pids = set(self._states.keys())

            # Remove PIDs no longer desired
            removed = current_pids - desired_pids
            if removed:
                for pid in removed:
                    st = self._states.pop(pid, None)
                    if st:
                        st.gen += 1
                        self._cleanup_q.put(st)
                # We'll lazily drop stale heap events by gen mismatch.

            # Add / update desired PIDs
            for pid in desired_pids:
                pct = _clamp_pct(desired_pcts.get(pid, 0))
                name = desired_names.get(pid, "") or f"PID {pid}"
                st = self._states.get(pid)
                if st is None:
                    st = _PidState(
                        pid=int(pid),
                        name=str(name),
                        pct=int(pct),
                        phase_seed=((int(pid) * 2654435761) & 0xFFFFFFFF),
                    )
                    self._states[pid] = st
                    st.gen += 1
                else:
                    if st.name != str(name):
                        st.name = str(name)
                    if st.pct != int(pct):
                        st.pct = int(pct)
                        st.gen += 1

                # If held, treat as pct=0
                if float(hold_until.get(pid, 0.0)) > now_wall:
                    if st.pct != 0:
                        st.gen += 1
                    st.pct = 0

            # Force-resume requests
            for pid in force_resume:
                st = self._states.get(pid)
                if st is not None:
                    st.pct = 0
                    st.gen += 1
                    # resume ASAP
                    self._balanced_resume_all(st)

            # Auto-scale effective cycle based on *active* pids (pct>0)
            active_count = sum(1 for st in self._states.values() if st.pct > 0)
            effective_cycle = self._auto_scaled_cycle_ms(active_count)
            if effective_cycle != self._effective_cycle_ms:
                self._effective_cycle_ms = effective_cycle
                # reset schedule
                events.clear()
                for st in self._states.values():
                    st.gen += 1
                    # Ensure resumed when rescheduling (prevents getting "stuck")
                    self._balanced_resume_all(st)
                    if st.pct > 0:
                        offset = self._phase_offset_s(st, self._effective_cycle_ms)
                        when = now_mono + offset
                        heapq.heappush(events, (when, st.pid, 0, st.gen))
                        st.next_event_at = when
                        st.scheduled_gen = st.gen

            # Ensure every active PID has a scheduled event chain.
            # We avoid scanning the heap by keeping per-PID bookkeeping.
            for st in self._states.values():
                if st.pct <= 0:
                    if st.is_suspended or st.total_depth > 0:
                        self._balanced_resume_all(st)
                    st.next_event_at = 0.0
                    st.scheduled_gen = st.gen
                    continue

                # If config changed for this PID, restart its event chain.
                if st.scheduled_gen != st.gen:
                    self._balanced_resume_all(st)
                    st.is_suspended = False
                    offset = self._phase_offset_s(st, self._effective_cycle_ms)
                    when = now_mono + offset
                    heapq.heappush(events, (when, st.pid, 0, st.gen))
                    st.next_event_at = when
                    st.scheduled_gen = st.gen
                    continue

                # If it looks like this PID's chain stalled (no events fired for too long), restart.
                stall_s = max(1.0, (self._effective_cycle_ms / 1000.0) * 3.0)
                if st.next_event_at > 0.0 and (now_mono - st.next_event_at) > stall_s:
                    st.gen += 1  # invalidate any stale events that might still exist
                    self._balanced_resume_all(st)
                    st.is_suspended = False
                    offset = self._phase_offset_s(st, self._effective_cycle_ms)
                    when = now_mono + offset
                    heapq.heappush(events, (when, st.pid, 0, st.gen))
                    st.next_event_at = when
                    st.scheduled_gen = st.gen
                    continue

            # Refresh thread handles for all active PIDs at an interval
            now_mono = time.perf_counter()
            if now_mono >= next_refresh:
                try:
                    pids_to_refresh = [st.pid for st in self._states.values() if st.pct > 0 or st.total_depth > 0]
                    tids_map = list_thread_ids_for_pids(pids_to_refresh)
                    for pid, tids in tids_map.items():
                        st = self._states.get(pid)
                        if st is None:
                            continue
                        self._sync_handles(st, tids)
                        st.last_refresh_monotonic = now_mono
                except Exception as e:
                    self._log(f"[THREADS] Refresh sweep failed: {e}")
                next_refresh = now_mono + self._refresh_interval_s

            # Run due events
            now_mono = time.perf_counter()
            ran_any = False
            while events and events[0][0] <= now_mono:
                when, pid, action, gen = heapq.heappop(events)
                st = self._states.get(pid)
                if st is None or gen != st.gen:
                    continue

                # If pct dropped to 0, ensure resumed and stop scheduling
                if st.pct <= 0:
                    self._balanced_resume_all(st)
                    continue

                red_ms, green_ms = _compute_red_green_ms(cycle_ms=self._effective_cycle_ms, pct=st.pct)
                if action == 0:
                    # Suspend
                    suspended = 0
                    for tid, h in list(st.handles.items()):
                        prev = kernel32.SuspendThread(h)
                        if prev != 0xFFFFFFFF:
                            st.depth[tid] = int(st.depth.get(tid, 0)) + 1
                            st.total_depth += 1
                            suspended += 1
                    st.is_suspended = True
                    if suspended == 0 and not st.handles:
                        # No handles -> likely dead or access issue
                        st.gen += 1
                        continue
                    when2 = now_mono + (red_ms / 1000.0)
                    heapq.heappush(events, (when2, pid, 1, st.gen))
                    st.next_event_at = when2
                    ran_any = True
                else:
                    # Resume (balanced one-step per suspend)
                    if st.total_depth > 0:
                        for tid, h in list(st.handles.items()):
                            if int(st.depth.get(tid, 0)) <= 0:
                                continue
                            prev = kernel32.ResumeThread(h)
                            if prev != 0xFFFFFFFF:
                                st.depth[tid] = int(st.depth.get(tid, 0)) - 1
                                st.total_depth = max(0, st.total_depth - 1)
                    st.is_suspended = False
                    when2 = now_mono + (green_ms / 1000.0)
                    heapq.heappush(events, (when2, pid, 0, st.gen))
                    st.next_event_at = when2
                    ran_any = True

            # Sleep until next event or wake signal
            if self._stop.is_set():
                break

            timeout: Optional[float] = None
            if events:
                timeout = max(0.0, events[0][0] - time.perf_counter())
                # Don't sleep too long; refresh/updates should still happen.
                timeout = min(timeout, 0.25)
            else:
                timeout = 0.25

            # Wait (woken early by apply/holds)
            self._wake.wait(timeout=timeout)
            self._wake.clear()

        # On exit: schedule cleanup for any remaining states
        for st in list(self._states.values()):
            try:
                self._cleanup_q.put(st)
            except Exception:
                pass
        self._states.clear()

