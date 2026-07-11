"""Corpus deduplication / normalization (paper_pipeline + fetch_papers).

These are the invariants the whole harness relies on to guarantee a paper is
never harvested or reproduced twice: title/DOI normalization, the stable
dedupe key, cross-source merging, and the multi-key ledger identity.
"""
import fetch_papers
import paper_pipeline as pp


# ---------------------------------------------------------------------------
# normalize_title / normalize_doi
# ---------------------------------------------------------------------------

def test_normalize_title_case_whitespace_punctuation():
    assert pp.normalize_title("  Attention Is  All You Need!  ") == \
        pp.normalize_title("attention is all you need")


def test_normalize_title_strips_punctuation_keeps_words():
    assert pp.normalize_title("GANs: (Generative) Adversarial-Networks?") == \
        "gans generative adversarialnetworks"


def test_normalize_doi_strips_scheme_and_case():
    for raw in ("https://doi.org/10.1000/XYZ.123",
                "http://doi.org/10.1000/xyz.123",
                "10.1000/XYZ.123 "):
        assert pp.normalize_doi(raw) == "10.1000/xyz.123"


def test_normalize_doi_none_and_empty():
    assert pp.normalize_doi(None) is None
    assert pp.normalize_doi("") is None


# ---------------------------------------------------------------------------
# dedupe_key precedence: doi > arxiv > s2 > openalex > title
# ---------------------------------------------------------------------------

def test_dedupe_key_prefers_doi():
    p = pp.Paper(title="T", doi="10.1/A", arxiv_id="2401.00001",
                 semantic_scholar_id="s2id")
    assert pp.dedupe_key(p) == "doi:10.1/a"


def test_dedupe_key_falls_back_in_order():
    assert pp.dedupe_key(pp.Paper(title="T", arxiv_id="2401.00001")) == \
        "arxiv:2401.00001"
    assert pp.dedupe_key(pp.Paper(title="T", semantic_scholar_id="S")) == "s2:S"
    assert pp.dedupe_key(pp.Paper(title="T", openalex_id="W1")) == "openalex:W1"
    assert pp.dedupe_key(pp.Paper(title="Some Title!")) == "title:some title"


def test_dedupe_key_same_paper_different_doi_forms_collide():
    a = pp.Paper(title="A", doi="https://doi.org/10.5/Q")
    b = pp.Paper(title="Completely different title", doi="10.5/q")
    assert pp.dedupe_key(a) == pp.dedupe_key(b)


# ---------------------------------------------------------------------------
# merge_papers / deduplicate_papers
# ---------------------------------------------------------------------------

def test_deduplicate_merges_by_key_and_unions_metadata():
    a = pp.Paper(title="Same Paper", doi="10.9/z", sources=["arxiv"],
                 citation_count=3, pdf_url=None, authors=["Bea"])
    b = pp.Paper(title="Same Paper", doi="10.9/Z", sources=["openalex"],
                 citation_count=10, pdf_url="https://x/p.pdf", authors=["Al"])
    out = pp.deduplicate_papers([a, b])
    assert len(out) == 1
    merged = out[0]
    assert merged.sources == ["arxiv", "openalex"]
    assert merged.citation_count == 10          # max wins
    assert merged.pdf_url == "https://x/p.pdf"  # first non-null wins
    assert merged.authors == ["Al", "Bea"]


def test_deduplicate_keeps_distinct_papers():
    out = pp.deduplicate_papers([pp.Paper(title="One", doi="10.1/one"),
                                 pp.Paper(title="Two", doi="10.1/two")])
    assert len(out) == 2


def test_merge_prefers_existing_scalar_fields():
    a = pp.Paper(title="P", doi="10.2/p", year=2024, abstract="first")
    b = pp.Paper(title="P", doi="10.2/p", year=2023, abstract="second",
                 venue="NeurIPS")
    merged = pp.merge_papers(a, b)
    assert merged.year == 2024          # existing non-null kept
    assert merged.abstract == "first"
    assert merged.venue == "NeurIPS"    # filled from the new record


# ---------------------------------------------------------------------------
# fetch_papers.paper_keys — the multi-key ledger identity
# ---------------------------------------------------------------------------

def test_paper_keys_emits_all_known_identifiers():
    p = pp.Paper(title="A Great Paper", doi="https://doi.org/10.7/G",
                 arxiv_id="2406.01234")
    keys = fetch_papers.paper_keys(p)
    assert keys == {"title:a great paper", "doi:10.7/g", "arxiv:2406.01234"}


def test_paper_keys_skips_missing_identifiers():
    keys = fetch_papers.paper_keys(pp.Paper(title="Only Title"))
    assert keys == {"title:only title"}


def test_paper_keys_empty_title_yields_no_title_key():
    keys = fetch_papers.paper_keys(pp.Paper(title="", arxiv_id="2401.5"))
    assert keys == {"arxiv:2401.5"}
