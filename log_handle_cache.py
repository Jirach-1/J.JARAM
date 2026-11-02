"""Bounded, leased log handles. Slow open/close calls never hold the cache lock."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import threading


def open_shared_log(path):
    """Read without buffered lookahead and allow Windows writers/rotation."""
    if os.name != 'nt':
        return open(path, 'rb', buffering=0)
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(os.path.abspath(path), 0x80000000, 0x1 | 0x2 | 0x4,
                    None, 3, 0x80, None)  # READ; SHARE_READ/WRITE/DELETE; OPEN_EXISTING
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(fd, 'rb', buffering=0)
    except BaseException:
        os.close(fd)
        raise


@dataclass
class _Entry:
    key: tuple
    handle: object = None
    users: int = 0
    retired: bool = False
    disposed: bool = False
    lock: object = field(default_factory=threading.Lock)


class LogHandleCache:
    def __init__(self, capacity=256, *, close_handle=None):
        self.capacity = max(1, int(capacity))
        self._close_handle = close_handle or (lambda handle: handle.close())
        self._condition = threading.Condition()
        self._entries = OrderedDict()
        self._allocated = 0  # Includes opens/closes in progress and retired leases.
        self._closed = False
        self._stats = dict(hits=0, misses=0, opens=0, open_handles=0, peak_open_handles=0,
                           evictions=0, invalidations=0, close_failures=0)

    def _retire_locked(self, entry):
        if not entry.retired:
            entry.retired = True
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key)
            self._stats['invalidations'] += 1

    def _dispose(self, entry):
        with self._condition:
            if entry.disposed or entry.users:
                return
            entry.disposed = True
            handle, entry.handle = entry.handle, None
        try:
            if handle is not None:
                self._close_handle(handle)
        except Exception:
            with self._condition:
                self._stats['close_failures'] += 1
        finally:
            with self._condition:
                if handle is not None:
                    self._stats['open_handles'] -= 1
                self._allocated -= 1
                self._condition.notify_all()

    def _acquire(self, key):
        while True:
            victim = None
            with self._condition:
                if self._closed:
                    raise RuntimeError('Log handle cache is closed')
                entry = self._entries.get(key)
                if entry is not None:
                    entry.users += 1
                    self._entries.move_to_end(key)
                    self._stats['hits'] += 1
                    return entry
                if self._allocated < self.capacity:
                    entry = _Entry(key, users=1)
                    self._entries[key] = entry
                    self._allocated += 1
                    self._stats['misses'] += 1
                    return entry
                victim = next((item for item in self._entries.values() if not item.users), None)
                if victim is None:
                    self._condition.wait()
                    continue
                self._retire_locked(victim)
                self._stats['evictions'] += 1
            self._dispose(victim)

    def _release(self, entry):
        with self._condition:
            entry.users -= 1
            dispose = entry.retired and not entry.users
            self._condition.notify_all()
        if dispose:
            self._dispose(entry)

    @contextmanager
    def borrow(self, key, opener):
        while True:
            entry = self._acquire(key)
            entry.lock.acquire()
            if not entry.retired:
                break
            entry.lock.release()
            self._release(entry)
        try:
            if entry.handle is None:
                entry.handle = opener()
                with self._condition:
                    self._stats['opens'] += 1
                    self._stats['open_handles'] += 1
                    self._stats['peak_open_handles'] = max(
                        self._stats['peak_open_handles'], self._stats['open_handles'])
            if entry.retired:
                raise OSError('Log handle was retired while opening')
            yield entry.handle
        except BaseException:
            with self._condition:
                self._retire_locked(entry)
            raise
        finally:
            entry.lock.release()
            self._release(entry)

    def discard_user(self, uid):
        with self._condition:
            entries = [entry for entry in self._entries.values() if entry.key[0] == str(uid)]
            for entry in entries:
                self._retire_locked(entry)
        for entry in entries:
            self._dispose(entry)

    def close(self):
        with self._condition:
            self._closed = True
            entries = list(self._entries.values())
            for entry in entries:
                self._retire_locked(entry)
            self._condition.notify_all()
        for entry in entries:
            self._dispose(entry)

    def diagnostics(self):
        with self._condition:
            return dict(self._stats, capacity=self.capacity, reserved_handles=self._allocated)
