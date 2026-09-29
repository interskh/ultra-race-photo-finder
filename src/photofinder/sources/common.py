import logging
import random
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

RETRYABLE = {429, 500, 502, 503, 504}

log = logging.getLogger("download")


class Blocked(Exception):
    pass


class RetriesExhausted(Blocked):
    pass


class AlreadyRunning(Exception):
    pass


def looks_like_jpeg(data: bytes) -> bool:
    return len(data) > 1024 and data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-64:]


def backoff_seconds(attempt: int) -> float:
    return min(60, 2 ** (attempt + 1)) + random.uniform(0, 1)


def retry_after(r: httpx.Response) -> float:
    value = r.headers.get("retry-after", "0")
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return 0.0


def write_atomic(dest: Path, data: bytes):
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)


def fetch_json(client: httpx.Client, method: str, url: str, *, check=lambda body: body, tries=5,
               sleep=time.sleep, **kw):
    for attempt in range(tries):
        wait = backoff_seconds(attempt)
        try:
            r = client.request(method, url, **kw)
            if r.status_code == 200:
                return check(r.json())
            if r.status_code not in RETRYABLE:
                raise Blocked(f"api {url} -> HTTP {r.status_code}")
            wait = max(wait, retry_after(r))
            log.warning("api %s -> HTTP %s (attempt %d)", url, r.status_code, attempt + 1)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("api %s error %r (attempt %d)", url, e, attempt + 1)
        if attempt < tries - 1:
            sleep(wait)
    raise RetriesExhausted(f"api {url} failed after {tries} attempts")
