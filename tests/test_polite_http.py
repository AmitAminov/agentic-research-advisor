"""Offline tests for scripts/polite_http.py.

NO network is touched: the module-level requests.Session and time.sleep are
replaced with fakes. Verifies the NETWORK_ETIQUETTE.md guarantees: mailto UA,
per-host minimum spacing (arXiv >= 3.1 s), Retry-After handling, exponential
backoff, ProviderBlocked + per-run host blacklisting, and GitHub
x-ratelimit-reset behavior.
"""
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import polite_http as ph


class FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Reset module state and capture every sleep instead of sleeping."""
    ph._reset_for_tests()
    recorded = []
    monkeypatch.setattr(ph, "_sleep", recorded.append)
    yield recorded
    ph._reset_for_tests()


def _fake(monkeypatch, responses):
    sess = FakeSession(responses)
    monkeypatch.setattr(ph, "_session", sess)
    return sess


def test_ua_has_mailto_and_suffix(monkeypatch):
    sess = _fake(monkeypatch, [FakeResponse(200)])
    ph.get("https://api.semanticscholar.org/graph/v1/paper/search", ua_suffix="+test")
    ua = sess.calls[0][2]["headers"]["User-Agent"]
    assert ua.startswith("ai-ds-ml-dl-researcher/")
    assert "mailto:mldsaidlagents@gmail.com" in ua
    assert ua.endswith("+test")


def test_caller_cannot_override_ua(monkeypatch):
    sess = _fake(monkeypatch, [FakeResponse(200)])
    ph.get("https://example.org/x", headers={"User-Agent": "Mozilla/5.0"})
    assert "Mozilla" not in sess.calls[0][2]["headers"]["User-Agent"]


def test_min_intervals_per_policy():
    assert ph._min_interval("export.arxiv.org", "https://export.arxiv.org/api/query") >= 3.0
    assert ph._min_interval("arxiv.org", "https://arxiv.org/pdf/1234.5678") >= 3.0
    assert ph._min_interval("api.github.com",
                            "https://api.github.com/search/repositories") == 2.5
    assert ph._min_interval("api.semanticscholar.org", "https://api.semanticscholar.org/x") == 1.1
    assert ph._min_interval("example.org", "https://example.org/") == 2.0


def test_throttle_spaces_same_host_requests(monkeypatch, sleeps):
    _fake(monkeypatch, [FakeResponse(200), FakeResponse(200)])
    ph.get("https://export.arxiv.org/api/query")
    ph.get("https://export.arxiv.org/api/query")
    # fake sleep -> almost no real time passes, so the full interval is slept
    assert sleeps and sleeps[0] > 2.9


def test_retry_after_seconds_honored(monkeypatch, sleeps):
    sess = _fake(monkeypatch, [
        FakeResponse(429, {"Retry-After": "7"}),
        FakeResponse(200),
    ])
    r = ph.get("https://api.semanticscholar.org/graph/v1/paper/search/bulk")
    assert r.status_code == 200
    assert len(sess.calls) == 2
    assert 7.0 in sleeps


def test_retry_after_http_date_parsing():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    parsed = ph._parse_retry_after(format_datetime(when))
    assert parsed is not None and 25 <= parsed <= 31
    assert ph._parse_retry_after("42") == 42.0
    assert ph._parse_retry_after(None) is None
    assert ph._parse_retry_after("not-a-date") is None


def test_huge_retry_after_blocks_immediately(monkeypatch):
    sess = _fake(monkeypatch, [FakeResponse(429, {"Retry-After": "3600"})])
    with pytest.raises(ph.ProviderBlocked):
        ph.get("https://example.org/api")
    assert len(sess.calls) == 1  # no camping, no retries


def test_persistent_503_blocks_provider_for_the_run(monkeypatch, sleeps):
    sess = _fake(monkeypatch, [FakeResponse(503)] * ph.MAX_ATTEMPTS)
    with pytest.raises(ph.ProviderBlocked) as ei:
        ph.get("https://example.org/data")
    assert ei.value.host == "example.org"
    assert ei.value.status == 503
    assert len(sess.calls) == ph.MAX_ATTEMPTS
    # backoff sleeps happened between attempts (base 2 s, +/-25% jitter)
    waits = [s for s in sleeps if s >= 1.4]
    assert len(waits) >= ph.MAX_ATTEMPTS - 1
    # host now blacklisted: next call fails fast WITHOUT touching the network
    with pytest.raises(ph.ProviderBlocked):
        ph.get("https://example.org/other")
    assert len(sess.calls) == ph.MAX_ATTEMPTS


def test_github_reset_beyond_cap_blocks(monkeypatch):
    reset = str(int(time.time()) + 3600)  # 1 h away > 15 min cap
    _fake(monkeypatch, [FakeResponse(
        403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})])
    with pytest.raises(ph.ProviderBlocked):
        ph.get("https://api.github.com/search/repositories")


def test_github_reset_within_cap_sleeps_then_retries(monkeypatch, sleeps):
    reset = str(int(time.time()) + 60)
    sess = _fake(monkeypatch, [
        FakeResponse(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        FakeResponse(200, {"x-ratelimit-remaining": "4999"}),
    ])
    r = ph.get("https://api.github.com/repos/owner/repo")
    assert r.status_code == 200
    assert len(sess.calls) == 2
    assert any(s > 30 for s in sleeps)  # slept toward x-ratelimit-reset


def test_success_and_404_pass_through_untouched(monkeypatch):
    _fake(monkeypatch, [FakeResponse(404)])
    assert ph.get("https://api.github.com/repos/a/missing").status_code == 404


def test_paper_pipeline_reexports_providerblocked():
    import paper_pipeline as pp
    assert pp.ProviderBlocked is ph.ProviderBlocked
