#!/usr/bin/env python3
"""Lightweight, dependency-free relevance engine over the LLM knowledge wiki.

The wiki (AI_DS_ML_DL/wiki/concepts/*.md) is the project's curated knowledge
base. This module lets the pipeline USE it as a source of knowledge, in two ways:

  * fetch_papers.py  -> score how relevant a candidate paper is to the knowledge
                        base, and bias harvest selection toward on-interest work
                        (WikiIndex.relevance).
  * reproduce.py     -> retrieve the concept pages most relevant to a paper and
                        inject them as grounding context into the reproduction
                        prompt (WikiIndex.retrieve).

Implementation is a classic TF-IDF vector space with cosine similarity, in pure
stdlib (no numpy / no embeddings), so it runs anywhere the pipeline runs and adds
no install burden. If the wiki is missing or empty, every method degrades to a
neutral no-op (relevance 0.0, retrieve []).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

# Function words + a few corpus-generic terms. TF-IDF already downweights terms
# that appear in most pages; this list just trims obvious noise up front.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "as",
    "at", "by", "is", "are", "be", "been", "being", "was", "were", "it", "its",
    "this", "that", "these", "those", "from", "into", "over", "under", "which",
    "such", "can", "may", "not", "no", "but", "if", "then", "than", "so", "we",
    "our", "you", "your", "they", "their", "he", "she", "his", "her", "them",
    "each", "any", "all", "both", "how", "what", "when", "where", "who", "why",
    "one", "two", "using", "used", "use", "via", "e.g", "i.e", "etc", "also",
    "source", "sources", "summary", "page", "pages", "paper", "papers",
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    # [[slug|Display Text]] -> "Display Text"; [[slug]] -> "slug"
    text = _WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1], text)
    toks = _TOKEN_RE.findall(text)
    out: list[str] = []
    for t in toks:
        t = t.strip(".+#")
        if len(t) < 2 or t in _STOPWORDS or t.isdigit():
            continue
        out.append(t)
    return out


@dataclass
class ConceptPage:
    slug: str
    title: str
    summary: str
    vec: dict[str, float] = field(default_factory=dict)  # L2-normalized tf-idf


def _parse_concept(path: Path) -> tuple[str, str, str]:
    """Return (title, summary, body_for_indexing) for one concept page.

    The huge ``**Sources**:`` link list and the ``**Last updated**`` line are
    dropped so they do not swamp the term statistics; title and summary are
    weighted more heavily than the body by repetition in the returned text.
    """
    title = path.stem.replace("-", " ")
    summary = ""
    body_lines: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("# ") and title == path.stem.replace("-", " "):
            title = s[2:].strip()
            continue
        if s.startswith("**Sources**") or s.startswith("**Last updated**"):
            continue
        if s.startswith("**Summary**"):
            summary = s.split(":", 1)[-1].strip() if ":" in s else ""
            continue
        body_lines.append(line)
    body = "\n".join(body_lines)
    return title, summary, body


class WikiIndex:
    """TF-IDF index over the wiki's concept pages."""

    def __init__(self, wiki_dir: Path | str):
        self.wiki_dir = Path(wiki_dir)
        self.pages: list[ConceptPage] = []
        self._idf: dict[str, float] = {}
        self._build()

    # ---- construction ----------------------------------------------------
    def _build(self) -> None:
        concepts = self.wiki_dir / "concepts"
        if not concepts.is_dir():
            return
        raw_docs: list[tuple[str, str, str, list[str]]] = []  # slug,title,summary,tokens
        df: dict[str, int] = {}
        for path in sorted(concepts.glob("*.md")):
            try:
                title, summary, body = _parse_concept(path)
            except Exception:  # noqa: BLE001
                continue
            # weight title x3 and summary x2 relative to body
            tokens = (_tokenize(title) * 3) + (_tokenize(summary) * 2) + _tokenize(body)
            if not tokens:
                continue
            raw_docs.append((path.stem, title, summary, tokens))
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        n = len(raw_docs)
        if n == 0:
            return
        # smoothed idf
        self._idf = {term: math.log((n + 1) / (d + 1)) + 1.0 for term, d in df.items()}
        for slug, title, summary, tokens in raw_docs:
            self.pages.append(ConceptPage(
                slug=slug, title=title, summary=summary,
                vec=self._vectorize(tokens),
            ))

    def _vectorize(self, tokens: list[str]) -> dict[str, float]:
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        vec: dict[str, float] = {}
        for term, c in tf.items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            vec[term] = (1.0 + math.log(c)) * idf
        norm = math.sqrt(sum(w * w for w in vec.values()))
        if norm > 0:
            for term in vec:
                vec[term] /= norm
        return vec

    # ---- queries ---------------------------------------------------------
    @property
    def ready(self) -> bool:
        return bool(self.pages)

    def _query_vec(self, text: str) -> dict[str, float]:
        return self._vectorize(_tokenize(text or ""))

    def _cosine(self, q: dict[str, float], d: dict[str, float]) -> float:
        # both already L2-normalized -> dot product is cosine
        if len(q) > len(d):
            q, d = d, q
        return sum(w * d.get(t, 0.0) for t, w in q.items())

    def scored(self, text: str) -> list[tuple[ConceptPage, float]]:
        """All concept pages scored against *text*, sorted high-to-low."""
        if not self.ready:
            return []
        q = self._query_vec(text)
        if not q:
            return []
        ranked = [(p, self._cosine(q, p.vec)) for p in self.pages]
        ranked.sort(key=lambda t: t[1], reverse=True)
        return ranked

    def retrieve(self, text: str, k: int = 5, min_score: float = 0.03
                 ) -> list[tuple[ConceptPage, float]]:
        """Top-*k* concept pages relevant to *text* (above *min_score*)."""
        return [(p, s) for p, s in self.scored(text)[:k] if s >= min_score]

    def relevance(self, text: str, top_n: int = 3) -> float:
        """Scalar 0..1 relevance of *text* to the knowledge base: the mean cosine
        over the *top_n* best-matching concept pages (rewards a paper that is
        close to SOME area of the wiki rather than the diffuse average)."""
        scored = self.scored(text)
        if not scored:
            return 0.0
        top = [s for _, s in scored[:top_n]]
        return sum(top) / len(top) if top else 0.0


# module-level cache so repeated calls in one process reuse the built index
_CACHE: dict[str, WikiIndex] = {}


def get_wiki_index(wiki_dir: Path | str) -> WikiIndex:
    key = str(Path(wiki_dir).resolve())
    idx = _CACHE.get(key)
    if idx is None:
        idx = WikiIndex(wiki_dir)
        _CACHE[key] = idx
    return idx


if __name__ == "__main__":  # tiny smoke/CLI: python wiki_index.py <wiki_dir> "<query>"
    import sys
    wd = sys.argv[1] if len(sys.argv) > 1 else "AI_DS_ML_DL/wiki"
    query = sys.argv[2] if len(sys.argv) > 2 else "diffusion models for image generation"
    ix = get_wiki_index(wd)
    print(f"[wiki_index] {len(ix.pages)} concept pages loaded from {wd}")
    print(f"[wiki_index] relevance({query!r}) = {ix.relevance(query):.4f}")
    for p, s in ix.retrieve(query, k=5):
        print(f"  {s:.3f}  {p.title}  ({p.slug})")
