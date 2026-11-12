"""Validador de SQL antes de tocar la base: una sola sentencia SELECT, solo tablas del modelo."""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from .semantic import SemanticModel


class GuardError(ValueError):
    pass


_FORBIDDEN = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Drop", "Create", "Merge", "Alter",
                                             "Command", "TruncateTable", "Grant") if hasattr(exp, n))


def check_sql(sql: str, model: SemanticModel) -> str:
    sql = sql.strip().rstrip(";").strip()
    try:
        stmts = [s for s in sqlglot.parse(sql, read="oracle") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise GuardError(f"SQL inválido para Oracle: {str(e)[:400]}") from e
    if len(stmts) != 1:
        raise GuardError("se permite exactamente una sentencia")
    root = stmts[0]
    if not isinstance(root, exp.Query):
        raise GuardError("solo se permiten consultas SELECT")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN):
            raise GuardError(f"operación no permitida: {type(node).__name__}")
    ctes = {c.alias_or_name.upper() for c in root.find_all(exp.CTE)}
    allowed = {t.name.upper() for t in model.answer_tables()} | {"DUAL"}
    for t in root.find_all(exp.Table):
        name = t.name.upper()
        if name and name not in ctes and name not in allowed:
            raise GuardError(f"tabla '{name}' no existe en el modelo o no está permitida. "
                             f"Permitidas: {', '.join(sorted(allowed - {'DUAL'}))}")
    return sql
