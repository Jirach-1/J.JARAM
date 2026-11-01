"""Bounded webhook delivery with HTTP acknowledgements and scheduled retries."""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
import random
import threading
import time
from urllib.parse import urlsplit

import requests


class WebhookError(RuntimeError):
    """Safe to display: messages must never contain webhook URLs or tokens."""


class DeliveryUnconfirmed(WebhookError):
    pass


class RetryWebhook(WebhookError):
    def __init__(self, reason, *, delay=None, global_limit=False):
        super().__init__(reason)
        self.delay = delay
        self.global_limit = global_limit


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    cooldown: float = 0.0


def _seconds(value, default=0.0):
    try:
        value = float(value)
        return value if 0 <= value < float("inf") else default
    except (ValueError, TypeError, OverflowError):
        return default


def post_webhook(url, payload):
    """Perform one attempt. The pool schedules retries without sleeping workers."""
    if not url:
        raise WebhookError("Webhook destination is empty")
    try:
        response = requests.post(url, json=payload, timeout=(3.05, 10))
    except requests.exceptions.ConnectTimeout:
        raise RetryWebhook("Webhook connection timed out") from None
    except requests.exceptions.RequestException:
        # A response can be lost after the server accepts a message. Retrying
        # these automatically can ping everyone twice; surface uncertainty.
        raise DeliveryUnconfirmed("Webhook delivery unconfirmed after a network error") from None
    try:
        status = int(response.status_code)
        if 200 <= status < 300:
            cooldown = 0.0
            if str(response.headers.get("X-RateLimit-Remaining", "")) == "0":
                cooldown = _seconds(response.headers.get("X-RateLimit-Reset-After"))
            return WebhookResponse(status, cooldown)
        if status == 429:
            try:
                body = response.json()
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, TypeError):
                body = {}
            delay = max(
                _seconds(response.headers.get("Retry-After")),
                _seconds(body.get("retry_after")),
                _seconds(response.headers.get("X-RateLimit-Reset-After")),
                0.05,
            )
            global_limit = bool(body.get("global")) or str(
                response.headers.get("X-RateLimit-Global", "")
            ).lower() == "true"
            raise RetryWebhook("Webhook rate limited (HTTP 429)", delay=delay,
                               global_limit=global_limit)
        if 500 <= status < 600:
            raise RetryWebhook(f"Webhook server error (HTTP {status})")
        raise WebhookError(f"Webhook rejected (HTTP {status})")
    finally:
        response.close()


