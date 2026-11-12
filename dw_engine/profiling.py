"""Perfilado determinístico (sin LLM): cardinalidades, nulos, rangos, valores frecuentes y ejemplos."""
from __future__ import annotations

from .db.base import Connector, TableMeta

SAMPLE_ABOVE_ROWS = 2_000_000   # arriba de esto, perfilar sobre muestra
SAMPLE_PCT = 5
TOP_K_MAX_DISTINCT = 40         # columnas con pocos valores distintos → top valores completos


def _src(t: TableMeta) -> str:
    if t.num_rows and t.num_rows > SAMPLE_ABOVE_ROWS and t.kind == "TABLE":
        return f"{t.name} SAMPLE ({SAMPLE_PCT})"
    return t.name


def profile_table(conn: Connector, t: TableMeta, max_samples: int = 8) -> TableMeta:
    src = _src(t)
    if t.num_rows is None:
        t.num_rows = int(conn.query(f"SELECT COUNT(*) N FROM {t.name}")[0].iloc[0, 0])
    parts = ["COUNT(*) AS N__"]
    for i, c in enumerate(t.columns):
        parts.append(f"COUNT(DISTINCT {c.name}) AS D{i}")
        parts.append(f"SUM(CASE WHEN {c.name} IS NULL THEN 1 ELSE 0 END) AS Z{i}")
        if c.is_numeric or c.is_temporal or c.is_text:
            parts.append(f"MIN({c.name}) AS L{i}")
            parts.append(f"MAX({c.name}) AS H{i}")
    df, _ = conn.query(f"SELECT {', '.join(parts)} FROM {src}")
    row = df.iloc[0]
    sampled = src != t.name
    for i, c in enumerate(t.columns):
        d = int(row[f"D{i}"])
        # en muestra, el distinct de columnas casi-únicas se subestima; escalamos de forma conservadora
        c.num_distinct = c.num_distinct or (d if not sampled or d < 1000 else int(d * 100 / SAMPLE_PCT))
        c.num_nulls = int(row[f"Z{i}"]) if not sampled else int(row[f"Z{i}"] * 100 / SAMPLE_PCT)
        if f"L{i}" in row:
            c.min_value = None if row[f"L{i}"] is None else str(row[f"L{i}"])[:60]
            c.max_value = None if row[f"H{i}"] is None else str(row[f"H{i}"])[:60]
    for c in t.columns:
        if c.num_distinct and c.num_distinct <= TOP_K_MAX_DISTINCT:
            q = (f"SELECT {c.name} AS V, COUNT(*) AS N FROM {src} GROUP BY {c.name} "
                 f"ORDER BY 2 DESC FETCH FIRST 15 ROWS ONLY")
            vals, _ = conn.query(q)
            c.top_values = [(str(v)[:60], int(n)) for v, n in zip(vals["V"], vals["N"])]
            c.samples = [v for v, _ in c.top_values[:max_samples]]
        elif c.is_text:
            vals, _ = conn.query(f"SELECT DISTINCT {c.name} AS V FROM {src} WHERE {c.name} IS NOT NULL "
                                 f"FETCH FIRST {max_samples} ROWS ONLY")
            c.samples = [str(v)[:60] for v in vals["V"]]
    return t
