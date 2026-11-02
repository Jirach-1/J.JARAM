"""Bounded webhook delivery with HTTP acknowledgements and scheduled retries."""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
import hashlib
import random
import threading
import time
from urllib.parse import urlsplit

import requests
from multiscope_metrics import RollingTimings


class WebhookError(RuntimeError):
    """Safe to display: messages must never contain webhook URLs or tokens."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class DeliveryUnconfirmed(WebhookError):
    pass


class DeliveryExpired(WebhookError):
    pass


class RetryWebhook(WebhookError):
    def __init__(self, reason, *, delay=None, global_limit=False, status_code=None, rate_limit=None):
        super().__init__(reason, status_code=status_code)
        self.delay = delay
        self.global_limit = global_limit
        self.rate_limit = rate_limit


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    cooldown: float = 0.0
    rate_limit: dict | None = field(default=None, compare=False, repr=False)


def _seconds(value, default=0.0):
    try:
        value = float(value)
        return value if 0 <= value < float("inf") else default
    except (ValueError, TypeError, OverflowError):
        return default


def safe_rate_limit_event(event):
    """Allowlist response evidence; never retain URLs, bodies or arbitrary headers."""
    if not isinstance(event, dict):
        return {}
    result = {}
    for key in ('observed_at_unix', 'http_status', 'selected_delay_seconds',
                'retry_after_header_seconds', 'retry_after_body_seconds', 'reset_after_seconds',
                'reset_unix', 'limit', 'remaining', 'body_code'):
        value = _seconds(event.get(key), None)
        if value is not None:
            result[key] = value
    enums = dict(delay_source={'retry_after_header', 'retry_after_body', 'retry_after_header_and_body',
                              'reset_after_fallback', 'minimum_fallback', 'success_reset_after', 'unspecified'},
                 scope={'user', 'shared', 'global', 'unknown'},
                 response_origin={'discord', 'other', 'unknown'})
    for key, allowed in enums.items():
        if isinstance(event.get(key), str) and event[key] in allowed:
            result[key] = event[key]
    for key in ('destination_id', 'bucket_id'):
        value = event.get(key)
        if isinstance(value, str) and len(value) == 12 and all(c in '0123456789abcdef' for c in value):
            result[key] = value
    if isinstance(event.get('global_limit'), bool):
        result['global_limit'] = event['global_limit']
    return result


def _response_evidence(response, body, delay, source, global_limit=False):
    headers = response.headers
    event = dict(http_status=int(response.status_code), selected_delay_seconds=delay,
                 delay_source=source, global_limit=bool(global_limit),
                 scope=str(headers.get('X-RateLimit-Scope', 'unknown')).lower())
    for key, value in (
        ('retry_after_header_seconds', headers.get('Retry-After')),
        ('retry_after_body_seconds', body.get('retry_after')),
        ('reset_after_seconds', headers.get('X-RateLimit-Reset-After')),
        ('reset_unix', headers.get('X-RateLimit-Reset')),
        ('limit', headers.get('X-RateLimit-Limit')),
        ('remaining', headers.get('X-RateLimit-Remaining')),
        ('body_code', body.get('code')),
    ):
        parsed = _seconds(value, None)
        if parsed is not None:
            event[key] = parsed
    bucket = headers.get('X-RateLimit-Bucket')
    if isinstance(bucket, str) and bucket:
        event['bucket_id'] = hashlib.sha256(bucket[:1024].encode()).hexdigest()[:12]
    response_url = getattr(response, 'url', None)
    try:
        host = urlsplit(response_url).hostname if isinstance(response_url, str) else None
    except ValueError:
        host = None
    event['response_origin'] = ('unknown' if not host else 'discord' if
        host in {'discord.com', 'discordapp.com'} or host.endswith('.discord.com') else 'other')
    return safe_rate_limit_event(event)


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
            return WebhookResponse(status, cooldown,
                _response_evidence(response, {}, cooldown, 'success_reset_after') if cooldown else None)
        if status == 429:
            try:
                body = response.json()
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, TypeError):
                body = {}
            # Retry-After/retry_after describe the limit that rejected this
            # request. Reset-After can describe a different bucket, so taking
            # the maximum across all three can unnecessarily block this hook
            # for minutes or hours. Use the bucket reset only as a fallback.
            retry_delays = [_seconds(value, None) for value in (
                response.headers.get("Retry-After"), body.get("retry_after"))]
            retry_delays = [value for value in retry_delays if value is not None]
            delay = max(.05, max(retry_delays) if retry_delays else
                        _seconds(response.headers.get("X-RateLimit-Reset-After")))
            header_delay = _seconds(response.headers.get('Retry-After'), None)
            body_delay = _seconds(body.get('retry_after'), None)
            source = ('retry_after_header_and_body' if header_delay is not None and header_delay == body_delay else
                      'retry_after_header' if header_delay is not None and (body_delay is None or header_delay > body_delay) else
                      'retry_after_body' if body_delay is not None else
                      'reset_after_fallback' if _seconds(response.headers.get('X-RateLimit-Reset-After'), None) is not None else
                      'minimum_fallback')
            global_limit = bool(body.get("global")) or str(
                response.headers.get("X-RateLimit-Global", "")
            ).lower() == "true"
            raise RetryWebhook("Webhook rate limited (HTTP 429)", delay=delay,
                               global_limit=global_limit, status_code=status,
                               rate_limit=_response_evidence(response, body, delay, source, global_limit))
        if 500 <= status < 600:
            raise RetryWebhook(f"Webhook server error (HTTP {status})", status_code=status)
        raise WebhookError(f"Webhook rejected (HTTP {status})", status_code=status)
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
        self._inflight_routes = set()
        self._rate_limit_events = deque(maxlen=32)
        self._success_cooldown_events = deque(maxlen=32)
        self._next_overflow_log = 0.0
        self._counts = dict(accepted=0, delivered=0, retried=0, failed=0,
                            unconfirmed=0, rejected=0, cancelled=0, expired=0,
                            http_attempts=0, rate_limited=0, server_errors=0)
        self._timings = RollingTimings()
        self._pending_created = {}
        self._last_http_status = None
        # Compatibility with queue instrumentation; includes delayed retries.
        self._jobs = self

    def qsize(self):
        with self._condition:
            return len(self._waiting)

    def diagnostics(self):
        with self._condition:
            now = time.monotonic()
            result = dict(self._counts, pending=self._pending, queued=len(self._waiting),
                          workers=len(self._threads), capacity=self._max_pending,
                          oldest_pending_seconds=max(0.0, now-min(self._pending_created.values()))
                          if self._pending_created else 0.0,
                          global_cooldown_seconds=max(0.0, self._global_until-now),
                          destination_cooldown_seconds=max([0.0]+[v-now for v in self._cooldowns.values()]),
                          active_destinations=len(self._inflight_routes),
                          recent_rate_limits=[dict(event) for event in self._rate_limit_events],
                          recent_success_cooldowns=[dict(event) for event in self._success_cooldown_events],
                          last_http_status=self._last_http_status)
        result['timings'] = self._timings.snapshot()
        return result

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
                self._pending_created[future] = now
                future.add_done_callback(self._completed)
                # Count total HTTP attempts separately from failures requiring
                # exponential backoff. Server-directed cooldowns must not
                # inflate the next server/network error's backoff.
                self._waiting.append([future, fn, args, kwargs, route, now, now, 0, 0, None])
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
            self._pending_created.pop(future, None)
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
                if isinstance(error, DeliveryExpired):
                    self._counts["expired"] += 1
                status = getattr(error, 'status_code', None)
                if isinstance(status, int):
                    self._last_http_status = status
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
                    future, _, _, _, route, created, due, _, _, _ = job
                    if future.cancelled() or created + self._max_age <= now:
                        del self._waiting[i]
                        return job, False
                    if route in self._inflight_routes:
                        expiry = created + self._max_age
                        soonest = expiry if soonest is None else min(soonest, expiry)
                        continue
                    ready = min(max(due, self._global_until,
                                    self._cooldowns.get(route, 0)), created + self._max_age)
                    if ready <= now:
                        del self._waiting[i]
                        self._inflight_routes.add(route)
                        return job, True
                    soonest = ready if soonest is None else min(soonest, ready)
                self._condition.wait(None if soonest is None else max(0.001, soonest - now))

    def _release_route(self, route, claimed):
        if claimed:
            with self._condition:
                self._inflight_routes.discard(route)
                self._condition.notify_all()

    def _cooldown_event_locked(self, route, evidence, status, delay, global_limit=False):
        event = safe_rate_limit_event(evidence)
        event.update(observed_at_unix=time.time(), http_status=status,
                     selected_delay_seconds=delay, global_limit=bool(global_limit),
                     destination_id=hashlib.sha256('\0'.join(route).encode()).hexdigest()[:12])
        event.setdefault('delay_source', 'unspecified')
        event.setdefault('scope', 'unknown')
        target = self._rate_limit_events if status == 429 else self._success_cooldown_events
        target.append(event)
        return event

    def _log_cooldown_event(self, event):
        details = '; '.join(f'{key}={event[key]}' for key in (
            'destination_id', 'selected_delay_seconds', 'delay_source', 'scope', 'global_limit',
            'retry_after_header_seconds', 'retry_after_body_seconds', 'reset_after_seconds',
            'limit', 'remaining', 'body_code', 'bucket_id', 'response_origin') if key in event)
        try:
            self._log(f'[Webhook] HTTP {event["http_status"]} cooldown response: {details}')
        except Exception:
            pass  # Diagnostic callbacks must not interrupt a delivery.

    def _run(self):
        while (taken := self._take()) is not None:
            job, claimed = taken
            future, fn, args, kwargs, route, created, _, attempts, backoff_failures, last_end = job
            if attempts == 0 and not future.set_running_or_notify_cancel():
                self._release_route(route, claimed)
                del job, taken, future, fn, args, kwargs
                continue
            terminal_error = None
            completed = False
            try:
                if time.monotonic() - created >= self._max_age:
                    now = time.monotonic()
                    with self._condition:
                        destination_wait = max(0.0, self._cooldowns.get(route, 0)-now)
                        global_wait = max(0.0, self._global_until-now)
                    detail = f"Webhook delivery expired while waiting ({self._max_age:g} s lifetime"
                    if max(destination_wait, global_wait) > 0:
                        scope = 'global' if global_wait > destination_wait else 'destination'
                        detail += f"; {scope} cooldown remaining {max(destination_wait, global_wait):.2f} s"
                    raise DeliveryExpired(detail+")")
                job[7] += 1
                began = time.monotonic()
                self._timings.record('queue_wait' if attempts == 0 else 'retry_wait',
                                     (began-(created if attempts == 0 else last_end))*1000)
                with self._condition:
                    self._counts['http_attempts'] += 1
                try:
                    result = fn(*args, **kwargs)
                finally:
                    job[9] = time.monotonic()
                    self._timings.record('http_attempt', (job[9]-began)*1000)
            except RetryWebhook as error:
                now = time.monotonic()
                if error.delay is not None:
                    delay = error.delay
                else:
                    delay = min(8.0, 0.5 * 2 ** backoff_failures) + random.uniform(0, 0.2)
                    job[8] += 1
                with self._condition:
                    self._last_http_status = error.status_code
                    if error.status_code == 429:
                        self._counts['rate_limited'] += 1
                        event = self._cooldown_event_locked(route, error.rate_limit, 429, delay,
                                                            error.global_limit)
                    elif error.status_code is not None and 500 <= error.status_code < 600:
                        self._counts['server_errors'] += 1
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
                if error.status_code == 429:
                    self._log_cooldown_event(event)
                if not retry:
                    reason = ("delivery stopped" if self._shutdown else
                              f"retry limit reached ({job[7]} attempts)" if job[7] >= self._max_attempts else
                              f"next retry would exceed {self._max_age:g} s delivery lifetime")
                    if error.delay is not None:
                        scope = 'global' if error.global_limit else 'destination'
                        reason += f"; {scope} cooldown {delay:.2f} s"
                    terminal_error = WebhookError(f"{error}; {reason}", status_code=error.status_code)
                    completed = True
            except BaseException as error:
                terminal_error = error
                completed = True
            else:
                self._timings.record('queue_to_ack', (time.monotonic()-created)*1000)
                with self._condition:
                    status = getattr(result, 'status_code', None)
                    if isinstance(status, int):
                        self._last_http_status = status
                cooldown = getattr(result, "cooldown", 0.0)
                if cooldown:
                    with self._condition:
                        self._cooldowns[route] = max(self._cooldowns.get(route, 0), time.monotonic() + cooldown)
                        event = self._cooldown_event_locked(route, getattr(result, 'rate_limit', None),
                                                            status, cooldown)
                        self._condition.notify_all()
                    if cooldown >= self._max_age:
                        self._log_cooldown_event(event)
                completed = True
            finally:
                # Apply response cooldowns before releasing the next request.
                # Release before Future callbacks, which may re-enter the pool.
                self._release_route(route, claimed)
            if completed:
                if terminal_error is not None:
                    future.set_exception(terminal_error)
                else:
                    future.set_result(result)
            # Don't retain a previous payload while a worker is idle.
            del job, taken, future, fn, args, kwargs, terminal_error

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
