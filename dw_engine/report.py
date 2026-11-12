"""Reporte por pregunta: HTML autocontenido (respuesta, hallazgos, gráficas, SQL) + CSV por paso + JSON."""
from __future__ import annotations

import html
import json
from pathlib import Path

import markdown as md
import pandas as pd
import plotly.express as px

from .semantic import SemanticModel


def _auto_chart(df: pd.DataFrame, kind: str):
    if df.empty or kind in ("none", "table") or len(df.columns) < 2:
        return None
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            conv = pd.to_numeric(df[c], errors="coerce")
            if conv.notna().mean() > 0.9:
                df[c] = conv
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cats = [c for c in df.columns if c not in nums]
    datey = [c for c in df.columns if any(k in c.upper() for k in ("FEC", "FECHA", "DATE", "MES", "SEMANA", "DIA"))]
    if not nums:
        return None
    y = [c for c in nums if c not in datey][:3] or nums[:1]
    x = next((c for c in datey if c not in y), None)
    if x is not None and (kind in ("auto", "line")) and df[x].nunique() > 3:
        color = next((c for c in cats if c != x and df[c].nunique() <= 15), None)
        d = df.sort_values(x)
        d[x] = d[x].astype(str)
        return px.line(d, x=x, y=y[0], color=color, markers=df[x].nunique() < 40)
    if cats:
        d = df.head(30)
        d = d.assign(**{cats[0]: d[cats[0]].astype(str)})
        return px.bar(d, x=cats[0], y=y[0], color=cats[1] if len(cats) > 1 and d[cats[1]].nunique() <= 12
                      else None)
    if len(nums) >= 2 and len(df) > 1:
        d = df.head(60)
        return px.bar(d.assign(**{nums[0]: d[nums[0]].astype(str)}), x=nums[0], y=nums[1])
    return None


def write_report(state: dict, model: SemanticModel, calls: list[dict]) -> Path:
    run_dir = Path(state["run_dir"])
    a = state.get("answer", {})
    plan = state.get("plan", {})
    e = html.escape
    first_js = True
    steps_html = ""
    for i, r in enumerate(state.get("results") or []):
        if not r:
            continue
        step = plan["steps"][i] if i < len(plan.get("steps", [])) else {}
        body = ""
        if r.get("error"):
            body = f"<p class='err'>Error: {e(r['error'])}</p>"
        else:
            df = pd.read_csv(r["csv"]) if r.get("csv") else pd.DataFrame()
            fig = _auto_chart(df, step.get("chart", "auto"))
            if fig is not None:
                fig.update_layout(margin=dict(l=10, r=10, t=30, b=10), height=360, template="plotly_white")
                body += fig.to_html(full_html=False, include_plotlyjs="cdn" if first_js else False)
                first_js = False
            body += (f"<div class='wrap'>{df.head(50).to_html(index=False, classes='t', border=0)}</div>"
                     f"<p class='mut'>{r['rows']} filas{' (truncado)' if r.get('truncated') else ''} · "
                     f"<a href='{Path(r['csv']).name}'>descargar CSV</a></p>")
            if r.get("detector"):
                det = r["detector"]
                body += (f"<p><b>Detector {e(det['detector'])}</b> → {len(det.get('findings', []))} hallazgos</p>"
                         f"<pre>{e(json.dumps(det.get('findings', []), ensure_ascii=False, indent=1)[:4000])}</pre>")
        if r.get("sql"):
            body += f"<details><summary>SQL (Oracle)</summary><pre>{e(r['sql'])}</pre></details>"
        steps_html += f"<section><h3>{i + 1}. {e(r['purpose'])}</h3>{body}</section>"

    sev = {"alta": "🔴", "media": "🟠", "baja": "🟡"}
    findings = "".join(f"<li>{sev.get(f['severity'], '')} <b>{e(f['title'])}</b><br>{e(f['detail'])}</li>"
                       for f in a.get("findings", []))
    kn = "".join(f"<div class='kpi'><div class='v'>{e(k['value'])}</div><div class='l'>{e(k['label'])}</div></div>"
                 for k in a.get("key_numbers", []))
    ents = "".join(f"<li>'{e(r['mention'])}' → {e(', '.join(r['labels']))} <span class='mut'>({r['how']})</span></li>"
                   for r in state.get("resolved", []))
    caveats = "".join(f"<li>{e(c)}</li>" for c in a.get("caveats", []))
    follow = "".join(f"<li>{e(c)}</li>" for c in a.get("follow_ups", []))
    u = state.get("understanding", {})
    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Reporte de análisis</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6b6b6b;--line:#e5e5e5;--card:#f6f7f9;--err:#b42318}}
@media (prefers-color-scheme: dark){{:root{{--bg:#15171a;--fg:#e9e9e9;--mut:#9a9a9a;--line:#30333a;--card:#1e2126;--err:#ff8a7a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;max-width:1100px;margin:auto;padding:16px}}
h1{{font-size:22px}} .mut{{color:var(--mut);font-size:13px}} .err{{color:var(--err)}}
.kpis{{display:flex;flex-wrap:wrap;gap:10px}} .kpi{{background:var(--card);border-radius:10px;padding:10px 14px;min-width:150px}}
.kpi .v{{font-size:20px;font-weight:600}} .kpi .l{{color:var(--mut);font-size:13px}}
section{{border-top:1px solid var(--line);margin-top:18px;padding-top:6px}} pre{{background:var(--card);padding:10px;
border-radius:8px;overflow-x:auto;font-size:12.5px}} .wrap{{overflow-x:auto}} table.t{{border-collapse:collapse;font-size:13px}}
table.t td,table.t th{{border-bottom:1px solid var(--line);padding:3px 8px;text-align:left}}
</style></head><body>
<h1>{e(state['question'])}</h1>
<p class="mut">Interpretación: {e(u.get('restated', ''))} · intención: {e(u.get('intent', ''))}</p>
{f"<p class='mut'>Entidades:</p><ul>{ents}</ul>" if ents else ""}
<div class="kpis">{kn}</div>
<div>{md.markdown(a.get('summary', ''))}</div>
{f"<h2>Hallazgos</h2><ol>{findings}</ol>" if findings else ""}
{f"<h2>Precauciones</h2><ul>{caveats}</ul>" if caveats else ""}
{f"<h2>Siguientes preguntas sugeridas</h2><ul>{follow}</ul>" if follow else ""}
<h2>Evidencia (plan: {e(plan.get('approach', ''))})</h2>{steps_html}
<p class="mut">Llamadas al cerebro: {len(calls)} · {sum(c['seconds'] for c in calls):.0f}s</p>
</body></html>"""
    path = run_dir / "reporte.html"
    path.write_text(page, encoding="utf-8")
    (run_dir / "respuesta.json").write_text(json.dumps(
        {k: state.get(k) for k in ("question", "understanding", "resolved", "plan", "answer")} |
        {"results": [{k: v for k, v in (r or {}).items() if k != "preview"} for r in state.get("results") or []],
         "brain_calls": calls}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path
