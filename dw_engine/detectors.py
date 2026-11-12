"""Caja de herramientas estadística para "hallazgos". El LLM ELIGE y PARAMETRIZA detectores; el cálculo es
determinístico (pandas). Así la calidad no depende de que el modelo "vea" anomalías en filas crudas.
"""
from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

DETECTOR_DOCS = """DETECTORES DISPONIBLES (se aplican sobre el resultado de la consulta del mismo paso):
- peer_outlier: grupos que se salen de sus pares (z robusto con mediana/MAD).
    params: group_col, value_col  |  o bien numerator_col + denominator_col (tasa), min_denominator,
            direction ('high'|'low'|'both'), threshold (default 3.5)
    ej: tasa de devolución por empleado; descuento promedio por cajero.
- share_outlier: proporción de una condición por grupo vs. la proporción global (prueba de proporciones).
    params: group_col, count_col (total del grupo), flag_col (cuántos cumplen la condición), min_count, threshold (4)
    ej: % de devoluciones sin ticket por empleado; % de montos redondos; % fuera de horario.
- time_spike: picos o caídas en una serie temporal vs. su línea base móvil (por grupo opcional).
    params: date_col, value_col, group_col (opcional), window (28), threshold (4), direction ('both')
    ej: devoluciones diarias por tienda; ventas diarias por tienda (detecta cierres).
- period_change: qué grupos cambiaron más entre dos periodos.
    params: group_col, current_col, previous_col, top (10)
- top_contributors: concentración / Pareto de un valor por grupo.
    params: group_col, value_col, top (10)
La consulta del paso debe devolver EXACTAMENTE las columnas que nombres en params (alias en MAYÚSCULAS)."""


class DetectorCall(BaseModel):
    type: Literal["peer_outlier", "share_outlier", "time_spike", "period_change", "top_contributors"]
    params: dict[str, Any] = Field(default_factory=dict)


def _col(df: pd.DataFrame, name: str | None) -> str | None:
    if name is None:
        return None
    for c in df.columns:
        if c.upper() == str(name).upper():
            return c
    raise KeyError(f"columna '{name}' no está en el resultado (columnas: {list(df.columns)})")


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype(float)


def _robust_z(x: pd.Series) -> pd.Series:
    med = x.median()
    mad = (x - med).abs().median()
    if not mad or np.isnan(mad):
        mad = (x - med).abs().mean() or 1e-9
        return (x - med) / (1.2533 * mad)
    return 0.6745 * (x - med) / mad


def peer_outlier(df, group_col, value_col=None, numerator_col=None, denominator_col=None, min_denominator=0,
                 direction="high", threshold=3.5, **_):
    g = _col(df, group_col)
    d = pd.DataFrame({"grupo": df[g].astype(str)})
    if numerator_col:
        num, den = _num(df[_col(df, numerator_col)]), _num(df[_col(df, denominator_col)])
        d["valor"] = num / den.replace(0, np.nan)
        d["numerador"], d["denominador"] = num, den
        d = d[d["denominador"] >= float(min_denominator or 0)]
    else:
        d["valor"] = _num(df[_col(df, value_col)])
    d = d.dropna(subset=["valor"])
    if len(d) < 5:
        return {"findings": [], "note": f"solo {len(d)} grupos; se necesitan ≥5 para comparar contra pares"}
    d["z"] = _robust_z(d["valor"])
    med = d["valor"].median()
    mask = (d.z >= threshold) if direction == "high" else (d.z <= -threshold) if direction == "low" \
        else (d.z.abs() >= threshold)
    out = d[mask].sort_values("z", key=abs, ascending=False).head(8)
    findings = [{"grupo": r.grupo, "valor": round(r.valor, 4), "mediana_pares": round(med, 4),
                 "veces_mediana": round(r.valor / med, 2) if med else None, "z_robusto": round(r.z, 1)}
                for r in out.itertuples()]
    return {"findings": findings, "grupos_analizados": len(d), "mediana": round(med, 4),
            "tabla": d.sort_values("z", ascending=False).head(30).round(4).to_dict("records")}


def share_outlier(df, group_col, count_col, flag_col, min_count=20, threshold=4.0, **_):
    g = _col(df, group_col)
    n, k = _num(df[_col(df, count_col)]), _num(df[_col(df, flag_col)])
    d = pd.DataFrame({"grupo": df[g].astype(str), "n": n, "k": k}).groupby("grupo", as_index=False).sum()
    p0 = d.k.sum() / d.n.sum() if d.n.sum() else 0
    d = d[d.n >= float(min_count or 0)].copy()
    if d.empty or p0 in (0, 1):
        return {"findings": [], "proporcion_global": round(p0, 4)}
    d["p"] = d.k / d.n
    d["z"] = (d.p - p0) / np.sqrt(p0 * (1 - p0) / d.n)
    out = d[d.z >= threshold].sort_values("z", ascending=False).head(8)
    findings = [{"grupo": r.grupo, "proporcion": round(r.p, 4), "proporcion_global": round(p0, 4),
                 "casos": int(r.k), "total": int(r.n), "veces_global": round(r.p / p0, 1), "z": round(r.z, 1)}
                for r in out.itertuples()]
    return {"findings": findings, "proporcion_global": round(p0, 4),
            "tabla": d.sort_values("z", ascending=False).head(30).round(4).to_dict("records")}


