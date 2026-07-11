#!/usr/bin/env python3
"""
Multi-source academic paper pipeline.

Features:
- Queries multiple public academic APIs.
- Normalizes results into one Paper schema.
- Deduplicates papers by DOI, arXiv ID, Semantic Scholar ID, or title.
- Ranks by citation impact, influence, recency, and PDF availability.
- Downloads open-access PDFs.
- Converts PDFs to Markdown.
- Stores metadata as JSONL.
- Maintains a pickle-based sorted skip list of already analyzed article titles.
- All HTTP is routed through scripts/polite_http.py (NETWORK_ETIQUETTE.md):
  mailto User-Agent, per-host throttling (arXiv >= 3.1 s), Retry-After backoff.

Supported sources:
- Semantic Scholar
- OpenAlex
- arXiv
- Crossref
- CORE, optional API key
- PubMed

Install:
    pip install requests pymupdf4llm

Usage:
    python paper_pipeline.py --query "Deep Learning" --min-year 2021 --top-k 25

Optional:
    export CORE_API_KEY="..."
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# NETWORK_ETIQUETTE.md compliance: ALL external HTTP goes through the shared
# polite client (honest mailto UA, per-host throttle -- arXiv >= 3.1 s per
# their ToS -- Retry-After-aware backoff, ProviderBlocked after persistent
# 429/403/503). Public function signatures/return types below are unchanged.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import polite_http
from polite_http import ProviderBlocked  # noqa: F401  (re-exported for callers)

try:  # optional: only needed for the PDF -> Markdown conversion step
    import pymupdf4llm
except ImportError:  # pragma: no cover - exercised only when the dep is absent
    pymupdf4llm = None  # convert_pdf_to_markdown() degrades gracefully


# -----------------------------
# Unified paper schema
# -----------------------------

@dataclass
class Paper:
    title: str
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    authors: list[str] = field(default_factory=list)

    doi: str | None = None
    arxiv_id: str | None = None
    semantic_scholar_id: str | None = None
    openalex_id: str | None = None
    pubmed_id: str | None = None

    citation_count: int = 0
    influential_citation_count: int = 0

    pdf_url: str | None = None
    landing_url: str | None = None

    sources: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    local_pdf: str | None = None
    local_markdown: str | None = None
    pipeline_score: float | None = None


# -----------------------------
# Utility functions
# -----------------------------

def normalize_title(title: str) -> str:
    """Normalize a title for duplicate detection and skip-list matching."""
    title = title.lower().strip()
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"[^\w\s]", "", title)
    return title


def normalize_doi(doi: str | None) -> str | None:
    """Normalize DOI strings."""
    if not doi:
        return None
    doi = doi.lower().strip()
    doi = doi.replace("https://doi.org/", "")
    doi = doi.replace("http://doi.org/", "")
    return doi


def safe_filename(text: str, max_len: int = 160) -> str:
    """Create a safe filename from a paper title."""
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", text.strip())
    return text[:max_len].strip("_") or "paper"


def request_json(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """GET JSON via the shared polite client (throttled, Retry-After-aware)."""
    response = polite_http.get(url, params=params, headers=headers,
                               timeout=40, ua_suffix="+harvest")
    response.raise_for_status()
    return response.json()


def request_text(url: str, params: dict[str, Any] | None = None) -> str:
    """GET text/XML via the shared polite client (throttled, Retry-After-aware)."""
    response = polite_http.get(url, params=params, timeout=40, ua_suffix="+harvest")
    response.raise_for_status()
    return response.text


# -----------------------------
# Persistent analyzed-title list
# -----------------------------

def load_analyzed_articles(path: Path) -> list[str]:
    """Load sorted pickle list of analyzed normalized titles."""
    if not path.exists():
        save_analyzed_articles([], path)
        return []

    with path.open("rb") as f:
        articles = pickle.load(f)

    if not isinstance(articles, list):
        raise ValueError(f"Invalid pickle format: {path}")

    return sorted(set(articles))


def save_analyzed_articles(articles: list[str], path: Path) -> None:
    """Persist sorted analyzed-title list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    articles = sorted(set(articles))

    with path.open("wb") as f:
        pickle.dump(articles, f)


