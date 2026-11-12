"""Modelo semántico: la "capa de negocio" que el motor construye en el onboarding y que un humano cura.

Se guarda como YAML legible/editable. Todo lo que hace el runtime sale de aquí.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

TableRole = Literal["fact", "dimension", "bridge", "lookup", "audit", "staging", "other"]
ColumnRole = Literal["key", "foreign_key", "measure", "attribute", "date", "time", "degenerate", "flag", "code",
                     "other"]


class Column(BaseModel):
    name: str
    data_type: str
    business_name: str = ""
    description: str = ""
    role: ColumnRole = "attribute"
    synonyms: list[str] = Field(default_factory=list)
    values: dict[str, str] = Field(default_factory=dict)   # decodificación de códigos: {"R": "Restaurante"}
    samples: list[str] = Field(default_factory=list)
    num_distinct: int | None = None


class Table(BaseModel):
    name: str
    role: TableRole = "other"
    business_name: str = ""
    description: str = ""
    grain: str = ""
    row_count: int | None = None
    primary_key: list[str] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    use_in_answers: bool = True

    def col(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name.upper() == name.upper()), None)


class Relationship(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    evidence: list[str] = Field(default_factory=list)
    inclusion: float | None = None
    confidence: float = 0.5


class Entity(BaseModel):
    """Cosas con nombre que un usuario menciona ("el restaurante Polanco", "la línea Lácteos")."""
    name: str
    synonyms: list[str] = Field(default_factory=list)
    table: str
    key_column: str
    label_columns: list[str]
    description: str = ""


class Metric(BaseModel):
    name: str                          # identificador snake_case
    business_name: str
    description: str = ""
    table: str | None = None           # tabla de hechos (None si es derivada)
    expression: str | None = None      # agregación SQL Oracle sobre columnas de `table`, ej. SUM(IMP_NTO)
    derived: str | None = None         # fórmula sobre otras métricas, ej. {devolucion_importe} / NULLIF({venta_neta},0)
    format: Literal["money", "number", "percent", "ratio"] = "number"
    additive: Literal["additive", "semi_additive", "non_additive"] = "additive"
    synonyms: list[str] = Field(default_factory=list)


class TimeConfig(BaseModel):
    date_table: str | None = None
    date_key: str | None = None        # llave en la dimensión fecha (ej. FEC_ID)
    date_column: str | None = None     # columna DATE real (ej. FEC)


class OpenQuestion(BaseModel):
    question: str
    proposed_answer: str = ""
    answer: str | None = None          # lo llena el humano


class SemanticModel(BaseModel):
    business_summary: str = ""
    tables: list[Table] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    time: TimeConfig = Field(default_factory=TimeConfig)
    rules: list[str] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)

    # ---------- persistencia ----------
    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude_none=True)
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "SemanticModel":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    # ---------- consultas ----------
    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name.upper() == name.upper()), None)

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name.lower() == name.lower()), None)

    def answer_tables(self) -> list[Table]:
        return [t for t in self.tables if t.use_in_answers and t.role not in ("audit", "staging")]

    def join_path(self, start: str, target: str) -> list[Relationship] | None:
        """Camino de joins siguiendo FK→PK (de hechos hacia dimensiones, incluye copo de nieve)."""
        start, target = start.upper(), target.upper()
        if start == target:
            return []
        adj: dict[str, list[Relationship]] = {}
        for r in self.relationships:
            adj.setdefault(r.from_table.upper(), []).append(r)
        q = deque([(start, [])])
        seen = {start}
        while q:
            node, path = q.popleft()
            for r in adj.get(node, []):
                nxt = r.to_table.upper()
                if nxt in seen:
                    continue
                if nxt == target:
                    return path + [r]
                seen.add(nxt)
                q.append((nxt, path + [r]))
        return None

    # ---------- contexto para el cerebro ----------
    def prompt_context(self, include_samples: bool = True, max_samples: int = 6) -> str:
        L = []
        if self.business_summary:
            L.append(f"NEGOCIO: {self.business_summary}\n")
        L.append("TABLAS (dialecto Oracle):")
        for t in self.answer_tables():
            L.append(f"\n## {t.name} [{t.role}] — {t.business_name}. {t.description}"
                     + (f" GRANO: {t.grain}." if t.grain else "") + f" (~{t.row_count or '?'} filas)")
            for c in t.columns:
                extra = ""
                if c.values:
                    extra += " valores=" + ", ".join(f"{k}:{v}" for k, v in list(c.values.items())[:12])
                elif include_samples and c.samples:
                    extra += " ej=" + " | ".join(c.samples[:max_samples])
                syn = f" (sinónimos: {', '.join(c.synonyms)})" if c.synonyms else ""
                L.append(f"  - {c.name} {c.data_type} [{c.role}] {c.business_name}: {c.description}{syn}{extra}")
        L.append("\nRELACIONES (FK → PK):")
        for r in self.relationships:
            L.append(f"  - {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}")
        if self.time.date_table:
            L.append(f"\nTIEMPO: dimensión fecha {self.time.date_table} (llave {self.time.date_key}, "
                     f"fecha real {self.time.date_column}). Filtra fechas con {self.time.date_table}."
                     f"{self.time.date_column} y literales DATE 'YYYY-MM-DD'.")
        L.append("\nMÉTRICAS:")
        for m in self.metrics:
            body = f"{m.table}: {m.expression}" if m.expression else f"derivada: {m.derived}"
            syn = f" (sinónimos: {', '.join(m.synonyms)})" if m.synonyms else ""
            L.append(f"  - {m.name} — {m.business_name}{syn} = {body} [{m.format}, {m.additive}]. {m.description}")
        L.append("\nENTIDADES (cosas con nombre que el usuario menciona):")
        for e in self.entities:
            syn = f" (sinónimos: {', '.join(e.synonyms)})" if e.synonyms else ""
            L.append(f"  - {e.name}{syn}: {e.table}.{e.key_column}, nombre en {', '.join(e.label_columns)}")
        rules = list(self.rules) + [f"{q.question} → {q.answer}" for q in self.open_questions if q.answer]
        if rules:
            L.append("\nREGLAS DE NEGOCIO:")
            L += [f"  - {r}" for r in rules]
        return "\n".join(L)
