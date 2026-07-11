"""DirLock (atomic-mkdir advisory lock) and the concurrency-safe Ledger.

The ledger is the one shared mutable resource between overlapping pipeline
runs; these tests pin down the lock's acquire/release/stale-steal semantics
and the claim-exactly-once guarantee that prevents duplicate reproductions.
"""
import json
import os
import time

import paper_pipeline as pp
from fetch_papers import DirLock, Ledger


def _paper(**kw):
    kw.setdefault("title", "A Test Paper")
    return pp.Paper(**kw)


# ---------------------------------------------------------------------------
# DirLock
# ---------------------------------------------------------------------------

def test_lock_acquire_creates_and_release_removes(tmp_path):
    target = tmp_path / "ledger.jsonl"
    lock = DirLock(target)
    with lock:
        assert lock.lockdir.is_dir()
    assert not lock.lockdir.exists()


def test_lock_is_mutually_exclusive_until_released(tmp_path):
    target = tmp_path / "ledger.jsonl"
    with DirLock(target) as first:
        second = DirLock(target, timeout=0.3, poll=0.05, stale_seconds=60)
        t0 = time.monotonic()
        with second:
            # it must have waited out its timeout before proceeding unlocked
            assert time.monotonic() - t0 >= 0.3
            assert second.acquired is False
        # the unlocked proceeder must NOT release the holder's lock on exit
        assert first.lockdir.is_dir()
    assert not (tmp_path / "ledger.jsonl.lock").exists()


def test_lock_steals_stale_lock(tmp_path):
    target = tmp_path / "ledger.jsonl"
    stale = tmp_path / "ledger.jsonl.lock"
    stale.mkdir()
    old = time.time() - 3600
    os.utime(stale, (old, old))  # looks abandoned by a dead process
    t0 = time.monotonic()
    with DirLock(target, timeout=5.0, stale_seconds=120) as lock:
        assert time.monotonic() - t0 < 2.0  # stolen, not waited out
        assert lock.lockdir.is_dir()
    assert not stale.exists()


def test_release_tolerates_already_removed_lockdir(tmp_path):
    lock = DirLock(tmp_path / "ledger.jsonl")
    with lock:
        lock.lockdir.rmdir()  # e.g. stolen by another process
    # __exit__ must not raise


# ---------------------------------------------------------------------------
# Ledger.claim — atomic claim-exactly-once
# ---------------------------------------------------------------------------

def test_claim_first_time_succeeds_and_persists(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    p = _paper(doi="10.1/x", arxiv_id="2406.1")
    assert ledger.claim(p, "ML", "month") is True
    line = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[0])
    assert line["status"] == "claimed"
    assert set(line["keys"]) == {"title:a test paper", "doi:10.1/x",
                                 "arxiv:2406.1"}


def test_claim_second_time_is_rejected(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    p = _paper(doi="10.1/x")
    assert ledger.claim(p, "ML", "month") is True
    assert ledger.claim(p, "ML", "month") is False


def test_claim_rejected_when_any_key_overlaps(tmp_path):
    """The same paper seen with different metadata must still be caught."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    assert ledger.claim(_paper(title="Original Title", doi="10.1/x"),
                        "ML", "month") is True
    # different title, same DOI -> already claimed
    assert ledger.claim(_paper(title="V2 Retitled", doi="10.1/X"),
                        "DL", "6h") is False
    # same normalized title, no DOI -> already claimed
    assert ledger.claim(_paper(title="original title!"), "AI", "6h") is False


def test_distinct_papers_both_claim(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    assert ledger.claim(_paper(title="First", doi="10.1/a"), "ML", "month")
    assert ledger.claim(_paper(title="Second", doi="10.1/b"), "ML", "month")
    assert len(ledger.load_keys()) == 4  # 2 title keys + 2 doi keys


def test_record_status_appends_audit_line_without_unclaiming(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    p = _paper(doi="10.1/x")
    ledger.claim(p, "ML", "month")
    ledger.record_status(p, "ML", "no-pdf", local_pdf=None)
    lines = [json.loads(ln) for ln in
             (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [ln["status"] for ln in lines] == ["claimed", "no-pdf"]
    assert ledger.claim(p, "ML", "month") is False  # still claimed


def test_load_keys_skips_malformed_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('not json\n{"keys": ["doi:10.1/x"]}\n\n', encoding="utf-8")
    assert Ledger(path).load_keys() == {"doi:10.1/x"}
