"""Un solo "cerebro" configurable. Todos los nodos piden salidas estructuradas (Pydantic) y se validan aquí.

Proveedores:
- openai_compat: cualquier servidor compatible OpenAI (vLLM en tus H200, Ollama, LM Studio, OpenAI)
- anthropic:     API de Anthropic (requiere `pip install anthropic`)
- claude_cli:    Claude Code en modo headless (`claude -p`). Útil para pruebas sin infraestructura.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import BrainCfg

T = TypeVar("T", bound=BaseModel)


class BrainError(RuntimeError):
    pass


def extract_json(text: str):
    """Extrae el primer objeto JSON de una respuesta (tolera ```json fences y texto alrededor)."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("No hay JSON en la respuesta")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON incompleto en la respuesta")


class Brain:
    """Interfaz común. Subclases implementan `_complete(system, user) -> str`."""

    def __init__(self, cfg: BrainCfg):
        self.cfg = cfg
        self.calls: list[dict] = []
        self._sem = threading.Semaphore(cfg.max_parallel)
        self._lock = threading.Lock()

    def _complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def complete(self, system: str, user: str, tag: str = "") -> str:
        with self._sem:
            t0 = time.time()
            out = self._complete(system, user)
            with self._lock:
                self.calls.append({"tag": tag, "seconds": round(time.time() - t0, 1),
                                   "prompt_chars": len(system) + len(user), "response_chars": len(out)})
            return out

    def structured(self, system: str, user: str, schema: type[T], tag: str = "", retries: int = 2) -> T:
        schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        base = (f"{user}\n\n---\nResponde ÚNICAMENTE con un objeto JSON válido (sin texto extra) que cumpla "
                f"este JSON Schema:\n{schema_txt}")
        prompt = base
        last_err = ""
        for attempt in range(retries + 1):
            raw = self.complete(system, prompt, tag=f"{tag}#{attempt}" if attempt else tag)
            try:
                return schema.model_validate(extract_json(raw))
            except (ValueError, ValidationError) as e:
                last_err = str(e)[:2000]
                prompt = (f"{base}\n\nTu respuesta anterior fue inválida:\n{raw[:3000]}\n\nError:\n{last_err}\n"
                          f"Corrige y responde solo el JSON.")
        raise BrainError(f"[{tag}] el cerebro no produjo JSON válido: {last_err}")


class OpenAICompatBrain(Brain):
    def __init__(self, cfg: BrainCfg):
        super().__init__(cfg)
        from openai import OpenAI
        key = os.getenv(cfg.api_key_env) if cfg.api_key_env else None
        self.client = OpenAI(base_url=cfg.base_url, api_key=key or "not-needed", timeout=cfg.timeout_s)

    def _complete(self, system: str, user: str) -> str:
        kw = {}
        if self.cfg.json_mode:
            kw["response_format"] = {"type": "json_object"}
        r = self.client.chat.completions.create(
            model=self.cfg.model, temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
        return r.choices[0].message.content or ""


class AnthropicBrain(Brain):
    def __init__(self, cfg: BrainCfg):
        super().__init__(cfg)
        import anthropic  # pip install anthropic
        key = os.getenv(cfg.api_key_env or "ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=key, timeout=cfg.timeout_s)

    def _complete(self, system: str, user: str) -> str:
        r = self.client.messages.create(model=self.cfg.model, max_tokens=self.cfg.max_tokens, system=system,
                                        messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


class ClaudeCLIBrain(Brain):
    """Usa `claude -p` sin herramientas y con system prompt propio. Solo para pruebas locales."""

    def __init__(self, cfg: BrainCfg):
        super().__init__(cfg)
        exe = shutil.which("claude")
        if os.name == "nt":  # en Windows, npm instala un .cmd que apunta a un .exe nativo
            cmd_shim = shutil.which("claude.cmd")
            if cmd_shim:
                native = Path(cmd_shim).parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
                exe = str(native) if native.exists() else cmd_shim
        if not exe:
            raise BrainError("No se encontró el CLI `claude` en PATH")
        self.exe = exe
        self.workdir = Path(tempfile.mkdtemp(prefix="dw_brain_"))

    def _complete(self, system: str, user: str) -> str:
        sp = self.workdir / f"sys_{threading.get_ident()}_{time.time_ns()}.txt"
        sp.write_text(system, encoding="utf-8")
        cmd = [self.exe, "-p", "--output-format", "json", "--model", self.cfg.model, "--tools", "",
               "--no-session-persistence", "--system-prompt-file", str(sp)]
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                               timeout=self.cfg.timeout_s, cwd=self.workdir)
        finally:
            sp.unlink(missing_ok=True)
        if r.returncode != 0:
            raise BrainError(f"claude CLI falló ({r.returncode}): {r.stderr[:500] or r.stdout[:500]}")
        data = json.loads(r.stdout)
        if data.get("is_error"):
            raise BrainError(f"claude CLI error: {str(data)[:500]}")
        return data.get("result", "")


def make_brain(cfg: BrainCfg) -> Brain:
    return {"openai_compat": OpenAICompatBrain, "anthropic": AnthropicBrain,
            "claude_cli": ClaudeCLIBrain}[cfg.provider](cfg)
