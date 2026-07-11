"""Ranking / recency scoring (fetch_papers + paper_pipeline).

Selection order decides which papers get the (expensive) reproduction budget,
so the scoring invariants are pinned here: PDF availability dominates,
recency is a bounded 0..1 fraction of the window, impact only breaks ties.
"""
from datetime import datetime, timedelta

import paper_pipeline as pp
from fetch_papers import parse_pub_datetime, rank_score

NOW = datetime(2026, 7, 1, 12, 0, 0)
DAY = 86400.0
MONTH = 35 * DAY


def _p(**kw):
    kw.setdefault("title", "T")
    return pp.Paper(**kw)


# ---------------------------------------------------------------------------
# rank_score
# ---------------------------------------------------------------------------

def test_pdf_availability_dominates_recency_and_impact():
    old_with_pdf = rank_score(_p(pdf_url="https://x/p.pdf"),
                              NOW - timedelta(days=34), NOW, MONTH)
    fresh_no_pdf = rank_score(_p(citation_count=100),
                              NOW, NOW, MONTH)
    assert old_with_pdf > fresh_no_pdf


def test_recency_is_fraction_of_window_remaining():
    just_now = rank_score(_p(), NOW, NOW, MONTH)
    half_way = rank_score(_p(), NOW - timedelta(days=17.5), NOW, MONTH)
    expired = rank_score(_p(), NOW - timedelta(days=40), NOW, MONTH)
    assert just_now == 500.0            # full recency bonus, nothing else
    assert abs(half_way - 250.0) < 1.0  # linear decay
    assert expired == 0.0               # clamped at zero, never negative


def test_unknown_date_gets_no_recency_bonus():
    assert rank_score(_p(), None, NOW, MONTH) == 0.0


def test_impact_and_sources_break_ties():
    dt = NOW - timedelta(days=10)
    base = rank_score(_p(), dt, NOW, MONTH)
    cited = rank_score(_p(citation_count=10, influential_citation_count=2),
                       dt, NOW, MONTH)
    multi = rank_score(_p(sources=["arxiv", "openalex"]), dt, NOW, MONTH)
    assert cited == base + 10 + 5 * 2
    assert multi == base + 25 * 2


def test_fresher_paper_outranks_older_within_same_window():
    older = rank_score(_p(pdf_url="u"), NOW - timedelta(hours=5), NOW, 6 * 3600)
    fresher = rank_score(_p(pdf_url="u"), NOW - timedelta(hours=1), NOW, 6 * 3600)
    assert fresher > older


# ---------------------------------------------------------------------------
# parse_pub_datetime — per-source timestamp extraction
# ---------------------------------------------------------------------------

def test_arxiv_full_timestamp_parses_with_subday_granularity():
    p = _p(raw={"arxiv": {"published": "2026-06-30T17:30:00Z"}})
    assert parse_pub_datetime(p) == datetime(2026, 6, 30, 17, 30, 0)


def test_arxiv_date_only_fallback():
    p = _p(raw={"arxiv": {"published": "2026-06-30 (approx)"}})
    assert parse_pub_datetime(p) == datetime(2026, 6, 30)


def test_semantic_scholar_and_openalex_dates():
    s2 = _p(raw={"semantic_scholar": {"publicationDate": "2026-06-15"}})
    oa = _p(raw={"openalex": {"publication_date": "2026-06-20"}})
    assert parse_pub_datetime(s2) == datetime(2026, 6, 15)
    assert parse_pub_datetime(oa) == datetime(2026, 6, 20)


def test_crossref_date_parts_padded():
    p = _p(raw={"crossref": {"published-print": {"date-parts": [[2026, 5]]}}})
    assert parse_pub_datetime(p) == datetime(2026, 5, 1)


def test_year_only_treated_as_midyear():
    assert parse_pub_datetime(_p(year=2025)) == datetime(2025, 6, 30)


def test_no_date_information_returns_none():
    assert parse_pub_datetime(_p()) is None


def test_arxiv_preferred_over_dateonly_sources():
    p = _p(raw={"arxiv": {"published": "2026-06-30T01:00:00Z"},
                "openalex": {"publication_date": "2026-06-29"}})
    assert parse_pub_datetime(p) == datetime(2026, 6, 30, 1, 0, 0)


# ---------------------------------------------------------------------------
# paper_pipeline.paper_score / rank_papers (the base pipeline's ranking)
# ---------------------------------------------------------------------------

def test_paper_score_rewards_pdf_and_citations():
    year = datetime.now().year
    no_pdf = pp.paper_score(_p(year=year))
    with_pdf = pp.paper_score(_p(year=year, pdf_url="u"))
    assert with_pdf == no_pdf + 300


def test_rank_papers_orders_descending():
    year = datetime.now().year
    weak = _p(title="weak", year=year - 8)
    strong = _p(title="strong", year=year, pdf_url="u", citation_count=50)
    ranked = pp.rank_papers([weak, strong])
    assert [p.title for p in ranked] == ["strong", "weak"]
