"""Backend de PRUEBAS: DuckDB. Recibe SQL en dialecto Oracle y lo traduce con sqlglot.

Así el motor completo (prompts, compilador, validador) trabaja siempre en Oracle, y este backend
solo existe para poder probar sin una instancia Oracle.
"""
from __future__ import annotations

import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.dialects.oracle import Oracle
from sqlglot.time import format_time

from .base import ColumnMeta, Connector, finalize_df, ForeignKeyMeta, TableMeta

_TRUNC_UNITS = {"MM": "month", "MON": "month", "MONTH": "month", "YYYY": "year", "YEAR": "year", "Y": "year",
                "DD": "day", "DDD": "day", "J": "day", "IW": "week", "WW": "week", "W": "week", "Q": "quarter",
                "HH": "hour", "HH24": "hour", "MI": "minute"}


def _oracle_fixups(node: exp.Expression) -> exp.Expression:
    # SELECT ... FROM DUAL -> SELECT ... (DuckDB no tiene DUAL)
    if isinstance(node, exp.Select):
        frm = node.args.get("from_") or node.args.get("from")
        if frm is not None and isinstance(frm.this, exp.Table) and frm.this.name.upper() == "DUAL":
            node.set("from_" if "from_" in node.args else "from", None)
        return node
    # TO_CHAR(fecha, 'YYYY-MM') -> STRFTIME(fecha, '%Y-%m')
    if isinstance(node, exp.ToChar) and node.args.get("format") is not None:
        fmt = node.args["format"]
        if isinstance(fmt, exp.Literal):
            py = format_time(fmt.this, Oracle.TIME_MAPPING, Oracle.TIME_TRIE)
            return exp.TimeToStr(this=node.this, format=exp.Literal.string(py))
    # TRUNC(fecha, 'MM') -> DATE_TRUNC('month', fecha)
    if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)):
        unit = node.args.get("unit")
        name = unit.name.upper() if unit is not None else ""
        if name in _TRUNC_UNITS:
            node.set("unit", exp.var(_TRUNC_UNITS[name]))
    return node


def oracle_to_duckdb(sql: str) -> str:
    tree = sqlglot.parse_one(sql.strip().rstrip(";"), read="oracle")
    tree = tree.transform(_oracle_fixups)
    return tree.sql(dialect="duckdb")


def _oracle_type(t: str) -> str:
    t = t.upper()
    if t.startswith("DECIMAL"):
        return t.replace("DECIMAL", "NUMBER")
    if t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT"):
        return "NUMBER(38,0)"
    if t in ("DOUBLE", "FLOAT", "REAL"):
        return "NUMBER"
    if t.startswith("VARCHAR"):
        return t.replace("VARCHAR", "VARCHAR2")
    if t.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    return t


class DuckDBConnector(Connector):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.con = duckdb.connect(cfg.path, read_only=True)

    def introspect(self) -> list[TableMeta]:
        tabs = self.con.execute(
            "SELECT table_name, comment, estimated_size FROM duckdb_tables() WHERE schema_name='main'").fetchall()
        cols = self.con.execute(
            "SELECT table_name, column_name, data_type, is_nullable, comment FROM duckdb_columns() "
            "WHERE schema_name='main' ORDER BY table_name, column_index").fetchall()
        cons = self.con.execute(
            "SELECT table_name, constraint_type, constraint_column_names, referenced_table, "
            "referenced_column_names FROM duckdb_constraints() WHERE schema_name='main' "
            "AND constraint_type IN ('PRIMARY KEY','FOREIGN KEY')").fetchall()
        out: dict[str, TableMeta] = {}
        for name, comment, est in tabs:
            n = self.con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out[name] = TableMeta(name=name.upper(), comment=comment or None, num_rows=n, columns=[])
        for tname, cname, dtype, nullable, comment in cols:
            if tname not in out:
                continue
            out[tname].columns.append(ColumnMeta(name=cname.upper(), data_type=_oracle_type(dtype),
                                                 nullable=bool(nullable), comment=comment or None))
        for tname, ctype, ccols, rtable, rcols in cons:
            if tname not in out:
                continue
            if ctype == "PRIMARY KEY":
                out[tname].primary_key = [c.upper() for c in ccols]
            else:
                out[tname].foreign_keys.append(ForeignKeyMeta(columns=[c.upper() for c in ccols],
                                                              ref_table=rtable.upper(),
                                                              ref_columns=[c.upper() for c in rcols]))
        return self.filter_tables(list(out.values()))

    def query(self, sql: str, max_rows: int | None = None) -> tuple[pd.DataFrame, bool]:
        duck_sql = oracle_to_duckdb(sql)
        cur = self.con.cursor()
        cur.execute(duck_sql)
        cols = [d[0].upper() for d in cur.description]
        if max_rows:
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
        else:
            rows, truncated = cur.fetchall(), False
        return finalize_df(pd.DataFrame(rows, columns=cols)), truncated
