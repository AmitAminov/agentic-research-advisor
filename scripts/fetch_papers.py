#!/usr/bin/env python3
"""
Recency-aware corpus-enlargement harvester.

Built ON TOP of paper_pipeline.py (a verbatim copy of the project's research.py)
so the original API/use-cases are preserved. This module ADDS a recency-aware
"top-N of the last month + top-M of the last few hours across the four areas"
workflow and routes each selected paper into the existing
raw/Research/<AreaFolder>/{pdfs,markdown,papers.jsonl,analyzed_articles.pkl}
layout using the original download/convert/dedupe/mark functions.

NEW behaviour per run
---------------------
Harvest, across the four areas (Data Science, Machine Learning, Deep Learning,
Artificial Intelligence):

  * the TOP 30 papers of the LAST MONTH, and
  * the TOP 3 papers of the LAST 6 HOURS

by submission / publication timestamp -> 33 papers total, ranked by
recency + availability (a downloadable open-access PDF dominates the score,
since it is required for the downstream reproduction step).

Global deduplication
--------------------
A paper is NEVER selected twice. It is skipped if it already appears in

  * ANY area's analyzed-title skip list (analyzed_articles.pkl), OR
  * the persistent processed-papers ledger at
    <repo>/state/processed_ledger.jsonl

The ledger is keyed by normalized-title + arXiv-id + DOI, so a paper cannot be
analyzed twice even across near-simultaneous iterations: each selected paper is
*claimed* in the ledger under a directory lock (an atomic read-modify-append)
before it is downloaded, which closes the race between overlapping runs.

CPU-only note
-------------
This harvester only downloads PDFs and converts them to Markdown; it needs no
GPU or large data, so nothing here is scaled down. The CPU/GPU/data scaling and
its documentation happen downstream in reproduce.py (per-paper REPRODUCTION.md).

Usage
-----
    python fetch_papers.py --config ../config.json
    python fetch_papers.py --config ../config.json --dry-run
    python fetch_papers.py --config ../config.json --top-k 30 --top-recent 3 \
                           --recent-hours 6 --since-days 35

Output
------
    - PDFs + markdown written under <raw_research_dir>/<AreaFolder>/{pdfs,markdown}
    - one JSON harvest record: <repo>/state/harvests/harvest-YYYY-MM-DD.json
      (consumed by reproduce.py and send_report.py)
    - append-only claims in    <repo>/state/processed_ledger.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# make scripts/ importable regardless of the current working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

# import the preserved original API (public functions/signatures unchanged;
# its HTTP is routed through scripts/polite_http.py per NETWORK_ETIQUETTE.md)
import paper_pipeline as pp  # noqa: E402
import pipeline_paths  # noqa: E402
import wiki_index  # noqa: E402  (dependency-free TF-IDF over the LLM knowledge wiki)


# -----------------------------------------------------------------------------
# config + date helpers
# -----------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_pub_datetime(paper: "pp.Paper") -> datetime | None:
    """Best-effort publication timestamp (UTC, naive) from whichever raw payload
    we have. arXiv gives full sub-day granularity (needed for the 6-hour window);
    the other sources are date-only and are treated as UTC midnight of that day.
    """
    raw = paper.raw or {}

    # arXiv: published = ISO 8601 with time, e.g. "2026-06-30T17:30:00Z"
    ax = raw.get("arxiv") or {}
    if ax.get("published"):
        s = str(ax["published"])
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=None)
            except ValueError:
                continue
        try:  # last resort: date part only
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            pass

    # Semantic Scholar: publicationDate = "YYYY-MM-DD"
    s2 = raw.get("semantic_scholar") or {}
    if s2.get("publicationDate"):
        try:
            return datetime.strptime(s2["publicationDate"], "%Y-%m-%d")
        except ValueError:
            pass

    # OpenAlex: publication_date = "YYYY-MM-DD"
    oa = raw.get("openalex") or {}
    if oa.get("publication_date"):
        try:
            return datetime.strptime(oa["publication_date"], "%Y-%m-%d")
        except ValueError:
            pass

    # Crossref: published-print / published-online date-parts
    cr = raw.get("crossref") or {}
    dp = (cr.get("published-print") or cr.get("published-online") or {}).get("date-parts")
    if dp and dp[0]:
        parts = list(dp[0]) + [1, 1]
        try:
            return datetime(parts[0], parts[1], parts[2])
        except (ValueError, TypeError):
            pass

    # Fall back to year only (treated as mid-year, low recency)
    if paper.year:
        try:
            return datetime(paper.year, 6, 30)
        except ValueError:
            return None
    return None


# kept for backwards compatibility with any external caller of the old helper
def parse_pub_date(paper: "pp.Paper") -> datetime | None:
    return parse_pub_datetime(paper)


def rank_score(paper: "pp.Paper", pub_dt: datetime | None, now: datetime,
               window_seconds: float, wiki_rel: float = 0.0,
               wiki_weight: float = 0.0) -> float:
    """Recency + availability + knowledge-base relevance score.

    A downloadable PDF dominates (it is required for the downstream reproduction
    step). Recency is a 0..1 fraction of the window still remaining, so fresher
    papers rank higher within the same window. ``wiki_rel`` (0..1, relevance to
    the user's LLM knowledge wiki) is added with weight ``wiki_weight`` so that,
    among comparably-fresh downloadable papers, the ones closest to the user's
    actual research interests are selected. Impact/multi-source presence break
    remaining ties.
    """
    pdf_bonus = 1000.0 if paper.pdf_url else 0.0
    if pub_dt is not None and window_seconds > 0:
        age = max(0.0, (now - pub_dt).total_seconds())
        recency = max(0.0, (window_seconds - age) / window_seconds)  # 1.0 == just now
    else:
        recency = 0.0
    impact = paper.citation_count + 5 * paper.influential_citation_count
    source_bonus = 25 * len(paper.sources)
    return pdf_bonus + 500.0 * recency + wiki_weight * wiki_rel + impact + source_bonus


# -----------------------------------------------------------------------------
# global dedup: analyzed skip-lists + persistent processed ledger
# -----------------------------------------------------------------------------

def paper_keys(paper: "pp.Paper") -> set[str]:
    """Ledger dedup keys for a paper: normalized title + arXiv id + DOI.
    A paper is considered already-processed if ANY of its keys is known.
    """
    keys: set[str] = set()
    nt = pp.normalize_title(paper.title or "")
    if nt:
        keys.add(f"title:{nt}")
    if paper.arxiv_id:
        keys.add(f"arxiv:{str(paper.arxiv_id).strip().lower()}")
    doi = pp.normalize_doi(paper.doi)
    if doi:
        keys.add(f"doi:{doi}")
    return keys


def already_known_titles(raw_research_dir: Path, area_folders: list[str]) -> set[str]:
    """Union of every area's analyzed-title skip list, so we never re-add a paper."""
    known: set[str] = set()
    for folder in area_folders:
        analyzed = raw_research_dir / folder / "analyzed_articles.pkl"
        if analyzed.exists():
            try:
                known.update(pp.load_analyzed_articles(analyzed))
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] could not read {analyzed}: {exc}")
    return known


