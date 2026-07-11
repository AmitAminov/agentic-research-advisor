#!/usr/bin/env python3
"""
polite_http.py -- the ONE shared polite HTTP client for the researcher pipeline.

Implements the binding policy in C:\\Users\\ADMIN\\Agentic_Projects\\NETWORK_ETIQUETTE.md
("Implementation requirements"): every external HTTP request made by the Python
scripts in this directory goes through this module. No bare requests.get/post to
external hosts anywhere else in scripts/.

What it guarantees
------------------
* Honest identification: User-Agent
  "ai-ds-ml-dl-researcher/1.0 (mailto:mldsaidlagents@gmail.com)" plus an
  optional per-caller suffix (e.g. "+reproduce"). The UA is NEVER spoofed or
  rotated, and blocks are NEVER worked around (rule 2).
* One connection per host, serial requests (rule 4): a module-level
  requests.Session behind a global lock, with a per-host minimum-interval
  throttle enforced by a sleep before every request:
    - arXiv hosts            >= 3.1 s   (their ToS: 1 request / 3 s)
    - api.github.com /search >= 2.5 s   (Search API is 30/min authenticated)
    - api.semanticscholar.org>= 1.1 s   (unauthenticated shared pool, <= 1/s)
    - every other host       >= 2.0 s
* Provider signals honored (rule 5): on HTTP 429/403/503 the Retry-After
  header (delta-seconds or HTTP-date) is obeyed when present (capped at 300 s;
  a longer server-mandated wait means "blocked" -> stop, don't camp);
  otherwise exponential backoff 2 s * 2**n with +/-25% jitter, capped at
  120 s. Max 4 attempts total, then ProviderBlocked is raised AND the host is
  blacklisted for the remainder of the process (rule 3: blocked means stop --
  later calls to that host fail fast without touching the network).
* GitHub rate-limit headers: every api.github.com response is checked for
  x-ratelimit-remaining == 0; when exhausted the client sleeps until
  x-ratelimit-reset (capped at 15 min, beyond which ProviderBlocked).

Volume budget (NETWORK_ETIQUETTE.md rule 8)
-------------------------------------------
Estimated requests/day at the current cadence (~4 session cycles + 1 daily
cycle = 5 pipeline cycles/day):

  fetch_papers harvest, per cycle: 4 areas x 4 metadata sources = 16 API
    queries + <= 33 PDF downloads (skip-if-exists caching; after the first
    cycle of the day the month bucket mostly dedupes, so typically ~0-5 PDFs)
    -> <= 49 requests/cycle; x5 cycles = <= 245/day (typical ~100/day).
  reproduce.py, per NEW paper: <= 1 GitHub search + <= 3 repo-verify GETs
    = <= 4 -> worst case 33 new papers/day x 4 = <= 132/day (typical <= 20).
  github_sync, per push: 2 API GETs (+1 one-off repo create) x ~5 pushes/day
    -> ~10-15/day.
  send_report: <= 2 emails/day over SMTP (not HTTP); the stale SendGrid path
    would add <= 2 POSTs/day if ever re-enabled.

  TOTAL worst case: 245 + 132 + 15 + 2 ~= 394 -> < ~400 HTTP requests/day,
  spread over >= 6 distinct hosts, each host strictly serial at the minimum
  spacing listed above.
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

__all__ = ["ProviderBlocked", "request", "get", "post", "USER_AGENT"]

USER_AGENT = "ai-ds-ml-dl-researcher/1.0 (mailto:mldsaidlagents@gmail.com)"

# per-host minimum seconds between consecutive requests (provider limits table)
_ARXIV_INTERVAL = 3.1  # arXiv ToS: >= 3 s between requests, single connection
HOST_MIN_INTERVAL: dict[str, float] = {
    "export.arxiv.org": _ARXIV_INTERVAL,
    "arxiv.org": _ARXIV_INTERVAL,
    "www.arxiv.org": _ARXIV_INTERVAL,
    "static.arxiv.org": _ARXIV_INTERVAL,
    "api.semanticscholar.org": 1.1,
}
DEFAULT_MIN_INTERVAL = 2.0
GITHUB_HOST = "api.github.com"
GITHUB_SEARCH_MIN_INTERVAL = 2.5  # Search API: 30/min auth'd -> stay well under

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 120.0
RETRY_AFTER_CAP_SECONDS = 300.0   # longer server-mandated waits => blocked
GITHUB_RESET_CAP_SECONDS = 900.0  # sleep to x-ratelimit-reset at most 15 min

_RETRY_STATUSES = frozenset({429, 403, 503})


class ProviderBlocked(Exception):
    """A provider is rate-limiting/blocking us: stop it for the rest of the run.

    Carries ``host`` and ``status`` so callers can log clearly and skip
    gracefully (one paper's PDF fails -> skip the paper; arXiv itself blocked
    -> end the harvest step for that provider). NEVER retried around.
    """

    def __init__(self, host: str, status: int | None = None, reason: str = "") -> None:
        self.host = host
        self.status = status
        msg = f"provider blocked for this run: {host}"
        if status is not None:
            msg += f" (HTTP {status})"
        if reason:
            msg += f" -- {reason}"
        super().__init__(msg)


_session = requests.Session()
_lock = threading.RLock()          # serializes ALL requests (rule 4)
_sleep = time.sleep                # module-level so offline tests can patch it
_last_request_at: dict[str, float] = {}   # host -> time.monotonic() of last request
_blocked: dict[str, ProviderBlocked] = {}  # host -> first block (fail-fast after)
_gh_reset_epoch: float | None = None       # x-ratelimit-reset seen with remaining==0


def _reset_for_tests() -> None:
    """Clear module state (used by the offline unit tests only)."""
    global _gh_reset_epoch
    _last_request_at.clear()
    _blocked.clear()
    _gh_reset_epoch = None


def _host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _min_interval(host: str, url: str) -> float:
    if host == GITHUB_HOST:
        path = urlsplit(url).path or ""
        if path.startswith("/search/"):
            return GITHUB_SEARCH_MIN_INTERVAL
        return DEFAULT_MIN_INTERVAL
    return HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)


def _throttle(host: str, url: str) -> None:
    """Sleep so consecutive requests to the same host keep the min spacing."""
    last = _last_request_at.get(host)
    if last is None:
        return
    wait = _min_interval(host, url) - (time.monotonic() - last)
    if wait > 0:
        _sleep(wait)


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After as seconds; accepts delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _note_github_headers(host: str, resp: requests.Response) -> None:
    """Track GitHub quota exhaustion from ANY api.github.com response."""
    global _gh_reset_epoch
    if host != GITHUB_HOST:
        return
    remaining = resp.headers.get("x-ratelimit-remaining")
    if remaining == "0":
        try:
            _gh_reset_epoch = float(resp.headers.get("x-ratelimit-reset", ""))
        except ValueError:
            _gh_reset_epoch = None
    elif remaining is not None:
        _gh_reset_epoch = None


def _github_preflight(host: str) -> None:
    """If the previous GitHub response said the quota is exhausted, sleep until
    the advertised reset (capped at 15 min) BEFORE sending anything else."""
    global _gh_reset_epoch
    if host != GITHUB_HOST or _gh_reset_epoch is None:
        return
    wait = _gh_reset_epoch - time.time()
    _gh_reset_epoch = None
    if wait <= 0:
        return
    if wait > GITHUB_RESET_CAP_SECONDS:
        raise _block(host, None,
                     f"x-ratelimit-reset {wait:.0f}s away "
                     f"(> {GITHUB_RESET_CAP_SECONDS:.0f}s cap)")
    print(f"[polite-http] {host}: rate limit exhausted; "
          f"sleeping {wait:.0f}s until x-ratelimit-reset")
    _sleep(wait + 1.0)


def _block(host: str, status: int | None, reason: str) -> ProviderBlocked:
    """Blacklist the host for the rest of the process and build the exception."""
    exc = ProviderBlocked(host, status, reason)
    _blocked[host] = exc
    print(f"[polite-http] {exc}")
    return exc


def _retry_wait(host: str, resp: requests.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429/403/503 response.

    Raises ProviderBlocked immediately when the server mandates a wait beyond
    the caps (Retry-After > 300 s, or GitHub reset > 15 min away).
    """
    ra = _parse_retry_after(resp.headers.get("Retry-After"))
    if ra is not None:
        if ra > RETRY_AFTER_CAP_SECONDS:
            raise _block(host, resp.status_code,
                         f"Retry-After {ra:.0f}s exceeds "
                         f"{RETRY_AFTER_CAP_SECONDS:.0f}s cap")
        return ra
    if host == GITHUB_HOST and resp.headers.get("x-ratelimit-remaining") == "0":
        try:
            wait = float(resp.headers.get("x-ratelimit-reset", "")) - time.time()
        except ValueError:
            wait = None
        if wait is not None:
            if wait > GITHUB_RESET_CAP_SECONDS:
                raise _block(host, resp.status_code,
                             f"x-ratelimit-reset {wait:.0f}s away "
                             f"(> {GITHUB_RESET_CAP_SECONDS:.0f}s cap)")
            return max(wait, 1.0)
    delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))
    return min(BACKOFF_CAP_SECONDS, delay * random.uniform(0.75, 1.25))


