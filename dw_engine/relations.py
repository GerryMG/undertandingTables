"""Descubrimiento de relaciones: FKs declaradas + candidatas por nombre, VERIFICADAS con datos (inclusión)."""
from __future__ import annotations

import re

from .db.base import Connector, TableMeta
from .semantic import Relationship

INCLUSION_MIN = 0.95


def _key_tokens(name: str) -> str:
    n = name.upper()
    n = re.sub(r"^(ID|CVE|COD|CD|NUM|NO|K|FK|PK)_", "", n)
    n = re.sub(r"_(ID|CVE|COD|CD|KEY|SK|NUM|NO|FK|PK)$", "", n)
    return n


def candidate_keys(t: TableMeta) -> list[str]:
    """PK declarada o columna única no nula (inferida)."""
    if len(t.primary_key) == 1:
        return t.primary_key
    if t.primary_key:
        return []
    out = []
    for c in t.columns:
        if (t.num_rows and c.num_distinct == t.num_rows and not c.num_nulls
                and (c.is_numeric or c.is_text) and re.search(r"(^|_)(ID|CVE|COD|KEY|SK|NUM)($|_)", c.name)):
            out.append(c.name)
    return out[:1]


def _compatible(a, b) -> bool:
    return (a.is_numeric and b.is_numeric) or (a.is_text and b.is_text)


def discover(conn: Connector, tables: list[TableMeta]) -> list[Relationship]:
    by_name = {t.name: t for t in tables}
    rels: dict[tuple, Relationship] = {}

    # 1) declaradas
    for t in tables:
        for fk in t.foreign_keys:
            if len(fk.columns) == 1 and fk.ref_table in by_name:
                rels[(t.name, fk.columns[0])] = Relationship(
                    from_table=t.name, from_column=fk.columns[0], to_table=fk.ref_table,
                    to_column=fk.ref_columns[0], evidence=["fk_declarada"], confidence=1.0)

    # 2) candidatas por nombre (misma "raíz" de llave + tipos compatibles)
    keys = {t.name: candidate_keys(t) for t in tables}
    for t in tables:
        for c in t.columns:
            if (t.name, c.name) in rels:
                continue
            if c.name in keys[t.name] and len(t.primary_key) <= 1:
                continue  # es la llave de su propia tabla
            best = None
            for d in tables:
                if d.name == t.name:
                    continue
                for k in keys[d.name]:
                    kc = d.col(k)
                    if not _compatible(c, kc):
                        continue
                    if c.name == k or _key_tokens(c.name) == _key_tokens(k):
                        # preferir la tabla "más chica" (dimensión) si hay varias con la misma llave
                        if best is None or (d.num_rows or 0) < (by_name[best[0]].num_rows or 0):
                            best = (d.name, k)
            if best:
                rels[(t.name, c.name)] = Relationship(
                    from_table=t.name, from_column=c.name, to_table=best[0], to_column=best[1],
                    evidence=["nombre_coincide"], confidence=0.5)

    # 3) verificación con datos: % de valores distintos del FK que existen en la llave destino
    for r in rels.values():
        q = (f"SELECT COUNT(*) AS TOTAL, SUM(CASE WHEN d.{r.to_column} IS NOT NULL THEN 1 ELSE 0 END) AS HIT "
             f"FROM (SELECT DISTINCT {r.from_column} AS V FROM {r.from_table} WHERE {r.from_column} IS NOT NULL) x "
             f"LEFT JOIN {r.to_table} d ON d.{r.to_column} = x.V")
        try:
            df, _ = conn.query(q)
            total, hit = int(df.iloc[0, 0]), int(df.iloc[0, 1] or 0)
            r.inclusion = round(hit / total, 4) if total else None
        except Exception as e:  # noqa: BLE001
            r.evidence.append(f"verificacion_fallo: {e}"[:200])
            continue
        if r.inclusion is not None and r.inclusion >= INCLUSION_MIN:
            r.evidence.append(f"verificado_datos({r.inclusion:.1%})")
            r.confidence = max(r.confidence, 0.95 if "nombre_coincide" in r.evidence else 1.0)
        else:
            r.evidence.append(f"inclusion_baja({r.inclusion})")
            r.confidence = min(r.confidence, 0.2)
    return [r for r in rels.values() if r.confidence >= 0.5]
