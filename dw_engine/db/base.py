from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod

import pandas as pd
from pydantic import BaseModel, Field


class ColumnMeta(BaseModel):
    name: str
    data_type: str                      # tipo normalizado estilo Oracle: NUMBER, VARCHAR2, DATE, TIMESTAMP
    nullable: bool = True
    comment: str | None = None
    # perfilado
    num_distinct: int | None = None
    num_nulls: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    top_values: list[tuple[str, int]] = Field(default_factory=list)
    samples: list[str] = Field(default_factory=list)

    @property
    def is_numeric(self) -> bool:
        return self.data_type.startswith(("NUMBER", "FLOAT", "BINARY", "INTEGER"))

    @property
    def is_text(self) -> bool:
        return self.data_type.startswith(("VARCHAR", "CHAR", "NVARCHAR", "NCHAR", "CLOB"))

    @property
    def is_temporal(self) -> bool:
        return self.data_type.startswith(("DATE", "TIMESTAMP"))


class ForeignKeyMeta(BaseModel):
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


class TableMeta(BaseModel):
    name: str
    kind: str = "TABLE"
    comment: str | None = None
    num_rows: int | None = None
    columns: list[ColumnMeta]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyMeta] = Field(default_factory=list)

    def col(self, name: str) -> ColumnMeta | None:
        return next((c for c in self.columns if c.name.upper() == name.upper()), None)


def finalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza tipos del driver (Decimal → float) para pandas/plotly."""
    from decimal import Decimal
    for c in df.columns:
        if df[c].dtype == object:
            first = df[c].dropna().head(1)
            if len(first) and isinstance(first.iloc[0], Decimal):
                df[c] = df[c].astype(float)
    return df


class Connector(ABC):
    """Todas las consultas entran en dialecto ORACLE. Backends no-Oracle traducen internamente."""

    def __init__(self, cfg):
        self.cfg = cfg

    @abstractmethod
    def introspect(self) -> list[TableMeta]: ...

    @abstractmethod
    def query(self, sql: str, max_rows: int | None = None) -> tuple[pd.DataFrame, bool]:
        """Ejecuta SQL Oracle de solo lectura. Regresa (df, truncado)."""

    def filter_tables(self, tables: list[TableMeta]) -> list[TableMeta]:
        inc = {t.upper() for t in self.cfg.include_tables}
        out = []
        for t in tables:
            if inc and t.name.upper() not in inc:
                continue
            if any(fnmatch.fnmatch(t.name.upper(), p.upper()) for p in self.cfg.exclude_patterns):
                continue
            out.append(t)
        return out


def make_connector(cfg) -> Connector:
    if cfg.kind == "oracle":
        from .oracle import OracleConnector
        return OracleConnector(cfg)
    if cfg.kind == "duckdb":
        from .duck import DuckDBConnector
        return DuckDBConnector(cfg)
    raise ValueError(f"Conexión no soportada: {cfg.kind}")
