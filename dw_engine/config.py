from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ConnectionCfg(BaseModel):
    kind: str = "oracle"                 # oracle | duckdb (duckdb = backend de pruebas)
    dsn: str | None = None               # oracle: host:port/service
    user_env: str = "DW_USER"
    password_env: str = "DW_PASSWORD"
    current_schema: str | None = None    # oracle: ALTER SESSION SET CURRENT_SCHEMA
    path: str | None = None              # duckdb: archivo
    include_tables: list[str] = Field(default_factory=list)   # vacío = todo lo visible
    exclude_patterns: list[str] = Field(default_factory=list)  # glob, ej. TMP_*
    call_timeout_ms: int = 120_000


class BrainCfg(BaseModel):
    provider: str = "openai_compat"      # openai_compat (vLLM/Ollama/OpenAI/LM Studio) | anthropic | claude_cli
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: int = 300
    json_mode: bool = True               # openai_compat: response_format=json_object
    max_parallel: int = 4


class PrivacyCfg(BaseModel):
    send_sample_values: bool = True      # ¿se mandan valores de ejemplo al cerebro?
    max_sample_values: int = 8


class EngineCfg(BaseModel):
    connection: ConnectionCfg
    brain: BrainCfg
    privacy: PrivacyCfg = Field(default_factory=PrivacyCfg)
    sql_mode: str = "hybrid"             # compiled | free | hybrid
    max_rows: int = 5000                 # filas máx por consulta de respuesta
    detector_max_rows: int = 200_000     # filas máx para consultas que alimentan detectores
    max_repairs: int = 3
    reference_date: str = "auto"         # auto = hoy | YYYY-MM-DD
    language: str = "es"
    business_context: str = ""           # texto libre: giro, reglas conocidas
    naming_hints: dict[str, str] = Field(default_factory=dict)  # abreviaturas: TDA -> tienda
    semantic_model_path: str = "semantic/model.yaml"
    output_dir: str = "outputs"

    def ref_date(self) -> date:
        return date.today() if self.reference_date == "auto" else date.fromisoformat(self.reference_date)


def load_config(path: str | os.PathLike = "config/engine.yaml") -> EngineCfg:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return EngineCfg.model_validate(data)