def mark_article_as_analyzed(title: str, articles: list[str], path: Path) -> list[str]:
    """Add title to analyzed list and save immediately."""
    normalized = normalize_title(title)

    if normalized not in articles:
        articles.append(normalized)
        articles = sorted(set(articles))
        save_analyzed_articles(articles, path)

    return articles


# -----------------------------
# Source adapters
# -----------------------------

def fetch_semantic_scholar(query: str, min_year: int, limit: int) -> list[Paper]:
    """Retrieve papers from Semantic Scholar."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"

    fields = ",".join([
        "paperId",
        "title",
        "abstract",
        "year",
        "venue",
        "citationCount",
        "influentialCitationCount",
        "openAccessPdf",
        "externalIds",
        "authors",
        "publicationDate",
        "url",
    ])

    params = {
        "query": query,
        "year": f"{min_year}-",
        "limit": limit,
        "fields": fields,
        "sort": "citationCount:desc",
    }

    data = request_json(url, params=params).get("data", [])
    papers: list[Paper] = []

    for item in data:
        external = item.get("externalIds") or {}
        pdf = item.get("openAccessPdf") or {}

        papers.append(Paper(
            title=item.get("title") or "Untitled",
            abstract=item.get("abstract"),
            year=item.get("year"),
            venue=item.get("venue"),
            authors=[a.get("name", "") for a in item.get("authors", []) if a.get("name")],
            doi=normalize_doi(external.get("DOI")),
            arxiv_id=external.get("ArXiv"),
            semantic_scholar_id=item.get("paperId"),
            citation_count=item.get("citationCount") or 0,
            influential_citation_count=item.get("influentialCitationCount") or 0,
            pdf_url=pdf.get("url"),
            landing_url=item.get("url"),
            sources=["semantic_scholar"],
            raw={"semantic_scholar": item},
        ))

    return papers

def fetch_openalex(query: str, min_year: int, limit: int) -> list[Paper]:
    """Retrieve papers from OpenAlex."""
    url = "https://api.openalex.org/works"

    params = {
        "search": query,
        "filter": f"from_publication_date:{min_year}-01-01,type:article",
        "per-page": min(limit, 200),
        "sort": "cited_by_count:desc",
    }

    data = request_json(url, params=params).get("results", [])
    papers: list[Paper] = []

    for item in data:
        best_oa = item.get("best_oa_location") or {}
        ids = item.get("ids") or {}

        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") or {}
        venue = source.get("display_name")

        authors = []
        for author_item in item.get("authorships", []) or []:
            author = author_item.get("author") or {}
            if author.get("display_name"):
                authors.append(author["display_name"])

        papers.append(Paper(
            title=item.get("title") or "Untitled",
            abstract=None,
            year=item.get("publication_year"),
            venue=venue,
            authors=authors,
            doi=normalize_doi(ids.get("doi")),
            openalex_id=item.get("id"),
            citation_count=item.get("cited_by_count") or 0,
            pdf_url=best_oa.get("pdf_url"),
            landing_url=best_oa.get("landing_page_url") or ids.get("doi"),
            sources=["openalex"],
            raw={"openalex": item},
        ))

    return papers


def fetch_arxiv(query: str, min_year: int, limit: int) -> list[Paper]:
    """Retrieve papers from arXiv Atom API."""
    url = "https://export.arxiv.org/api/query"

    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    xml_text = request_text(url, params=params)
    root = ET.fromstring(xml_text)

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    papers: list[Paper] = []

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

        papers.append(Paper(
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


def fetch_crossref(query: str, min_year: int, limit: int) -> list[Paper]:
    """Retrieve paper metadata from Crossref. PDF URLs are usually unavailable."""
    url = "https://api.crossref.org/works"

    params = {
        "query": query,
        "filter": f"from-pub-date:{min_year}",
        "rows": min(limit, 100),
        "sort": "is-referenced-by-count",
        "order": "desc",
    }

    items = request_json(url, params=params).get("message", {}).get("items", [])
    papers: list[Paper] = []

    for item in items:
        title_list = item.get("title") or []
        title = title_list[0] if title_list else "Untitled"

        year = None
        date_parts = (item.get("published-print") or item.get("published-online") or {}).get("date-parts")
        if date_parts and date_parts[0]:
            year = date_parts[0][0]

        authors = []
        for a in item.get("author", []):
            name = " ".join(filter(None, [a.get("given"), a.get("family")]))
            if name:
                authors.append(name)

        papers.append(Paper(
            title=title,
            abstract=item.get("abstract"),
            year=year,
            venue=(item.get("container-title") or [None])[0],
            authors=authors,
            doi=normalize_doi(item.get("DOI")),
            citation_count=item.get("is-referenced-by-count") or 0,
            landing_url=item.get("URL"),
            sources=["crossref"],
            raw={"crossref": item},
        ))

    return papers


def fetch_core(query: str, min_year: int, limit: int, api_key: str | None) -> list[Paper]:
    """Retrieve papers from CORE. Requires CORE_API_KEY for reliable access."""
    if not api_key:
        return []

    url = "https://api.core.ac.uk/v3/search/works"
    headers = {"Authorization": f"Bearer {api_key}"}

    payload = {
        "q": f'{query} AND yearPublished>={min_year}',
        "limit": min(limit, 100),
    }

    response = polite_http.post(url, headers=headers, json=payload,
                                timeout=40, ua_suffix="+harvest")
    response.raise_for_status()
    results = response.json().get("results", [])

    papers: list[Paper] = []

    for item in results:
        download_url = item.get("downloadUrl")

        papers.append(Paper(
            title=item.get("title") or "Untitled",
            abstract=item.get("abstract"),
            year=item.get("yearPublished"),
            authors=item.get("authors") or [],
            doi=normalize_doi(item.get("doi")),
            citation_count=item.get("citationCount") or 0,
            pdf_url=download_url,
            landing_url=item.get("publisherUrl") or item.get("sourceFulltextUrls", [None])[0],
            sources=["core"],
            raw={"core": item},
        ))

    return papers


def fetch_pubmed(query: str, min_year: int, limit: int) -> list[Paper]:
    """Retrieve PubMed metadata. Usually no direct PDF unless PMC is available."""
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

    search_params = {
        "db": "pubmed",
        "term": f"{query} AND {min_year}:3000[pdat]",
        "retmode": "json",
        "retmax": limit,
        "sort": "relevance",
    }

    ids = request_json(search_url, params=search_params).get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    summary_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    }

    result = request_json(summary_url, params=summary_params).get("result", {})
    papers: list[Paper] = []

    for pmid in ids:
        item = result.get(pmid) or {}
        title = item.get("title") or "Untitled"

        pubdate = item.get("pubdate", "")
        year_match = re.search(r"\d{4}", pubdate)
        year = int(year_match.group()) if year_match else None

        authors = [
            a.get("name")
            for a in item.get("authors", [])
            if a.get("name")
        ]

        papers.append(Paper(
            title=title,
            year=year,
            venue=item.get("source"),
            authors=authors,
            pubmed_id=pmid,
            citation_count=0,
            landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            sources=["pubmed"],
            raw={"pubmed": item},
        ))

    return papers


# -----------------------------
# Retrieval layer
# -----------------------------

def retrieve_from_sources(
    query: str,
    min_year: int,
    limit_per_source: int,
    sources: list[str],
    core_api_key: str | None,
) -> list[Paper]:
    """Retrieve papers from selected sources."""
    adapters = {
        "semantic_scholar": lambda: fetch_semantic_scholar(query, min_year, limit_per_source),
        "openalex": lambda: fetch_openalex(query, min_year, limit_per_source),
        "arxiv": lambda: fetch_arxiv(query, min_year, limit_per_source),
        "crossref": lambda: fetch_crossref(query, min_year, limit_per_source),
        "core": lambda: fetch_core(query, min_year, limit_per_source, core_api_key),
        "pubmed": lambda: fetch_pubmed(query, min_year, limit_per_source),
    }

    all_papers: list[Paper] = []

    for source in sources:
        if source not in adapters:
            print(f"[warn] Unknown source: {source}")
            continue

        try:
            print(f"[retrieve] {source}")
            papers = adapters[source]()
            print(f"[retrieve] {source}: {len(papers)} papers")
            all_papers.extend(papers)
            # courtesy pause between *sources*; the per-host minimum spacing
            # (arXiv >= 3.1 s etc.) is enforced inside polite_http per request.
            time.sleep(1.0)
        except ProviderBlocked as exc:
            # NETWORK_ETIQUETTE rule 3: blocked means stop that provider for
            # the rest of the run; retrieval continues with the other sources.
            print(f"[blocked] Source stopped: {source}: {exc}")
        except Exception as exc:
            print(f"[error] Source failed: {source}: {exc}")

    return all_papers


def dedupe_key(paper: Paper) -> str:
    """Create stable deduplication key."""
    if paper.doi:
        return f"doi:{normalize_doi(paper.doi)}"
    if paper.arxiv_id:
        return f"arxiv:{paper.arxiv_id.lower()}"
    if paper.semantic_scholar_id:
        return f"s2:{paper.semantic_scholar_id}"
    if paper.openalex_id:
        return f"openalex:{paper.openalex_id}"
    return f"title:{normalize_title(paper.title)}"


def merge_papers(existing: Paper, new: Paper) -> Paper:
    """Merge duplicate records from multiple sources."""
    existing.sources = sorted(set(existing.sources + new.sources))

    existing.abstract = existing.abstract or new.abstract
    existing.year = existing.year or new.year
    existing.venue = existing.venue or new.venue
    existing.pdf_url = existing.pdf_url or new.pdf_url
    existing.landing_url = existing.landing_url or new.landing_url

    existing.doi = existing.doi or new.doi
    existing.arxiv_id = existing.arxiv_id or new.arxiv_id
    existing.semantic_scholar_id = existing.semantic_scholar_id or new.semantic_scholar_id
    existing.openalex_id = existing.openalex_id or new.openalex_id
    existing.pubmed_id = existing.pubmed_id or new.pubmed_id

    existing.citation_count = max(existing.citation_count, new.citation_count)
    existing.influential_citation_count = max(
        existing.influential_citation_count,
        new.influential_citation_count,
    )

    existing.authors = sorted(set(existing.authors + new.authors))
    existing.raw.update(new.raw)

    return existing


def deduplicate_papers(papers: list[Paper]) -> list[Paper]:
    """Deduplicate and merge papers."""
    merged: dict[str, Paper] = {}

    for paper in papers:
        key = dedupe_key(paper)

        if key in merged:
            merged[key] = merge_papers(merged[key], paper)
        else:
            merged[key] = paper

    return list(merged.values())


# -----------------------------
# Ranking, download, conversion
# -----------------------------

def paper_score(paper: Paper) -> float:
    """Score papers by impact, recency, and availability."""
    current_year = datetime.now().year
    year = paper.year or 0

    age = max(1, current_year - year + 1) if year else 10
    citations_per_year = paper.citation_count / age

    recency_bonus = max(0, year - (current_year - 5)) * 20 if year else 0
    pdf_bonus = 300 if paper.pdf_url else 0
    source_bonus = 25 * len(paper.sources)

    return (
        paper.citation_count
        + 5 * paper.influential_citation_count
        + 20 * citations_per_year
        + recency_bonus
        + pdf_bonus
        + source_bonus
    )


def rank_papers(papers: list[Paper]) -> list[Paper]:
    """Rank papers descending."""
    return sorted(papers, key=paper_score, reverse=True)


def download_pdf(paper: Paper, pdf_dir: Path) -> Path | None:
    """Download open-access PDF."""
    if not paper.pdf_url:
        return None

    pdf_path = pdf_dir / f"{safe_filename(paper.title)}.pdf"

    if pdf_path.exists():
        return pdf_path

    try:
        # polite client: mailto UA, >= 3.1 s spacing on arXiv hosts, cached by
        # the skip-if-exists check above. A ProviderBlocked/download failure is
        # caught below -> this one paper is skipped and the run stays alive.
        response = polite_http.get(paper.pdf_url, timeout=80, ua_suffix="+harvest")
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        is_pdf = "pdf" in content_type or response.content.startswith(b"%PDF")

        if not is_pdf:
            print(f"[skip] Not a PDF response: {paper.title}")
            return None

        pdf_path.write_bytes(response.content)
        return pdf_path

    except Exception as exc:
        print(f"[error] Download failed: {paper.title}: {exc}")
        return None


def convert_pdf_to_markdown(pdf_path: Path, markdown_dir: Path) -> Path | None:
    """Convert PDF to Markdown."""
    md_path = markdown_dir / f"{pdf_path.stem}.md"

    if md_path.exists():
        return md_path

    try:
        markdown_text = pymupdf4llm.to_markdown(str(pdf_path))
        md_path.write_text(markdown_text, encoding="utf-8")
        return md_path
    except Exception as exc:
        print(f"[error] Markdown conversion failed: {pdf_path.name}: {exc}")
        return None


def append_jsonl(record: dict[str, Any], path: Path) -> None:
    """Append JSONL metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# -----------------------------
