"""Bounded background delivery for notifications that may stall on the network."""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable


class BackgroundDeliveryPool:
    """Bound best-effort notifications without making them an exit dependency.

    ThreadPoolExecutor joins running tasks during interpreter shutdown, even
    after shutdown(wait=False). A stalled network send must not keep the app
    alive after its window has closed, so these workers are owned daemon threads.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        max_pending: int = 256,
        thread_name_prefix: str,
        overflow_log: Callable[[str], None],
    ) -> None:
        if max_workers <= 0 or max_pending <= 0:
            raise ValueError("max_workers and max_pending must be positive")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._jobs = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._shutdown = False
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_pending)
        self._overflow_log = overflow_log
        self._overflow_lock = threading.Lock()
        self._next_overflow_log = 0.0

    def submit(self, fn, /, *args, **kwargs) -> Future | None:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            accepted = self._slots.acquire(blocking=False)
            if accepted:
                future = Future()
                # Completion and cancellation both return the reserved slot.
                future.add_done_callback(lambda _completed: self._slots.release())
                try:
                    if len(self._threads) < self._max_workers:
                        thread = threading.Thread(
                            target=self._run,
                            args=(self._jobs,),
                            name=f"{self._thread_name_prefix}_{len(self._threads)}",
                            daemon=True,
                        )
                        thread.start()
                        self._threads.append(thread)
                    self._jobs.put((future, fn, args, kwargs))
                except BaseException:
                    future.cancel()
                    raise
        if not accepted:
            now = time.monotonic()
            with self._overflow_lock:
                log_overflow = now >= self._next_overflow_log
                if log_overflow:
                    self._next_overflow_log = now + 30.0
            if log_overflow:
                self._overflow_log("[Webhook] Delivery queue full; skipped notification.")
            return None
        return future

    @staticmethod
    def _run(jobs) -> None:
        while True:
            job = jobs.get()
            if job is None:
                return
            future, fn, args, kwargs = job
            if future.set_running_or_notify_cancel():
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
                    del result
            del job, future, fn, args, kwargs

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        cancelled = []
        with self._lock:
            was_shutdown = self._shutdown
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    if job is not None:
                        cancelled.append(job[0])
            if not was_shutdown or cancel_futures:
                for _ in self._threads:
                    self._jobs.put(None)
            threads = list(self._threads)
        # Future callbacks may call back into the pool; run them outside its lock.
        for future in cancelled:
            future.cancel()
        if wait:
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join()