class DirLock:
    """Cross-process advisory lock using an atomic mkdir. Windows-safe (no fcntl).
    Steals the lock if it looks stale (older than ``stale_seconds``)."""

    def __init__(self, target: Path, timeout: float = 30.0, poll: float = 0.15,
                 stale_seconds: float = 120.0) -> None:
        self.lockdir = Path(str(target) + ".lock")
        self.timeout = timeout
        self.poll = poll
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "DirLock":
        deadline = time.time() + self.timeout
        while True:
            try:
                self.lockdir.mkdir()
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.lockdir.stat().st_mtime
                    if age > self.stale_seconds:
                        self.lockdir.rmdir()
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    # Availability over strictness: proceed without the lock
                    # rather than blocking the pipeline. acquired stays False,
                    # so exit will NOT release the current holder's lock.
                    print(f"[warn] ledger lock timeout; proceeding unlocked: {self.lockdir}")
                    return self
                time.sleep(self.poll)

    def __exit__(self, *exc: Any) -> None:
        if not self.acquired:
            return  # never release a lock owned by another process
        self.acquired = False
        try:
            self.lockdir.rmdir()
        except OSError:
            pass


class Ledger:
    """Persistent processed-papers ledger at state/processed_ledger.jsonl.

    Each line: {"keys": [...], "title", "area_code", "arxiv_id", "doi",
                "status", "claimed_at", ...}. A paper is skipped if any of its
    keys already appears. ``claim`` atomically re-reads and appends under a
    directory lock so overlapping runs cannot both claim the same paper.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_keys(self) -> set[str]:
        known: set[str] = set()
        if not self.path.exists():
            return known
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for k in rec.get("keys", []):
                known.add(k)
        return known

    def _append(self, rec: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def claim(self, paper: "pp.Paper", area_code: str, bucket: str) -> bool:
        """Atomically claim a paper. Returns False if a concurrent run already
        claimed it (any key present) between our initial load and now."""
        keys = sorted(paper_keys(paper))
        with DirLock(self.path):
            live = self.load_keys()
            if any(k in live for k in keys):
                return False
            self._append({
                "keys": keys,
                "title": paper.title,
                "area_code": area_code,
                "bucket": bucket,
                "arxiv_id": paper.arxiv_id,
                "doi": pp.normalize_doi(paper.doi),
                "status": "claimed",
                "claimed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
        return True

    def record_status(self, paper: "pp.Paper", area_code: str, status: str,
                      **extra: Any) -> None:
        """Append a terminal status line for an already-claimed paper (audit
        trail). Dedup keys off any line, so this never un-claims a paper."""
        rec = {
            "keys": sorted(paper_keys(paper)),
            "title": paper.title,
            "area_code": area_code,
            "status": status,
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        rec.update(extra)
        with DirLock(self.path):
            self._append(rec)


# -----------------------------------------------------------------------------
# harvest
# -----------------------------------------------------------------------------

def _is_known(paper: "pp.Paper", known_titles: set[str], known_keys: set[str]) -> bool:
    if pp.normalize_title(paper.title or "") in known_titles:
        return True
    return any(k in known_keys for k in paper_keys(paper))


def fetch_arxiv_categorized(categories: list[str], min_year: int, limit: int) -> list["pp.Paper"]:
    """arXiv fetch restricted to a set of submission categories (e.g. cs.LG,
    stat.ML) instead of a free-text term.

    pp.fetch_arxiv wraps the query in ``all:{query}`` and sorts by submittedDate,
    so a broad area name like "Data Science" returns the newest submissions that
    merely *mention* the words -- which floods the corpus with off-topic
    physics/astro papers. Filtering by ``cat:`` keeps each area on-topic while
    still returning the freshest papers (sortBy=submittedDate). Reuses
    pp.request_text / pp.Paper so paper_pipeline.py stays untouched.
    """
    cat_expr = " OR ".join(f"cat:{c}" for c in categories)
    params = {
        "search_query": f"({cat_expr})",
        "start": 0,
        "max_results": limit,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    xml_text = pp.request_text("https://export.arxiv.org/api/query", params=params)
    root = ET.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    papers: list[pp.Paper] = []
    for entry in root.findall("atom:entry", ns):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns)).split())
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns)).split())
        published = entry.findtext("atom:published", default="", namespaces=ns)
        year = int(published[:4]) if published[:4].isdigit() else None
        if year and year < min_year:
            continue

        entry_id = entry.findtext("atom:id", default="", namespaces=ns)
        arxiv_id = entry_id.rstrip("/").split("/")[-1]

        pdf_url = None
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href")
                break

        authors = [
            a.findtext("atom:name", default="", namespaces=ns)
            for a in entry.findall("atom:author", ns)
        ]

        papers.append(pp.Paper(
            title=title or "Untitled",
            abstract=summary,
            year=year,
            venue="arXiv",
            authors=[a for a in authors if a],
            arxiv_id=arxiv_id,
            citation_count=0,
            pdf_url=pdf_url,
            landing_url=entry_id,
            sources=["arxiv"],
            raw={"arxiv": {"id": entry_id, "published": published}},
        ))

    return papers


def retrieve_area(code: str, query: str, arxiv_categories: list[str], min_year: int,
                  limit_per_source: int, sources: list[str],
                  core_key: str | None) -> list["pp.Paper"]:
    """Retrieve one area's papers across the configured sources.

    Keyword sources (Semantic Scholar / OpenAlex / Crossref / CORE / PubMed) use
    the free-text ``query``; arXiv uses category filtering when
    ``arxiv_categories`` is set (falling back to pp.fetch_arxiv otherwise). This
    replaces the single shared-query pp.retrieve_from_sources call so arXiv can
    be scoped by ``cat:`` while the other sources keep their relevance ranking.
    """
    papers: list[pp.Paper] = []
    for source in sources:
        try:
            if source == "arxiv":
                if arxiv_categories:
                    found = fetch_arxiv_categorized(arxiv_categories, min_year, limit_per_source)
                else:
                    found = pp.fetch_arxiv(query, min_year, limit_per_source)
            elif source == "semantic_scholar":
                found = pp.fetch_semantic_scholar(query, min_year, limit_per_source)
            elif source == "openalex":
                found = pp.fetch_openalex(query, min_year, limit_per_source)
            elif source == "crossref":
                found = pp.fetch_crossref(query, min_year, limit_per_source)
            elif source == "core":
                found = pp.fetch_core(query, min_year, limit_per_source, core_key)
            elif source == "pubmed":
                found = pp.fetch_pubmed(query, min_year, limit_per_source)
            else:
                print(f"[warn] unknown source: {source}")
                continue
            print(f"[retrieve] {code}/{source}: {len(found)} papers")
            papers.extend(found)
            # courtesy pause between *sources*; per-host minimum spacing
            # (arXiv >= 3.1 s per ToS, S2 >= 1.1 s, default >= 2.0 s) is
            # enforced inside polite_http for every single request.
            time.sleep(1.0)
        except pp.ProviderBlocked as exc:
            # NETWORK_ETIQUETTE rule 3: blocked means stop that provider for
            # the rest of the run; the harvest continues on the other sources.
            print(f"[blocked] {code}/{source}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[error] {code}/{source} failed: {exc}")
    return papers


def harvest(config: dict[str, Any], top_month: int, top_recent: int,
            recent_hours: float, since_days: int, dry_run: bool) -> dict[str, Any]:
    h = config["harvest"]
    areas: dict[str, str] = h["areas"]                 # code -> free-text query
    arxiv_cats: dict[str, list[str]] = h.get("arxiv_categories", {})  # code -> [cat, ...]
    folder_map: dict[str, str] = h["area_folder_map"]  # code -> raw subfolder
    # relative raw_research_dir values are resolved against the repo root
    raw_research_dir = pipeline_paths.resolve_under_repo(h["raw_research_dir"], config)
    sources = h.get("sources", ["semantic_scholar", "openalex", "arxiv", "crossref"])
    core_key = h.get("core_api_key") or None
    limit_per_source = h.get("limit_per_source", 50)
    min_year = (datetime.utcnow() - timedelta(days=since_days)).year

    repo_root = pipeline_paths.repo_root(config)
    ledger = Ledger(repo_root / "state" / "processed_ledger.jsonl")

    # Corpus PDFs are offloaded to the cloud (gdrive); pull just the small dedup
    # state (analyzed_articles.pkl) locally so we still skip already-known titles
    # without re-downloading the whole 10+ GB corpus. Best-effort (no-op if rclone
    # or the remote is unavailable).
    pipeline_paths.ensure_corpus_dedup_state(raw_research_dir, config)
    known_titles = already_known_titles(raw_research_dir, list(folder_map.values()))
    known_keys = ledger.load_keys()
    print(f"[harvest] {len(known_titles)} analyzed titles + {len(known_keys)} ledger keys "
          f"already in corpus (will be skipped)")

    # 1) retrieve per-area, tag area, pool
    pooled: list[pp.Paper] = []
    area_of: dict[str, str] = {}   # dedupe_key -> area code
    for code, query in areas.items():
        cats = arxiv_cats.get(code, [])
        cat_note = f" arxiv-cats={cats}" if cats else ""
        print(f"\n[area {code}] query='{query}'{cat_note} since {min_year}")
        try:
            papers = retrieve_area(
                code=code,
                query=query,
                arxiv_categories=cats,
                min_year=min_year,
                limit_per_source=limit_per_source,
                sources=sources,
                core_key=core_key,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[error] area {code} retrieval failed: {exc}")
            continue
        for p in papers:
            k = pp.dedupe_key(p)
            area_of.setdefault(k, code)  # first area that saw it wins
        pooled.extend(papers)

    # 2) global dedupe/merge across areas + sources
    deduped = pp.deduplicate_papers(pooled)
    print(f"\n[harvest] pooled={len(pooled)} deduped={len(deduped)}")

    # 3) build candidates: not already known, parseable date, within last month
    now = datetime.utcnow()
    month_window = timedelta(days=since_days)
    recent_window = timedelta(hours=recent_hours)
    future_slack = timedelta(days=1)  # tolerate small tz/clock skew on fresh submissions

    candidates: list[tuple[pp.Paper, datetime]] = []
    for p in deduped:
        if _is_known(p, known_titles, known_keys):
            continue
        dt = parse_pub_datetime(p)
        if dt is None:
            continue
        if dt > now + future_slack:
            continue
        if now - dt > month_window:
            continue
        candidates.append((p, dt))

    # 3.5) knowledge-base relevance: score each candidate against the LLM wiki so
    # selection favors papers close to the user's actual research interests. Weight
    # 0 (or an empty/missing wiki) makes this a no-op, preserving prior behavior.
    wiki_weight = float(h.get("wiki_relevance_weight", 0.0))
    rel_of: dict[str, float] = {}
    if wiki_weight > 0 and candidates:
        wiki = wiki_index.get_wiki_index(repo_root / "AI_DS_ML_DL" / "wiki")
        if wiki.ready:
            for p, _dt in candidates:
                text = f"{p.title or ''}. {p.abstract or ''}"
                rel_of[pp.dedupe_key(p)] = wiki.relevance(text)
            top = sorted(rel_of.values(), reverse=True)[:5]
            print(f"[harvest] wiki relevance scored {len(rel_of)} candidates against "
                  f"{len(wiki.pages)} concept pages (weight={wiki_weight:g}; "
                  f"top rel={', '.join(f'{r:.3f}' for r in top)})")
        else:
            print("[harvest] wiki index empty/missing; relevance weighting skipped")

    def _rel(p: "pp.Paper") -> float:
        return rel_of.get(pp.dedupe_key(p), 0.0)

    # 3a) TOP-M of the last N hours
    recent = [(p, dt) for (p, dt) in candidates if (now - dt) <= recent_window]
    recent.sort(key=lambda t: rank_score(t[0], t[1], now, recent_window.total_seconds(),
                                         _rel(t[0]), wiki_weight),
                reverse=True)
    recent_sel = recent[:top_recent]
    recent_ids = {pp.dedupe_key(p) for p, _ in recent_sel}

    # 3b) TOP-N of the last month, excluding the recent picks (no double counting)
    month = [(p, dt) for (p, dt) in candidates if pp.dedupe_key(p) not in recent_ids]
    month.sort(key=lambda t: rank_score(t[0], t[1], now, month_window.total_seconds(),
                                        _rel(t[0]), wiki_weight),
               reverse=True)
    month_sel = month[:top_month]

    selected: list[tuple[str, pp.Paper, datetime]] = (
        [("6h", p, dt) for p, dt in recent_sel]
        + [("month", p, dt) for p, dt in month_sel]
    )
    print(f"[harvest] candidates={len(candidates)} "
          f"selected: 6h={len(recent_sel)} month={len(month_sel)} total={len(selected)}")

    # 4) download + convert into the area folder, claim ledger, record harvest
    today = datetime.now().strftime("%Y-%m-%d")
    records: list[dict[str, Any]] = []
    for i, (bucket, p, dt) in enumerate(selected, 1):
        code = area_of.get(pp.dedupe_key(p), "AI")
        folder = folder_map.get(code, "AI")
        out_dir = raw_research_dir / folder
        pdf_dir = out_dir / "pdfs"
        md_dir = out_dir / "markdown"
        rec: dict[str, Any] = {
            "area_code": code, "area_folder": folder, "bucket": bucket,
            "title": p.title, "date": dt.strftime("%Y-%m-%d"),
            "published_utc": dt.isoformat(timespec="seconds") + "Z",
            "score": round(rank_score(p, dt, now,
                    (recent_window if bucket == "6h" else month_window).total_seconds(),
                    _rel(p), wiki_weight), 1),
            "wiki_relevance": round(_rel(p), 4),
            "doi": p.doi, "arxiv_id": p.arxiv_id, "pdf_url": p.pdf_url,
            "landing_url": p.landing_url, "sources": p.sources,
            "citation_count": p.citation_count,
            "local_pdf": None, "local_markdown": None, "status": "pending",
        }
        print(f"\n[{i}/{len(selected)}] ({code}/{bucket}) {p.title[:88]}  "
              f"score={rec['score']} rel={rec['wiki_relevance']:.3f}")

        if dry_run:
            rec["status"] = "dry-run"
            records.append(rec)
            continue

        # claim FIRST to close the race with near-simultaneous iterations
        if not ledger.claim(p, code, bucket):
            print("    [skip] claimed by a concurrent run")
            rec["status"] = "dup-skip"
            records.append(rec)
            continue

        pdf_dir.mkdir(parents=True, exist_ok=True)
        md_dir.mkdir(parents=True, exist_ok=True)

        pdf_path = pp.download_pdf(p, pdf_dir)
        if not pdf_path:
            rec["status"] = "no-pdf"
            records.append(rec)
            ledger.record_status(p, code, "no-pdf")
            continue

        md_path = pp.convert_pdf_to_markdown(pdf_path, md_dir)
        if not md_path:
            rec["status"] = "md-failed"
            rec["local_pdf"] = str(pdf_path)
            records.append(rec)
            ledger.record_status(p, code, "md-failed", local_pdf=str(pdf_path))
            continue

        p.local_pdf = str(pdf_path)
        p.local_markdown = str(md_path)
        p.pipeline_score = rec["score"]
        pp.append_jsonl(pp.asdict(p), out_dir / "papers.jsonl")
        analyzed_path = out_dir / "analyzed_articles.pkl"
        pp.mark_article_as_analyzed(
            p.title, pp.load_analyzed_articles(analyzed_path), analyzed_path)

        rec.update(status="added", local_pdf=str(pdf_path), local_markdown=str(md_path))
        records.append(rec)
        ledger.record_status(p, code, "added",
                             local_pdf=str(pdf_path), local_markdown=str(md_path))
        # keep the in-run known set current (belt-and-braces vs. same-run dupes)
        known_titles.add(pp.normalize_title(p.title or ""))
        known_keys.update(paper_keys(p))

    # 5) persist harvest record (schema consumed by reproduce.py / send_report.py)
    hdir = repo_root / "state" / "harvests"
    hdir.mkdir(parents=True, exist_ok=True)
    summary = {
        "date": today,
        "selected": len(selected),
        "selected_6h": len(recent_sel),
        "selected_month": len(month_sel),
        "added": sum(1 for r in records if r["status"] == "added"),
        "no_pdf": sum(1 for r in records if r["status"] == "no-pdf"),
        "dup_skip": sum(1 for r in records if r["status"] == "dup-skip"),
        "records": records,
    }
    out = hdir / f"harvest-{today}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] added={summary['added']} no_pdf={summary['no_pdf']} "
          f"dup_skip={summary['dup_skip']} -> {out}")
    return summary


def main() -> int:
    # Windows consoles default to cp1252, which raises UnicodeEncodeError when a
    # paper title contains characters like U+2010 (non-breaking hyphen) or curly
    # quotes. Reconfigure stdout/stderr to UTF-8 and replace anything still
    # unencodable so a single exotic title can never abort the whole harvest.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description="Recency-aware paper harvester: top-N of last month + top-M of "
                    "last few hours across four areas (extends paper_pipeline).")
    here = Path(__file__).resolve().parent
    ap.add_argument("--config", type=Path, default=here.parent / "config.json")
    ap.add_argument("--top-k", type=int, default=None,
                    help="top papers of the last month (default: config top_month/top_k_per_day=30)")
    ap.add_argument("--top-recent", type=int, default=None,
                    help="top papers of the last few hours (default: config top_recent=3)")
    ap.add_argument("--recent-hours", type=float, default=None,
                    help="the 'last few hours' window in hours (default: config recent_hours=6)")
    ap.add_argument("--since-days", type=int, default=None,
                    help="the 'last month' window in days (default: config since_days=35)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.config.exists():
        print(f"[fatal] config not found: {args.config}  (copy config.example.json -> config.json)")
        return 2

    cfg = load_config(args.config)
    h = cfg.get("harvest", {})
    top_month = args.top_k if args.top_k is not None else h.get("top_month", h.get("top_k_per_day", 30))
    top_recent = args.top_recent if args.top_recent is not None else h.get("top_recent", 3)
    recent_hours = args.recent_hours if args.recent_hours is not None else h.get("recent_hours", 6)
    since = args.since_days if args.since_days is not None else h.get("since_days", 35)

    harvest(cfg, top_month=top_month, top_recent=top_recent,
            recent_hours=recent_hours, since_days=since, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
