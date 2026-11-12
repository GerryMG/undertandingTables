"""Documentación del modelo: diagramas estrella (Mermaid), bus matrix de Kimball y glosario en HTML."""
from __future__ import annotations

import html
from pathlib import Path

from .semantic import SemanticModel


def star_for_fact(m: SemanticModel, fact: str) -> str:
    """Mermaid erDiagram del hecho y todo lo que alcanza (estrella o copo de nieve)."""
    lines = ["erDiagram"]
    seen_edges, tables = set(), {fact}
    frontier = [fact]
    while frontier:
        cur = frontier.pop()
        for r in m.relationships:
            if r.from_table == cur and (r.from_table, r.from_column) not in seen_edges:
                seen_edges.add((r.from_table, r.from_column))
                lines.append(f'  {r.to_table} ||--o{{ {r.from_table} : "{r.from_column}"')
                if r.to_table not in tables:
                    tables.add(r.to_table)
                    frontier.append(r.to_table)
    for tn in sorted(tables):
        t = m.table(tn)
        if not t:
            continue
        lines.append(f"  {tn} {{")
        for c in t.columns[:14]:
            dtype = c.data_type.split("(")[0]
            tag = " PK" if c.name in t.primary_key else (" FK" if c.role == "foreign_key" else "")
            label = (c.business_name or "").replace('"', "'")[:40]
            lines.append(f'    {dtype} {c.name}{tag} "{label}"')
        lines.append("  }")
    return "\n".join(lines)


def bus_matrix(m: SemanticModel) -> tuple[list[str], list[str], dict[tuple[str, str], bool]]:
    facts = [t.name for t in m.answer_tables() if t.role == "fact"]
    dims = [t.name for t in m.answer_tables() if t.role in ("dimension", "lookup")]
    cells = {(f, d): m.join_path(f, d) is not None for f in facts for d in dims}
    return facts, dims, cells


def write_docs(m: SemanticModel, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    facts, dims, cells = bus_matrix(m)
    md = ["# Diagramas estrella\n"]
    for f in facts:
        md += [f"## {f} — {m.table(f).business_name}\n", "```mermaid", star_for_fact(m, f), "```\n"]
    (folder / "diagramas.md").write_text("\n".join(md), encoding="utf-8")

    e = html.escape
    bus = ["<table class='bus'><tr><th>Hecho \\ Dimensión</th>" +
           "".join(f"<th>{e(m.table(d).business_name or d)}<br><small>{d}</small></th>" for d in dims) + "</tr>"]
    for f in facts:
        bus.append(f"<tr><th>{e(m.table(f).business_name or f)}<br><small>{f}</small></th>" +
                   "".join("<td class='y'>●</td>" if cells[(f, d)] else "<td></td>" for d in dims) + "</tr>")
    bus.append("</table>")
    stars = "".join(f"<h3>{e(m.table(f).business_name)} <small>{f}</small></h3><p>{e(m.table(f).grain)}</p>"
                    f"<pre class='mermaid'>{e(star_for_fact(m, f))}</pre>" for f in facts)
    metrics = "".join(f"<tr><td><b>{e(x.business_name)}</b><br><small>{x.name}</small></td>"
                      f"<td><code>{e(x.expression or x.derived or '')}</code></td><td>{e(x.description)}</td></tr>"
                      for x in m.metrics)
    tables = ""
    for t in m.tables:
        rows = "".join(f"<tr><td><code>{c.name}</code></td><td>{e(c.business_name)}</td><td>{c.role}</td>"
                       f"<td>{e(c.description)}</td></tr>" for c in t.columns)
        tables += (f"<details><summary><b>{t.name}</b> — {e(t.business_name)} <span class='tag'>{t.role}</span>"
                   f"</summary><p>{e(t.description)}</p><table>{rows}</table></details>")
    qs = "".join(f"<li><b>{e(q.question)}</b><br>Propuesta: {e(q.proposed_answer)}"
                 f"{'<br>✅ ' + e(q.answer) if q.answer else ''}</li>" for q in m.open_questions)
    rules = "".join(f"<li>{e(r)}</li>" for r in m.rules)
    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Modelo semántico</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#e3e3e3;--acc:#2b6cb0;--accbg:#ebf4ff}}
@media (prefers-color-scheme: dark){{:root{{--bg:#16181c;--fg:#e8e8e8;--mut:#9aa;--line:#333;--acc:#7fb3ff;--accbg:#1d2a3a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:auto;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0}} td,th{{border:1px solid var(--line);padding:4px 8px;
text-align:left;vertical-align:top}} .bus td.y{{text-align:center;color:var(--acc);background:var(--accbg)}}
small{{color:var(--mut)}} .tag{{font-size:12px;padding:1px 6px;border-radius:8px;background:var(--accbg)}}
details{{margin:6px 0}} pre.mermaid{{background:transparent}} .wrap{{overflow-x:auto}}
</style></head><body>
<h1>Modelo semántico del DW</h1><p>{e(m.business_summary)}</p>
<h2>Bus matrix (procesos × dimensiones)</h2><div class="wrap">{''.join(bus)}</div>
<h2>Diagramas estrella</h2>{stars}
<h2>Métricas</h2><div class="wrap"><table>{metrics}</table></div>
<h2>Reglas de negocio</h2><ul>{rules}</ul>
<h2>Preguntas abiertas (confírmalas en model.yaml → open_questions[].answer)</h2><ol>{qs}</ol>
<h2>Diccionario de tablas</h2>{tables}
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
mermaid.initialize({{startOnLoad:true, theme: matchMedia('(prefers-color-scheme: dark)').matches?'dark':'default'}});
</script></body></html>"""
    (folder / "modelo.html").write_text(page, encoding="utf-8")
