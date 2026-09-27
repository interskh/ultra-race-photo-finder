import logging
import resource
import subprocess
import time

import psutil

NORMAL, WARN, CRITICAL = 1, 2, 4
LEVEL_NAMES = {NORMAL: "normal", WARN: "warn", CRITICAL: "critical"}

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


def max_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


class AdaptiveBatcher:
    def __init__(self, max_size: int, reader=pressure_level, sleep=time.sleep, *,
                 poll=5.0, grow_after=20, log_every=50):
        self.max_size = max_size
        self.size = max_size
        self.reader = reader
        self.sleep = sleep
        self.poll = poll
        self.grow_after = grow_after
        self.log_every = log_every
        self.normal_streak = 0
        self.batches = 0

    def _log(self, level, msg="memory"):
        log.info("%s: level=%s batch=%d rss=%.0fMB max_rss=%.0fMB", msg, LEVEL_NAMES.get(level, level),
                 self.size, rss_mb(), max_rss_mb())

    def next_size(self) -> int:
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
        i = 0
        while i < len(items):
            n = self.next_size()
            yield items[i:i + n]
            i += n
