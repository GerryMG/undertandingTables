"""Compilador determinístico: QuerySpec (lo que pide el negocio) → SQL Oracle correcto.

El cerebro solo elige métricas, dimensiones y filtros. Joins, fórmulas y drill-across entre hechos
los resuelve este compilador con el modelo semántico, así un modelo local pequeño también acierta.
"""
from __future__ import annotations

import re
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from .semantic import Column, Metric, SemanticModel


class CompileError(ValueError):
    pass


class Filter(BaseModel):
    column: str = Field(description="TABLA.COLUMNA")
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "between", "like", "is null", "is not null"]
    value: Any = None


class OrderBy(BaseModel):
    field: str = Field(description="nombre de métrica o TABLA.COLUMNA de dimensions")
    desc: bool = True


class QuerySpec(BaseModel):
    metrics: list[str] = Field(description="nombres de métricas del modelo")
    dimensions: list[str] = Field(default_factory=list, description="TABLA.COLUMNA para agrupar")
    filters: list[Filter] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = None


def _split(ref: str) -> tuple[str, str]:
    if "." not in ref:
        raise CompileError(f"'{ref}' debe tener forma TABLA.COLUMNA")
    t, c = ref.upper().split(".", 1)
    return t, c


def _lit(v: Any, col: Column) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if col.data_type.startswith(("DATE", "TIMESTAMP")) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"DATE '{s}'"
    if col.data_type.startswith(("NUMBER", "FLOAT", "INTEGER")) and re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    return "'" + s.replace("'", "''") + "'"


def _qualify(expr: str, alias: str) -> str:
    tree = sqlglot.parse_one(expr, read="oracle")

    def tx(node):
        if isinstance(node, exp.Column) and not node.table:
            return exp.column(node.name, table=alias)
        return node
    return tree.transform(tx).sql(dialect="oracle")


def _deps(m: SemanticModel, metric: Metric, seen=None) -> list[Metric]:
    """Métricas base necesarias (expande derivadas recursivamente)."""
    seen = seen or set()
    if metric.expression:
        return [metric]
    out = []
    for ref in re.findall(r"\{(\w+)\}", metric.derived or ""):
        if ref in seen:
            continue
        seen.add(ref)
        dm = m.metric(ref)
        if not dm:
            raise CompileError(f"la métrica derivada {metric.name} usa {{{ref}}} que no existe")
        out += _deps(m, dm, seen)
    return out


