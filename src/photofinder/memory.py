import ctypes
import logging
import os
import subprocess
import time

import psutil

NORMAL, WARN, CRITICAL = 1, 2, 4
LEVEL_NAMES = {NORMAL: "normal", WARN: "warn", CRITICAL: "critical"}
MAX_FOOTPRINT_MB = 4096

log = logging.getLogger("memory")


def pressure_level() -> int:
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                             capture_output=True, text=True, timeout=5).stdout
        level = int(out.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return NORMAL
    return CRITICAL if level >= CRITICAL else WARN if level >= WARN else NORMAL


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


class _RUsageInfoV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("fields", ctypes.c_uint64 * 22)]


def footprint_mb(pid: int | None = None) -> float:
    info = _RUsageInfoV2()
    if ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage(pid or os.getpid(), 2, ctypes.byref(info)) != 0:
        return rss_mb()
    return info.fields[7] / (1024 * 1024)


def watch_parent(parent: int):
    while os.getppid() == parent:
        time.sleep(2)
    os._exit(0)


class FootprintExceeded(RuntimeError):
    pass


class AdaptiveBatcher:
    def __init__(self, max_size: int, reader=pressure_level, sleep=time.sleep, *,
                 poll=5.0, grow_after=20, log_every=50, footprint=footprint_mb, max_footprint_mb=MAX_FOOTPRINT_MB):
        self.max_size = max_size
        self.size = max_size
        self.reader = reader
        self.sleep = sleep
        self.poll = poll
        self.grow_after = grow_after
        self.log_every = log_every
        self.normal_streak = 0
        self.batches = 0
        self.items = 0
        self.pending = 0
        self.footprint = footprint
        self.max_footprint_mb = max_footprint_mb

    def _log(self, level, msg="memory"):
        log.info("%s: level=%s batch=%d footprint=%.0fMB rss=%.0fMB", msg, LEVEL_NAMES.get(level, level),
                 self.size, self.footprint(), rss_mb())

    def next_size(self) -> int:
        used = self.footprint()
        if used > self.max_footprint_mb:
            raise FootprintExceeded(f"process memory footprint {used:.0f} MB exceeds {self.max_footprint_mb} MB; "
                                    "stopping so the Mac doesn't swap (progress is saved, rerun to resume)")
        level = self.reader()
        while level >= CRITICAL:
            self._log(level, "memory critical, paused")
            self.sleep(self.poll)
            level = self.reader()
        before = self.size
        if level >= WARN:
            self.size = max(1, self.size // 2)
            self.normal_streak = 0
        else:
            self.normal_streak += 1
            if self.normal_streak >= self.grow_after:
                self.size = min(self.max_size, self.size * 2)
                self.normal_streak = 0
        self.batches += 1
        if self.size != before or self.batches % self.log_every == 0:
            self._log(level)
        return self.size

    def chunks(self, items):
        items = list(items)
        self.pending = len(items)
        i = 0
        while i < len(items):
            n = self.next_size()
            chunk = items[i:i + n]
            self.items += len(chunk)
            yield chunk
            i += n
