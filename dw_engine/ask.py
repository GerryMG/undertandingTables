"""Grafo de preguntas (runtime). No es lineal:

understand → resolve_entities ──(interrupt si hay ambigüedad)──► plan → execute ─┬─► narrate → report
                                                                   ▲      ▲ ▲ ▲  │
                                                                   │      │ │ └─ repair (error SQL, ≤N veces)
                                                                   │      │ └─── deepen (hay hallazgos: bajar un nivel, 1 vez)
                                                                   └──── replan (resultado vacío, 1 vez)
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

import pandas as pd
from pydantic import BaseModel, Field

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .brains import Brain, make_brain
from .compiler import CompileError, QuerySpec, compile_spec
from .config import EngineCfg
from .db import make_connector
from .db.base import Connector
from .detectors import DETECTOR_DOCS, DetectorCall, run_detector
from .guard import GuardError, check_sql
from .report import write_report
from .semantic import SemanticModel
from .value_index import ValueIndex

SYSTEM = """Eres un analista de datos senior de la empresa. Respondes preguntas de negocio usando EXCLUSIVAMENTE
el modelo semántico que se te da (tablas, métricas, entidades, reglas). SQL siempre en dialecto ORACLE.
Nunca inventes tablas, columnas ni cifras. Si algo no se puede responder con el modelo, dilo."""


# ---------------- esquemas de salida del cerebro ----------------
class Mention(BaseModel):
    text: str = Field(description="El nombre tal como lo dijo el usuario, ej. 'Polanco', 'lácteos'")
    entity: str | None = Field(default=None, description="Entidad del modelo a la que se refiere")
    plural: bool = Field(default=False, description="True si se refiere a varias (ej. 'las tiendas de Puebla')")


class Understanding(BaseModel):
    intent: Literal["dato", "exploracion", "hallazgos"] = Field(
        description="dato = cifra puntual; exploracion = desglose/comparación/tendencia; "
                    "hallazgos = buscar cosas raras, anomalías, riesgos, oportunidades")
    restated: str = Field(description="La pregunta reformulada sin ambigüedad, en términos del negocio")
    mentions: list[Mention] = Field(default_factory=list,
                                    description="SOLO instancias con nombre propio (tiendas, productos, personas, "
                                                "líneas...). No incluyas conceptos como 'ventas' o 'devoluciones'")
    period_start: date | None = None
    period_end: date | None = None
    period_text: str = ""
    metrics_hint: list[str] = Field(default_factory=list)


class Step(BaseModel):
    purpose: str
    spec: QuerySpec | None = Field(default=None, description="Consulta declarativa (preferida)")
    sql: str | None = Field(default=None, description="SQL Oracle libre, solo si spec no alcanza")
    detector: DetectorCall | None = None
    chart: Literal["auto", "bar", "line", "table", "none"] = "auto"


class Plan(BaseModel):
    approach: str
    steps: list[Step] = Field(min_length=1, description="máximo 8 pasos")


class KeyNumber(BaseModel):
    label: str
    value: str


class FindingOut(BaseModel):
    title: str
    detail: str
    severity: Literal["alta", "media", "baja"] = "media"


class Answer(BaseModel):
    summary: str = Field(description="Respuesta directa en markdown, 1-3 párrafos")
    key_numbers: list[KeyNumber] = Field(default_factory=list)
    findings: list[FindingOut] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)


class AskState(TypedDict, total=False):
    question: str
    run_dir: str
    understanding: dict
    resolved: list[dict]
    plan: dict
    results: list[dict | None]
    repairs: int
    replans: int
    deepened: int
    feedback: str
    answer: dict
    report: str


# ---------------- helpers ----------------
def _preview(df: pd.DataFrame, n: int = 25) -> str:
    if df.empty:
        return "(sin filas)"
    return df.head(n).to_csv(index=False)[:4000]


def _mode_rules(mode: str) -> str:
    if mode == "compiled":
        return "Usa SIEMPRE `spec` (nunca `sql`)."
    if mode == "free":
        return "Escribe `sql` Oracle para cada paso (no uses `spec`)."
    return ("Prefiere `spec` (el compilador garantiza joins y fórmulas correctas). Usa `sql` Oracle libre SOLO "
            "cuando spec no alcance: condiciones calculadas (CASE WHEN), horas, MOD, comparaciones entre "
            "periodos, subconsultas, o alimentar un detector con columnas específicas.")


def build_ask_graph(cfg: EngineCfg, model: SemanticModel, conn: Connector, brain: Brain, index: ValueIndex):
    ctx = model.prompt_context(include_samples=cfg.privacy.send_sample_values)
    today = cfg.ref_date()

    def understand(state: AskState):
        prompt = (f"{ctx}\n\nHOY ES {today.isoformat()} ({today.strftime('%A')}).\n"
                  f"PREGUNTA DEL USUARIO: {state['question']}\n\n"
                  "Interpreta la pregunta: intención, menciones de cosas con nombre (para buscarlas en el catálogo; "
                  "usa como `entity` uno de los nombres de ENTIDADES), periodo exacto (fechas ISO; 'ayer', "
                  "'este año', 'últimos 6 meses' se calculan desde HOY), y métricas probables.")
        u = brain.structured(SYSTEM, prompt, Understanding, tag="understand")
        print(f"[understand] {u.intent}: {u.restated}  | menciones={[m.text for m in u.mentions]} "
              f"| periodo={u.period_start}..{u.period_end}")
        return {"understanding": u.model_dump(mode="json")}

    def resolve_entities(state: AskState):
        u = Understanding.model_validate(state["understanding"])
        resolved = []
        for m in u.mentions:
            r = index.resolve(m.text, m.entity)
            if r.status == "resolved" and not m.plural:
                c = r.candidates[0]
                choice = [c]
                how = "exacto" if c.score == 100 else f"difuso ({c.score:.0f})"
            else:
                opts = [f"{c.label} [{c.entity}]" for c in r.candidates]
                if r.status == "not_found" and not r.candidates:
                    print(f"[resolve] '{m.text}': no encontrado en catálogos")
                    continue
                answer = interrupt({
                    "kind": "ambiguous" if r.status != "not_found" else "not_found",
                    "question": (f"¿A qué te refieres con '{m.text}'?" if r.status != "not_found"
                                 else f"No encontré '{m.text}' exacto. ¿Es alguno de estos?"),
                    "options": opts, "allow_all": True, "plural": m.plural})
                if answer in (None, "none", "ninguno"):
                    continue
                choice = r.candidates if answer == "all" else [r.candidates[int(i)] for i in
                                                               (answer if isinstance(answer, list) else [answer])]
                how = "usuario"
            c0 = choice[0]
            resolved.append({"mention": m.text, "entity": c0.entity, "table": c0.table,
                             "key_column": c0.key_column, "keys": [c.key for c in choice],
                             "labels": [c.label for c in choice], "how": how})
            print(f"[resolve] '{m.text}' → {c0.table}.{c0.key_column} IN {[c.key for c in choice]} "
                  f"({', '.join(c.label for c in choice)}) [{how}]")
        return {"resolved": resolved}

    def plan(state: AskState):
        u = Understanding.model_validate(state["understanding"])
        ents = "\n".join(f"  - '{r['mention']}' = {', '.join(r['labels'])} → filtra {r['table']}.{r['key_column']} "
                         f"{'= ' + repr(r['keys'][0]) if len(r['keys']) == 1 else 'IN ' + repr(r['keys'])}"
                         for r in state.get("resolved", [])) or "  (ninguna)"
        fb = f"\n\nRETROALIMENTACIÓN DEL INTENTO ANTERIOR:\n{state['feedback']}" if state.get("feedback") else ""
        intent_rules = {
            "dato": "Normalmente 1 paso basta.",
            "exploracion": "2-4 pasos: la cifra principal y los desgloses que la expliquen.",
            "hallazgos": ("3-6 pasos. Piensa como auditor/analista: qué patrones serían raros en ESTE negocio "
                          "(concentración en una persona, montos redondos, faltantes de referencia, horarios "
                          "fuera de operación, picos/caídas, productos que no corresponden al tipo de tienda...). "
                          "Cubre CADA proceso (tabla de hechos) relevante al alcance pedido, y para cada uno incluye "
                          "al menos una serie temporal (time_spike) filtrada al alcance del usuario (el detector la "
                          "compara contra su propia historia). Los detectores contra pares (peer_outlier, "
                          "share_outlier) necesitan muchos grupos: ahí no filtres a un solo grupo. Usa periodos de "
                          "comparación cuando ayuden."),
        }[u.intent]
        prompt = (f"{ctx}\n\n{DETECTOR_DOCS}\n\nHOY ES {today.isoformat()}.\nPREGUNTA: {state['question']}\n"
                  f"INTERPRETACIÓN: {u.restated} (intención: {u.intent}; periodo: {u.period_start} a "
                  f"{u.period_end})\nENTIDADES RESUELTAS (usa estos filtros por llave, nunca por nombre):\n{ents}"
                  f"\n\nArma el plan de consultas. {intent_rules}\n{_mode_rules(cfg.sql_mode)}\n"
                  "Reglas spec: metrics = nombres de MÉTRICAS; dimensions/filters = TABLA.COLUMNA; fechas como "
                  "'YYYY-MM-DD' sobre la columna DATE de la dimensión fecha. Reglas sql: Oracle, alias de columnas "
                  "en MAYÚSCULAS, FETCH FIRST n ROWS ONLY en vez de LIMIT, DATE 'YYYY-MM-DD' para literales."
                  + fb)
        p = brain.structured(SYSTEM, prompt, Plan, tag="plan")
        p.steps = p.steps[:8]
        if cfg.sql_mode == "compiled":
            for s in p.steps:
                s.sql = None
        print(f"[plan] {p.approach}  ({len(p.steps)} pasos)")
        for i, s in enumerate(p.steps):
            kind = "spec" if s.spec else "sql"
            print(f"    {i + 1}. [{kind}{'+' + s.detector.type if s.detector else ''}] {s.purpose}")
        return {"plan": p.model_dump(mode="json"), "results": [None] * len(p.steps), "feedback": ""}

    def execute(state: AskState):
        p = Plan.model_validate(state["plan"])
        results = list(state.get("results") or [None] * len(p.steps))
        run_dir = Path(state["run_dir"])
        for i, s in enumerate(p.steps):
            if results[i] is not None and not results[i].get("error"):
                continue
            res = {"purpose": s.purpose, "sql": None, "error": None, "rows": 0, "warnings": []}
            try:
                if s.spec and (cfg.sql_mode != "free" or not s.sql):
                    sql, res["warnings"] = compile_spec(s.spec, model)
                elif s.sql:
                    sql = s.sql
                else:
                    raise CompileError("el paso no tiene spec ni sql")
                res["sql"] = check_sql(sql, model)
                limit = cfg.detector_max_rows if s.detector else cfg.max_rows
                t0 = time.time()
                df, truncated = conn.query(res["sql"], max_rows=limit)
                res.update(rows=len(df), truncated=truncated, seconds=round(time.time() - t0, 2))
                csv = run_dir / f"paso_{i + 1}.csv"
                df.to_csv(csv, index=False, encoding="utf-8-sig")
                res["csv"] = str(csv)
                res["preview"] = _preview(df)
                res["columns"] = list(df.columns)
                if s.detector and not df.empty:
                    try:
                        det = run_detector(s.detector, df)
                        gcol = s.detector.params.get("group_col")
                        for f in det.get("findings", []):  # IDs → nombres legibles para el narrador
                            if gcol and "grupo" in f:
                                lab = index.label_for(gcol, f["grupo"])
                                if lab:
                                    f["nombre"] = lab
                        res["detector"] = json.loads(json.dumps(det, default=str))
                    except Exception as e:  # noqa: BLE001  (error del detector = error del paso → reparar)
                        raise CompileError(f"detector {s.detector.type}: {e}") from e
                print(f"[execute] paso {i + 1}: {len(df)} filas"
                      + (f", hallazgos={len(res['detector']['findings'])}" if res.get("detector") else ""))
            except (CompileError, GuardError) as e:
                res["error"] = str(e)
                print(f"[execute] paso {i + 1}: ERROR validación → {str(e)[:160]}")
            except Exception as e:  # noqa: BLE001  (error de la base: se le regresa al cerebro)
                res["error"] = f"{type(e).__name__}: {str(e)[:600]}"
                print(f"[execute] paso {i + 1}: ERROR base → {str(e)[:160]}")
            results[i] = res
        return {"results": results}

    def route_after_execute(state: AskState):
        res = state["results"]
        if any(r and r.get("error") for r in res) and state.get("repairs", 0) < cfg.max_repairs:
            return "repair"
        ok = [r for r in res if r and not r.get("error")]
        if ok and all(r["rows"] == 0 for r in ok) and state.get("replans", 0) < 1:
            return "replan"
        intent = state["understanding"]["intent"]
        has_findings = any(r.get("detector", {}).get("findings") for r in ok)
        if intent == "hallazgos" and has_findings and state.get("deepened", 0) < 1:
            return "deepen"
        return "narrate"

    def deepen(state: AskState):
        """Como un analista: ante un hallazgo, baja un nivel para explicar QUIÉN/QUÉ/CUÁNDO lo provoca."""
        p = Plan.model_validate(state["plan"])
        summ = []
        for i, r in enumerate(state["results"]):
            if r and r.get("detector", {}).get("findings"):
                summ.append(f"- Paso {i + 1} ({r['purpose']}): "
                            f"{json.dumps(r['detector']['findings'][:4], ensure_ascii=False, default=str)[:900]}")
        prompt = (f"{ctx}\n\n{DETECTOR_DOCS}\n\nHOY ES {today.isoformat()}.\nPREGUNTA: {state['question']}\n"
                  f"PASOS YA EJECUTADOS: {[s.purpose for s in p.steps]}\n\nHALLAZGOS HASTA AHORA:\n"
                  + "\n".join(summ) +
                  "\n\nElige los 1-2 hallazgos más relevantes para la pregunta y planea 1-3 pasos NUEVOS que los "
                  "expliquen bajando un nivel: ¿qué empleado, producto, motivo, hora o periodo los concentra? "
                  "Usa detectores contra pares dentro del alcance del hallazgo (ej. empleados de esa tienda, o "
                  "empleados de toda la cadena). No repitas pasos ya hechos. "
                  f"{_mode_rules(cfg.sql_mode)}")
        extra = brain.structured(SYSTEM, prompt, Plan, tag="deepen")
        steps = extra.steps[:3]
        print(f"[deepen] {extra.approach[:160]} (+{len(steps)} pasos)")
        p.steps += steps
        return {"plan": p.model_dump(mode="json"), "results": list(state["results"]) + [None] * len(steps),
                "deepened": state.get("deepened", 0) + 1}

    def repair(state: AskState):
        p = Plan.model_validate(state["plan"])
        results = list(state["results"])
        for i, r in enumerate(results):
            if not (r and r.get("error")):
                continue
            s = p.steps[i]
            prompt = (f"{ctx}\n\n{DETECTOR_DOCS}\n\nPREGUNTA: {state['question']}\n"
                      f"Este paso falló.\nPROPÓSITO: {s.purpose}\nPASO: {s.model_dump_json(exclude_none=True)}\n"
                      f"SQL GENERADO: {r.get('sql')}\nERROR: {r['error']}\n\n"
                      f"Devuelve el paso corregido. {_mode_rules(cfg.sql_mode)}")
            fixed = brain.structured(SYSTEM, prompt, Step, tag=f"repair:{i + 1}")
            p.steps[i] = fixed
            results[i] = None
            print(f"[repair] paso {i + 1} corregido")
        return {"plan": p.model_dump(mode="json"), "results": results, "repairs": state.get("repairs", 0) + 1}

    def replan(state: AskState):
        sqls = "\n".join(f"- {r['purpose']}: {r['sql']}" for r in state["results"] if r)
        fb = ("Todas las consultas regresaron 0 filas. Revisa fechas (¿el periodo tiene datos?), filtros y "
              f"llaves de entidades. Consultas usadas:\n{sqls}")
        print("[replan] resultados vacíos → replanteando")
        return {"feedback": fb, "replans": state.get("replans", 0) + 1}

    def narrate(state: AskState):
        u = Understanding.model_validate(state["understanding"])
        parts = []
        for i, r in enumerate(state["results"]):
            if not r:
                continue
            block = f"### Paso {i + 1}: {r['purpose']}\n"
            if r.get("error"):
                block += f"ERROR (no se pudo ejecutar): {r['error']}\n"
            else:
                block += f"Filas: {r['rows']}{' (truncado)' if r.get('truncated') else ''}\n{r['preview']}\n"
                if r.get("warnings"):
                    block += f"Advertencias: {r['warnings']}\n"
                if r.get("detector"):
                    d = dict(r["detector"])
                    d.pop("tabla", None)
                    block += f"Detector {d['detector']}: {json.dumps(d, ensure_ascii=False, default=str)[:3000]}\n"
            parts.append(block)
        ents = "; ".join(f"'{r['mention']}' = {', '.join(r['labels'])}" for r in state.get("resolved", []))
        prompt = (f"NEGOCIO: {model.business_summary}\nREGLAS: {model.rules}\n\nPREGUNTA: {state['question']}\n"
                  f"INTERPRETACIÓN: {u.restated}\nENTIDADES: {ents or 'ninguna'}\nHOY: {today}\n\n"
                  f"RESULTADOS:\n{''.join(parts)}\n\n"
                  "Redacta la respuesta para un usuario de negocio. Usa SOLO cifras presentes en los resultados "
                  "(formato de moneda MXN cuando aplique). Si es de hallazgos, cada hallazgo debe citar su "
                  "evidencia numérica y por qué es raro; ordénalos por severidad; distingue lo sospechoso de lo "
                  "operativo. Lenguaje de negocio: NO menciones nombres de tablas, columnas ni IDs técnicos. "
                  "caveats: solo limitaciones que cambien la interpretación (máx. 3); no repitas supuestos "
                  "triviales del modelo.")
        a = brain.structured(SYSTEM, prompt, Answer, tag="narrate")
        return {"answer": a.model_dump()}

    def report(state: AskState):
        path = write_report(state, model, brain.calls)
        print(f"[report] {path}")
        return {"report": str(path)}

    g = StateGraph(AskState)
    for name, fn in [("understand", understand), ("resolve_entities", resolve_entities), ("plan", plan),
                     ("execute", execute), ("repair", repair), ("replan", replan), ("deepen", deepen),
                     ("narrate", narrate),
                     ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "understand")
    g.add_edge("understand", "resolve_entities")
    g.add_edge("resolve_entities", "plan")
    g.add_edge("plan", "execute")
    g.add_conditional_edges("execute", route_after_execute, ["repair", "replan", "deepen", "narrate"])
    g.add_edge("deepen", "execute")
    g.add_edge("repair", "execute")
    g.add_edge("replan", "plan")
    g.add_edge("narrate", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=InMemorySaver())


class Engine:
    """Carga modelo, conexión, cerebro e índice una vez; responde muchas preguntas."""

    def __init__(self, cfg: EngineCfg):
        self.cfg = cfg
        self.model = SemanticModel.load(cfg.semantic_model_path)
        self.conn = make_connector(cfg.connection)
        self.brain = make_brain(cfg.brain)
        self.index = ValueIndex(self.model, self.conn)
        self.graph = build_ask_graph(cfg, self.model, self.conn, self.brain, self.index)

    # ---- API por pasos (para la UI): start → stream → [interrupt → resume → stream] → final ----
    def start(self, question: str) -> tuple[dict, dict]:
        slug = re.sub(r"[^\w]+", "_", question.lower())[:40].strip("_")
        run_dir = Path(self.cfg.output_dir) / f"{time.strftime('%Y%m%d_%H%M%S')}_{slug}"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.brain.calls.clear()
        conf = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 40}
        inp = {"question": question, "run_dir": str(run_dir), "repairs": 0, "replans": 0, "deepened": 0}
        return conf, inp

    def stream(self, inp, conf):
        """Itera (nodo, actualización) mientras el grafo avanza. inp = estado inicial o resume=valor."""
        if not isinstance(inp, dict):
            inp = Command(resume=inp)
        for chunk in self.graph.stream(inp, conf, stream_mode="updates"):
            for node, upd in chunk.items():
                if node != "__interrupt__":
                    yield node, upd

    def pending(self, conf) -> dict | None:
        """Pregunta pendiente para el usuario (desambiguación), si el grafo está en pausa."""
        snap = self.graph.get_state(conf)
        for t in snap.tasks:
            if t.interrupts:
                return t.interrupts[0].value
        return None

    def result(self, conf) -> dict:
        return dict(self.graph.get_state(conf).values)

    def ask(self, question: str, on_interrupt) -> dict:
        conf, inp = self.start(question)
        t0 = time.time()
        out = self.graph.invoke(inp, conf)
        while out.get("__interrupt__"):
            payload = out["__interrupt__"][0].value
            out = self.graph.invoke(Command(resume=on_interrupt(payload)), conf)
        out["seconds"] = round(time.time() - t0, 1)
        out["brain_calls"] = list(self.brain.calls)
        return out


def cli_interrupt(interactive: bool):
    def handler(p: dict):
        print(f"\n❓ {p['question']}")
        for i, o in enumerate(p["options"]):
            print(f"   {i}) {o}")
        if p.get("allow_all"):
            print("   a) todas las anteriores")
        print("   n) ninguna")
        if not interactive:
            ans = "all" if p.get("plural") else "0"
            print(f"   → (no interactivo) elijo: {ans}")
        else:
            ans = input("   tu elección: ").strip().lower()
        if ans in ("a", "all", "todas"):
            return "all"
        if ans in ("n", "none", "ninguna", ""):
            return "none"
        return [int(x) for x in re.findall(r"\d+", ans)] or "none"
    return handler


def run_question(cfg: EngineCfg, question: str, interactive: bool = True) -> dict:
    eng = Engine(cfg)
    out = eng.ask(question, cli_interrupt(interactive))
    a = out.get("answer", {})
    print("\n" + "=" * 80 + f"\n{a.get('summary', '')}\n")
    for k in a.get("key_numbers", []):
        print(f"  • {k['label']}: {k['value']}")
    for f in a.get("findings", []):
        print(f"  [{f['severity'].upper()}] {f['title']} — {f['detail']}")
    for c in a.get("caveats", []):
        print(f"  ⚠ {c}")
    print(f"\nReporte: {out.get('report')}  ({out['seconds']}s, {len(out['brain_calls'])} llamadas al cerebro)")
    return out
