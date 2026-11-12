"""Onboarding: grafo LangGraph que construye el modelo semántico de un DW.

harvest → profile → relationships ─┬─► enrich_table (Send, una por tabla, en paralelo) ─► synthesize ⇄ validate ─► publish
                                   └─ (map-reduce)                                        (loop de corrección)
"""
from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Annotated, TypedDict

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .brains import Brain
from .config import EngineCfg
from .db.base import Connector, TableMeta
from .diagrams import write_docs
from .profiling import profile_table
from .relations import discover
from .semantic import (Column, ColumnRole, Entity, Metric, OpenQuestion, Relationship, SemanticModel, Table,
                       TableRole, TimeConfig)

SYSTEM = """Eres un arquitecto de datos senior experto en modelado dimensional (Kimball) y en Oracle.
Tu trabajo es entender el NEGOCIO detrás de un esquema con nombres crípticos. Usa toda la evidencia: nombres,
abreviaturas, comentarios, tipos, cardinalidades, valores de ejemplo y relaciones verificadas.
No inventes: si algo es ambiguo, dilo en la descripción con "(supuesto)" y plantéalo como pregunta abierta.
Escribe en español claro para un usuario de negocio."""


# ---------------- esquemas de salida del cerebro ----------------
class ColumnDoc(BaseModel):
    name: str
    business_name: str
    description: str
    role: ColumnRole
    synonyms: list[str] = Field(default_factory=list)
    values: dict[str, str] = Field(default_factory=dict, description="Decodificación de códigos si aplica")


class EntityDoc(BaseModel):
    name: str = Field(description="Nombre de negocio en singular, ej. 'tienda', 'producto', 'empleado'")
    synonyms: list[str] = Field(default_factory=list)
    label_columns: list[str] = Field(description="Columnas de texto con las que un humano nombra la cosa")


class TableDoc(BaseModel):
    name: str
    role: TableRole
    business_name: str
    description: str
    grain: str = Field(default="", description="Para hechos: qué representa UNA fila")
    columns: list[ColumnDoc]
    entity: EntityDoc | None = Field(default=None, description="Solo si la tabla describe cosas con nombre")


class Synthesis(BaseModel):
    business_summary: str
    metrics: list[Metric]
    time: TimeConfig
    entity_synonyms: dict[str, list[str]] = Field(default_factory=dict,
                                                  description="Sinónimos extra por entidad (nombre → lista)")
    rules: list[str] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


class OnbState(TypedDict, total=False):
    tables: list[dict]
    relationships: list[dict]
    table_docs: Annotated[list[dict], operator.add]
    synthesis: dict
    errors: list[str]
    attempts: int
    model_path: str


# ---------------- helpers de prompt ----------------
def describe_table(t: TableMeta, rels: list[Relationship], send_samples: bool) -> str:
    L = [f"TABLA {t.name} ({t.kind}), filas≈{t.num_rows}" + (f", comentario: '{t.comment}'" if t.comment else ""),
         f"PK declarada: {t.primary_key or 'ninguna'}", "COLUMNAS:"]
    for c in t.columns:
        s = f"  - {c.name} {c.data_type} distintos={c.num_distinct} nulos={c.num_nulls}"
        if c.min_value is not None:
            s += f" rango=[{c.min_value} .. {c.max_value}]"
        if c.comment:
            s += f" comentario='{c.comment}'"
        if send_samples and c.top_values:
            s += " top=" + ", ".join(f"{v}({n})" for v, n in c.top_values[:10])
        elif send_samples and c.samples:
            s += " ej=" + " | ".join(c.samples)
        L.append(s)
    out = [r for r in rels if r.from_table == t.name]
    inc = [r for r in rels if r.to_table == t.name]
    if out:
        L.append("SALE HACIA (FK verificadas): " + "; ".join(f"{r.from_column}→{r.to_table}.{r.to_column}"
                                                             for r in out))
    if inc:
        L.append("LA REFERENCIAN: " + "; ".join(f"{r.from_table}.{r.from_column}" for r in inc))
    return "\n".join(L)