# Main pipeline
# -----------------------------

def run_pipeline(
    query: str,
    min_year: int,
    limit_per_source: int,
    top_k: int,
    out_dir: Path,
    sources: list[str],
    core_api_key: str | None,
    sleep_seconds: float,
) -> None:
    """Run full retrieval/download/conversion pipeline."""
    pdf_dir = out_dir / "pdfs"
    markdown_dir = out_dir / "markdown"
    metadata_path = out_dir / "papers.jsonl"
    analyzed_path = out_dir / "analyzed_articles.pkl"

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    markdown_dir.mkdir(parents=True, exist_ok=True)

    analyzed_articles = load_analyzed_articles(analyzed_path)

    raw_papers = retrieve_from_sources(
        query=query,
        min_year=min_year,
        limit_per_source=limit_per_source,
        sources=sources,
        core_api_key=core_api_key,
    )

    deduped = deduplicate_papers(raw_papers)
    ranked = rank_papers(deduped)

    print(f"\n[summary] Retrieved: {len(raw_papers)}")
    print(f"[summary] Deduplicated: {len(deduped)}")
    print(f"[summary] Already analyzed: {len(analyzed_articles)}")

    processed = 0

    for paper in ranked:
        if processed >= top_k:
            break

        normalized_title = normalize_title(paper.title)

        if normalized_title in analyzed_articles:
            print(f"[skip] Already analyzed: {paper.title}")
            continue

        print(f"\n[{processed + 1}/{top_k}] {paper.title}")
        print(f"[sources] {', '.join(paper.sources)}")
        print(f"[score] {paper_score(paper):.2f}")

        pdf_path = download_pdf(paper, pdf_dir)
        if not pdf_path:
            print("[skip] No downloadable open-access PDF")
            continue

        md_path = convert_pdf_to_markdown(pdf_path, markdown_dir)
        if not md_path:
            print("[skip] Markdown conversion failed")
            continue

        paper.local_pdf = str(pdf_path)
        paper.local_markdown = str(md_path)
        paper.pipeline_score = paper_score(paper)

        append_jsonl(asdict(paper), metadata_path)

        analyzed_articles = mark_article_as_analyzed(
            title=paper.title,
            articles=analyzed_articles,
            path=analyzed_path,
        )

        processed += 1

        print(f"[saved] PDF: {pdf_path}")
        print(f"[saved] Markdown: {md_path}")
        print(f"[updated] Analyzed list size: {len(analyzed_articles)}")

        time.sleep(sleep_seconds)

    print(f"\n[done] Processed {processed} new papers")
    print(f"[metadata] {metadata_path}")
    print(f"[analyzed] {analyzed_path}")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Retrieve influential academic papers from multiple APIs and convert PDFs to Markdown."
    )

    parser.add_argument("--query", required=True)
    parser.add_argument("--min-year", type=int, default=2021)
    parser.add_argument("--limit-per-source", type=int, default=50)
    parser.add_argument("--top-k", type=int, default=25)
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--sleep-seconds", type=float, default=1.0)

    parser.add_argument(
        "--sources",
        nargs="+",
        default=["semantic_scholar", "openalex", "arxiv", "crossref"],
        choices=["semantic_scholar", "openalex", "arxiv", "crossref", "core", "pubmed"],
        help="Sources to query.",
    )

    parser.add_argument(
        "--core-api-key",
        default=os.environ.get("CORE_API_KEY"),
        help="Optional CORE API key. Defaults to CORE_API_KEY env variable.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    run_pipeline(
        query=args.query,
        min_year=args.min_year,
        limit_per_source=args.limit_per_source,
        top_k=args.top_k,
        out_dir=args.out_dir,
        sources=args.sources,
        core_api_key=args.core_api_key,
        sleep_seconds=args.sleep_seconds,
    )