class WebhookDeliveryPool:
    """One bounded queue, endpoint/global cooldowns, and daemon HTTP workers.

    Futures represent complete deliveries, including retries. A retry never
    reserves an additional queue slot or sleeps in a network worker.
    """

    def __init__(self, *, max_workers=16, max_pending=1024,
                 thread_name_prefix="ms-send", overflow_log=lambda _message: None,
                 max_attempts=5, max_age=120.0):
        if min(max_workers, max_pending, max_attempts) <= 0 or max_age <= 0:
            raise ValueError("Delivery limits must be positive")
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._max_attempts = max_attempts
        self._max_age = max_age
        self._prefix = thread_name_prefix
        self._log = overflow_log
        self._condition = threading.Condition()
        self._waiting = deque()
        self._threads = []
        self._shutdown = False
        self._pending = 0
        self._cooldowns = {}
        self._global_until = 0.0
        self._next_overflow_log = 0.0
        self._counts = dict(accepted=0, delivered=0, retried=0, failed=0,
                            unconfirmed=0, rejected=0, cancelled=0)
        # Compatibility with queue instrumentation; includes delayed retries.
        self._jobs = self

    def qsize(self):
        with self._condition:
            return len(self._waiting)

    def diagnostics(self):
        with self._condition:
            return dict(self._counts, pending=self._pending, queued=len(self._waiting),
                        workers=len(self._threads), capacity=self._max_pending)

    def submit(self, fn, /, *args, **kwargs):
        now = time.monotonic()
        url = str(args[0]) if args else ""
        parsed = urlsplit(url)
        route = (parsed.scheme, parsed.netloc, parsed.path)
        with self._condition:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if self._pending >= self._max_pending:
                self._counts["rejected"] += 1
                report = now >= self._next_overflow_log
                if report:
                    self._next_overflow_log = now + 30.0
            else:
                future = Future()
                self._pending += 1
                self._counts["accepted"] += 1
                future.add_done_callback(self._completed)
                # Count total HTTP attempts separately from failures requiring
                # exponential backoff. Server-directed cooldowns must not
                # inflate the next server/network error's backoff.
                self._waiting.append([future, fn, args, kwargs, route, now, now, 0, 0])
                if len(self._threads) < self._max_workers:
                    thread = threading.Thread(target=self._run, daemon=True,
                                              name=f"{self._prefix}_{len(self._threads)}")
                    self._threads.append(thread)
                    thread.start()
                self._condition.notify_all()
                return future
        if report:
            self._log("[Webhook] Delivery queue full; notification rejected.")
        return None

    def _completed(self, future):
        with self._condition:
            self._pending -= 1
            if future.cancelled():
                self._counts["cancelled"] += 1
                # Cancellation releases capacity immediately. Remove its
                # payload too, even if every HTTP worker is currently stalled.
                self._waiting = deque(job for job in self._waiting if job[0] is not future)
            else:
                error = future.exception()
                self._counts["delivered" if error is None else "failed"] += 1
                if isinstance(error, DeliveryUnconfirmed):
                    self._counts["unconfirmed"] += 1
            self._condition.notify_all()

    def _take(self):
        with self._condition:
            while True:
                if self._shutdown and not self._waiting:
                    return None
                now = time.monotonic()
                soonest = None
                # Clear obsolete endpoint keys so reconfigured hooks don't
                # retain secret URLs indefinitely over a long runtime.
                self._cooldowns = {key: until for key, until in self._cooldowns.items()
                                   if until > now}
                for i, job in enumerate(self._waiting):
                    future, _, _, _, route, created, due, _, _ = job
                    ready = min(max(due, self._global_until,
                                    self._cooldowns.get(route, 0)), created + self._max_age)
                    if future.cancelled() or ready <= now:
                        del self._waiting[i]
                        return job
                    soonest = ready if soonest is None else min(soonest, ready)
                self._condition.wait(None if soonest is None else max(0.001, soonest - now))

    def _run(self):
        while (job := self._take()) is not None:
            future, fn, args, kwargs, route, created, _, attempts, backoff_failures = job
            if attempts == 0 and not future.set_running_or_notify_cancel():
                del job, future, fn, args, kwargs
                continue
            try:
                if time.monotonic() - created >= self._max_age:
                    raise WebhookError("Webhook delivery expired while waiting")
                job[7] += 1
                result = fn(*args, **kwargs)
            except RetryWebhook as error:
                now = time.monotonic()
                if error.delay is not None:
                    delay = error.delay
                else:
                    delay = min(8.0, 0.5 * 2 ** backoff_failures) + random.uniform(0, 0.2)
                    job[8] += 1
                with self._condition:
                    if error.delay is not None:
                        if error.global_limit:
                            self._global_until = max(self._global_until, now + delay)
                        else:
                            self._cooldowns[route] = max(self._cooldowns.get(route, 0), now + delay)
                    retry = (not self._shutdown and job[7] < self._max_attempts
                             and now + delay < created + self._max_age)
                    if retry:
                        job[6] = now + delay
                        # Prefer retrying an older alert to accumulating newer ones.
                        self._waiting.appendleft(job)
                        self._counts["retried"] += 1
                    self._condition.notify_all()
                if not retry:
                    future.set_exception(WebhookError(f"{error}; retry limit reached"))
            except BaseException as error:
                future.set_exception(error)
            else:
                cooldown = getattr(result, "cooldown", 0.0)
                if cooldown:
                    with self._condition:
                        self._cooldowns[route] = max(self._cooldowns.get(route, 0), time.monotonic() + cooldown)
                        self._condition.notify_all()
                future.set_result(result)
            # Don't retain a previous payload while a worker is idle.
            del job, future, fn, args, kwargs

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._condition:
            self._shutdown = True
            waiting = list(self._waiting) if cancel_futures else []
            if cancel_futures:
                self._waiting.clear()
            self._condition.notify_all()
        for job in waiting:
            future = job[0]
            if not future.cancel():
                future.set_exception(WebhookError("Webhook delivery stopped during retry"))
        if wait:
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join()
