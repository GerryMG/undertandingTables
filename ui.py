"""Interfaz web del motor:  streamlit run ui.py

Páginas: Preguntar · Modelo semántico · Configuración · Reportes · Evaluación
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yaml

from dw_engine.ask import Engine
from dw_engine.brains import make_brain
from dw_engine.config import EngineCfg
from dw_engine.db import make_connector
from dw_engine.diagrams import write_docs
from dw_engine.onboarding import build_onboarding_graph, validate_model
from dw_engine.report import _auto_chart
from dw_engine.semantic import Entity, Metric, OpenQuestion, SemanticModel

for _s in (sys.stdout, sys.stderr):  # la consola de Windows (cp1252) no soporta "→" de los logs del motor
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

CONFIG_PATH = Path("config/engine.yaml")
EVAL_PATH = Path("tests/eval.yaml")

st.set_page_config(page_title="DW Engine", page_icon="🧭", layout="wide")


# ======================================================================= helpers
def read_cfg_dict() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def write_cfg_dict(d: dict) -> None:
    EngineCfg.model_validate(d)  # valida antes de escribir
    CONFIG_PATH.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")


def cfg() -> EngineCfg:
    return EngineCfg.model_validate(read_cfg_dict())


def mtime(p) -> float:
    p = Path(p)
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_resource(show_spinner="Cargando motor (modelo semántico, conexión e índice de nombres)…")
def _engine(_cfg_mtime: float, _model_mtime: float) -> Engine:
    return Engine(cfg())


def engine() -> Engine:
    c = cfg()
    return _engine(mtime(CONFIG_PATH), mtime(c.semantic_model_path))


def model_exists() -> bool:
    return Path(cfg().semantic_model_path).exists()


def csv_list(xs) -> str:
    return ", ".join(xs or [])


def list_csv(s) -> list[str]:
    return [x.strip() for x in str(s or "").split(",") if x.strip()]


def none_if_blank(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "" else str(v).strip()


# ======================================================================= Preguntar
EXAMPLES = ["¿Cuánto vendió Polanco ayer?",
            "Top 5 productos por venta en la tienda Satélite en octubre",
            "¿Qué movimientos te parecen raros en las devoluciones de los últimos 6 meses?",
            "Busca hallazgos en la línea lácteos de la tienda Zapopan"]


def node_msg(node: str, upd: dict) -> str | None:
    if node == "understand":
        u = upd["understanding"]
        per = f" · periodo {u['period_start']} → {u['period_end']}" if u.get("period_start") else ""
        return f"🧩 **Entendí** ({u['intent']}): {u['restated']}{per}"
    if node == "resolve_entities":
        rs = upd.get("resolved", [])
        return "🔎 **Entidades:** " + ("; ".join(f"'{r['mention']}' → {', '.join(r['labels'])} ({r['how']})"
                                               for r in rs) or "ninguna que buscar")
    if node == "plan":
        p = upd["plan"]
        steps = "\n".join(f"   {i + 1}. {s['purpose']}" for i, s in enumerate(p["steps"]))
        return f"🗺️ **Plan:** {p['approach']}\n{steps}"
    if node == "execute":
        rs = [r for r in upd["results"] if r]
        err = sum(1 for r in rs if r.get("error"))
        fnd = sum(len(r.get("detector", {}).get("findings", [])) for r in rs if not r.get("error"))
        return f"⚙️ **Ejecuté** {len(rs)} consultas · {err} con error · {fnd} señales de detectores"
    if node == "repair":
        return "🔧 **Corrigiendo** consultas con el error que regresó la base"
    if node == "replan":
        return "↩️ **Sin resultados:** replanteando filtros y fechas"
    if node == "deepen":
        return f"🔬 **Profundizando** en los hallazgos ({len(upd['plan']['steps'])} pasos en total)"
    if node == "narrate":
        return "✍️ **Redactando** la respuesta"
    if node == "report":
        return "📄 **Reporte** generado"
    return None


def advance(run: dict, inp) -> None:
    """Avanza el grafo (inicio o reanudación) mostrando el progreso en vivo."""
    eng = engine()
    with st.status("Analizando…", expanded=True) as status:
        try:
            for node, upd in eng.stream(inp, run["conf"]):
                if not upd:  # nodo reanudado tras una pausa puede emitir una actualización vacía
                    continue
                msg = node_msg(node, upd)
                if msg:
                    run["log"].append(msg)
                    st.markdown(msg)
            pend = eng.pending(run["conf"])
            if pend:
                run.update(status="waiting", pending=pend)
                status.update(label="Necesito que me aclares algo", state="complete")
            else:
                run.update(status="done", pending=None, result=eng.result(run["conf"]),
                           calls=list(eng.brain.calls))
                status.update(label="Listo", state="complete", expanded=False)
        except Exception as e:  # noqa: BLE001
            import traceback
            run.update(status="error", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
            status.update(label="Error", state="error")


def render_answer(run: dict, idx: int) -> None:
    res = run["result"]
    a = res.get("answer") or {}
    st.markdown(a.get("summary", ""))
    kn = a.get("key_numbers", [])
    for i in range(0, len(kn), 4):
        cols = st.columns(4)
        for c, k in zip(cols, kn[i:i + 4]):
            c.metric(k["label"], k["value"])
    if a.get("findings"):
        st.markdown("#### Hallazgos")
        sev = {"alta": "🔴", "media": "🟠", "baja": "🟡"}
        for f in a["findings"]:
            st.markdown(f"{sev.get(f['severity'], '')} **{f['title']}**  \n{f['detail']}")
    for c in a.get("caveats", []):
        st.caption(f"⚠️ {c}")
    if a.get("follow_ups"):
        st.markdown("**Siguientes preguntas sugeridas**")
        for j, q in enumerate(a["follow_ups"][:4]):
            if st.button(q, key=f"fu_{idx}_{j}"):
                st.session_state.queued_question = q
                st.rerun()

    plan = res.get("plan", {})
    with st.expander(f"Evidencia: {len(res.get('results') or [])} consultas"):
        for i, r in enumerate(res.get("results") or []):
            if not r:
                continue
            st.markdown(f"**{i + 1}. {r['purpose']}**")
            if r.get("error"):
                st.error(r["error"])
            elif r.get("csv") and Path(r["csv"]).exists():
                df = pd.read_csv(r["csv"])
                kind = plan["steps"][i].get("chart", "auto") if i < len(plan.get("steps", [])) else "auto"
                fig = _auto_chart(df, kind)
                if fig is not None:
                    fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10))
                    st.plotly_chart(fig, use_container_width=True, key=f"fig_{idx}_{i}")
                st.dataframe(df.head(200), use_container_width=True, hide_index=True)
                c1, c2 = st.columns([1, 3])
                c1.download_button("CSV", Path(r["csv"]).read_bytes(), file_name=Path(r["csv"]).name,
                                   key=f"csv_{idx}_{i}")
                c2.caption(f"{r['rows']} filas{' (truncado)' if r.get('truncated') else ''}")
                if r.get("detector"):
                    d = r["detector"]
                    st.caption(f"Detector **{d['detector']}** → {len(d.get('findings', []))} señales")
                    if d.get("findings"):
                        st.dataframe(pd.DataFrame(d["findings"]), use_container_width=True, hide_index=True)
            if r.get("sql"):
                st.code(r["sql"], language="sql")
    with st.expander("Cómo lo resolví"):
        st.markdown("\n\n".join(run["log"]))
        calls = run.get("calls", [])
        st.caption(f"{len(calls)} llamadas al cerebro · {sum(c['seconds'] for c in calls):.0f}s")
    if res.get("report") and Path(res["report"]).exists():
        st.download_button("⬇️ Reporte HTML", Path(res["report"]).read_bytes(),
                           file_name="reporte.html", mime="text/html", key=f"rep_{idx}")


def page_ask():
    st.title("💬 Preguntar")
    if not model_exists():
        st.warning("Primero genera el modelo semántico en **Modelo semántico**.")
        return
    try:
        eng = engine()
    except Exception as e:  # noqa: BLE001
        st.error(f"No pude cargar el motor: {e}")
        st.info("Revisa **Configuración** (conexión y cerebro).")
        return
    st.caption(f"Cerebro: `{eng.cfg.brain.provider}/{eng.cfg.brain.model}` · SQL: `{eng.cfg.sql_mode}` · "
               f"hoy = {eng.cfg.ref_date()}")
    runs = st.session_state.setdefault("runs", [])

    if not runs:
        st.markdown("Pregunta en lenguaje natural. Ejemplos:")
        cols = st.columns(2)
        for i, q in enumerate(EXAMPLES):
            if cols[i % 2].button(q, key=f"ex_{i}", use_container_width=True):
                st.session_state.queued_question = q
                st.rerun()

    for idx, run in enumerate(runs):
        with st.chat_message("user"):
            st.markdown(run["question"])
        with st.chat_message("assistant"):
            if run["status"] == "done":
                render_answer(run, idx)
            elif run["status"] == "error":
                st.error(run["error"])
                if run.get("trace"):
                    with st.expander("Detalle técnico"):
                        st.code(run["trace"])
            elif run["status"] == "waiting":
                for m in run["log"]:
                    st.markdown(m)
                p = run["pending"]
                st.info(f"❓ {p['question']}")
                opts = list(p["options"]) + (["Todas las anteriores"] if p.get("allow_all") else []) + ["Ninguna"]
                choice = st.radio("Elige una opción", opts, key=f"choice_{idx}",
                                  index=len(p["options"]) if p.get("plural") and p.get("allow_all") else 0)
                if st.button("Continuar", key=f"cont_{idx}", type="primary"):
                    if choice == "Todas las anteriores":
                        ans = "all"
                    elif choice == "Ninguna":
                        ans = "none"
                    else:
                        ans = [opts.index(choice)]
                    advance(run, ans)
                    st.rerun()

    q = st.chat_input("Ej. ¿Cuánto vendió el restaurante Polanco el 3 de marzo?")
    q = q or st.session_state.pop("queued_question", None)
    if q:
        conf, inp = eng.start(q)
        run = {"question": q, "conf": conf, "status": "running", "log": []}
        runs.append(run)
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            advance(run, inp)
        st.rerun()
    if runs and st.button("Limpiar conversación"):
        st.session_state.runs = []
        st.rerun()


# ======================================================================= Modelo semántico
def run_onboarding():
    c = cfg()
    try:
        conn, brain = make_connector(c.connection), make_brain(c.brain)
    except Exception as e:  # noqa: BLE001
        st.error(f"No pude conectar: {e}")
        return
    graph = build_onboarding_graph(c, conn, brain)
    with st.status("Aprendiendo el negocio…", expanded=True) as s:
        try:
            for chunk in graph.stream({}, stream_mode="updates"):
                for node, upd in chunk.items():
                    if node == "harvest":
                        s.write(f"📥 {len(upd['tables'])} tablas visibles: "
                                f"{', '.join(t['name'] for t in upd['tables'])}")
                    elif node == "profile":
                        s.write("📊 Perfilado de columnas (cardinalidad, nulos, rangos, valores frecuentes)")
                    elif node == "relationships":
                        s.write(f"🔗 {len(upd['relationships'])} relaciones encontradas y verificadas con datos")
                    elif node == "enrich_table":
                        d = upd["table_docs"][0]
                        s.write(f"📝 {d['name']} → **{d['business_name']}** ({d['role']})")
                    elif node == "synthesize":
                        syn = upd["synthesis"]
                        s.write(f"🧠 {len(syn['metrics'])} métricas · {len(syn['open_questions'])} preguntas abiertas")
                    elif node == "validate" and upd.get("errors"):
                        s.write(f"🔁 Corrigiendo {len(upd['errors'])} errores de validación")
                    elif node == "publish":
                        s.write("✅ Modelo publicado")
            s.update(label="Modelo generado", state="complete", expanded=False)
        except Exception as e:  # noqa: BLE001
            s.update(label="Falló el onboarding", state="error")
            st.error(f"{type(e).__name__}: {e}")
            return
    st.cache_resource.clear()
    st.session_state.pop("model_edit", None)


def page_model():
    st.title("🧠 Modelo semántico")
    c = cfg()
    path = Path(c.semantic_model_path)
    with st.expander("Generar / regenerar con el cerebro (onboarding)", expanded=not path.exists()):
        st.markdown("Lee la estructura de la base, perfila los datos, descubre y verifica relaciones, y el cerebro "
                    "documenta cada tabla y propone métricas, reglas y preguntas abiertas.")
        overwrite = True
        if path.exists():
            overwrite = st.checkbox("Sobrescribir el modelo actual (se pierde lo que hayas editado)")
        if st.button("Ejecutar onboarding", type="primary", disabled=not overwrite):
            run_onboarding()
            st.rerun()
    if not path.exists():
        return

    key = f"{path}:{mtime(path)}"
    if st.session_state.get("model_key") != key:
        st.session_state.model_edit = SemanticModel.load(path)
        st.session_state.model_key = key
    m: SemanticModel = st.session_state.model_edit

    open_q = sum(1 for q in m.open_questions if not q.answer)
    t_doc, t_q, t_met, t_tab, t_ent, t_yaml = st.tabs(
        ["Resumen y diagramas", f"Preguntas abiertas ({open_q})", f"Métricas ({len(m.metrics)})",
         f"Tablas ({len(m.tables)})", f"Entidades ({len(m.entities)})", "YAML"])

    with t_doc:
        m.business_summary = st.text_area("Resumen del negocio", m.business_summary, height=110)
        html_path = path.parent / "modelo.html"
        if html_path.exists():
            components.html(html_path.read_text(encoding="utf-8"), height=900, scrolling=True)

    with t_q:
        st.caption("Lo que el cerebro no pudo saber con certeza. Tus respuestas se vuelven reglas de negocio.")
        for i, q in enumerate(m.open_questions):
            st.markdown(f"**{i + 1}. {q.question}**")
            st.caption(f"Propuesta: {q.proposed_answer}")
            q.answer = none_if_blank(st.text_input("Respuesta", q.answer or "", key=f"oq_{i}",
                                                   label_visibility="collapsed",
                                                   placeholder="Escribe la respuesta correcta (o acepta la propuesta)"))
        rules = st.text_area("Reglas de negocio (una por línea)", "\n".join(m.rules), height=220)
        m.rules = [r.strip() for r in rules.splitlines() if r.strip()]

    with t_met:
        st.caption("`expression` = agregación Oracle sobre columnas de `table`. `derived` = fórmula con "
                   "{otras_métricas}. Puedes agregar filas.")
        df = pd.DataFrame([{**x.model_dump(), "synonyms": csv_list(x.synonyms)} for x in m.metrics],
                          columns=["name", "business_name", "table", "expression", "derived", "format", "additive",
                                   "synonyms", "description"])
        ed = st.data_editor(df, num_rows="dynamic", use_container_width=True, hide_index=True, key="met_ed",
                            column_config={
                                "format": st.column_config.SelectboxColumn(options=["money", "number", "percent",
                                                                                    "ratio"]),
                                "additive": st.column_config.SelectboxColumn(options=["additive", "semi_additive",
                                                                                      "non_additive"]),
                                "table": st.column_config.SelectboxColumn(options=[t.name for t in m.tables])})
        mets = []
        for r in ed.to_dict("records"):
            if none_if_blank(r.get("name")):
                mets.append(Metric(name=str(r["name"]).strip(), business_name=none_if_blank(r["business_name"]) or "",
                                   table=none_if_blank(r["table"]), expression=none_if_blank(r["expression"]),
                                   derived=none_if_blank(r["derived"]), format=r.get("format") or "number",
                                   additive=r.get("additive") or "additive", synonyms=list_csv(r["synonyms"]),
                                   description=none_if_blank(r["description"]) or ""))
        m.metrics = mets

    with t_tab:
        tname = st.selectbox("Tabla", [t.name for t in m.tables],
                             format_func=lambda n: f"{n} — {m.table(n).business_name} [{m.table(n).role}]")
        t = m.table(tname)
        c1, c2, c3 = st.columns([2, 1, 1])
        t.business_name = c1.text_input("Nombre de negocio", t.business_name, key=f"tbn_{tname}")
        roles = ["fact", "dimension", "bridge", "lookup", "audit", "staging", "other"]
        t.role = c2.selectbox("Rol", roles, index=roles.index(t.role), key=f"trole_{tname}")
        t.use_in_answers = c3.checkbox("Usar en respuestas", t.use_in_answers, key=f"tuse_{tname}")
        t.description = st.text_area("Descripción", t.description, key=f"tdesc_{tname}", height=80)
        if t.role == "fact":
            t.grain = st.text_input("Grano (qué es una fila)", t.grain, key=f"tgrain_{tname}")
        cdf = pd.DataFrame([{"name": col.name, "data_type": col.data_type, "business_name": col.business_name,
                             "role": col.role, "description": col.description, "synonyms": csv_list(col.synonyms),
                             "values": json.dumps(col.values, ensure_ascii=False) if col.values else "",
                             "samples": " | ".join(col.samples[:5])} for col in t.columns])
        ced = st.data_editor(cdf, use_container_width=True, hide_index=True, key=f"cols_{tname}",
                             disabled=["name", "data_type", "samples"],
                             column_config={"role": st.column_config.SelectboxColumn(options=[
                                 "key", "foreign_key", "measure", "attribute", "date", "time", "degenerate", "flag",
                                 "code", "other"]),
                                 "values": st.column_config.TextColumn(help='Códigos: {"R": "Restaurante"}')})
        for col, r in zip(t.columns, ced.to_dict("records")):
            col.business_name, col.role = r["business_name"] or "", r["role"] or "other"
            col.description, col.synonyms = r["description"] or "", list_csv(r["synonyms"])
            try:
                col.values = json.loads(r["values"]) if none_if_blank(r["values"]) else {}
            except json.JSONDecodeError:
                st.warning(f"{col.name}: `values` no es JSON válido; se conserva el anterior")

    with t_ent:
        st.caption("Cosas con nombre que la gente menciona. `label_columns` = columnas donde se busca el nombre.")
        edf = pd.DataFrame([{**e.model_dump(), "synonyms": csv_list(e.synonyms),
                             "label_columns": csv_list(e.label_columns)} for e in m.entities],
                           columns=["name", "synonyms", "table", "key_column", "label_columns", "description"])
        eed = st.data_editor(edf, num_rows="dynamic", use_container_width=True, hide_index=True, key="ent_ed",
                             column_config={"table": st.column_config.SelectboxColumn(
                                 options=[t.name for t in m.tables])})
        m.entities = [Entity(name=str(r["name"]).strip(), synonyms=list_csv(r["synonyms"]), table=str(r["table"]),
                             key_column=str(r["key_column"]).upper(),
                             label_columns=[x.upper() for x in list_csv(r["label_columns"])],
                             description=none_if_blank(r["description"]) or "")
                      for r in eed.to_dict("records") if none_if_blank(r.get("name")) and none_if_blank(r.get("table"))]

    with t_yaml:
        st.caption("Edición directa (avanzado). Al guardar aquí se ignoran los cambios de las otras pestañas.")
        raw = st.text_area("model.yaml", path.read_text(encoding="utf-8"), height=500, key="raw_yaml")
        if st.button("Guardar YAML"):
            try:
                mm = SemanticModel.model_validate(yaml.safe_load(raw))
                errs = validate_model(mm)
                if errs:
                    st.error("No guardé: " + "; ".join(errs[:8]))
                else:
                    mm.save(path)
                    write_docs(mm, path.parent)
                    st.cache_resource.clear()
                    st.success("Guardado")
                    st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"YAML inválido: {e}")

    st.divider()
    if st.button("💾 Guardar cambios del modelo", type="primary"):
        errs = validate_model(m)
        if errs:
            st.error("Hay errores, no guardé:\n\n- " + "\n- ".join(errs[:12]))
        else:
            m.save(path)
            write_docs(m, path.parent)
            st.cache_resource.clear()
            st.toast("Modelo guardado; el motor se recargará con los cambios")
            st.rerun()


# ======================================================================= Configuración
PROVIDERS = {"openai_compat": "Compatible OpenAI (vLLM / Ollama / LM Studio / OpenAI)",
             "anthropic": "Anthropic API", "claude_cli": "Claude Code CLI (pruebas)"}


def page_config():
    st.title("⚙️ Configuración")
    d = read_cfg_dict()
    conn, brain = dict(d.get("connection", {})), dict(d.get("brain", {}))
    priv = dict(d.get("privacy", {}))

    st.subheader("Base de datos")
    kinds = ["oracle", "duckdb"]
    conn["kind"] = st.selectbox("Tipo", kinds, index=kinds.index(conn.get("kind", "oracle")),
                                format_func=lambda k: {"oracle": "Oracle", "duckdb": "DuckDB (pruebas)"}[k])
    if conn["kind"] == "oracle":
        c1, c2 = st.columns(2)
        conn["dsn"] = c1.text_input("DSN (host:puerto/servicio)", conn.get("dsn") or "")
        conn["current_schema"] = none_if_blank(c2.text_input("Esquema (opcional)", conn.get("current_schema") or ""))
        c1, c2 = st.columns(2)
        conn["user_env"] = c1.text_input("Variable de entorno del usuario", conn.get("user_env", "DW_USER"))
        conn["password_env"] = c2.text_input("Variable de entorno de la contraseña",
                                             conn.get("password_env", "DW_PASSWORD"))
        with st.expander("Credenciales solo para esta sesión (no se guardan en disco)"):
            u = st.text_input("Usuario", os.environ.get(conn["user_env"], ""))
            pw = st.text_input("Contraseña", type="password")
            if st.button("Usar en esta sesión"):
                os.environ[conn["user_env"]] = u
                if pw:
                    os.environ[conn["password_env"]] = pw
                st.cache_resource.clear()
                st.success("Credenciales cargadas en memoria")
    else:
        conn["path"] = st.text_input("Archivo DuckDB", conn.get("path") or "data/test_dw.duckdb")
    c1, c2 = st.columns(2)
    conn["include_tables"] = list_csv(c1.text_input("Solo estas tablas (vacío = todas las visibles)",
                                                    csv_list(conn.get("include_tables"))))
    conn["exclude_patterns"] = list_csv(c2.text_input("Excluir patrones", csv_list(conn.get("exclude_patterns"))))

    st.subheader("Cerebro")
    provs = list(PROVIDERS)
    brain["provider"] = st.selectbox("Proveedor", provs, index=provs.index(brain.get("provider", "openai_compat")),
                                     format_func=PROVIDERS.get)
    c1, c2 = st.columns(2)
    brain["model"] = c1.text_input("Modelo", brain.get("model", ""))
    if brain["provider"] == "openai_compat":
        brain["base_url"] = none_if_blank(c2.text_input("Base URL (vacío = OpenAI)", brain.get("base_url") or "",
                                                        placeholder="http://gpu-server:8000/v1"))
    if brain["provider"] in ("openai_compat", "anthropic"):
        c1, c2 = st.columns(2)
        brain["api_key_env"] = none_if_blank(c1.text_input("Variable de entorno de la API key",
                                                           brain.get("api_key_env") or ""))
        key = c2.text_input("API key solo para esta sesión (no se guarda)", type="password")
        if key and brain.get("api_key_env"):
            os.environ[brain["api_key_env"]] = key
    c1, c2, c3 = st.columns(3)
    brain["timeout_s"] = c1.number_input("Timeout (s)", 10, 3600, int(brain.get("timeout_s", 300)))
    brain["max_parallel"] = c2.number_input("Llamadas en paralelo", 1, 32, int(brain.get("max_parallel", 4)))
    brain["temperature"] = c3.number_input("Temperatura", 0.0, 1.0, float(brain.get("temperature", 0.0)), 0.1)

    st.subheader("Comportamiento")
    modes = ["hybrid", "compiled", "free"]
    d["sql_mode"] = st.radio("Modo SQL", modes, index=modes.index(d.get("sql_mode", "hybrid")), horizontal=True,
                             captions=["spec primero, SQL libre si hace falta",
                                       "solo compilador (más seguro, modelos chicos)",
                                       "SQL libre (modelos grandes)"])
    c1, c2, c3 = st.columns(3)
    d["max_rows"] = c1.number_input("Filas máx. por consulta", 100, 1_000_000, int(d.get("max_rows", 5000)))
    d["max_repairs"] = c2.number_input("Reintentos de corrección", 0, 10, int(d.get("max_repairs", 3)))
    fixed = c3.checkbox("Fecha de referencia fija", d.get("reference_date", "auto") != "auto",
                        help="Para pruebas reproducibles; si no, 'hoy'")
    if fixed:
        cur = d.get("reference_date")
        d["reference_date"] = c3.date_input("Hoy es", date.fromisoformat(cur) if cur not in (None, "auto")
                                            else date.today()).isoformat()
    else:
        d["reference_date"] = "auto"
    priv["send_sample_values"] = st.checkbox("Enviar valores de ejemplo al cerebro",
                                             priv.get("send_sample_values", True),
                                             help="Si se desactiva, el cerebro solo ve metadata (nombres, tipos, "
                                                  "estadísticas), nunca datos")
    d["business_context"] = st.text_area("Contexto del negocio (ayuda al onboarding)",
                                         d.get("business_context", ""), height=80)
    hints = st.text_area("Pistas de nombres (una por línea: ABREV=significado)",
                         "\n".join(f"{k}={v}" for k, v in (d.get("naming_hints") or {}).items()), height=80)
    d["naming_hints"] = {k.strip(): v.strip() for k, v in
                         (ln.split("=", 1) for ln in hints.splitlines() if "=" in ln)}

    d["connection"], d["brain"], d["privacy"] = conn, brain, priv
    st.divider()
    c1, c2, c3 = st.columns(3)
    if c1.button("💾 Guardar configuración", type="primary", use_container_width=True):
        try:
            write_cfg_dict(d)
            st.cache_resource.clear()
            st.success("Configuración guardada")
        except Exception as e:  # noqa: BLE001
            st.error(f"Configuración inválida: {e}")
    if c2.button("🔌 Probar conexión", use_container_width=True):
        try:
            tabs = make_connector(EngineCfg.model_validate(d).connection).introspect()
            st.success(f"Conectado. {len(tabs)} tablas visibles: {', '.join(t.name for t in tabs[:30])}")
        except Exception as e:  # noqa: BLE001
            st.error(f"No conecta: {e}")
    if c3.button("🧠 Probar cerebro", use_container_width=True):
        from pydantic import BaseModel

        class Ping(BaseModel):
            ok: bool
            modelo: str

        try:
            with st.spinner("Preguntando al cerebro…"):
                b = make_brain(EngineCfg.model_validate(d).brain)
                r = b.structured("Responde en JSON.", "Di ok=true y tu nombre de modelo.", Ping, tag="ping")
            st.success(f"Responde ({b.calls[-1]['seconds']}s): {r.model_dump()}")
        except Exception as e:  # noqa: BLE001
            st.error(f"El cerebro no responde: {e}")


# ======================================================================= Reportes
def page_reports():
    st.title("📊 Reportes")
    out = Path(cfg().output_dir)
    runs = sorted([p for p in out.glob("*") if (p / "reporte.html").exists()], reverse=True) if out.exists() else []
    if not runs:
        st.info("Aún no hay reportes. Haz una pregunta en **Preguntar**.")
        return

    def label(p: Path) -> str:
        try:
            q = json.loads((p / "respuesta.json").read_text(encoding="utf-8"))["question"]
        except Exception:  # noqa: BLE001
            q = p.name
        return f"{p.name[:15].replace('_', ' ')} · {q}"

    sel = st.selectbox("Reporte", runs, format_func=label)
    c1, c2 = st.columns([1, 4])
    c1.download_button("⬇️ HTML", (sel / "reporte.html").read_bytes(), file_name=f"{sel.name}.html",
                       mime="text/html")
    with c2:
        for csv in sorted(sel.glob("*.csv")):
            st.download_button(f"⬇️ {csv.name}", csv.read_bytes(), file_name=csv.name, key=str(csv))
    components.html((sel / "reporte.html").read_text(encoding="utf-8"), height=1100, scrolling=True)


# ======================================================================= Evaluación
def page_eval():
    from dw_engine.evaluate import eval_case, save_rows
    st.title("✅ Evaluación")
    st.markdown("Preguntas con respuesta conocida para medir objetivamente el cerebro y la configuración "
                "actuales. Cambia de cerebro en **Configuración**, vuelve a correr y compara.")
    cases = yaml.safe_load(EVAL_PATH.read_text(encoding="utf-8")) if EVAL_PATH.exists() else []
    t_run, t_edit, t_hist = st.tabs(["Correr", "Editar preguntas", "Historial"])
    with t_run:
        sel = st.multiselect("Preguntas", [c["id"] for c in cases], default=[c["id"] for c in cases],
                             format_func=lambda i: f"{i}. {next(c['question'] for c in cases if c['id'] == i)}")
        st.caption(f"≈ {len(sel)} min con un cerebro lento; mucho menos con un modelo local rápido.")
        if st.button("▶️ Correr evaluación", type="primary", disabled=not sel or not model_exists()):
            eng = engine()
            rows, bar, box = [], st.progress(0.0), st.empty()
            todo = [c for c in cases if c["id"] in sel]
            for i, case in enumerate(todo):
                bar.progress(i / len(todo), text=f"[{case['id']}] {case['question']}")
                rows.append(eval_case(eng, case))
                box.dataframe(pd.DataFrame(rows).drop(columns=["reporte"]), hide_index=True,
                              use_container_width=True)
            bar.progress(1.0, text="Terminado")
            ok = sum(r["ok"] for r in rows)
            st.metric("Aciertos", f"{ok}/{len(rows)}", f"{ok / len(rows):.0%}")
            st.caption(f"Guardado en {save_rows(cfg(), rows)}")
    with t_edit:
        raw = st.text_area("tests/eval.yaml", EVAL_PATH.read_text(encoding="utf-8") if EVAL_PATH.exists() else "",
                           height=520)
        if st.button("Guardar preguntas"):
            try:
                yaml.safe_load(raw)
                EVAL_PATH.write_text(raw, encoding="utf-8")
                st.success("Guardado")
            except yaml.YAMLError as e:
                st.error(f"YAML inválido: {e}")
    with t_hist:
        hist = []
        for p in sorted(Path(cfg().output_dir).glob("eval_*.json"), reverse=True):
            data = json.loads(p.read_text(encoding="utf-8"))
            rows = data["rows"] if isinstance(data, dict) else data
            hist.append({"corrida": p.stem[5:], "cerebro": data.get("brain", "?") if isinstance(data, dict) else "?",
                         "modo": data.get("sql_mode", "?") if isinstance(data, dict) else "?",
                         "aciertos": f"{sum(r['ok'] for r in rows)}/{len(rows)}",
                         "preguntas": ", ".join(str(r["id"]) for r in rows)})
        st.dataframe(pd.DataFrame(hist), hide_index=True, use_container_width=True)


# ======================================================================= navegación
def sidebar_status():
    try:
        c = cfg()
    except Exception as e:  # noqa: BLE001
        st.sidebar.error(f"config inválida: {e}")
        return
    st.sidebar.markdown("### Estado")
    target = c.connection.dsn if c.connection.kind == "oracle" else c.connection.path
    st.sidebar.caption(f"🗄️ {c.connection.kind}: `{target}`")
    st.sidebar.caption(f"🧠 {c.brain.provider}: `{c.brain.model}`")
    if model_exists():
        m = SemanticModel.load(c.semantic_model_path)
        pend = sum(1 for q in m.open_questions if not q.answer)
        st.sidebar.caption(f"📘 Modelo: {len(m.tables)} tablas · {len(m.metrics)} métricas · "
                           f"{pend} preguntas abiertas")
    else:
        st.sidebar.caption("📘 Modelo: no generado")


pg = st.navigation([st.Page(page_ask, title="Preguntar", icon="💬", default=True),
                    st.Page(page_model, title="Modelo semántico", icon="🧠"),
                    st.Page(page_config, title="Configuración", icon="⚙️"),
                    st.Page(page_reports, title="Reportes", icon="📊"),
                    st.Page(page_eval, title="Evaluación", icon="✅")])
sidebar_status()
pg.run()