def request(method: str, url: str, *, params=None, headers=None, json=None,
            data=None, timeout: float = 40.0, ua_suffix: str = "") -> requests.Response:
    """Polite, throttled, Retry-After-aware HTTP request.

    Returns the final requests.Response (callers keep their own
    raise_for_status()/status_code handling). Raises ProviderBlocked when the
    host answered 429/403/503 on all MAX_ATTEMPTS tries, when a
    server-mandated wait exceeds the caps, or when the host was already
    blocked earlier in this run (fail-fast, no network touch).
    """
    host = _host_of(url)
    with _lock:
        if host in _blocked:
            prior = _blocked[host]
            raise ProviderBlocked(host, prior.status, "blocked earlier in this run")
        hdrs = dict(headers or {})
        # our honest UA is authoritative -- never spoofed, rotated, or overridden
        hdrs["User-Agent"] = USER_AGENT + (f" {ua_suffix}" if ua_suffix else "")
        last_status: int | None = None
        for attempt in range(MAX_ATTEMPTS):
            _github_preflight(host)
            _throttle(host, url)
            try:
                resp = _session.request(method, url, params=params, headers=hdrs,
                                        json=json, data=data, timeout=timeout)
            finally:
                _last_request_at[host] = time.monotonic()
            _note_github_headers(host, resp)
            if resp.status_code not in _RETRY_STATUSES:
                return resp
            last_status = resp.status_code
            if attempt == MAX_ATTEMPTS - 1:
                break
            wait = _retry_wait(host, resp, attempt)  # may raise ProviderBlocked
            print(f"[polite-http] {host}: HTTP {resp.status_code}; retrying in "
                  f"{wait:.1f}s (attempt {attempt + 2}/{MAX_ATTEMPTS})")
            _sleep(wait)
        raise _block(host, last_status,
                     f"HTTP {last_status} persisted after {MAX_ATTEMPTS} attempts")


def get(url: str, **kwargs) -> requests.Response:
    """Polite GET (see request())."""
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    """Polite POST (see request())."""
    return request("POST", url, **kwargs)
