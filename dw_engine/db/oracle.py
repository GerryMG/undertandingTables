"""Conector Oracle (python-oracledb, modo thin: no requiere Instant Client).

Solo usa vistas ALL_* → ve exactamente lo que el usuario tiene permitido, sin privilegios de DBA.
"""
from __future__ import annotations

import os

import pandas as pd

from .base import ColumnMeta, Connector, finalize_df, ForeignKeyMeta, TableMeta


class OracleConnector(Connector):
    def __init__(self, cfg):
        super().__init__(cfg)
        import oracledb
        self._oracledb = oracledb
        self.conn = oracledb.connect(user=os.environ[cfg.user_env], password=os.environ[cfg.password_env],
                                     dsn=cfg.dsn)
        self.conn.call_timeout = cfg.call_timeout_ms
        with self.conn.cursor() as cur:
            if cfg.current_schema:
                cur.execute(f'ALTER SESSION SET CURRENT_SCHEMA = "{cfg.current_schema.upper()}"')
            cur.execute("SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL")
            self.owner = cur.fetchone()[0]

    def _rows(self, sql: str, **binds):
        with self.conn.cursor() as cur:
            cur.execute(sql, binds)
            return cur.fetchall()

    def introspect(self) -> list[TableMeta]:
        o = self.owner
        objs = self._rows("""
            SELECT t.TABLE_NAME, 'TABLE', t.NUM_ROWS, c.COMMENTS FROM ALL_TABLES t
              LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = t.OWNER AND c.TABLE_NAME = t.TABLE_NAME
             WHERE t.OWNER = :o AND t.NESTED = 'NO' AND t.SECONDARY = 'N'
            UNION ALL
            SELECT v.VIEW_NAME, 'VIEW', NULL, c.COMMENTS FROM ALL_VIEWS v
              LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = v.OWNER AND c.TABLE_NAME = v.VIEW_NAME
             WHERE v.OWNER = :o""", o=o)
        cols = self._rows("""
            SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.DATA_PRECISION, c.DATA_SCALE, c.CHAR_LENGTH,
                   c.NULLABLE, cc.COMMENTS, s.NUM_DISTINCT, s.NUM_NULLS
              FROM ALL_TAB_COLUMNS c
              LEFT JOIN ALL_COL_COMMENTS cc ON cc.OWNER = c.OWNER AND cc.TABLE_NAME = c.TABLE_NAME
                                           AND cc.COLUMN_NAME = c.COLUMN_NAME
              LEFT JOIN ALL_TAB_COL_STATISTICS s ON s.OWNER = c.OWNER AND s.TABLE_NAME = c.TABLE_NAME
                                                AND s.COLUMN_NAME = c.COLUMN_NAME
             WHERE c.OWNER = :o ORDER BY c.TABLE_NAME, c.COLUMN_ID""", o=o)
        cons = self._rows("""
            SELECT c.CONSTRAINT_TYPE, c.TABLE_NAME, cc.COLUMN_NAME, cc.POSITION, c.CONSTRAINT_NAME,
                   r.TABLE_NAME, rc.COLUMN_NAME
              FROM ALL_CONSTRAINTS c
              JOIN ALL_CONS_COLUMNS cc ON cc.OWNER = c.OWNER AND cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
              LEFT JOIN ALL_CONSTRAINTS r ON r.OWNER = c.R_OWNER AND r.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME
              LEFT JOIN ALL_CONS_COLUMNS rc ON rc.OWNER = r.OWNER AND rc.CONSTRAINT_NAME = r.CONSTRAINT_NAME
                                           AND rc.POSITION = cc.POSITION
             WHERE c.OWNER = :o AND c.CONSTRAINT_TYPE IN ('P','R')
             ORDER BY c.CONSTRAINT_NAME, cc.POSITION""", o=o)

        tables: dict[str, TableMeta] = {}
        for name, kind, nrows, comment in objs:
            tables[name] = TableMeta(name=name, kind=kind, num_rows=nrows, comment=comment, columns=[])
        for tname, cname, dtype, prec, scale, clen, nullable, comment, ndist, nnull in cols:
            if tname not in tables:
                continue
            if dtype == "NUMBER" and prec is not None:
                dtype = f"NUMBER({prec},{scale or 0})"
            elif dtype in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR") and clen:
                dtype = f"{dtype}({clen})"
            tables[tname].columns.append(ColumnMeta(name=cname, data_type=dtype, nullable=nullable == "Y",
                                                    comment=comment, num_distinct=ndist, num_nulls=nnull))
        fks: dict[str, ForeignKeyMeta] = {}
        for ctype, tname, cname, _pos, cons_name, rtable, rcol in cons:
            if tname not in tables:
                continue
            if ctype == "P":
                tables[tname].primary_key.append(cname)
            else:
                fk = fks.setdefault(cons_name, ForeignKeyMeta(columns=[], ref_table=rtable, ref_columns=[]))
                fk.columns.append(cname)
                fk.ref_columns.append(rcol)
                if fk not in tables[tname].foreign_keys:
                    tables[tname].foreign_keys.append(fk)
        return self.filter_tables(list(tables.values()))

    def query(self, sql: str, max_rows: int | None = None) -> tuple[pd.DataFrame, bool]:
        sql = sql.strip().rstrip(";")
        with self.conn.cursor() as cur:
            cur.arraysize = 5000
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            if max_rows:
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
            else:
                rows, truncated = cur.fetchall(), False
        return finalize_df(pd.DataFrame(rows, columns=cols)), truncated
