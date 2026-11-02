"""Small, bounded timing samples shared by the reader and webhook sender."""
from collections import deque
from contextlib import contextmanager
import math
import threading
import time


TIMING_LABELS = {
    'source_to_detection': 'Log timestamp → detection',
    'detection_to_queue': 'Detection → queued',
    'queue_wait': 'Initial send queue wait',
    'http_attempt': 'HTTP attempt (includes failures)',
    'retry_wait': 'Wait between retry attempts',
    'queue_to_ack': 'Queued → HTTP acknowledged',
    'detection_to_ack': 'Detection → HTTP acknowledged',
    'reader_cycle': 'Reader cycle',
    'reader_discovery': 'Log discovery / index refresh',
    'reader_resolve': 'Log resolution / cold state seed',
    'reader_metadata': 'Log file metadata checks',
    'reader_file_read': 'Log file I/O total',
    'reader_file_open': 'Log file open',
    'reader_file_seek': 'Log file seek',
    'reader_file_read_call': 'Log file read',
    'reader_file_close': 'Log file close',
    'reader_parse': 'Log parsing / event dispatch',
    'reader_tail_queue_wait': 'Tail worker queue wait',
    'reader_batch_wait': 'Wait for tail workers (overlaps work)',
    'reader_scope_lock_wait': 'Contended server-state lock wait',
    'statistics_write': 'Statistics file write',
}
TIMING_FIELDS = ('last_ms', 'p50_ms', 'p95_ms', 'max_ms', 'samples', 'total_samples')
TIMING_DESCRIPTIONS = {
    'reader_cycle': 'Wall time for one complete reader cycle. Other reader stages can overlap and should not be added together.',
    'reader_discovery': 'Per index poll: directory/username discovery and refresh of attached-file metadata.',
    'reader_resolve': 'Per resolution pass: resolve usernames to current logs, including cold state seeding when needed.',
    'reader_metadata': 'Per metadata check: path existence/size checks and handle identity/size verification. Handle verification overlaps Log file I/O total; path checks are outside it.',
    'reader_file_read': 'Per live tail chunk: acquire/reuse a handle, verify its identity, seek and read. Includes cache/timing overhead and any eviction/error cleanup. Excludes parsing, discovery and cold state seeding. Do not add percentile rows; concurrent Last values can refer to different chunks.',
    'reader_file_open': 'Only actual live tail open attempts are sampled, including failures. Cache hits do not add open samples. Part of Log file I/O total; excludes discovery, resume validation and cold state seeding.',
    'reader_file_seek': 'Per attempted live tail seek: time positioning the unbuffered file handle at the saved byte offset, including failed seeks. Part of Log file I/O total.',
    'reader_file_read_call': 'Per attempted live tail read: time in read(chunk_size), including failed reads. The OS cache affects this call; it is not physical disk latency alone. Part of Log file I/O total.',
    'reader_file_close': 'Only actual cached-handle closes are sampled: eviction, detachment, replacement/truncation, I/O failure, import or shutdown. Successful tail reads keep handles open. Close samples need not occur in a reader chunk; includes failed closes.',
    'reader_parse': 'Per log chunk: decode/frame records, parse and dispatch events. Includes synchronous callbacks and any lock waits inside dispatch.',
    'reader_tail_queue_wait': 'Per submitted tail job: time from submission until its worker starts. Excludes the job itself.',
    'reader_batch_wait': 'Per nonempty batch: wall time the main reader waits for tail futures. Overlaps file I/O, parsing and queue waits; it is not additional processing time.',
    'reader_scope_lock_wait': 'Only contended server-state lock acquisitions are sampled. Overlaps parsing/dispatch. No samples means no observed contention.',
}


class RollingTimings:
    def __init__(self, capacity=512):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._values = {}
        self._totals = {}

    @contextmanager
    def measure(self, stage):
        began = time.monotonic()
        try:
            yield
        finally:
            self.record(stage, (time.monotonic()-began)*1000)

    @contextmanager
    def wait_for(self, lock, stage):
        if not lock.acquire(blocking=False):
            began = time.monotonic()
            lock.acquire()
            self.record(stage, (time.monotonic()-began)*1000)
        try:
            yield
        finally:
            lock.release()

    def record(self, stage, milliseconds):
        if stage not in TIMING_LABELS:
            return
        try:
            value = float(milliseconds)
        except (ValueError, TypeError, OverflowError):
            return
        if not math.isfinite(value) or value < 0:
            return
        with self._lock:
            self._values.setdefault(stage, deque(maxlen=self.capacity)).append(value)
            self._totals[stage] = self._totals.get(stage, 0) + 1

    def snapshot(self):
        with self._lock:
            values = {key: (list(samples), self._totals[key]) for key, samples in self._values.items()}
        result = {}
        for key, (samples, total) in values.items():
            ordered = sorted(samples)
            result[key] = dict(last_ms=samples[-1], p50_ms=ordered[math.ceil(len(ordered)*.50)-1],
                               p95_ms=ordered[math.ceil(len(ordered)*.95)-1], max_ms=ordered[-1],
                               samples=len(samples), total_samples=total)
        return result