def time_spike(df, date_col, value_col, group_col=None, window=28, threshold=4.0, direction="both", **_):
    dc, vc = _col(df, date_col), _col(df, value_col)
    gc = _col(df, group_col) if group_col else None
    d = pd.DataFrame({"fecha": pd.to_datetime(df[dc].astype(str), errors="coerce"), "valor": _num(df[vc]),
                      "grupo": df[gc].astype(str) if gc else "total"}).dropna(subset=["fecha"])
    window = int(window or 28)
    findings = []
    for grp, s in d.groupby("grupo"):
        s = s.groupby("fecha").valor.sum().sort_index()
        steps = s.index.to_series().diff().dropna()
        if len(steps) and steps.median() == pd.Timedelta(days=1):
            s = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="D"), fill_value=0)
        if len(s) < window + 3:
            continue
        base = s.shift(1).rolling(window, min_periods=max(7, window // 2)).median()
        mad = (s.shift(1) - base).abs().rolling(window, min_periods=max(7, window // 2)).median()
        scale = (1.4826 * mad).clip(lower=max(1e-9, float(s.std() or 1) * 0.1))
        z = (s - base) / scale
        flag = (z >= threshold) if direction == "high" else (z <= -threshold) if direction == "low" \
            else (z.abs() >= threshold)
        # ruido de bajo volumen: exige un exceso absoluto relevante (≈ 4 desviaciones tipo Poisson)
        flag &= (s - base).abs() >= 4 * np.sqrt(base.clip(lower=0) + 1)
        flagged = s[flag.fillna(False)]
        if flagged.empty:
            continue
        # agrupa días contiguos en episodios
        ep_id = (flagged.index.to_series().diff() != pd.Timedelta(days=1)).cumsum()
        for _, ep in flagged.groupby(ep_id.values):
            idx = ep.index
            findings.append({"grupo": grp, "desde": idx.min().date().isoformat(),
                             "hasta": idx.max().date().isoformat(), "dias": len(idx),
                             "tipo": "pico" if z[idx].mean() > 0 else "caída",
                             "valor_total": round(float(ep.sum()), 2),
                             "linea_base_por_dia": round(float(base[idx].mean()), 2),
                             "z_max": round(float(z[idx].abs().max()), 1)})
    findings.sort(key=lambda f: -f["z_max"] * f["dias"])
    return {"findings": findings[:12], "series": d.groupby(["fecha", "grupo"]).valor.sum().reset_index()}


def period_change(df, group_col, current_col, previous_col, top=10, **_):
    g = _col(df, group_col)
    d = pd.DataFrame({"grupo": df[g].astype(str), "actual": _num(df[_col(df, current_col)]).fillna(0),
                      "anterior": _num(df[_col(df, previous_col)]).fillna(0)})
    d["cambio"] = d.actual - d.anterior
    d["cambio_pct"] = np.where(d.anterior != 0, d.cambio / d.anterior.abs(), np.nan)
    top = int(top or 10)
    up = d.sort_values("cambio", ascending=False).head(top)
    down = d.sort_values("cambio").head(top)
    return {"findings": [{"tipo": "sube", **r} for r in up.round(4).to_dict("records")[:5]] +
                        [{"tipo": "baja", **r} for r in down.round(4).to_dict("records")[:5]],
            "tabla": d.sort_values("cambio", key=abs, ascending=False).head(30).round(4).to_dict("records")}


def top_contributors(df, group_col, value_col, top=10, **_):
    g = _col(df, group_col)
    d = pd.DataFrame({"grupo": df[g].astype(str), "valor": _num(df[_col(df, value_col)])})
    d = d.groupby("grupo", as_index=False).valor.sum().sort_values("valor", ascending=False)
    tot = d.valor.sum()
    d["participacion"] = d.valor / tot if tot else np.nan
    d["acumulado"] = d.participacion.cumsum()
    n80 = int((d.acumulado < 0.8).sum() + 1)
    return {"findings": [{"grupos_que_suman_80pct": n80, "de_un_total_de": len(d)}],
            "tabla": d.head(int(top or 10)).round(4).to_dict("records")}


REGISTRY = {"peer_outlier": peer_outlier, "share_outlier": share_outlier, "time_spike": time_spike,
            "period_change": period_change, "top_contributors": top_contributors}


def run_detector(call: DetectorCall, df: pd.DataFrame) -> dict:
    out = REGISTRY[call.type](df, **call.params)
    out.pop("series", None)
    out["detector"] = call.type
    out["params"] = call.params
    return out
