"""CLI:
  python -m dw_engine.cli onboard [--force]
  python -m dw_engine.cli ask "¿cuánto vendió el restaurante Polanco ayer?"
  python -m dw_engine.cli eval [--only N]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .brains import make_brain
from .config import load_config
from .db import make_connector


def cmd_onboard(args, cfg):
    path = Path(cfg.semantic_model_path)
    if path.exists() and not args.force:
        sys.exit(f"{path} ya existe (puede tener curaduría humana). Usa --force para regenerarlo.")
    from .onboarding import build_onboarding_graph
    conn, brain = make_connector(cfg.connection), make_brain(cfg.brain)
    t0 = time.time()
    build_onboarding_graph(cfg, conn, brain).invoke({})
    print(f"\nOnboarding en {time.time() - t0:.0f}s, {len(brain.calls)} llamadas al cerebro.")


def cmd_ask(args, cfg):
    from .ask import run_question
    run_question(cfg, args.question, interactive=not args.yes)


def cmd_eval(args, cfg):
    from .evaluate import run_eval
    run_eval(cfg, args.file, only=args.only)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="dw_engine")
    ap.add_argument("--config", default="config/engine.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("onboard")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--yes", action="store_true", help="no interactivo: elige la opción más probable")
    p = sub.add_parser("eval")
    p.add_argument("--file", default="tests/eval.yaml")
    p.add_argument("--only", type=int, nargs="*")
    args = ap.parse_args()
    cfg = load_config(args.config)
    {"onboard": cmd_onboard, "ask": cmd_ask, "eval": cmd_eval}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