def validate_model(m: SemanticModel) -> list[str]:
    errs = []
    names = {x.name for x in m.metrics}
    for x in m.metrics:
        if x.expression:
            t = m.table(x.table or "")
            if not t:
                errs.append(f"métrica {x.name}: tabla '{x.table}' no existe")
                continue
            try:
                tree = sqlglot.parse_one(x.expression, read="oracle")
            except Exception as e:  # noqa: BLE001
                errs.append(f"métrica {x.name}: expresión inválida ({e})")
                continue
            if not list(tree.find_all(exp.AggFunc)):
                errs.append(f"métrica {x.name}: la expresión debe ser una agregación (SUM/COUNT/AVG...)")
            for col in tree.find_all(exp.Column):
                if not t.col(col.name):
                    errs.append(f"métrica {x.name}: columna {col.name} no existe en {t.name}")
        elif x.derived:
            for ref in _refs(x.derived):
                if ref not in names:
                    errs.append(f"métrica {x.name}: referencia a métrica inexistente {{{ref}}}")
        else:
            errs.append(f"métrica {x.name}: necesita expression o derived")
    for e in m.entities:
        t = m.table(e.table)
        if not t or not t.col(e.key_column) or any(not t.col(c) for c in e.label_columns):
            errs.append(f"entidad {e.name}: tabla/columnas inválidas")
    tm = m.time
    if tm.date_table:
        t = m.table(tm.date_table)
        if not t or not t.col(tm.date_key or "") or not t.col(tm.date_column or ""):
            errs.append("time: date_table/date_key/date_column no existen")
    return errs


def _refs(formula: str) -> list[str]:
    import re
    return re.findall(r"\{(\w+)\}", formula)


# ---------------- grafo ----------------
def build_onboarding_graph(cfg: EngineCfg, conn: Connector, brain: Brain):
    send_samples = cfg.privacy.send_sample_values
    ctx = ""
    if cfg.business_context:
        ctx += f"\nCONTEXTO DE NEGOCIO (del usuario): {cfg.business_context}"
    if cfg.naming_hints:
        ctx += "\nPISTAS DE NOMBRES: " + ", ".join(f"{k}={v}" for k, v in cfg.naming_hints.items())

    def harvest(state: OnbState):
        tables = conn.introspect()
        print(f"[harvest] {len(tables)} tablas visibles: {', '.join(t.name for t in tables)}")
        return {"tables": [t.model_dump() for t in tables]}

    def profile(state: OnbState):
        out = []
        for d in state["tables"]:
            t = profile_table(conn, TableMeta.model_validate(d), cfg.privacy.max_sample_values)
            out.append(t.model_dump())
        print(f"[profile] perfiladas {len(out)} tablas")
        return {"tables": out}

    def relationships(state: OnbState):
        tables = [TableMeta.model_validate(d) for d in state["tables"]]
        rels = discover(conn, tables)
        for r in rels:
            print(f"[relaciones] {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}  {r.evidence}")
        return {"relationships": [r.model_dump() for r in rels]}

    def fan_out(state: OnbState):
        return [Send("enrich_table", {"tables": [d], "relationships": state["relationships"]})
                for d in state["tables"]]

    def enrich_table(state: OnbState):
        t = TableMeta.model_validate(state["tables"][0])
        rels = [Relationship.model_validate(r) for r in state["relationships"]]
        prompt = (f"Documenta esta tabla de un data warehouse.{ctx}\n\n{describe_table(t, rels, send_samples)}\n\n"
                  "Instrucciones:\n- role de tabla: fact (hechos/eventos medibles), dimension, bridge, lookup "
                  "(catálogo pequeño código-descripción), audit, staging u other.\n"
                  "- Documenta TODAS las columnas, con nombre de negocio y rol (measure = numérico agregable; "
                  "degenerate = folio/ticket en un hecho; code = código que conviene decodificar en `values`).\n"
                  "- Si la tabla describe cosas que un usuario nombraría (tiendas, productos, personas, líneas, "
                  "motivos...), llena `entity` con las columnas de texto que las nombran.\n"
                  "- Sinónimos: cómo le diría la gente de negocio (ej. tienda/sucursal/restaurante).")
        doc = brain.structured(SYSTEM, prompt, TableDoc, tag=f"enrich:{t.name}")
        doc.name = t.name
        print(f"[enrich] {t.name}: {doc.role} — {doc.business_name}")
        return {"table_docs": [doc.model_dump()]}

    def synthesize(state: OnbState):
        model = _assemble(state, with_synthesis=False)
        prev = ""
        if state.get("errors"):
            prev = ("\n\nTU PROPUESTA ANTERIOR TUVO ESTOS ERRORES, corrígelos:\n- " + "\n- ".join(state["errors"])
                    + f"\nPropuesta anterior: {json.dumps(state.get('synthesis', {}), ensure_ascii=False)[:6000]}")
        prompt = (f"Este es el modelo documentado tabla por tabla:{ctx}\n\n{model.prompt_context(send_samples)}\n\n"
                  "Ahora sintetiza el modelo de negocio:\n"
                  "1. business_summary: qué negocio es y qué procesos captura el DW (2-4 frases).\n"
                  "2. metrics: las métricas de negocio útiles (8-20). `expression` = agregación Oracle usando SOLO "
                  "columnas de `table` sin alias (ej. SUM(IMP_NTO), COUNT(DISTINCT TKT_NUM)). Métricas que "
                  "combinan hechos distintos van como `derived` con {nombre_metrica}, protegiendo divisiones con "
                  "NULLIF. Marca semi_additive lo que no se suma en el tiempo (existencias, saldos).\n"
                  "3. time: la dimensión fecha (tabla, llave, columna DATE).\n"
                  "4. rules: reglas de negocio inferidas que un analista debe saber (qué es 'venta', IVA, etc.).\n"
                  "5. open_questions: lo que NO puedes saber con certeza y debe confirmar un humano, "
                  "con tu respuesta propuesta." + prev)
        syn = brain.structured(SYSTEM, prompt, Synthesis, tag="synthesize")
        print(f"[synthesize] {len(syn.metrics)} métricas, {len(syn.open_questions)} preguntas abiertas")
        return {"synthesis": syn.model_dump(), "attempts": state.get("attempts", 0) + 1}

    def validate(state: OnbState):
        errs = validate_model(_assemble(state))
        if errs:
            print(f"[validate] {len(errs)} errores → corrigiendo: {errs[:3]}")
        return {"errors": errs}

    def route_after_validate(state: OnbState):
        return "synthesize" if state["errors"] and state.get("attempts", 0) < 3 else "publish"

    def publish(state: OnbState):
        model = _assemble(state)
        if state.get("errors"):  # tras 3 intentos, quitamos lo inválido en vez de publicarlo roto
            bad = {e.split(":")[0].replace("métrica ", "") for e in state["errors"] if e.startswith("métrica")}
            model.metrics = [m for m in model.metrics if m.name not in bad]
        path = Path(cfg.semantic_model_path)
        model.save(path)
        write_docs(model, path.parent)
        print(f"[publish] modelo → {path}  |  diagramas y documentación → {path.parent}")
        return {"model_path": str(path)}

    g = StateGraph(OnbState)
    for name, fn in [("harvest", harvest), ("profile", profile), ("relationships", relationships),
                     ("enrich_table", enrich_table), ("synthesize", synthesize), ("validate", validate),
                     ("publish", publish)]:
        g.add_node(name, fn)
    g.add_edge(START, "harvest")
    g.add_edge("harvest", "profile")
    g.add_edge("profile", "relationships")
    g.add_conditional_edges("relationships", fan_out, ["enrich_table"])
    g.add_edge("enrich_table", "synthesize")
    g.add_edge("synthesize", "validate")
    g.add_conditional_edges("validate", route_after_validate, ["synthesize", "publish"])
    g.add_edge("publish", END)
    return g.compile()


