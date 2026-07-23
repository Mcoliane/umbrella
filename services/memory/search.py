#!/usr/bin/env python3
"""Lexical ranking for node search — pure stdlib (no embeddings, no third-party).

The previous matcher was whole-query substring containment: a node matched only
if the *entire* query string appeared verbatim in its title or content. That
almost never fires when the query is a real sentence (e.g. a user's message), so
recall was effectively dead for anything but one-word lookups.

This replaces it with token-level BM25 over the candidate set being searched.
BM25 ranks by how many query *terms* a document contains, weighting rare terms
higher (IDF) and saturating repeated terms (k1) while normalizing for document
length (b). At this scale (a town's memory is dozens–low-hundreds of nodes) the
corpus is the candidate set itself, scored in-process — no index needed.
"""
from __future__ import annotations

import math
import re

# Unicode-aware: match word characters (letters/digits, any script) except the
# underscore, so snake_case splits into terms while accented/non-Latin text survives.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# A minimal stopword set. BM25's IDF already down-weights ubiquitous terms, but
# dropping the most common function words keeps short-document scores cleaner.
_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can did do does for from had has have
    how i if in into is it its of on or that the their them then there these they
    this to was were what when where which who will with you your
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric runs, drop stopwords."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def contains_query(title: str, content_text: str, query: str) -> bool:
    """Legacy whole-string substring check. Retained for callers that still want
    a boolean 'does this literal phrase appear' test; search ranking no longer
    uses it."""
    q = (query or '').strip().lower()
    if not q:
        return True
    return q in (title or '').lower() or q in (content_text or '').lower()


def rank_bm25(docs, query: str, *, k1: float = 1.5, b: float = 0.75) -> dict:
    """Score `docs` against `query` with BM25 over the doc set as the corpus.

    docs: iterable of (doc_id, title, content_text).
    Returns {doc_id: score} for docs with score > 0. Empty query -> {} (callers
    handle the no-query case as a recency listing).
    """
    q_tokens = tokenize(query)
    if not q_tokens:
        return {}
    q_set = set(q_tokens)

    doc_tokens: dict = {}
    lengths: dict = {}
    df: dict = {}
    for doc_id, title, content_text in docs:
        toks = tokenize(f"{title or ''} {content_text or ''}")
        doc_tokens[doc_id] = toks
        lengths[doc_id] = len(toks)
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    n_docs = len(doc_tokens)
    if n_docs == 0:
        return {}
    avgdl = (sum(lengths.values()) / n_docs) or 1.0

    scores: dict = {}
    for doc_id, toks in doc_tokens.items():
        if not toks:
            continue
        tf: dict = {}
        for t in toks:
            if t in q_set:
                tf[t] = tf.get(t, 0) + 1
        if not tf:
            continue
        dl = lengths[doc_id] or 1
        s = 0.0
        for term, freq in tf.items():
            n_t = df.get(term, 0)
            # Lucene-style non-negative IDF: never zero out or penalize a term
            # that appears in most docs (matters when the corpus is tiny).
            idf = math.log(1 + (n_docs - n_t + 0.5) / (n_t + 0.5))
            s += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scores[doc_id] = s
    return scores
