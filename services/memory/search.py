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


_VOWELS = frozenset("aeiou")


def _undouble(s: str) -> str:
    """Collapse a trailing doubled consonant (runn->run) but keep ll/ss/zz/ff."""
    if len(s) >= 3 and s[-1] == s[-2] and s[-1] not in _VOWELS and s[-1] not in "lszf":
        return s[:-1]
    return s


def _stem(word: str) -> str:
    """Light, conservative English stemmer — pure Python, no deps. Collapses the common
    inflections (plurals, -ing/-ed, -ly) so query and document morphology match. It is
    deliberately simple and predictable, not full Porter: it favors not mangling a word
    over maximal stemming, and a few silent-e cases (use/using) are knowingly left alone.
    Because the SAME function runs on both indexed text and queries, exact stems don't
    matter — only that variants of a word map to the same token."""
    w = word
    if len(w) <= 3:
        return w
    # plural / 3rd-person -s
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"                                             # ponies->pony
    elif w.endswith("sses"):
        w = w[:-2]                                                  # caresses->caress
    elif w.endswith("es") and len(w) > 3 and (w[-3] in "sxzo" or w[-4:-2] in ("ch", "sh")):
        w = w[:-2]                                                  # boxes->box, matches->match
    elif w.endswith("s") and not w.endswith("ss") and w[-2:] not in ("is", "us"):
        w = w[:-1]                                                  # capitals->capital (but not paris/status/basis)
    # gerund / past
    if w.endswith("ing") and len(w) > 5:
        w = _undouble(w[:-3])                                       # running->run
    elif w.endswith("ed") and len(w) > 4:
        w = _undouble(w[:-2])                                       # stopped->stop
    # adverb -ly
    if w.endswith("ly") and len(w) > 4:
        w = w[:-2]                                                  # quickly->quick
    return w


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric runs, drop stopwords, and light-stem so
    morphological variants (install/installing/installed) collapse to one term."""
    if not text:
        return []
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


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