def _assemble(state: OnbState, with_synthesis: bool = True) -> SemanticModel:
    metas = {d["name"]: TableMeta.model_validate(d) for d in state["tables"]}
    docs = {d["name"]: TableDoc.model_validate(d) for d in state.get("table_docs", [])}
    tables, entities = [], []
    for name, meta in metas.items():
        doc = docs.get(name)
        cdocs = {c.name.upper(): c for c in (doc.columns if doc else [])}
        cols = []
        for c in meta.columns:
            cd = cdocs.get(c.name.upper())
            cols.append(Column(name=c.name, data_type=c.data_type, num_distinct=c.num_distinct,
                               samples=c.samples[:8], business_name=cd.business_name if cd else "",
                               description=cd.description if cd else "", role=cd.role if cd else "other",
                               synonyms=cd.synonyms if cd else [], values=cd.values if cd else {}))
        role = doc.role if doc else "other"
        tables.append(Table(name=name, role=role, business_name=doc.business_name if doc else "",
                            description=doc.description if doc else "", grain=doc.grain if doc else "",
                            row_count=meta.num_rows, primary_key=meta.primary_key, columns=cols,
                            use_in_answers=role not in ("audit", "staging")))
        # entidades solo en dimensiones/catálogos (un hecho no es "una cosa con nombre")
        if (doc and doc.entity and doc.entity.label_columns and role in ("dimension", "lookup")
                and not any(e.name.lower() == doc.entity.name.lower() for e in entities)):
            key = meta.primary_key[0] if len(meta.primary_key) == 1 else meta.columns[0].name
            entities.append(Entity(name=doc.entity.name, synonyms=doc.entity.synonyms, table=name, key_column=key,
                                   label_columns=[c.upper() for c in doc.entity.label_columns],
                                   description=doc.business_name))
    m = SemanticModel(tables=tables, entities=entities,
                      relationships=[Relationship.model_validate(r) for r in state.get("relationships", [])])
    if with_synthesis and state.get("synthesis"):
        s = Synthesis.model_validate(state["synthesis"])
        m.business_summary, m.metrics, m.time = s.business_summary, s.metrics, s.time
        m.rules, m.open_questions = s.rules, s.open_questions
        for e in m.entities:
            extra = s.entity_synonyms.get(e.name, [])
            e.synonyms = sorted(set(e.synonyms) | set(extra))
    return m