def compile_spec(spec: QuerySpec, m: SemanticModel) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if not spec.metrics:
        raise CompileError("la consulta necesita al menos una métrica")
    metrics = []
    for name in spec.metrics:
        mt = m.metric(name)
        if not mt:
            raise CompileError(f"métrica desconocida '{name}'. Disponibles: {', '.join(x.name for x in m.metrics)}")
        metrics.append(mt)
    base: dict[str, Metric] = {}
    for mt in metrics:
        for b in _deps(m, mt):
            base[b.name] = b
    by_fact: dict[str, list[Metric]] = {}
    for b in base.values():
        by_fact.setdefault(b.table.upper(), []).append(b)

    # dimensiones
    dims = []
    for ref in spec.dimensions:
        t, c = _split(ref)
        tab = m.table(t)
        if not tab or not tab.col(c):
            raise CompileError(f"dimensión {ref} no existe en el modelo")
        dims.append((t, c))
    names = [c for _, c in dims]
    dim_alias = {(t, c): (c if names.count(c) == 1 else f"{t}_{c}") for t, c in dims}

    filters = []
    for f in spec.filters:
        t, c = _split(f.column)
        tab = m.table(t)
        if not tab or not tab.col(c):
            raise CompileError(f"filtro sobre {f.column}: no existe en el modelo")
        filters.append((t, c, f, tab.col(c)))

    for mt in base.values():
        if mt.additive == "semi_additive":
            date_dim = m.time.date_table
            pinned = any(t == (date_dim or "").upper() and f.op == "=" for t, _, f, _ in filters) or \
                any(t == (date_dim or "").upper() for t, _ in dims)
            if not pinned:
                warnings.append(f"{mt.name} es semiaditiva (saldo): fija una fecha o agrupa por fecha; "
                                "sumarla en varias fechas no tiene sentido")

    ctes = []
    for i, (fact, mets) in enumerate(by_fact.items()):
        alias = {fact: "f"}
        joins: list[str] = []

        def ensure(table: str, required: bool) -> bool:
            if table in alias:
                return True
            path = m.join_path(fact, table)
            if path is None:
                if required:
                    raise CompileError(f"no hay camino de joins de {fact} a {table} (revisa relaciones)")
                return False
            for r in path:
                tt = r.to_table.upper()
                if tt in alias:
                    continue
                alias[tt] = f"d{len(alias)}"
                joins.append(f"JOIN {tt} {alias[tt]} ON {alias[tt]}.{r.to_column} = "
                             f"{alias[r.from_table.upper()]}.{r.from_column}")
            return True

        for t, c in dims:
            ensure(t, required=True)
        where = []
        for t, c, f, col in filters:
            if not ensure(t, required=len(by_fact) == 1):
                continue  # en drill-across, el filtro aplica solo a los hechos que lo alcanzan
            ref = f"{alias[t]}.{c}"
            op = f.op.lower()
            if op in ("is null", "is not null"):
                where.append(f"{ref} {op.upper()}")
            elif op in ("in", "not in"):
                vals = f.value if isinstance(f.value, list) else [f.value]
                where.append(f"{ref} {op.upper()} ({', '.join(_lit(v, col) for v in vals)})")
            elif op == "between":
                if not isinstance(f.value, list) or len(f.value) != 2:
                    raise CompileError(f"between en {f.column} requiere [desde, hasta]")
                where.append(f"{ref} BETWEEN {_lit(f.value[0], col)} AND {_lit(f.value[1], col)}")
            elif op == "like":
                where.append(f"UPPER({ref}) LIKE UPPER({_lit(f.value, col)})")
            else:
                where.append(f"{ref} {f.op} {_lit(f.value, col)}")
        sel = [f"{alias[t]}.{c} AS {dim_alias[(t, c)]}" for t, c in dims]
        sel += [f"{_qualify(mt.expression, 'f')} AS {mt.name.upper()}" for mt in mets]
        sql = f"SELECT {', '.join(sel)}\n  FROM {fact} f"
        if joins:
            sql += "\n  " + "\n  ".join(joins)
        if where:
            sql += "\n WHERE " + "\n   AND ".join(where)
        if dims:
            sql += "\n GROUP BY " + ", ".join(f"{alias[t]}.{c}" for t, c in dims)
        ctes.append((f"q{i}", sql, [mt.name.upper() for mt in mets]))

    # SELECT exterior: une los hechos por las dimensiones (drill-across) y calcula derivadas
    dcols = [dim_alias[d] for d in dims]
    q0 = ctes[0][0]
    if len(ctes) == 1:
        from_sql = f"{q0}"
        dim_sel = [f"{q0}.{d}" for d in dcols]
    else:
        from_sql = q0
        for j, (name, _, _) in enumerate(ctes[1:], start=1):
            if dcols:
                conds = []
                for d in dcols:
                    prev = ", ".join(f"{ctes[k][0]}.{d}" for k in range(j))
                    left = f"COALESCE({prev})" if j > 1 else f"{q0}.{d}"
                    conds.append(f"{left} = {name}.{d}")
                from_sql += f"\n  FULL OUTER JOIN {name} ON " + " AND ".join(conds)
            else:
                from_sql += f"\n  CROSS JOIN {name}"
        dim_sel = [f"COALESCE({', '.join(f'{c[0]}.{d}' for c in ctes)}) AS {d}" for d in dcols]
    where_from = {mn: cte for cte, _, mns in ctes for mn in mns}

    def metric_sql(mt: Metric) -> str:
        if mt.expression:
            return f"{where_from[mt.name.upper()]}.{mt.name.upper()}"
        return "(" + re.sub(r"\{(\w+)\}", lambda g: metric_sql(m.metric(g.group(1))), mt.derived) + ")"

    out_sel = dim_sel + [f"{metric_sql(mt)} AS {mt.name.upper()}" for mt in metrics]
    sql = "WITH " + ",\n".join(f"{n} AS (\n{s}\n)" for n, s, _ in ctes)
    sql += f"\nSELECT {', '.join(out_sel)}\n  FROM {from_sql}"

    order = []
    valid = {d.upper(): d for d in dcols} | {mt.name.upper(): mt.name.upper() for mt in metrics}
    for ob in spec.order_by:
        key = ob.field.upper()
        if "." in key:
            t, c = _split(key)
            key = dim_alias.get((t, c), c)
        if key not in valid:
            raise CompileError(f"order_by '{ob.field}' no está en dimensiones ni métricas pedidas")
        order.append(f"{valid[key]} {'DESC' if ob.desc else 'ASC'} NULLS LAST")
    if not order and dcols:
        order = [f"{d} ASC" for d in dcols]
    if order:
        sql += "\n ORDER BY " + ", ".join(order)
    if spec.limit:
        sql += f"\n FETCH FIRST {int(spec.limit)} ROWS ONLY"
    return sql, warnings
