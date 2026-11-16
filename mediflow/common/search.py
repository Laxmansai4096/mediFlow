"""Knowledge retrieval - local BM25 index (free stand-in for Azure AI Search).

Azure AI Search would host the same documents in an index with a language
field, using hybrid (keyword + vector) search and semantic ranking for RAG.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

KB_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "kb.json"
_SPLIT = re.compile(r"[\s,.!?;:()\"'।॥/\-–]+")  # includes Devanagari danda
_STOP = {"the", "a", "an", "is", "are", "to", "of", "and", "or", "for", "in", "on", "my", "i", "do", "how",
         "what", "can", "you", "me", "please", "है", "का", "की", "के", "में", "और", "को", "क्या", "कैसे", "मैं"}


def tokenize(text: str) -> list[str]:
    return [t for t in _SPLIT.split(text.lower()) if t and t not in _STOP]


class BM25Index:
    def __init__(self, docs: list[dict], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.tokens = [tokenize(f"{d['title']} {d['title']} {d.get('keywords', '')} {d['text']}") for d in docs]
        self.avgdl = sum(map(len, self.tokens)) / max(len(self.tokens), 1)
        df = Counter(t for toks in self.tokens for t in set(toks))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(toks) for toks in self.tokens]

    def _prefix_tf(self, tf: Counter, term: str) -> int:
        """Light stemming: count tokens sharing a 5+ char prefix (handles plural/inflection)."""
        if term in tf:
            return tf[term]
        if len(term) >= 5:
            return sum(c for tok, c in tf.items() if tok.startswith(term[:5]))
        return 0

    def search(self, query: str, lang: str | None = None, top: int = 3) -> list[dict]:
        q = tokenize(query)
        results = []
        for i, d in enumerate(self.docs):
            if lang and d["lang"] != lang:
                continue
            dl = len(self.tokens[i])
            score = 0.0
            for term in q:
                f = self._prefix_tf(self.tf[i], term)
                if not f:
                    continue
                idf = self.idf.get(term, math.log(1 + len(self.docs) / 1.5))
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                results.append({**d, "score": round(score, 3)})
        return sorted(results, key=lambda r: r["score"], reverse=True)[:top]


@lru_cache(maxsize=1)
def knowledge_index() -> BM25Index:
    return BM25Index(json.loads(KB_PATH.read_text(encoding="utf-8")))
