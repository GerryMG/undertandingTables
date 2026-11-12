"""Resolución de entidades: "el restaurante Polanco" → DIM_TDA.TDA_ID = 1, sin que el usuario dé IDs.

Indexa los valores de las columnas-etiqueta de cada entidad y busca de forma difusa (acentos, typos, orden).
"""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel
from rapidfuzz import fuzz

from .db.base import Connector
from .semantic import SemanticModel


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


class Candidate(BaseModel):
    entity: str
    table: str
    key_column: str
    key: str | int
    label_column: str
    label: str
    score: float


class Resolution(BaseModel):
    status: str                          # resolved | ambiguous | not_found
    candidates: list[Candidate]


class ValueIndex:
    def __init__(self, model: SemanticModel, conn: Connector, max_values: int = 50_000):
        self.model = model
        self.rows: list[tuple] = []      # (entity, table, key_col, key, label_col, label, norm_label)
        for e in model.entities:
            for lc in e.label_columns:
                df, _ = conn.query(f"SELECT DISTINCT {e.key_column} AS K, {lc} AS L FROM {e.table} "
                                   f"WHERE {lc} IS NOT NULL FETCH FIRST {max_values} ROWS ONLY")
                for k, lab in zip(df["K"], df["L"]):
                    k = int(k) if hasattr(k, "__int__") and not isinstance(k, str) else k
                    self.rows.append((e.name, e.table, e.key_column, k, lc, str(lab), norm(lab)))
        # palabras genéricas de cada entidad ("restaurante", "tienda"...) para no sesgar la búsqueda
        self.generic = {e.name: {norm(w) for w in [e.name, *e.synonyms]} for e in model.entities}

    def label_for(self, column: str, key) -> str | None:
        """Traduce una llave (ej. EMP_ID=538) a su nombre legible, si la columna es llave de una entidad."""
        col = str(column).upper()
        for (_e, _t, kc, k, _lc, lab, _nl) in self.rows:
            if kc.upper() == col and str(k) == str(key).split(".")[0]:
                return lab
        return None

    def entity_for_hint(self, hint: str | None) -> str | None:
        if not hint:
            return None
        h = norm(hint)
        best, score = None, 0
        for e in self.model.entities:
            for w in [e.name, *e.synonyms]:
                s = fuzz.ratio(h, norm(w))
                if s > score:
                    best, score = e.name, s
        return best if score >= 80 else None

    def search(self, text: str, entity_hint: str | None = None, limit: int = 8) -> list[Candidate]:
        ent = self.entity_for_hint(entity_hint)
        q = norm(text)
        out: dict[tuple, Candidate] = {}
        for (e, t, kc, k, lc, lab, nl) in self.rows:
            if ent and e != ent:
                continue
            words = [w for w in q.split() if w not in self.generic.get(e, set())]
            q2 = " ".join(words) or q
            base = max(fuzz.token_set_ratio(q, nl), fuzz.token_set_ratio(q2, nl), fuzz.WRatio(q2, nl))
            full = max(fuzz.ratio(q, nl), fuzz.ratio(q2, nl))
            score = round(0.85 * base + 0.15 * full, 1)
            if q == nl or q2 == nl:
                score = 100.0
            key = (e, k)
            if key not in out or out[key].score < score:
                out[key] = Candidate(entity=e, table=t, key_column=kc, key=k, label_column=lc, label=lab,
                                     score=score)
        return sorted(out.values(), key=lambda c: -c.score)[:limit]

    def resolve(self, text: str, entity_hint: str | None = None) -> Resolution:
        cands = self.search(text, entity_hint)
        if not cands or cands[0].score < 70:
            return Resolution(status="not_found", candidates=cands[:5])
        exact = [c for c in cands if c.score == 100.0 and norm(c.label) in (norm(text),)]
        if len(exact) == 1:
            return Resolution(status="resolved", candidates=exact)
        top = cands[0].score
        close = [c for c in cands if c.score >= top - 6 and c.score >= 70]
        if len(close) == 1 and top >= 80:
            return Resolution(status="resolved", candidates=close)
        return Resolution(status="ambiguous", candidates=close[:6])
