"""Evaluación: corre preguntas con verdad conocida y mide aciertos. Es la forma objetiva de comparar cerebros."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import yaml

from .ask import Engine
from .config import EngineCfg
from .value_index import norm


def _numbers(out: dict) -> list[float]:
    vals = []
    for r in out.get("results") or []:
        if r and r.get("csv") and Path(r["csv"]).exists():
            df = pd.read_csv(r["csv"])
            for c in df.columns:
                vals += pd.to_numeric(df[c], errors="coerce").dropna().tolist()
    return vals


def _text(out: dict) -> str:
    a = out.get("answer") or {}
    parts = [a.get("summary", "")] + [f"{f['title']} {f['detail']}" for f in a.get("findings", [])]
    parts += [f"{k['label']} {k['value']}" for k in a.get("key_numbers", [])]
    return norm(" ".join(parts))


def load_cases(file: str) -> list[dict]:
    return yaml.safe_load(Path(file).read_text(encoding="utf-8"))


def eval_case(eng: Engine, case: dict) -> dict:
    """Corre una pregunta del set y la califica contra la verdad calculada en la base."""
    asked = []

    def on_interrupt(p, case=case):
        asked.append(p)
        for want in case.get("choose", []):
            if want == "all":
                return "all"
            for i, o in enumerate(p["options"]):
                if want.lower() in o.lower():
                    return [i]
        return "all" if p.get("plural") else [0]

    t0 = time.time()
    try:
        out = eng.ask(case["question"], on_interrupt)
        err = None
    except Exception as e:  # noqa: BLE001
        out, err = {}, f"{type(e).__name__}: {e}"
    exp, checks = case["expect"], {}
    if "number" in exp:
        try:
            truth = float(eng.conn.query(exp["number"]["truth_sql"])[0].iloc[0, 0])
            tol = exp["number"].get("tol", 0.005)
            checks["numero"] = any(abs(v - truth) <= abs(truth) * tol for v in _numbers(out))
            checks["_verdad"] = round(truth, 4)
        except Exception as e:  # noqa: BLE001  (verdad mal escrita: no debe tumbar toda la corrida)
            checks["numero"] = False
            err = err or f"truth_sql falló: {e}"
    txt = _text(out)
    if "any_text" in exp:
        checks["texto"] = any(norm(t) in txt for t in exp["any_text"])
    if "all_text_groups" in exp:
        hits = [any(norm(t) in txt for t in grp) for grp in exp["all_text_groups"]]
        checks["texto"] = all(hits)
        checks["_grupos"] = f"{sum(hits)}/{len(hits)}"
    if exp.get("asked"):
        checks["pregunto_ambiguedad"] = bool(asked)
    ok = err is None and all(v for k, v in checks.items() if not k.startswith("_"))
    return {"id": case["id"], "pregunta": case["question"], "ok": ok, "segundos": round(time.time() - t0, 1),
            "error": err, "reparaciones": out.get("repairs", 0), "reporte": out.get("report"), **checks}


def save_rows(cfg: EngineCfg, rows: list[dict]) -> Path:
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({"brain": f"{cfg.brain.provider}/{cfg.brain.model}", "sql_mode": cfg.sql_mode,
                                "rows": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def run_eval(cfg: EngineCfg, file: str, only: list[int] | None = None):
    cases = load_cases(file)
    if only:
        cases = [c for c in cases if c["id"] in only]
    eng = Engine(cfg)
    rows = []
    for case in cases:
        print(f"\n### [{case['id']}] {case['question']}")
        row = eval_case(eng, case)
        rows.append(row)
        checks = {k: v for k, v in row.items() if k in ("numero", "texto", "pregunto_ambiguedad", "_verdad", "_grupos")}
        print(f"→ {'✅' if row['ok'] else '❌'} {checks} {row['error'] or ''}")
    df = pd.DataFrame(rows)
    print("\n" + df.drop(columns=["reporte", "pregunta"]).to_string(index=False))
    print(f"\nACIERTOS: {int(df.ok.sum())}/{len(df)}  ({df.ok.mean():.0%})  |  cerebro: {cfg.brain.provider}/"
          f"{cfg.brain.model}  |  modo SQL: {cfg.sql_mode}")
    print(f"Detalle: {save_rows(cfg, rows)}")
