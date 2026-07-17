#!/usr/bin/env python3
"""
Per-paper reproduction harness.

For every freshly-harvested paper that has NOT yet been reproduced (deduplicated
against the append-only ledger  state/processed_ledger.jsonl) it:

  1. scaffolds the canonical per-paper repo under
        <repo>/<AREA>/<paper-slug>/
     with the fixed structure:
        src/                reproduced Python code (starts from the paper's own
                            GitHub repo if one is found + cloned)
        original_data/      authors' data (downloaded, or a DATA_SOURCE.md link)
        original_results/   the paper's key figures (from its repo, else
                            extracted from the PDF)
        reproduced_results/ figures/metrics produced by src/
        tests/              pytest unit + result-validation tests
        manim/              manim animation of the core finding
        summary.pdf         methodology + how the code reproduces the results
     and drops in paper.pdf + paper.md.

  2. best-effort locates the paper's OFFICIAL code repo (scanning the paper text
     / landing links, then a GitHub API search) and shallow-clones it into
        src/upstream/
     so the reproduction starts from the authors' own code.

  3. auto-extracts the paper's figures from paper.pdf into original_results/.

  4. drives the reproduction by invoking the local `claude` CLI headlessly
     (bounded per-paper wall-clock, cwd = the paper dir, exactly like the wiki
     ingest runner: `claude -p <prompt> --dangerously-skip-permissions`),
     instructing it to reproduce the paper's MAIN result(s) + KEY figure(s),
     fetch the original data, write src/tests/manim, and write summary.md.

  5. guarantees summary.pdf exists (renders summary.md -> summary.pdf via
     reportlab if claude did not produce the PDF itself).

  6. records the outcome in  state/progress.jsonl  and appends the dedup key to
     state/processed_ledger.jsonl.

Reproducing arbitrary frontier papers fully is not always possible (GPU-scale
training, proprietary data). The harness targets a *faithful minimal
reproduction*: the core method on small/public/synthetic data, key figures
regenerated, unit tests, an honest deviations section, and a manim animation of
the central finding. Papers accumulate day by day.

Constraints honoured: Windows, CPU-only, Python 3.10 in the project .venv. The
GitHub token (read from the environment for API search only) is NEVER printed,
logged, or written to any file. config.json is read-only here.

Quota pacing (Amit directive 2026-07-12) - the reproduction loop works WITH the
account budget instead of bursting into the quota wall (the root cause of ~190
burned papers, docs/FAILURE_ANALYSIS.md). Two knobs, under config
``reproduce.pacing``:
  * ``consecutive_limit_stop`` (default 2): after this many CONSECUTIVE papers
    return a quota/limit outcome (BLOCKED_USAGE_LIMIT / BLOCKED_SESSION_LIMIT /
    BLOCKED_API_OVERLOAD) the cycle STOPS gracefully; because dedup is
    outcome-based the un-attempted papers are NOT burned and retry next cycle.
    Set 0 to disable the circuit-breaker.
  * ``min_seconds_between_calls`` (default 15): minimum wall-clock gap enforced
    between successive headless-claude spawns so a cycle cannot machine-gun the
    API. Set 0 to disable inter-spawn pacing.
Concurrency is also lowered elsewhere (wiki parallel-ingest workers 4->2) so
wiki-ingest and reproduction do not both draw heavy concurrent quota.

Usage:
    python reproduce.py --config ../config.json                 # today's harvest
    python reproduce.py --config ../config.json --harvest 2026-06-30
    python reproduce.py --config ../config.json --backfill      # any un-reproduced paper in the corpus
    python reproduce.py --config ../config.json --backfill --deadline-minutes 300
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ensure the shared pdf styling module (scripts/pdf_style.py) is importable
# regardless of the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_paths  # noqa: E402  (needs the sys.path insert above)
import wiki_index  # noqa: E402  (TF-IDF retrieval over the LLM knowledge wiki)

# -----------------------------------------------------------------------------
# canonical per-paper directory layout
# -----------------------------------------------------------------------------
CANONICAL_SUBDIRS = (
    "src",
    "original_data",
    "original_results",
    "reproduced_results",
    "tests",
    "manim",
)

# -----------------------------------------------------------------------------
# run-status vocabulary (Amit directive 2026-07-12)
# -----------------------------------------------------------------------------
# Root-cause analysis (docs/FAILURE_ANALYSIS.md) showed the dominant failure was
# NOT intrinsic paper difficulty but the harness itself: the headless `claude`
# CLI returned an account usage/session limit / API 529 / empty output, produced
# nothing, and the run was then recorded in the dedup ledger as "completed" -
# permanently burning the paper so it was never retried. These statuses let the
# harness (a) distinguish graceful, first-class terminal outcomes from (b)
# retryable infrastructure failures that must NOT burn the paper, and (c) cheap
# pre-screen skips - and let downstream (webapp/digest/QA) read them as plain
# strings without breaking.
#
# Terminal, produced a real reproduction:
STATUS_REPRODUCED = "reproduced"
# Genuine attempt finished but the artifact contract was not met. RETRYABLE as
# of 2026-07-17 (Amit directive - failure investigation): a "minimal" outcome is
# re-attempted with a continuation prompt (bounded by reproduce.max_retries) and
# then settled honestly as EXHAUSTED_RETRIES; it is never published as done.
STATUS_COMPLETED_MINIMAL = "completed-minimal"
# Terminal, first-class non-failure skips:
STATUS_SKIPPED_SECURITY = "SKIPPED_SECURITY_POLICY"
STATUS_INELIGIBLE_HARDWARE = "INELIGIBLE_HARDWARE"
STATUS_NOT_REPRODUCIBLE = "NOT_A_REPRODUCIBLE_PROJECT"
STATUS_NO_CODE = "NO_CODE"
STATUS_EXHAUSTED = "EXHAUSTED_RETRIES"
# Retryable infrastructure / transient failures (do NOT burn the paper):
STATUS_BLOCKED_USAGE = "BLOCKED_USAGE_LIMIT"
STATUS_BLOCKED_SESSION = "BLOCKED_SESSION_LIMIT"
STATUS_BLOCKED_API = "BLOCKED_API_OVERLOAD"
STATUS_EMPTY_OUTPUT = "EMPTY_OUTPUT"
STATUS_TIMEOUT_NO_OUTPUT = "TIMEOUT_NO_OUTPUT"
# Timeout that left real partial artifacts on disk (src/ code, partial results):
# retryable, and the retry runs with a CONTINUATION note so the next attempt
# finishes the work instead of restarting - a segmented budget across attempts
# (2026-07-17 fix: 4 of the 15 stub papers died at exactly the 35-min cap with
# substantial src/ already written, labelled TIMEOUT_NO_OUTPUT).
STATUS_TIMEOUT_PARTIAL = "TIMEOUT_PARTIAL_OUTPUT"
STATUS_CLAUDE_NOT_FOUND = "claude-not-found"
STATUS_HARNESS_ERROR = "harness-error"

# Statuses that mean "this paper is settled - do not auto-retry it" even without
# a produced reproduction. (A produced==True record is always terminal too.)
# NOTE (2026-07-17): STATUS_COMPLETED_MINIMAL was REMOVED from this set - a
# minimal attempt is now retryable (bounded), then settles as EXHAUSTED_RETRIES.
TERMINAL_NONPRODUCED_STATUSES = frozenset({
    STATUS_SKIPPED_SECURITY, STATUS_INELIGIBLE_HARDWARE,
    STATUS_NOT_REPRODUCIBLE, STATUS_NO_CODE, STATUS_EXHAUSTED,
})
# Retryable statuses: an infra/transient failure OR an honest-but-insufficient
# attempt; re-attempt on a future run (bounded by reproduce.max_retries so a
# genuinely hard paper cannot loop forever - it settles as EXHAUSTED_RETRIES).
RETRYABLE_STATUSES = frozenset({
    STATUS_BLOCKED_USAGE, STATUS_BLOCKED_SESSION, STATUS_BLOCKED_API,
    STATUS_EMPTY_OUTPUT, STATUS_TIMEOUT_NO_OUTPUT, STATUS_TIMEOUT_PARTIAL,
    STATUS_COMPLETED_MINIMAL, STATUS_CLAUDE_NOT_FOUND,
    STATUS_HARNESS_ERROR, "timeout",
})

# -----------------------------------------------------------------------------
# quota pacing (Amit directive 2026-07-12) - live within the account budget
# -----------------------------------------------------------------------------
# The dominant historical failure (docs/FAILURE_ANALYSIS.md) was a cycle firing
# many headless-claude spawns that ALL slammed into the account quota wall in a
# burst, burning ~190 papers. The knobs below (config reproduce.pacing.*) make a
# cycle pace itself so it works WITH the budget instead of exhausting it. See the
# module docstring for the config schema.
#
# Account quota/session/API-limit statuses; K consecutive of these trip the
# circuit-breaker (they are also a strict subset of RETRYABLE_STATUSES, so a
# tripped cycle leaves every un-attempted paper eligible for the next cycle).
QUOTA_LIMIT_STATUSES = frozenset({
    STATUS_BLOCKED_USAGE, STATUS_BLOCKED_SESSION, STATUS_BLOCKED_API,
})

# Pacing defaults (used when config omits reproduce.pacing.*):
DEFAULT_CONSECUTIVE_LIMIT_STOP = 2
DEFAULT_MIN_SECONDS_BETWEEN_CALLS = 15.0


def is_quota_limit(status: str | None) -> bool:
    """True if a run status is an account quota / session / API-limit wall."""
    return status in QUOTA_LIMIT_STATUSES


def update_quota_streak(status: str | None, streak: int) -> int:
    """Advance the consecutive-quota-limit streak given the latest run status.

    Returns ``streak + 1`` for a quota/limit outcome, else resets to 0. Pure (no
    I/O) so the circuit-breaker decision is unit-testable offline.
    """
    return streak + 1 if status in QUOTA_LIMIT_STATUSES else 0


def quota_circuit_tripped(streak: int, threshold: int) -> bool:
    """Whether ``streak`` consecutive quota-limit outcomes should stop the cycle.

    ``threshold <= 0`` disables the circuit-breaker (never trips).
    """
    return threshold > 0 and streak >= threshold


class SpawnPacer:
    """Enforce a minimum wall-clock gap between successive claude spawns.

    Call :meth:`wait` immediately before each claude spawn; it sleeps only the
    remaining time needed so consecutive spawns are at least ``min_seconds``
    apart, and never sleeps before the first spawn. ``sleep`` and ``clock`` are
    injectable so the pacing is fully unit-testable without real waiting.

    Parameters
    ----------
    min_seconds : float
        Minimum gap between spawns (``<= 0`` disables pacing).
    sleep : callable, optional
        ``sleep(seconds)`` function (default :func:`time.sleep`).
    clock : callable, optional
        Monotonic clock returning seconds (default :func:`time.monotonic`).
    """

    def __init__(self, min_seconds: float,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        self.min_seconds = max(0.0, float(min_seconds))
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> float:
        """Sleep as needed before the next spawn; return the seconds slept."""
        slept = 0.0
        if self._last is not None and self.min_seconds > 0.0:
            remaining = self.min_seconds - (self._clock() - self._last)
            if remaining > 0.0:
                self._sleep(remaining)
                slept = remaining
        self._last = self._clock()
        return slept


# A genuine reproduction attempt (read paper, clone/inspect, write code, run it)
# cannot finish in under this many seconds. A "completed" run faster than this
# with no artifacts is an infrastructure fast-fail (e.g. an account-limit
# message the signature list does not know yet) - retryable, never terminal.
# Evidence: 2026-07-15, 8 papers burned in 8-15 s each by the then-unknown
# "You've hit your weekly limit" message (docs/FAILURE_ANALYSIS.md addendum).
FAST_FAIL_SECONDS = 60


def classify_run_outcome(log_text: str, run_status: str, produced: bool,
                         skip_status: str | None = None,
                         elapsed_s: float | None = None,
                         partial_output: bool = False) -> str:
    """Map a finished claude run to a status from the vocabulary above.

    Parameters
    ----------
    log_text : str
        The full text captured from the headless claude run (may be empty).
    run_status : str
        The raw status from :func:`run_claude` ("completed" / "timeout" /
        "claude-not-found").
    produced : bool
        Whether the run met the reproduction artifact contract
        (:func:`meets_reproduction_contract`).
    skip_status : str, optional
        A terminal skip already determined from a skip file / pre-screen; takes
        precedence over everything else.
    elapsed_s : float, optional
        Wall-clock seconds of the claude run. A "completed" run faster than
        ``FAST_FAIL_SECONDS`` with nothing produced is classified as a
        retryable fast-fail (unknown infra/limit message), never as a terminal
        attempt.
    partial_output : bool, optional
        Whether the run left real partial artifacts on disk (own src code or
        reproduced outputs). Distinguishes TIMEOUT_PARTIAL_OUTPUT (continue
        next attempt) from TIMEOUT_NO_OUTPUT.

    Returns
    -------
    str
        One status from the vocabulary. Retryable infra failures are detected
        from the log signatures observed across the corpus so a doomed run does
        not masquerade as a genuine "completed" attempt.
    """
    if skip_status:
        return skip_status
    if run_status == "claude-not-found":
        return STATUS_CLAUDE_NOT_FOUND
    low = (log_text or "").lower()
    # infra signatures (account-level, transient) - retryable
    if "session limit" in low or "hit your session" in low:
        return STATUS_BLOCKED_SESSION
    # Account usage/credit/plan-window limits. "hit your ... limit" catches the
    # whole family (weekly/5-hour/opus/...): on 2026-07-15 the then-unmatched
    # "You've hit your weekly limit - resets Jul 17" burned 8 papers as
    # terminal completed-minimal in 8-15 s each.
    if "usage-credits" in low or ("reached your" in low and "limit" in low) \
            or ("hit your" in low and "limit" in low) \
            or ("fable" in low and "limit" in low):
        return STATUS_BLOCKED_USAGE
    if "529 overloaded" in low or "api error: 529" in low or "overloaded_error" in low:
        return STATUS_BLOCKED_API
    if produced:
        return STATUS_REPRODUCED
    if run_status == "timeout":
        return STATUS_TIMEOUT_PARTIAL if partial_output else STATUS_TIMEOUT_NO_OUTPUT
    if not (log_text or "").strip():
        return STATUS_EMPTY_OUTPUT
    # Fast-fail guard: "completed" in under a minute with nothing produced is
    # not a genuine attempt - it is an unrecognized infra/limit failure. Keep it
    # retryable so a new limit message can never silently burn papers again.
    if elapsed_s is not None and elapsed_s < FAST_FAIL_SECONDS:
        return STATUS_EMPTY_OUTPUT
    # ran to its own completion with real output on the transcript but the
    # artifact contract was not met - an honest minimal attempt (retryable,
    # bounded by max_retries, then settled as EXHAUSTED_RETRIES).
    return STATUS_COMPLETED_MINIMAL


def read_skip_status(paper_dir: Path, skiplist_slugs: set[str] | None = None) -> str | None:
    """Terminal skip determined from files the agent wrote, or the skiplist.

    The reproduction prompt instructs claude to write
    ``reproduced_results/SECURITY_SKIP.txt`` (content "SKIPPED_SECURITY_POLICY")
    or ``reproduced_results/HARDWARE_SKIP.txt`` ("INELIGIBLE_HARDWARE") when it
    detects an adversarial-security corpus or a hardware-infeasible paper. Those
    are first-class terminal non-failures - surface them here so the run record
    reflects them instead of a misleading score-0 "minimal".
    """
    slug = paper_dir.name
    if skiplist_slugs and slug in skiplist_slugs:
        return STATUS_SKIPPED_SECURITY
    rr = paper_dir / "reproduced_results"
    if (rr / "SECURITY_SKIP.txt").exists():
        return STATUS_SKIPPED_SECURITY
    if (rr / "HARDWARE_SKIP.txt").exists():
        return STATUS_INELIGIBLE_HARDWARE
    if (paper_dir / "NOT_REPRODUCIBLE.txt").exists():
        return STATUS_NOT_REPRODUCIBLE
    return None


# Conservative pre-screen keyword gates (only skip on STRONG signals; a normal
# robustness/efficiency paper must NOT be skipped). Security tokens mirror
# DOWNLOAD_SAFETY.md's detection words; a security skip is never a failure.
_SEC_TOKENS = re.compile(
    r"\b(prompt[- ]injection|jailbreak(ing)?|backdoor|malware|red[- ]team|"
    r"exploit\s+(generation|payload)|cve-\d|audit\s+fixture)\b", re.I)
_SURVEY_TOKENS = re.compile(
    r"\b(this\s+(survey|review)|a\s+survey\s+of|we\s+survey|systematic\s+review|"
    r"literature\s+review|is\s+a\s+position\s+paper|survey\s+paper)\b", re.I)
_HW_STRONG = re.compile(
    r"\b(\d{2,4}\s*[x×]\s*(a100|h100|v100|tpu)|"
    r"\b(64|128|256|512|1024)\s+gpus?|"
    r"\b(7|8|13|30|34|65|70|175|405)\s*b\b.*\bfrom\s+scratch|"
    r"pre[- ]?train(ing|ed)?\s+(a\s+)?(large\s+)?(language\s+)?model)\b", re.I)


def pre_screen(title: str, md_text: str, cfg: dict[str, Any] | None = None) -> str | None:
    """Cheap, conservative pre-flight check BEFORE spawning claude.

    Returns a terminal status (skip claude entirely) or None (proceed). Only the
    strongest, low-false-positive signals trigger a skip; everything ambiguous
    defers to the in-prompt SECURITY/HARDWARE gates and the agent's judgement.
    """
    if cfg is not None and not cfg.get("reproduce", {}).get("prescreen", True):
        return None
    hay = f"{title or ''}\n{(md_text or '')[:4000]}"
    if _SEC_TOKENS.search(hay):
        return STATUS_SKIPPED_SECURITY
    head = f"{title or ''}\n{(md_text or '')[:1800]}"
    if _SURVEY_TOKENS.search(head):
        return STATUS_NOT_REPRODUCIBLE
    if _HW_STRONG.search((md_text or "")[:6000]):
        return STATUS_INELIGIBLE_HARDWARE
    return None

# -----------------------------------------------------------------------------
# the reproduction prompt handed to the headless claude CLI
# -----------------------------------------------------------------------------
PROMPT_TEMPLATE = """You are reproducing a published research paper. Work tirelessly and fully autonomously; never ask questions; never stop early. Everything you write must actually RUN.
If the `ml_ultracode` skill is available, invoke it first and follow its workflow (data-first inspection, explicit metrics, baseline, leakage-safe pipelines, multi-seed validation, honest reporting).
SECURITY GATE (binding, C:\\Users\\ADMIN\\Agentic_Projects\\DOWNLOAD_SAFETY.md): STOP IMMEDIATELY and write reproduced_results/SECURITY_SKIP.txt containing exactly "SKIPPED_SECURITY_POLICY" plus the reason, then end, IF this paper is a security / prompt-injection / malware / exploit / red-team / backdoor benchmark or otherwise ships adversarial payloads or audit fixtures (detect from the title/paper.md/repo: "injection", "backdoor", "exploit", "malware", "jailbreak", "red-team", "audit" fixtures, CVE payloads). Do NOT clone, extract, execute, or reconstruct such content — a skip is NOT a failure. For any download you DO make: official source + pinned commit/version only; never load a third-party pickle/joblib/model-binary with full unpickling (weights_only=True or don't load); if antivirus flags anything, STOP and report — never disable it or bypass a quarantine.
HARDWARE GATE (binding, C:\\Users\\ADMIN\\Agentic_Projects\\HARDWARE_ENVELOPE.md): This host is SMALL — 6 GB GPU VRAM (RTX 3060), ~24 GB usable RAM, 4C/8T CPU, keep disk small. BEFORE downloading data/weights, assess this paper's hardware needs from paper.md + the repo. If the faithful result REQUIRES anything beyond the envelope — GPU >6 GB VRAM (e.g. LLMs >=7B even 4-bit, large diffusion/ViT/video), multi-GPU, CUDA-scale training-from-scratch (NAS/pretraining/large RL), >~24 GB RAM working set, or >~20-30 GB of required downloads — and it CANNOT be scaled down while staying faithful to the method, then STOP: write reproduced_results/HARDWARE_SKIP.txt containing exactly "INELIGIBLE_HARDWARE" plus the specific limit exceeded, and end. Do NOT download multi-GB artifacts or start a run you cannot finish — that is what fills the disk and wastes the budget. A faithful scale-down (fewer epochs/params, a small public/synthetic stand-in) is PREFERRED when it stays true to the method; use it instead of skipping whenever possible, and document the deviation. Only skip as INELIGIBLE_HARDWARE when no faithful small version exists (the claim itself is the large-scale result). A hardware skip is NOT a reproduction failure.

PAPER: {title}
AREA: {area}
Working directory (your cwd) contains: paper.pdf (original) and paper.md (extracted text).
Python interpreter to use for EVERYTHING: {python}
   (invoke it explicitly, e.g.  {python} -m pytest -q  , and add any pip installs to requirements.txt in this directory)
{wiki_note}
GOAL - produce a faithful, runnable reproduction of the paper's MAIN result(s) and KEY figure(s) in THIS directory, using this EXACT fixed structure (all folders already exist):
  src/                 your clean, documented, importable reproduction code + a runnable entrypoint  src/reproduce.py
  original_data/       the authors' original data (see DATA below)
  original_results/    the paper's own key figures (see FIGURES below)
  reproduced_results/  figures + metrics YOUR src/ actually produces (reproduced_results/metrics.json + PNGs). These must be generated by running your code, never hand-written. metrics.json is MANDATORY and MUST follow the schema in the METRICS section below (named paper-vs-reproduced numbers + an overall verdict).
  tests/               pytest tests (test_*.py) that validate the implementation at INTERMEDIATE stages AND the FINAL results
  manim/               a manim animation of the paper's core finding (manim/scene.py with a Scene subclass). Render it to manim/<name>.mp4 or .gif if manim is installed; if manim/ffmpeg is unavailable, keep scene.py runnable and note it in summary.md.
  summary.md           concise, technically-correct methodology write-up (the harness converts this to summary.pdf)

CODE - src/:
{github_note}
Keep the method/architecture/algorithm TRUE to the paper. Make src/reproduce.py the single entrypoint that regenerates everything in reproduced_results/.

DATA - original_data/:
{data_note}

FIGURES - original_results/:
{figures_note}

METRICS - reproduced_results/metrics.json (MANDATORY, machine-readable, produced by running src/):
Write a single JSON object with EXACTLY these top-level keys:
  "paper_title": string
  "metrics": a list of objects, one per headline quantity you compare, each:
       {{"name": short metric name (e.g. "test accuracy", "FID", "chi^2/dof", "AUROC"),
         "paper_value": the number/string the paper reports (or null if the paper gives none),
         "reproduced_value": the number YOUR code produced (never hand-typed - read it from your run),
         "unit": string or null,
         "abs_diff": reproduced-minus-paper as a number, or null if not comparable,
         "within_tolerance": true/false/null (your judgement of whether they agree),
         "notes": one short clause on scaling/caveats}}
  "verdict": one of "full" | "partial" | "minimal" | "infeasible"
  "verdict_reason": one sentence justifying the verdict
  "reproduced_on": ISO date string
Include at least one metric whenever the paper reports ANY quantitative result. If the
paper is purely qualitative, still emit metrics with paper_value=null describing what you
measured. Keep names stable and human-readable - a downstream digest reads this file.

HARD CONSTRAINTS:
- CPU ONLY. No GPU, modest RAM. If the paper needs GPU-scale training or proprietary/huge data, SCALE DOWN FAITHFULLY: a small public or synthetic dataset, fewer epochs/params/layers - but keep the core method intact. Document every deviation.
- Prefer libraries installable via pip (numpy, scipy, scikit-learn, pandas, matplotlib; torch CPU wheels only if essential; manim if you can). Pin nothing exotic. Record installs in requirements.txt.
- Actually EXECUTE src/reproduce.py and the tests with {python}; capture the real outputs into reproduced_results/. Do NOT fabricate numbers or figures - if something could not run, say so explicitly in summary.md.
- Keep total work within your time budget. A correct MINIMAL reproduction beats an unfinished ambitious one.
- NETWORK ETIQUETTE (binding - C:\\Users\\ADMIN\\Agentic_Projects\\NETWORK_ETIQUETTE.md): fetch only through sanctioned channels - `git clone` for repos, library downloaders (torchvision/sklearn/huggingface_hub datasets), official APIs, direct data links from the paper. Never scrape HTML that has an API; web search ONLY via the built-in WebSearch tool. At most ONE download attempt per file, >=3 s between requests to the same host. If a host 403s/429s/captchas or a link is dead: STOP trying that host, note it in summary.md, and use a small public/synthetic stand-in. Never retry through a different User-Agent/IP/route - a skipped download is fine, a bot ban is not.

summary.md MUST be substantive (aim for 400-800 words) and contain these headings IN ORDER:
  1. Central claim - the paper's main result(s) in 2-3 sentences, with the specific numbers it claims.
  2. Method - how the method/algorithm/model works, faithfully and concretely (equations in words, key hyperparameters, the training/eval loop). Enough that a reader understands WHAT you implemented.
  3. Reproduction pipeline - how src/ regenerates everything: the exact command (`{python} src/reproduce.py`), the data it uses, each stage data -> pipeline -> outputs, and which files in reproduced_results/ each stage writes.
  4. Results comparison - a MARKDOWN TABLE with columns | Metric | Paper | Reproduced | Diff | Agree? | drawn from reproduced_results/metrics.json, one row per metric. Reference the reproduced figure files and their original_results/ counterparts. State plainly where you matched and where you did not.
  5. Deviations & scaling-down - every change you made for CPU/data/time limits (smaller dataset, fewer epochs/params, synthetic stand-ins) and WHY each is faithful to the method.
  6. Threats to validity - what could make these numbers misleading (tiny sample, seed sensitivity, missing baseline, un-tuned hyperparameters).
  7. Reproducibility verdict - one of full / partial / minimal / infeasible, matching metrics.json's "verdict", plus a one-sentence reason. Do NOT overclaim; an honest "partial" beats a false "full".

Consistency: the numbers in summary.md's table, in reproduced_results/metrics.json, and in your final message MUST agree. Never fabricate - if a stage could not run, say so explicitly here and set the verdict accordingly.

When finished, stop. Your final message must be a 3-5 line summary of what was reproduced, the headline paper-vs-reproduced numbers, and the verdict.
"""


# -----------------------------------------------------------------------------
# small utilities
# -----------------------------------------------------------------------------

def slugify(title: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip().lower()).strip("-")
    return (s[:max_len].strip("-")) or "paper"


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl_slugs(path: Path) -> set[str]:
    done: set[str] = set()
    if path.exists():
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            slug = obj.get("slug")
            if slug:
                done.add(slug)
    return done


def already_processed(ledger_path: Path, progress_path: Path) -> set[str]:
    """Dedup keys: union of the processed ledger and the progress log.

    Legacy helper (presence-based). New callers should use
    :func:`compute_done_slugs`, which dedups on OUTCOME so an infra-killed run
    does not permanently burn a paper.
    """
    return _load_jsonl_slugs(ledger_path) | _load_jsonl_slugs(progress_path)


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            yield json.loads(ln)
        except Exception:  # noqa: BLE001
            continue


def load_requeued(path: Path) -> dict[str, str]:
    """Map slug -> cutoff date for re-queued papers (state/requeued_papers.jsonl).

    A re-queued paper's runs recorded on or before its cutoff date are FORGIVEN
    by :func:`compute_done_slugs`, making it eligible for a fresh attempt while
    every other pruned/terminal paper stays settled. Later cutoff wins if a slug
    is listed twice. Missing file -> empty map (no-op). See the Amit directive
    2026-07-12 re-queue of the 16 code-only papers (docs/FAILURE_ANALYSIS.md).
    """
    out: dict[str, str] = {}
    for obj in _iter_jsonl(path):
        slug = obj.get("slug")
        if not slug:
            continue
        cut = obj.get("cutoff_date") or obj.get("date") or ""
        if slug not in out or cut > out[slug]:
            out[slug] = cut
    return out


def compute_done_slugs(ledger_path: Path, progress_path: Path,
                       pruned_path: Path, max_retries: int = 3,
                       requeued: dict[str, str] | None = None) -> set[str]:
    """Slugs that must NOT be attempted again (outcome-based dedup).

    A paper is DONE when ANY of:
      * a record shows ``produced == True`` (a real reproduction), or
      * a record's status is a terminal non-produced status
        (skips / completed-minimal / exhausted), or
      * it was explicitly pruned (``state/pruned_papers.jsonl``), or
      * it has already been ATTEMPTED ``>= max_retries`` times.

    Retryable infrastructure failures (usage/session limit, API 529, empty
    output, timeout-with-no-output) do NOT, on their own, mark a paper done -
    that is the fix for the burned-paper bug (docs/FAILURE_ANALYSIS.md): a run
    killed by an account usage limit is re-attempted on a later run instead of
    being recorded as a processed score-0 "minimal" forever.

    ``requeued`` (slug -> cutoff date, e.g. from :func:`load_requeued`) FORGIVES
    a paper's history: any run recorded on or before its cutoff date is ignored
    for both done-marking and attempt-counting, and the slug is dropped from the
    pruned set. A fresh outcome recorded AFTER the cutoff (i.e. on resume) counts
    normally, so re-queuing a paper cannot loop it forever. This is how the 16
    code-only papers (Amit directive 2026-07-12) become eligible again while the
    other 190 pruned papers stay settled.
    """
    requeued = requeued or {}

    def _forgiven(slug: str, rec: dict[str, Any]) -> bool:
        # A record that proves a STRICT-contract reproduction is NEVER forgiven:
        # a genuine success stays done even if the slug was re-queued with a
        # later cutoff (2026-07-17 fix - the 07-19 requeue cutoff was forgetting
        # a genuine 07-15 success and would have wastefully re-run it). Records
        # claiming produced=True under the old WEAK contract (no metrics, no
        # reproduced outputs - the stub bug) remain forgivable by design: those
        # are exactly the papers the requeue exists to retry.
        if rec.get("produced") is True and \
                meets_reproduction_contract(rec.get("artifacts") or {}):
            return False
        cut = requeued.get(slug)
        return cut is not None and (rec.get("date") or "") <= cut

    done: set[str] = {s for s in _load_jsonl_slugs(pruned_path) if s not in requeued}
    attempts: dict[str, int] = {}
    for rec in _iter_jsonl(progress_path):
        slug = rec.get("slug")
        if not slug or _forgiven(slug, rec):
            continue
        if rec.get("run_status") is not None:
            attempts[slug] = attempts.get(slug, 0) + 1
        if rec.get("produced") is True:
            done.add(slug)
        st = rec.get("run_status") or rec.get("status")
        if st in TERMINAL_NONPRODUCED_STATUSES:
            done.add(slug)
    for rec in _iter_jsonl(ledger_path):
        slug = rec.get("slug")
        if not slug or _forgiven(slug, rec):
            continue
        if rec.get("produced") is True:
            done.add(slug)
        st = rec.get("run_status") or rec.get("status")
        if st in TERMINAL_NONPRODUCED_STATUSES:
            done.add(slug)
    if max_retries and max_retries > 0:
        for slug, n in attempts.items():
            if n >= max_retries:
                done.add(slug)
    return done


def settle_exhausted(ledger_path: Path, progress_path: Path, pruned_path: Path,
                     max_retries: int,
                     requeued: dict[str, str] | None = None) -> list[str]:
    """Write an honest EXHAUSTED_RETRIES ledger record for retry-capped papers.

    A paper whose attempts reached ``max_retries`` without ever producing a
    reproduction used to drop out of the queue SILENTLY (excluded by the
    attempt cap in :func:`compute_done_slugs` but with no terminal record).
    2026-07-17 fix: such papers are settled explicitly as EXHAUSTED_RETRIES in
    the dedup ledger - a first-class, honest failure that downstream (webapp /
    digest / public aggregates) can count, instead of an ambiguous stub.

    Idempotent: a slug already carrying any terminal/produced record (or already
    pruned) is never re-settled. Returns the slugs settled by this call.
    """
    if not max_retries or max_retries <= 0:
        return []
    requeued = requeued or {}

    def _forgiven(slug: str, rec: dict[str, Any]) -> bool:
        # mirror compute_done_slugs: only a STRICT-contract success is
        # unforgivable; weak-contract produced stubs stay forgivable.
        if rec.get("produced") is True and \
                meets_reproduction_contract(rec.get("artifacts") or {}):
            return False
        cut = requeued.get(slug)
        return cut is not None and (rec.get("date") or "") <= cut

    attempts: dict[str, int] = {}
    last_rec: dict[str, dict[str, Any]] = {}
    settled: set[str] = {s for s in _load_jsonl_slugs(pruned_path) if s not in requeued}
    for rec in list(_iter_jsonl(progress_path)) + list(_iter_jsonl(ledger_path)):
        slug = rec.get("slug")
        if not slug or _forgiven(slug, rec):
            continue
        if rec.get("produced") is True or \
                (rec.get("run_status") or rec.get("status")) in TERMINAL_NONPRODUCED_STATUSES:
            settled.add(slug)
    for rec in _iter_jsonl(progress_path):
        slug = rec.get("slug")
        if not slug or _forgiven(slug, rec):
            continue
        if rec.get("run_status") is not None:
            attempts[slug] = attempts.get(slug, 0) + 1
            last_rec[slug] = rec
    out: list[str] = []
    today = datetime.now().strftime("%Y-%m-%d")
    for slug, n in attempts.items():
        if n < max_retries or slug in settled:
            continue
        prev = last_rec.get(slug, {})
        append_jsonl(ledger_path, {
            "slug": slug, "area": prev.get("area"), "title": prev.get("title"),
            "date": today, "produced": False, "run_status": STATUS_EXHAUSTED,
            "attempts": n,
            "note": f"retry budget exhausted after {n} attempt(s); "
                    f"last status: {prev.get('run_status')}",
        })
        out.append(slug)
    return out


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# -----------------------------------------------------------------------------
# scaffolding
# -----------------------------------------------------------------------------

def scaffold(paper_dir: Path, pdf: str | None, md: str | None) -> None:
    for sub in CANONICAL_SUBDIRS:
        (paper_dir / sub).mkdir(parents=True, exist_ok=True)
    if pdf and Path(pdf).exists() and not (paper_dir / "paper.pdf").exists():
        shutil.copyfile(pdf, paper_dir / "paper.pdf")
    if md and Path(md).exists() and not (paper_dir / "paper.md").exists():
        shutil.copyfile(md, paper_dir / "paper.md")


# -----------------------------------------------------------------------------
# GitHub repo discovery + shallow clone
# -----------------------------------------------------------------------------

_GH_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_GH_BAD_OWNERS = {"sponsors", "features", "about", "topics", "marketplace",
                  "orgs", "settings", "notifications", "explore"}


def _clean_repo(owner: str, repo: str) -> tuple[str, str] | None:
    repo = repo.strip().rstrip(".,);:'\"]}>")
    if repo.endswith(".git"):
        repo = repo[:-4]
    owner = owner.strip()
    if not owner or not repo:
        return None
    if owner.lower() in _GH_BAD_OWNERS:
        return None
    if repo.lower() in {"blob", "tree", "raw"}:
        return None
    return owner, repo


def discover_github_urls(md_text: str, rec: dict[str, Any]) -> list[str]:
    """Scan the paper text + landing links for candidate official-code repos."""
    hay = md_text or ""
    for key in ("landing_url", "pdf_url"):
        v = rec.get(key)
        if v:
            hay += "\n" + str(v)
    seen: list[str] = []
    for m in _GH_RE.finditer(hay):
        cleaned = _clean_repo(m.group(1), m.group(2))
        if not cleaned:
            continue
        url = f"https://github.com/{cleaned[0]}/{cleaned[1]}"
        if url not in seen:
            seen.append(url)
    return seen


def _github_headers() -> dict[str, str]:
    """Authorization header from the environment token (NEVER logged/written).

    The User-Agent is set by the shared polite client (mailto contact +
    "+reproduce" suffix), not here (NETWORK_ETIQUETTE.md).
    """
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_ACCESS_TOKEN")
    h = {"Accept": "application/vnd.github+json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def search_github_by_title(title: str) -> list[str]:
    """Best-effort: query the GitHub search API for the paper's official repo.

    Routed through polite_http: Search API throttled to >= 2.5 s between
    requests, x-ratelimit-* headers honored, Retry-After-aware backoff.
    """
    try:
        import polite_http  # shared polite client (scripts/ is on sys.path)
    except Exception:  # noqa: BLE001
        return []
    words = re.findall(r"[A-Za-z0-9]+", title)
    q = " ".join(words[:8])
    if not q:
        return []
    try:
        r = polite_http.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": 3},
            headers=_github_headers(),
            timeout=30,
            ua_suffix="+reproduce",
        )
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    except polite_http.ProviderBlocked as exc:
        # rule 3: GitHub API blocked -> skip search for this run, keep going
        print(f"[net] {exc} (GitHub search skipped)")
        return []
    except Exception:  # noqa: BLE001
        return []
    return [it["html_url"] for it in items if it.get("html_url")]


def verify_repo(url: str) -> bool:
    """Best-effort existence check via the GitHub API (token used, never logged)."""
    try:
        import polite_http  # shared polite client (scripts/ is on sys.path)
    except Exception:  # noqa: BLE001
        return True  # can't verify -> let the clone decide
    m = _GH_RE.match(url)
    if not m:
        return False
    owner, repo = m.group(1), m.group(2)
    try:
        r = polite_http.get(f"https://api.github.com/repos/{owner}/{repo}",
                            headers=_github_headers(), timeout=30,
                            ua_suffix="+reproduce")
        return r.status_code == 200
    except polite_http.ProviderBlocked:
        return True  # API blocked for this run -> let the git-protocol clone decide
    except Exception:  # noqa: BLE001
        return True


def _rmtree_robust(path: Path) -> None:
    """rmtree that survives Windows read-only files (git object store).

    ``shutil.rmtree(..., ignore_errors=True)`` silently LEAVES read-only files
    behind on Windows - which is how stale ``.git`` dirs were surviving inside
    src/upstream/. Clear the read-only bit and retry on each failure.
    """
    import stat

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            pass
    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def clone_repo(url: str, dest: Path) -> tuple[bool, str]:
    """Shallow-clone url into dest. GIT_TERMINAL_PROMPT=0 avoids credential hangs.

    Public paper repos clone without credentials; we deliberately do NOT inject
    the token into the clone URL so it can never leak into .git/config.
    """
    if dest.exists():
        _rmtree_robust(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=240,
        )
    except FileNotFoundError:
        return (False, "git-not-found")
    except subprocess.TimeoutExpired:
        _rmtree_robust(dest)
        return (False, "clone-timeout")
    if proc.returncode == 0 and dest.exists():
        # PIN THE COMMIT BEFORE DROPPING .git (reproducibility fix, 2026-07-12):
        # we intentionally delete .git (we want the source as a starting point,
        # not a submodule), but a reproduction must record WHICH commit it started
        # from. Capture HEAD first and write it to UPSTREAM_PROVENANCE.json.
        head_sha = ""
        try:
            rp = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=30)
            if rp.returncode == 0:
                head_sha = rp.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            (dest / "UPSTREAM_PROVENANCE.json").write_text(json.dumps({
                "url": url,
                "commit": head_sha or None,
                "cloned_utc": datetime.utcnow().isoformat() + "Z",
                "depth": 1,
                "note": "shallow clone; .git removed after pinning HEAD",
            }, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        _rmtree_robust(dest / ".git")
        return (True, f"cloned@{head_sha[:12]}" if head_sha else "cloned")
    _rmtree_robust(dest)
    return (False, f"clone-failed({proc.returncode})")


def locate_and_clone_repo(paper_dir: Path, md_text: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Find the paper's official repo and clone it into src/upstream/."""
    info: dict[str, Any] = {"candidates": [], "cloned_url": None, "clone_note": None}
    candidates = discover_github_urls(md_text, rec)
    if not candidates:
        candidates = search_github_by_title(rec.get("title", ""))
    info["candidates"] = candidates
    dest = paper_dir / "src" / "upstream"
    for url in candidates:
        if not verify_repo(url):
            continue
        ok, note = clone_repo(url, dest)
        info["clone_note"] = note
        if ok:
            info["cloned_url"] = url
            break
    return info


# -----------------------------------------------------------------------------
# original-figure extraction from the PDF
# -----------------------------------------------------------------------------

def extract_pdf_figures(pdf_path: Path, out_dir: Path, max_images: int = 40,
                        min_pixels: int = 90 * 90) -> int:
    """Extract the paper's ORIGINAL figures from paper.pdf into original_results/.

    Delegates to :mod:`figure_extract`, which captures BOTH vector figures
    (matplotlib/plot-style, found by anchoring on caption lines and rendering the
    page region above each caption) AND large embedded rasters, writing captioned
    ``fig-<NN>-<slug>.png`` files plus ``captions.json``. Falls back to the legacy
    embedded-raster-only extractor if that module cannot be imported. Returns the
    number of images written. Never raises (the pipeline runs unattended).

    Public signature preserved for backwards compatibility.
    """
    if not pdf_path.exists():
        return 0
    # Preferred path: caption-aware vector + raster extraction.
    try:
        import figure_extract  # scripts/ is on sys.path
        info = figure_extract.extract_figures(
            pdf_path, out_dir, dpi=200, max_figures=max_images,
            min_raster_pixels=min_pixels)
        return int(info.get("count", 0))
    except Exception:  # noqa: BLE001
        pass  # fall through to the legacy embedded-raster extractor
    return _extract_embedded_rasters_legacy(pdf_path, out_dir, max_images, min_pixels)


def _extract_embedded_rasters_legacy(pdf_path: Path, out_dir: Path,
                                     max_images: int = 40,
                                     min_pixels: int = 90 * 90) -> int:
    """Legacy fallback: embedded raster figures only (no caption/vector support)."""
    if not pdf_path.exists():
        return 0
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    seen_xrefs: set[int] = set()
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return 0
    try:
        for pno in range(doc.page_count):
            if written >= max_images:
                break
            page = doc[pno]
            for img in page.get_images(full=True):
                if written >= max_images:
                    break
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    ext = doc.extract_image(xref)
                except Exception:  # noqa: BLE001
                    continue
                w, h = ext.get("width", 0), ext.get("height", 0)
                if w * h < min_pixels:
                    continue
                imgext = ext.get("ext", "png")
                fname = out_dir / f"fig_p{pno + 1:02d}_{xref}.{imgext}"
                try:
                    fname.write_bytes(ext["image"])
                    written += 1
                except Exception:  # noqa: BLE001
                    continue
        if written == 0:
            # fallback: render up to 6 pages as page images
            for pno in range(min(doc.page_count, 6)):
                try:
                    pix = doc[pno].get_pixmap(dpi=120)
                    pix.save(str(out_dir / f"page_{pno + 1:02d}.png"))
                    written += 1
                except Exception:  # noqa: BLE001
                    continue
    finally:
        doc.close()
    # leave a small manifest so downstream steps know what was auto-extracted
    try:
        (out_dir / "EXTRACTION_NOTE.md").write_text(
            "# Auto-extracted figures\n\n"
            f"{written} image(s) were auto-extracted from paper.pdf by the harness.\n"
            "These are the paper's ORIGINAL figures (some may be logos/decorations - "
            "prune those). If key figures are missing, extract them from paper.pdf.\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    return written


# -----------------------------------------------------------------------------
# headless claude invocation (mirrors the wiki-ingest runner)
# -----------------------------------------------------------------------------

def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a child process and its descendants (claude spawns node children).

    Uses taskkill on Windows (no process groups); plain kill elsewhere.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()


def run_claude(claude_exe: str, repo_root: Path, paper_dir: Path, prompt: str,
               minutes: int, logf: Path) -> tuple[str, int]:
    """Invoke claude headlessly in paper_dir with a hard timeout. Returns (status, code)."""
    args = [claude_exe, "-p", prompt, "--dangerously-skip-permissions",
            "--add-dir", str(repo_root)]
    with logf.open("w", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(
                args, cwd=str(paper_dir), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, text=True,
            )
        except FileNotFoundError:
            return ("claude-not-found", -1)
        try:
            proc.wait(timeout=minutes * 60)
            return ("completed", proc.returncode)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            return ("timeout", -2)


# -----------------------------------------------------------------------------
# summary.md -> summary.pdf (reportlab)
# -----------------------------------------------------------------------------

def markdown_to_pdf(md_path: Path, pdf_path: Path) -> bool:
    """Render a reproduction summary markdown file to a branded PDF.

    Delegates all styling to the shared :mod:`pdf_style` module so that every
    summary.pdf across the project is visually identical. Area/title/key-figure
    metadata are derived from the paper directory. Returns True on success.
    """
    try:
        # scripts/ is on sys.path (added at import time); import the shared styler
        from pdf_style import build_summary_pdf, first_h1_title, humanize_title, \
            pick_key_figure
    except Exception:  # noqa: BLE001
        return False

    paper_dir = md_path.parent
    meta = {
        "title": first_h1_title(md_path) or humanize_title(paper_dir.name),
        "area": paper_dir.parent.name,        # AI / DS / ML / DL (or folder name)
        "date": datetime.now().strftime("%Y-%m-%d"),
        "key_figure_path": pick_key_figure(paper_dir),
    }
    try:
        return build_summary_pdf(md_path, pdf_path, meta)
    except Exception:  # noqa: BLE001
        return False


def _write_fallback_summary_md(paper_dir: Path, rec: dict[str, Any],
                               art: dict[str, Any], repo_info: dict[str, Any]) -> None:
    """If claude produced no summary.md, synthesise a minimal honest one."""
    md = paper_dir / "summary.md"
    if md.exists():
        return
    repro = paper_dir / "REPRODUCTION.md"
    if repro.exists():
        try:
            shutil.copyfile(repro, md)
            return
        except Exception:  # noqa: BLE001
            pass
    verdict = art.get("verdict")
    verdict_reason = art.get("verdict_reason")
    lines = [
        f"# Reproduction summary: {rec.get('title', paper_dir.name)}",
        "",
        f"**Area:** {rec.get('area_code', '?')}    ",
        f"**Slug:** {paper_dir.name}    ",
        f"**Generated:** {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## Status",
        "",
        "This summary was auto-generated by the harness because the reproduction "
        "agent did not leave a `summary.md`. It reports only what artifacts were "
        "found on disk; the reproduction may be incomplete.",
        "",
        "## Artifacts produced",
        "",
        f"- Source files in `src/`: {art.get('src_files', 0)}",
        f"- Original figures in `original_results/`: {art.get('original_figs', 0)} "
        f"({art.get('original_captions', 0)} captioned)",
        f"- Reproduced outputs in `reproduced_results/`: {art.get('reproduced_files', 0)} "
        f"({art.get('reproduced_imgs', 0)} image(s))",
        f"- Metrics recorded (`metrics.json`): "
        f"{'yes, ' + str(art.get('n_metrics', 0)) + ' metric(s)' if art.get('has_metrics') else 'no'}",
        f"- Tests in `tests/`: {art.get('tests', 0)}",
        f"- Manim scenes in `manim/`: {art.get('manim_files', 0)} "
        f"(rendered: {art.get('manim_render', 0)})",
        "",
        "## Results comparison",
        "",
        ("A machine-readable `reproduced_results/metrics.json` is present; see it for "
         "the named paper-vs-reproduced numbers."
         if art.get("has_metrics")
         else "No `metrics.json` was produced, so no paper-vs-reproduced comparison "
              "is available for this run."),
        "",
        "## Official code",
        "",
        (f"Cloned from {repo_info.get('cloned_url')} into `src/upstream/`."
         if repo_info.get("cloned_url")
         else "No official repo was located/cloned automatically."),
        "",
        "## Reproducibility verdict",
        "",
        (f"**{verdict}** - {verdict_reason}" if verdict and verdict_reason
         else f"**{verdict}** (from metrics.json)." if verdict
         else "infeasible/incomplete - the agent run did not finish the write-up. "
              "See the run log referenced in `state/progress.jsonl`."),
        "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")


def ensure_summary_pdf(paper_dir: Path, rec: dict[str, Any], art: dict[str, Any],
                       repo_info: dict[str, Any]) -> bool:
    """Guarantee summary.pdf exists (rendering summary.md if needed)."""
    pdf = paper_dir / "summary.pdf"
    md = paper_dir / "summary.md"
    if not md.exists():
        _write_fallback_summary_md(paper_dir, rec, art, repo_info)
    # (Re)render if the PDF is missing or older than the markdown.
    if pdf.exists() and md.exists() and pdf.stat().st_mtime >= md.stat().st_mtime:
        return True
    if md.exists():
        return markdown_to_pdf(md, pdf)
    return pdf.exists()


# -----------------------------------------------------------------------------
# artifact assessment
# -----------------------------------------------------------------------------

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".mp4"}


def _count_files(d: Path, exts: set[str] | None = None) -> int:
    if not d.exists():
        return 0
    n = 0
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        if exts is None or p.suffix.lower() in exts:
            n += 1
    return n


def _read_metrics(paper_dir: Path) -> dict[str, Any]:
    """Best-effort read of reproduced_results/metrics.json for the digest.

    Returns {"verdict": str|None, "verdict_reason": str|None, "n_metrics": int}.
    Never raises; tolerates the legacy results.json name and malformed content.
    """
    out: dict[str, Any] = {"verdict": None, "verdict_reason": None, "n_metrics": 0}
    for name in ("metrics.json", "results.json"):
        p = paper_dir / "reproduced_results" / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            v = data.get("verdict")
            if isinstance(v, str):
                out["verdict"] = v.strip()[:20]
            vr = data.get("verdict_reason")
            if isinstance(vr, str):
                out["verdict_reason"] = vr.strip()[:280]
            m = data.get("metrics")
            if isinstance(m, list):
                out["n_metrics"] = len(m)
            elif isinstance(m, dict):
                out["n_metrics"] = len(m)
        break
    return out


def _count_captions(paper_dir: Path) -> int:
    """Number of captioned original figures recorded in captions.json (0 if none)."""
    p = paper_dir / "original_results" / "captions.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return len(data) if isinstance(data, dict) else 0
    except Exception:  # noqa: BLE001
        return 0


def _count_own_src(src_dir: Path) -> int:
    """Count .py files the AGENT wrote under src/, excluding src/upstream/.

    The upstream clone is the authors' code, not the reproduction: counting it
    (the pre-2026-07-17 behaviour) let a bare clone with zero agent work satisfy
    the "has source code" leg of the produced contract.
    """
    if not src_dir.exists():
        return 0
    n = 0
    for p in src_dir.rglob("*.py"):
        if p.is_file() and "upstream" not in p.relative_to(src_dir).parts:
            n += 1
    return n


def assess(paper_dir: Path) -> dict[str, Any]:
    src_files = _count_own_src(paper_dir / "src")
    upstream_files = _count_files(paper_dir / "src" / "upstream", {".py"})
    upstream = (paper_dir / "src" / "upstream").exists()
    original_figs = _count_files(paper_dir / "original_results", _IMG_EXTS)
    original_captions = _count_captions(paper_dir)
    reproduced_files = _count_files(paper_dir / "reproduced_results")
    reproduced_imgs = _count_files(paper_dir / "reproduced_results", _IMG_EXTS)
    has_metrics = (paper_dir / "reproduced_results" / "metrics.json").exists() or \
                  (paper_dir / "reproduced_results" / "results.json").exists()
    metrics_meta = _read_metrics(paper_dir)
    tests = len(list((paper_dir / "tests").glob("test_*.py")))
    manim_files = _count_files(paper_dir / "manim", {".py"})
    manim_render = _count_files(paper_dir / "manim", {".mp4", ".gif"})
    data_files = _count_files(paper_dir / "original_data")
    has_data_source = (paper_dir / "original_data" / "DATA_SOURCE.md").exists()
    has_summary_md = (paper_dir / "summary.md").exists()
    has_summary_pdf = (paper_dir / "summary.pdf").exists()
    has_requirements = (paper_dir / "requirements.txt").exists()
    return {
        "src_files": src_files,          # agent-written .py under src/ (upstream EXCLUDED)
        "upstream_files": upstream_files,
        "has_upstream": upstream,
        "original_figs": original_figs,
        "original_captions": original_captions,
        "reproduced_files": reproduced_files,
        "reproduced_imgs": reproduced_imgs,
        "has_metrics": has_metrics,
        "n_metrics": metrics_meta["n_metrics"],
        "verdict": metrics_meta["verdict"],
        "verdict_reason": metrics_meta["verdict_reason"],
        "tests": tests,
        "manim_files": manim_files,
        "manim_render": manim_render,
        "data_files": data_files,
        "has_data_source": has_data_source,
        "has_summary_md": has_summary_md,
        "has_summary_pdf": has_summary_pdf,
        "has_requirements": has_requirements,
        # back-compat aliases for the older send_report.py digest
        "figures": reproduced_imgs,
        "has_results_json": has_metrics,
    }


def meets_reproduction_contract(art: dict[str, Any]) -> bool:
    """Strict artifact contract for claiming ``produced=True`` (a reproduction).

    2026-07-17 tightening (Amit directive - failure investigation): the old
    contract (any src file INCLUDING the upstream clone + any reproduced_results
    file + a summary.pdf the HARNESS itself auto-generates) let scaffold-level
    stubs be recorded as "reproduced". A run now counts as produced only when
    ALL hold:

      * agent-written source code exists under src/ (upstream clone excluded);
      * a machine-readable reproduced_results/metrics.json exists whose own
        verdict is "full" or "partial" (an honest agent-reported "minimal" or
        "infeasible" is NOT a produced reproduction);
      * real reproduced outputs exist (>=1 reproduced image or >=1 metric);
      * the AGENT wrote summary.md (evaluate this contract on the assessment
        taken BEFORE the harness synthesises its fallback summary).
    """
    if art.get("src_files", 0) <= 0:
        return False
    if not art.get("has_metrics"):
        return False
    verdict = (art.get("verdict") or "").strip().lower()
    if verdict not in ("full", "partial"):
        return False
    if art.get("reproduced_imgs", 0) <= 0 and art.get("n_metrics", 0) <= 0:
        return False
    if not art.get("has_summary_md"):
        return False
    return True


# -----------------------------------------------------------------------------
# per-paper orchestration
# -----------------------------------------------------------------------------

def _wiki_note(cfg: dict[str, Any], repo_root: Path, title: str, md_text: str) -> str:
    """Retrieve the wiki concept pages most relevant to this paper and format them
    as a grounding block for the reproduction prompt. Returns '' when disabled,
    when the wiki is empty, or when nothing clears the relevance floor."""
    k = int(cfg.get("reproduce", {}).get("wiki_context_pages", 5))
    if k <= 0:
        return ""
    try:
        wiki = wiki_index.get_wiki_index(repo_root / "AI_DS_ML_DL" / "wiki")
    except Exception:  # noqa: BLE001
        return ""
    if not wiki.ready:
        return ""
    # title + the paper's opening text (abstract/intro) is a strong topic query
    query = f"{title}. {md_text[:1500]}"
    hits = wiki.retrieve(query, k=k)
    if not hits:
        return ""
    lines = [
        f"- {p.title}: {(p.summary or '').strip()[:240]}  "
        f"(full page: AI_DS_ML_DL/wiki/concepts/{p.slug}.md)"
        for p, _s in hits
    ]
    return (
        "BACKGROUND FROM YOUR KNOWLEDGE BASE (the most relevant concept pages from "
        "the maintained LLM wiki - use them to ground terminology, standard method "
        "choices, baselines, and expected result ranges; they are distilled summaries, "
        "NOT this paper, so defer to paper.md wherever they differ):\n"
        + "\n".join(lines) + "\n"
    )


def _build_prompt(rec: dict[str, Any], python_exe: str, repo_root: Path,
                  repo_info: dict[str, Any], n_figs: int, wiki_note: str = "") -> str:
    if repo_info.get("cloned_url"):
        github_note = (
            f"The paper's OFFICIAL code has ALREADY been cloned into src/upstream/ "
            f"(from {repo_info['cloned_url']}). START FROM IT: read it, then port/adapt "
            f"the key parts into clean modules directly under src/ (do not just call into "
            f"upstream blindly - reproduce the method). Reuse its logic and configs where "
            f"they help; scale down anything GPU/data-heavy.")
    elif repo_info.get("candidates"):
        cands = ", ".join(repo_info["candidates"][:5])
        github_note = (
            f"Candidate official repositories were found but not auto-cloned: {cands}. "
            f"Check which (if any) is the paper's real code; if correct, clone it into "
            f"src/upstream/ and adapt from it. Otherwise implement from the paper text.")
    else:
        github_note = (
            "No official code link was found in the paper text. Briefly look for the "
            "paper's official repo (arXiv abstract page / project page / a web search); "
            "if you find it, clone it into src/upstream/ and adapt. Otherwise implement "
            "the method faithfully from paper.md.")

    figures_note = (
        f"{n_figs} image(s) were auto-extracted from paper.pdf into original_results/ "
        f"as captioned files named fig-<NN>-<slug>.png. BOTH vector plots (matplotlib "
        f"-style, rendered from the page region above each 'Figure N' caption) AND "
        f"embedded rasters are captured. original_results/captions.json maps each file "
        f"to its caption text - use it to identify which figure is which and to pick the "
        f"KEY figure(s) to reproduce. See original_results/EXTRACTION_NOTE.md. Verify the "
        f"crops are the paper's real figures; delete any decorative/duplicate/mis-cropped "
        f"ones. If the paper's official repo has a figures/ or results dir, prefer those. "
        f"If a KEY figure is still missing or poorly cropped, re-extract it from paper.pdf. "
        f"original_results/ must end up holding the paper's real key figures.")

    data_note = (
        "Put the authors' ORIGINAL data in original_data/. Follow data links in paper.pdf "
        "and the official repo's data/ dir. If the dataset is openly available and small, "
        "download it into original_data/. If it is huge/proprietary/gated, DO NOT download "
        "it - instead write original_data/DATA_SOURCE.md with the exact URL(s), size, "
        "license, and access instructions, and use a small public or synthetic stand-in "
        "for the actual reproduction (document this in summary.md).")

    return PROMPT_TEMPLATE.format(
        title=rec.get("title", "Untitled"),
        area=rec.get("area_code", "?"),
        python=python_exe,
        github_note=github_note,
        figures_note=figures_note,
        data_note=data_note,
        wiki_note=wiki_note,
    )


def _run_provenance(cfg: dict[str, Any]) -> dict[str, Any]:
    """Cheap, dependency-free run provenance for the record (reproducibility).

    Records a config hash + coarse host fingerprint so every run report carries
    the environment it ran in (uml_dev doctrine: pin hardware + config). Seeds
    live inside each paper's own src/ (the agent sets them); the upstream commit
    pin is written to src/upstream/UPSTREAM_PROVENANCE.json by clone_repo.
    """
    import hashlib
    import platform
    try:
        cfg_hash = hashlib.sha256(
            json.dumps(cfg.get("reproduce", {}), sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        cfg_hash = None
    return {
        "config_hash": cfg_hash,
        "host": {"os": platform.system(), "cpu_count": os.cpu_count(),
                 "python": platform.python_version()},
    }


def _skiplist_slugs(repo_root: Path) -> set[str]:
    p = repo_root / "state" / "security_skiplist.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return set()
    return {e.get("slug") for e in data.get("entries", []) if isinstance(e, dict) and e.get("slug")}


def _write_skip_marker(paper_dir: Path, status: str, reason: str) -> None:
    """Write the first-class skip marker file the assess/read_skip_status pair reads."""
    (paper_dir / "reproduced_results").mkdir(parents=True, exist_ok=True)
    if status == STATUS_SKIPPED_SECURITY:
        (paper_dir / "reproduced_results" / "SECURITY_SKIP.txt").write_text(
            f"{status}\n{reason}\n", encoding="utf-8")
    elif status == STATUS_INELIGIBLE_HARDWARE:
        (paper_dir / "reproduced_results" / "HARDWARE_SKIP.txt").write_text(
            f"{status}\n{reason}\n", encoding="utf-8")
    elif status == STATUS_NOT_REPRODUCIBLE:
        (paper_dir / "NOT_REPRODUCIBLE.txt").write_text(
            f"{status}\n{reason}\n", encoding="utf-8")


def reproduce_one(cfg: dict[str, Any], rec: dict[str, Any], repo_root: Path,
                  ledger_path: Path,
                  pacer: "SpawnPacer | None" = None) -> dict[str, Any]:
    area = rec["area_code"]
    title = rec["title"]
    slug = slugify(title)
    paper_dir = repo_root / area / slug
    today = datetime.now().strftime("%Y-%m-%d")

    def _finish(status: str, produced: bool, art: dict[str, Any],
                repo_info: dict[str, Any], elapsed: int, code: int,
                logf: Path | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build the run record and append to the dedup ledger ONLY on a terminal
        outcome. Retryable infra failures are recorded in progress (by the caller)
        but NOT burned into the ledger, so the paper is re-attempted later."""
        terminal = bool(produced) or status in TERMINAL_NONPRODUCED_STATUSES
        record = {
            "slug": slug, "area": area, "title": title, "date": today,
            "run_status": status, "exit_code": code, "elapsed_s": elapsed,
            "github_repo": repo_info.get("cloned_url"),
            "github_candidates": repo_info.get("candidates", []),
            "artifacts": art, "produced": produced,
            "terminal": terminal, "retryable": status in RETRYABLE_STATUSES,
            "provenance": _run_provenance(cfg),
            "log": str(logf) if logf else None, "paper_dir": str(paper_dir),
        }
        if extra:
            record.update(extra)
        if terminal:
            append_jsonl(ledger_path, {
                "slug": slug, "area": area, "title": title, "date": today,
                "produced": produced, "run_status": status,
                "github_repo": repo_info.get("cloned_url"),
            })
        return record

    # 1. scaffold canonical structure + drop in paper.pdf / paper.md
    scaffold(paper_dir, rec.get("local_pdf"), rec.get("local_markdown"))

    # read extracted text for repo/data discovery + pre-screen
    md_text = ""
    md_local = paper_dir / "paper.md"
    if md_local.exists():
        md_text = md_local.read_text(encoding="utf-8", errors="replace")

    # 1b. CHEAP PRE-SCREEN (before any clone/figure-extract/claude spawn):
    # on a strong security / survey / hardware-scale signal, skip claude entirely
    # and record a first-class terminal status - never download an adversarial
    # repo or start a doomed hardware run (HARDWARE_ENVELOPE / DOWNLOAD_SAFETY).
    pre = pre_screen(title, md_text, cfg)
    if pre in (STATUS_SKIPPED_SECURITY, STATUS_INELIGIBLE_HARDWARE, STATUS_NOT_REPRODUCIBLE):
        reason = f"pre-screen: {pre} (no claude spawn)"
        _write_skip_marker(paper_dir, pre, reason)
        empty_repo_info: dict[str, Any] = {"candidates": [], "cloned_url": None}
        art = assess(paper_dir)
        ensure_summary_pdf(paper_dir, rec, art, empty_repo_info)
        art = assess(paper_dir)
        print(f"    [pre-screen] {pre} -> skipping claude spawn")
        return _finish(pre, False, art, empty_repo_info, 0, 0, None,
                       {"pre_screen": pre})

    # 2. locate + clone the paper's official repo into src/upstream/
    repo_info = locate_and_clone_repo(paper_dir, md_text, rec)

    # 3. extract the paper's original figures from the PDF
    n_figs = extract_pdf_figures(paper_dir / "paper.pdf", paper_dir / "original_results")

    # 4. drive the reproduction via headless claude
    python_exe = pipeline_paths.python_exe(cfg)
    wiki_note = _wiki_note(cfg, repo_root, title, md_text)
    if wiki_note:
        print(f"    [wiki] injected {wiki_note.count(chr(10)) - 1} related concept page(s) "
              f"into the reproduction prompt")
    prompt = _build_prompt(rec, python_exe, repo_root, repo_info, n_figs, wiki_note)

    # CONTINUATION (segmented budget across retries, 2026-07-17): if a previous
    # attempt already left agent-written code / partial outputs in this paper
    # dir, tell the agent to FINISH that work instead of restarting from
    # scratch - this is how a paper that outgrows one per-paper time slice
    # accumulates a real reproduction across bounded retries.
    pre_art = assess(paper_dir)
    if pre_art["src_files"] > 0 or pre_art["reproduced_files"] > 0:
        prompt = (
            "CONTINUATION OF A PREVIOUS ATTEMPT: this directory already contains "
            "partial work from an earlier bounded run (src/ code and possibly "
            "partial reproduced_results/). Do NOT start over and do NOT rewrite "
            "working code: read what exists, fix/finish it, RUN it, and complete "
            "the missing deliverables (reproduced_results/metrics.json + figures, "
            "tests, summary.md). Prioritise producing final results over "
            "refactoring.\n\n" + prompt
        )
        print(f"    [continue] prior partial work detected "
              f"(src={pre_art['src_files']}, repro_files={pre_art['reproduced_files']}) "
              f"-> continuation prompt")
    minutes = cfg.get("reproduce", {}).get("per_paper_minutes", 35)
    claude_exe = pipeline_paths.claude_exe(cfg)
    logs = repo_root / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = logs / f"reproduce-{slug[:40]}-{stamp}.log"

    # inter-spawn pacing: enforce a minimum gap between claude spawns so a cycle
    # cannot machine-gun the account API (Amit directive 2026-07-12). Applied
    # only here, where a spawn actually happens - pre-screen skips return earlier
    # and never consume pacing budget.
    if pacer is not None:
        slept = pacer.wait()
        if slept > 0:
            print(f"    [pace] waited {slept:.0f}s before spawn "
                  f"(min gap {pacer.min_seconds:.0f}s)")

    started = datetime.now()
    raw_status, code = run_claude(claude_exe, repo_root, paper_dir, prompt, minutes, logf)
    elapsed = int((datetime.now() - started).total_seconds())

    # 5. STRICT produced contract, evaluated BEFORE the harness synthesises its
    # fallback summary (so the harness's own summary.pdf can never satisfy the
    # agent-summary leg). See meets_reproduction_contract for the full rule.
    art = assess(paper_dir)
    produced = meets_reproduction_contract(art)
    ensure_summary_pdf(paper_dir, rec, art, repo_info)
    art = assess(paper_dir)  # re-assess for the record (summary.pdf/md may now exist)

    # 5b. classify the outcome into the status vocabulary. A skip file the agent
    # wrote (or the security skiplist) wins; otherwise infra signatures in the
    # log map to retryable BLOCKED_* / EMPTY_OUTPUT / TIMEOUT_* so an
    # account-limit death or a budget kill does not masquerade as a processed
    # "minimal"/"reproduced" paper.
    try:
        log_text = logf.read_text(encoding="utf-8", errors="replace")[:200_000] if logf.exists() else ""
    except Exception:  # noqa: BLE001
        log_text = ""
    skip_status = read_skip_status(paper_dir, _skiplist_slugs(repo_root))
    partial_output = art["src_files"] > 0 or art["reproduced_files"] > 0
    status = classify_run_outcome(log_text, raw_status, produced, skip_status,
                                  elapsed_s=elapsed, partial_output=partial_output)

    return _finish(status, produced, art, repo_info, elapsed, code, logf)


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def gather_todo(repo_root: Path, harvest_files: list[Path], done: set[str],
                cap: int) -> list[dict[str, Any]]:
    todo: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for hf in harvest_files:
        if not hf.exists():
            continue
        try:
            data = json.loads(hf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] bad harvest file {hf}: {exc}")
            continue
        for rec in data.get("records", []):
            if rec.get("status") != "added":
                continue
            slug = slugify(rec.get("title", ""))
            if slug in done or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            todo.append(rec)
    return todo[:cap]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-paper reproduction harness (invokes claude headlessly).")
    here = Path(__file__).resolve().parent
    ap.add_argument("--config", type=Path, default=here.parent / "config.json")
    ap.add_argument("--harvest", type=str, default=None,
                    help="harvest date YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill", action="store_true",
                    help="reproduce any un-reproduced paper found in state/harvests")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--deadline-minutes", type=int, default=None,
                    help="stop launching new papers after this wall-clock budget "
                         "(rest backfill next run)")
    args = ap.parse_args()

    if not args.config.exists():
        print(f"[fatal] config not found: {args.config}")
        return 2
    cfg = load_config(args.config)  # read-only; never modified here
    repo_root = pipeline_paths.repo_root(cfg)
    state = repo_root / "state"
    progress_path = state / "progress.jsonl"
    ledger_path = state / "processed_ledger.jsonl"
    pruned_path = state / "pruned_papers.jsonl"
    # Outcome-based dedup (not mere presence): retryable infra failures do NOT
    # burn a paper, so a run killed by an account usage limit is re-attempted
    # (bounded by reproduce.max_retries). Pruned + terminal outcomes stay skipped.
    max_retries = int(cfg.get("reproduce", {}).get("max_retries", 3))
    requeued = load_requeued(state / "requeued_papers.jsonl")
    # settle retry-capped papers as honest EXHAUSTED_RETRIES ledger records
    # (first-class failures, never silent drop-outs) before computing dedup.
    exhausted = settle_exhausted(ledger_path, progress_path, pruned_path,
                                 max_retries, requeued)
    if exhausted:
        print(f"[reproduce] settled {len(exhausted)} paper(s) as EXHAUSTED_RETRIES "
              f"(retry budget spent): {', '.join(sorted(exhausted)[:8])}"
              + (" ..." if len(exhausted) > 8 else ""))
    done = compute_done_slugs(ledger_path, progress_path, pruned_path, max_retries, requeued)
    if requeued:
        print(f"[reproduce] {len(requeued)} re-queued paper(s) forgiven and eligible "
              f"again (state/requeued_papers.jsonl)")

    hdir = state / "harvests"
    if args.backfill:
        harvest_files = sorted(hdir.glob("harvest-*.json"))
    else:
        date = args.harvest or datetime.now().strftime("%Y-%m-%d")
        harvest_files = [hdir / f"harvest-{date}.json"]

    cap = args.max if args.max is not None else cfg["reproduce"].get("max_papers_per_day", 30)
    todo = gather_todo(repo_root, harvest_files, done, cap)
    print(f"[reproduce] {len(todo)} paper(s) to attempt "
          f"(cap={cap}, already-processed={len(done)})")

    # quota pacing: minimum gap between spawns + circuit-breaker after K
    # consecutive quota-limit outcomes (Amit directive 2026-07-12).
    pacing = cfg.get("reproduce", {}).get("pacing", {}) or {}
    consecutive_limit_stop = int(pacing.get("consecutive_limit_stop",
                                            DEFAULT_CONSECUTIVE_LIMIT_STOP))
    min_between = float(pacing.get("min_seconds_between_calls",
                                   DEFAULT_MIN_SECONDS_BETWEEN_CALLS))
    pacer = SpawnPacer(min_between)
    quota_streak = 0
    print(f"[reproduce] pacing: >= {min_between:.0f}s between spawns; circuit-breaker "
          + (f"after {consecutive_limit_stop} consecutive quota-limit outcome(s)"
             if consecutive_limit_stop > 0 else "disabled"))

    summary = {"date": datetime.now().strftime("%Y-%m-%d"),
               "attempted": 0, "produced": 0, "records": []}
    started_all = datetime.now()
    for i, rec in enumerate(todo, 1):
        if args.deadline_minutes is not None:
            spent = (datetime.now() - started_all).total_seconds() / 60
            if spent >= args.deadline_minutes:
                print(f"[reproduce] wall-clock budget {args.deadline_minutes}m reached "
                      f"after {i - 1} papers; remaining {len(todo) - (i - 1)} will "
                      f"backfill next run.")
                break
        print(f"\n[{i}/{len(todo)}] {rec['area_code']} :: {rec['title'][:90]}")
        try:
            out = reproduce_one(cfg, rec, repo_root, ledger_path, pacer=pacer)
        except Exception as exc:  # noqa: BLE001
            print(f"    !! error: {exc}")
            # harness-error is RETRYABLE: record it in progress (audit + attempt
            # counter) but do NOT append to the dedup ledger, so a transient
            # harness crash does not permanently burn the paper.
            out = {
                "slug": slugify(rec.get("title", "")), "area": rec.get("area_code"),
                "title": rec.get("title"), "date": datetime.now().strftime("%Y-%m-%d"),
                "run_status": STATUS_HARNESS_ERROR, "exit_code": -3, "elapsed_s": 0,
                "error": str(exc), "produced": False, "terminal": False,
                "retryable": True, "artifacts": {},
            }
        append_jsonl(progress_path, out)
        summary["attempted"] += 1
        summary["produced"] += 1 if out.get("produced") else 0
        summary["records"].append(out)
        a = out.get("artifacts", {})
        print(f"    -> {out.get('run_status')} produced={out.get('produced')} "
              f"src={a.get('src_files', 0)} orig_figs={a.get('original_figs', 0)} "
              f"repro={a.get('reproduced_files', 0)} metrics={a.get('n_metrics', 0)} "
              f"verdict={a.get('verdict') or '-'} tests={a.get('tests', 0)} "
              f"manim={a.get('manim_files', 0)} pdf={a.get('has_summary_pdf', False)} "
              f"{out.get('elapsed_s', 0)}s")

        # circuit-breaker: stop the cycle on K consecutive quota/limit outcomes so
        # a burst cannot exhaust the account budget and burn the remaining papers.
        # Outcome-based dedup keeps the un-attempted papers eligible next cycle.
        quota_streak = update_quota_streak(out.get("run_status"), quota_streak)
        if quota_circuit_tripped(quota_streak, consecutive_limit_stop):
            print(f"[reproduce] quota exhausted - stopping cycle to preserve budget; "
                  f"remaining papers retried next cycle "
                  f"({quota_streak} consecutive quota-limit outcome(s)).")
            break

    sdir = state / "daily"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"reproduce-{summary['date']}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] attempted={summary['attempted']} produced={summary['produced']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
