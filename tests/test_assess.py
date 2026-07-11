"""reproduce.assess() — the artifact-scoring step of the QA loop.

assess() inspects a per-paper directory after a worker run and produces the
artifact census that decides `produced`, feeds the ledger, the daily digest
and the web app. It must be honest about missing/malformed artifacts and
never raise on garbage input.
"""
import json

import reproduce


def _metrics(verdict="partial", n=2, reason="scaled down"):
    return {
        "paper_title": "T",
        "metrics": [{"name": f"m{i}", "paper_value": 1.0,
                     "reproduced_value": 0.9, "unit": None,
                     "abs_diff": -0.1, "within_tolerance": True,
                     "notes": ""} for i in range(n)],
        "verdict": verdict,
        "verdict_reason": reason,
        "reproduced_on": "2026-07-01",
    }


def _scaffold(tmp_path):
    d = tmp_path / "AI" / "some-paper"
    for sub in reproduce.CANONICAL_SUBDIRS:
        (d / sub).mkdir(parents=True)
    return d


def test_assess_empty_scaffold_reports_nothing(tmp_path):
    art = reproduce.assess(_scaffold(tmp_path))
    assert art["src_files"] == 0
    assert art["has_metrics"] is False
    assert art["verdict"] is None
    assert art["tests"] == 0
    assert art["has_summary_pdf"] is False


def test_assess_full_artifact_census(tmp_path):
    d = _scaffold(tmp_path)
    # src: nested .py files count; non-.py files do not
    (d / "src" / "reproduce.py").write_text("print('hi')")
    (d / "src" / "model" ).mkdir()
    (d / "src" / "model" / "net.py").write_text("x = 1")
    (d / "src" / "README.md").write_text("doc")
    (d / "src" / "upstream").mkdir()
    # original figures + captions
    (d / "original_results" / "fig-01-key.png").write_bytes(b"\x89PNG")
    (d / "original_results" / "fig-02-other.png").write_bytes(b"\x89PNG")
    (d / "original_results" / "captions.json").write_text(
        json.dumps({"fig-01-key.png": "Figure 1", "fig-02-other.png": "Figure 2"}))
    # reproduced outputs + metrics
    (d / "reproduced_results" / "metrics.json").write_text(
        json.dumps(_metrics(verdict="partial", n=3)))
    (d / "reproduced_results" / "fig1.png").write_bytes(b"\x89PNG")
    # tests / manim / data / summary
    (d / "tests" / "test_core.py").write_text("def test_ok(): pass")
    (d / "tests" / "helper.py").write_text("")          # not test_*.py
    (d / "manim" / "scene.py").write_text("class S: pass")
    (d / "manim" / "finding.mp4").write_bytes(b"00")
    (d / "original_data" / "DATA_SOURCE.md").write_text("url")
    (d / "summary.md").write_text("# Summary")
    (d / "requirements.txt").write_text("numpy")

    art = reproduce.assess(d)
    assert art["src_files"] == 2
    assert art["has_upstream"] is True
    assert art["original_figs"] == 2
    assert art["original_captions"] == 2
    assert art["reproduced_imgs"] == 1
    assert art["reproduced_files"] == 2       # metrics.json + fig1.png
    assert art["has_metrics"] is True
    assert art["n_metrics"] == 3
    assert art["verdict"] == "partial"
    assert art["verdict_reason"] == "scaled down"
    assert art["tests"] == 1
    assert art["manim_files"] == 1
    assert art["manim_render"] == 1
    assert art["has_data_source"] is True
    assert art["has_summary_md"] is True
    assert art["has_summary_pdf"] is False
    assert art["has_requirements"] is True
    # back-compat aliases consumed by the older digest
    assert art["figures"] == art["reproduced_imgs"]
    assert art["has_results_json"] is art["has_metrics"]


def test_assess_malformed_metrics_is_tolerated(tmp_path):
    d = _scaffold(tmp_path)
    (d / "reproduced_results" / "metrics.json").write_text("{not json!!")
    art = reproduce.assess(d)
    assert art["has_metrics"] is True   # the file exists...
    assert art["n_metrics"] == 0        # ...but yields no usable content
    assert art["verdict"] is None


def test_assess_reads_legacy_results_json(tmp_path):
    d = _scaffold(tmp_path)
    (d / "reproduced_results" / "results.json").write_text(
        json.dumps(_metrics(verdict="minimal", n=1)))
    art = reproduce.assess(d)
    assert art["has_metrics"] is True
    assert art["verdict"] == "minimal"
    assert art["n_metrics"] == 1


def test_assess_truncates_untrusted_verdict_strings(tmp_path):
    """Worker-written strings are clamped before entering reports."""
    d = _scaffold(tmp_path)
    (d / "reproduced_results" / "metrics.json").write_text(json.dumps(
        _metrics(verdict="x" * 500, reason="r" * 1000)))
    art = reproduce.assess(d)
    assert len(art["verdict"]) <= 20
    assert len(art["verdict_reason"]) <= 280


def test_assess_metrics_dict_shape_counts(tmp_path):
    d = _scaffold(tmp_path)
    (d / "reproduced_results" / "metrics.json").write_text(json.dumps(
        {"metrics": {"acc": 0.9, "f1": 0.8}, "verdict": "partial"}))
    assert reproduce.assess(d)["n_metrics"] == 2


def test_produced_criterion_matches_reproduce_one_logic(tmp_path):
    """`produced` requires src + (outputs or metrics) + summary.pdf."""
    d = _scaffold(tmp_path)
    (d / "src" / "reproduce.py").write_text("pass")
    (d / "reproduced_results" / "metrics.json").write_text(
        json.dumps(_metrics()))
    art = reproduce.assess(d)
    produced = (art["src_files"] > 0
                and (art["reproduced_files"] > 0 or art["has_metrics"])
                and art["has_summary_pdf"])
    assert produced is False            # no summary.pdf yet
    (d / "summary.pdf").write_bytes(b"%PDF-1.4")
    art = reproduce.assess(d)
    assert (art["src_files"] > 0
            and (art["reproduced_files"] > 0 or art["has_metrics"])
            and art["has_summary_pdf"]) is True


# ---------------------------------------------------------------------------
# slugify + ledger-slug loading (reproduce's own dedup layer)
# ---------------------------------------------------------------------------

def test_slugify_normalizes_and_truncates():
    assert reproduce.slugify("Attention Is All You Need!") == \
        "attention-is-all-you-need"
    assert len(reproduce.slugify("word " * 60)) <= 80
    assert reproduce.slugify("!!!") == "paper"


def test_already_processed_unions_ledger_and_progress(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    progress = tmp_path / "progress.jsonl"
    ledger.write_text('{"slug": "a"}\nbroken\n{"no_slug": 1}\n')
    progress.write_text('{"slug": "b"}\n')
    assert reproduce.already_processed(ledger, progress) == {"a", "b"}
