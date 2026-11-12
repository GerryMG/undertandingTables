"""Genera un DW sintético (estilo AdventureWorks, giro restaurantes + tiendas) en DuckDB.

- Nombres de tablas/columnas crípticos a propósito (TDA_TP, IMP_NTO, MTV_CVE...)
- Solo algunas tablas/columnas tienen comentarios (documentación parcial, como en la vida real)
- Sin FKs declaradas en los hechos (el motor debe inferirlas y verificarlas)
- Anomalías sembradas; la verdad queda en data/anomalias_sembradas.md (el motor NO la ve)

Uso:  python scripts/make_test_dw.py  [--out data/test_dw.duckdb]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

RNG = np.random.default_rng(42)
START, END = "2025-01-01", "2026-11-11"

REGIONES = [(1, "Centro"), (2, "Occidente"), (3, "Norte"), (4, "Bajío")]

# id, nombre, tipo(R/T), ciudad, region, apertura, m2, base tickets/día
TIENDAS = [
    (1, "Restaurante Polanco", "R", "Ciudad de México", 1, "2019-03-01", 320, 70),
    (2, "Restaurante Polanco II", "R", "Ciudad de México", 1, "2023-08-15", 210, 45),
    (3, "Restaurante Condesa", "R", "Ciudad de México", 1, "2020-11-20", 180, 55),
    (4, "Restaurante Andares", "R", "Zapopan", 2, "2021-05-10", 260, 60),
    (5, "Restaurante San Pedro", "R", "San Pedro Garza García", 3, "2022-02-01", 240, 50),
    (6, "Tienda Satélite", "T", "Naucalpan", 1, "2018-06-01", 900, 140),
    (7, "Tienda Coyoacán", "T", "Ciudad de México", 1, "2019-09-01", 750, 120),
    (8, "Tienda Zapopan", "T", "Zapopan", 2, "2020-01-15", 820, 110),
    (9, "Tienda Cumbres", "T", "Monterrey", 3, "2021-07-01", 980, 125),
    (10, "Tienda Puebla Centro", "T", "Puebla", 1, "2022-04-01", 640, 90),
    (11, "Restaurante Angelópolis", "R", "Puebla", 1, "2023-01-20", 200, 40),
    (12, "Tienda Juriquilla", "T", "Querétaro", 4, "2024-03-01", 700, 85),
]

# lineas: id, desc, tipo (R restaurante, T tienda, A ambos)
LINEAS = [
    (1, "Entradas", "R"), (2, "Platos fuertes", "R"), (3, "Postres", "R"), (4, "Bebidas", "A"),
    (5, "Lácteos", "T"), (6, "Panadería", "T"), (7, "Carnes frías", "T"), (8, "Abarrotes", "T"),
    (9, "Limpieza", "T"),
]
PRODUCTOS_BASE = {
    1: (["Guacamole", "Sopa de tortilla", "Queso fundido", "Tostadas de atún", "Ensalada César", "Esquites",
         "Sopes de chorizo", "Ceviche de pescado"], (95, 210)),
    2: (["Tacos al pastor", "Arrachera", "Enchiladas suizas", "Mole poblano", "Pescado a la talla",
         "Chiles en nogada", "Cochinita pibil", "Pozole rojo", "Hamburguesa de la casa", "Salmón al grill",
         "Pechuga rellena", "Costillas BBQ"], (165, 420)),
    3: (["Flan napolitano", "Pastel de chocolate", "Churros", "Pay de queso", "Helado artesanal", "Tres leches"],
        (75, 140)),
    4: (["Agua mineral 600ml", "Refresco cola 600ml", "Agua de horchata", "Limonada", "Café americano",
         "Cerveza clara", "Jugo de naranja 1L", "Té helado"], (25, 85)),
    5: (["Leche entera 1L", "Leche deslactosada 1L", "Yogur natural 1kg", "Queso panela 400g", "Crema 500ml",
         "Mantequilla 90g", "Queso Oaxaca 400g", "Yogur bebible fresa"], (18, 120)),
    6: (["Bolillo pieza", "Pan de caja blanco", "Concha pieza", "Pan integral", "Cuernito pieza", "Dona glaseada"],
        (4, 55)),
    7: (["Jamón de pavo 250g", "Salchicha viena 500g", "Chorizo 250g", "Tocino 250g", "Salami 200g"], (35, 110)),
    8: (["Arroz 1kg", "Frijol negro 1kg", "Aceite vegetal 1L", "Azúcar 1kg", "Atún en agua", "Pasta spaghetti",
         "Café soluble 200g", "Cereal de maíz", "Galletas surtidas", "Sal 1kg", "Harina de trigo 1kg",
         "Chiles jalapeños lata"], (15, 160)),
    9: (["Detergente 1kg", "Cloro 1L", "Jabón de trastes", "Papel higiénico 4p", "Suavizante 850ml",
         "Limpiador multiusos"], (20, 95)),
}

NOMBRES = ["Ana", "Luis", "María", "José", "Carmen", "Jorge", "Sofía", "Miguel", "Daniela", "Fernando", "Paola",
           "Ricardo", "Gabriela", "Alejandro", "Valeria", "Héctor", "Mónica", "Arturo", "Claudia", "Raúl",
           "Patricia", "Eduardo", "Verónica", "Sergio", "Adriana", "Manuel", "Leticia", "Óscar", "Diana", "Iván"]
APELLIDOS = ["García", "Hernández", "López", "Martínez", "González", "Pérez", "Rodríguez", "Sánchez", "Ramírez",
             "Cruz", "Flores", "Gómez", "Morales", "Vázquez", "Reyes", "Jiménez", "Torres", "Díaz", "Gutiérrez",
             "Ruiz", "Aguilar", "Castillo", "Ortiz", "Navarro", "Romero", "Medina", "Herrera", "Castro"]

MOTIVOS = [("CAD", "Producto caducado"), ("DAN", "Producto dañado"), ("ERR", "Error de cobro"),
           ("INS", "Insatisfacción del cliente"), ("OTR", "Otro")]

ANOM_EMP_SATELITE = "Rodrigo Fuentes Ibarra"
ANOM_EMP_ANDARES = "Lucía Méndez Ortega"
ANOM_EMP_CONDESA = "Javier Ríos Salinas"


def nombre_aleatorio(usados: set[str]) -> str:
    while True:
        n = f"{RNG.choice(NOMBRES)} {RNG.choice(APELLIDOS)} {RNG.choice(APELLIDOS)}"
        if n not in usados:
            usados.add(n)
            return n


def build():
    days = pd.date_range(START, END, freq="D")
    nd = len(days)

    # ---------------- dimensiones ----------------
    dim_rgn = pd.DataFrame(REGIONES, columns=["RGN_ID", "RGN_DESC"])
    dim_tda = pd.DataFrame(
        [(t[0], t[1], t[2], t[3], t[4], pd.Timestamp(t[5]).date(), t[6]) for t in TIENDAS],
        columns=["TDA_ID", "TDA_NOM", "TDA_TP", "CD_NOM", "RGN_ID", "FEC_APE", "M2_SUP"],
    )
    dim_lin = pd.DataFrame(LINEAS, columns=["LIN_ID", "LIN_DESC", "LIN_TP"])

    prods = []
    pid = 1000
    for lin_id, (names, (lo, hi)) in PRODUCTOS_BASE.items():
        for n in names:
            pid += 1
            prods.append((pid, n, lin_id, round(float(RNG.uniform(lo, hi)), 2), "PZA"))
    dim_prod = pd.DataFrame(prods, columns=["PRD_ID", "PRD_DESC", "LIN_ID", "PRC_LST", "UOM_CVE"])
    dim_prod["POP"] = RNG.gamma(1.5, 1.0, len(dim_prod))  # popularidad (no se exporta)

    usados: set[str] = {ANOM_EMP_SATELITE, ANOM_EMP_ANDARES, ANOM_EMP_CONDESA}
    emps = []
    eid = 500
    for t in TIENDAS:
        tda_id, tipo = t[0], t[2]
        n = 6 if tipo == "T" else 7
        for i in range(n):
            eid += 1
            if tda_id == 6 and i == 2:
                nom = ANOM_EMP_SATELITE
            elif tda_id == 4 and i == 3:
                nom = ANOM_EMP_ANDARES
            elif tda_id == 3 and i == 1:
                nom = ANOM_EMP_CONDESA
            else:
                nom = nombre_aleatorio(usados)
            pto = "GTE" if i == 0 else ("CAJ" if tipo == "T" else ("MES" if i > 2 else "CAJ"))
            emps.append((eid, nom, tda_id, pto))
    dim_emp = pd.DataFrame(emps, columns=["EMP_ID", "EMP_NOM", "TDA_ID", "PTO_CVE"])

    ctes = [(0, "PUBLICO EN GENERAL", "GEN")]
    usados_c: set[str] = set()
    for i in range(1, 2001):
        ctes.append((i, nombre_aleatorio(usados_c).upper(), RNG.choice(["FRE", "GEN", "EMP"], p=[.3, .6, .1])))
    dim_cte = pd.DataFrame(ctes, columns=["CTE_ID", "CTE_NOM", "SEG_CVE"])

    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre",
             "Noviembre", "Diciembre"]
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    festivos = {"2025-01-01", "2025-02-03", "2025-03-17", "2025-05-01", "2025-09-16", "2025-11-17", "2025-12-25",
                "2026-01-01", "2026-02-02", "2026-03-16", "2026-05-01", "2026-09-16", "2026-11-16"}
    dim_fec = pd.DataFrame({
        "FEC_ID": days.strftime("%Y%m%d").astype(int),
        "FEC": days.date,
        "ANIO": days.year,
        "MES": days.month,
        "NOM_MES": [meses[m - 1] for m in days.month],
        "DIA_SEM": days.day_of_week + 1,
        "NOM_DIA": [dias[d] for d in days.day_of_week],
        "SEM_ANIO": days.isocalendar().week.values.astype(int),
        "FES_FLG": ["S" if d in festivos else "N" for d in days.strftime("%Y-%m-%d")],
    })
    dim_mtv = pd.DataFrame(MOTIVOS, columns=["MTV_CVE", "MTV_DESC"])

    # ---------------- ventas ----------------
    month_f = {1: .9, 2: .9, 3: 1, 4: 1.02, 5: 1.05, 6: 1, 7: 1, 8: 1, 9: .97, 10: 1, 11: 1.08, 12: 1.25}
    lines_parts = []
    tk_seq = {}
    for t in TIENDAS:
        tda_id, tipo, base = t[0], t[2], t[7]
        dow = days.day_of_week.values
        if tipo == "R":
            dow_f = np.where(dow >= 4, 1.35, 0.9)
            pool = dim_prod[dim_prod.LIN_ID.isin([1, 2, 3, 4])]
            hrs = np.arange(13, 24)
        else:
            dow_f = np.where(dow >= 5, 1.25, 0.95)
            pool = dim_prod[dim_prod.LIN_ID.isin([4, 5, 6, 7, 8, 9])]
            hrs = np.arange(8, 22)
        mf = np.array([month_f[m] for m in days.month])
        trend = 1 + 0.08 * (np.arange(nd) / 365)
        lam = base * dow_f * mf * trend
        if tda_id == 9:  # cierre Tienda Cumbres
            closed = (days >= "2026-02-10") & (days <= "2026-02-12")
            lam = np.where(closed, 0, lam)
        nt = RNG.poisson(lam)
        day_idx = np.repeat(np.arange(nd), nt)
        T = len(day_idx)
        store_emps = dim_emp[(dim_emp.TDA_ID == tda_id) & (dim_emp.PTO_CVE != "GTE")].EMP_ID.values
        emp = RNG.choice(store_emps, T)
        cte = np.where(RNG.random(T) < 0.7, 0, RNG.integers(1, 2001, T))
        hr = RNG.choice(hrs, T)
        tk = np.array([f"{tda_id:02d}-{i:07d}" for i in range(1, T + 1)])
        tk_seq[tda_id] = T
        nl = 1 + RNG.poisson(1.6 if tipo == "T" else 1.3, T)
        rep = np.repeat(np.arange(T), nl)
        L = len(rep)
        w = pool.POP.values / pool.POP.values.sum()
        pidx = RNG.choice(len(pool), L, p=w)
        price = pool.PRC_LST.values[pidx] * RNG.uniform(.98, 1.02, L)
        qty = (1 + RNG.poisson(0.8 if tipo == "T" else 0.3, L)).astype(int)
        brt = np.round(price * qty, 2)
        dsc_pct = np.where(RNG.random(L) < 0.1, RNG.uniform(.05, .15, L), 0)
        emp_l = emp[rep]
        df = pd.DataFrame({
            "TKT_NUM": tk[rep],
            "TKT_LIN": pd.Series(rep).groupby(rep).cumcount().values + 1,
            "FEC_ID": dim_fec.FEC_ID.values[day_idx[rep]],
            "HR_VTA": hr[rep],
            "TDA_ID": tda_id,
            "PRD_ID": pool.PRD_ID.values[pidx],
            "EMP_ID": emp_l,
            "CTE_ID": cte[rep],
            "CANT": qty,
            "IMP_BRT": brt,
            "DSC_PCT": dsc_pct,
        })
        if tda_id == 3:  # descuentos excesivos de Javier (Condesa) desde abril 2026
            jav = dim_emp[dim_emp.EMP_NOM == ANOM_EMP_CONDESA].EMP_ID.iloc[0]
            m = (df.EMP_ID == jav) & (df.FEC_ID >= 20260401) & (RNG.random(len(df)) < 0.3)
            df.loc[m, "DSC_PCT"] = RNG.uniform(.5, 1.0, m.sum())
        lines_parts.append(df)
    vta = pd.concat(lines_parts, ignore_index=True)
    vta["IMP_DSC"] = np.round(vta.IMP_BRT * vta.DSC_PCT, 2)
    vta["IMP_NTO"] = np.round(vta.IMP_BRT - vta.IMP_DSC, 2)
    vta["IMP_IVA"] = np.round(vta.IMP_NTO * 0.16, 2)
    vta = vta.drop(columns=["DSC_PCT"])

    # ---------------- devoluciones ----------------
    fec_ids = dim_fec.FEC_ID.values
    fec_pos = {f: i for i, f in enumerate(fec_ids)}
    tipo_tda = dict(zip(dim_tda.TDA_ID, dim_tda.TDA_TP))
    p = np.where(vta.TDA_ID.map(tipo_tda) == "T", 0.012, 0.004)
    base = vta[RNG.random(len(vta)) < p].copy()
    n = len(base)
    lag = np.minimum(RNG.geometric(0.4, n) - 1, 10)
    pos = np.minimum(base.FEC_ID.map(fec_pos).values + lag, len(fec_ids) - 1)
    is_t = base.TDA_ID.map(tipo_tda).values == "T"
    hr_dev = np.where(is_t, RNG.integers(9, 21, n), RNG.integers(13, 23, n))
    cajeros = dim_emp[dim_emp.PTO_CVE == "CAJ"].groupby("TDA_ID").EMP_ID.apply(list).to_dict()
    emp_dev = np.array([RNG.choice(cajeros[t]) for t in base.TDA_ID.values])
    qret = np.array([RNG.integers(1, q + 1) for q in base.CANT.values])
    mtv_t = RNG.choice(["CAD", "DAN", "ERR", "INS", "OTR"], n, p=[.3, .3, .15, .15, .1])
    mtv_r = RNG.choice(["ERR", "INS", "OTR"], n, p=[.3, .55, .15])
    dev = pd.DataFrame({
        "FEC_ID": fec_ids[pos],
        "HR_DEV": hr_dev,
        "TDA_ID": base.TDA_ID.values,
        "PRD_ID": base.PRD_ID.values,
        "EMP_ID": emp_dev,
        "TKT_ORIG": np.where(RNG.random(n) < 0.04, None, base.TKT_NUM.values),
        "CANT": qret,
        "IMP_DEV": np.round(base.IMP_NTO.values * qret / base.CANT.values, 2),
        "MTV_CVE": np.where(is_t, mtv_t, mtv_r),
    })

    extras = []
    # A) Rodrigo (Satélite): devoluciones sin ticket, montos redondos, desde jun-2026
    rod = dim_emp[dim_emp.EMP_NOM == ANOM_EMP_SATELITE].EMP_ID.iloc[0]
    sat_pool = dim_prod[dim_prod.LIN_ID.isin([5, 6, 7, 8, 9])]
    window = dim_fec[(dim_fec.FEC_ID >= 20260601)].FEC_ID.values
    k = 260
    extras.append(pd.DataFrame({
        "FEC_ID": RNG.choice(window, k), "HR_DEV": RNG.integers(10, 20, k), "TDA_ID": 6,
        "PRD_ID": RNG.choice(sat_pool.PRD_ID.values, k), "EMP_ID": rod,
        "TKT_ORIG": np.where(RNG.random(k) < 0.85, None, RNG.choice(vta[vta.TDA_ID == 6].TKT_NUM.values, k)),
        "CANT": 1, "IMP_DEV": RNG.choice([200.0, 250.0, 300.0, 400.0, 500.0], k),
        "MTV_CVE": RNG.choice(["OTR", "INS"], k, p=[.6, .4]),
    }))
    # B) Zapopan, Lácteos, semana 10-16 ago 2026: pico de caducados (falla de cadena de frío)
    lac = vta[(vta.TDA_ID == 8) & (vta.FEC_ID.between(20260801, 20260815)) &
              (vta.PRD_ID.isin(dim_prod[dim_prod.LIN_ID == 5].PRD_ID))].sample(120, random_state=1)
    extras.append(pd.DataFrame({
        "FEC_ID": RNG.choice(dim_fec[dim_fec.FEC_ID.between(20260810, 20260816)].FEC_ID.values, 120),
        "HR_DEV": RNG.integers(9, 21, 120), "TDA_ID": 8, "PRD_ID": lac.PRD_ID.values,
        "EMP_ID": RNG.choice(cajeros[8], 120), "TKT_ORIG": lac.TKT_NUM.values, "CANT": lac.CANT.values,
        "IMP_DEV": lac.IMP_NTO.values, "MTV_CVE": "CAD",
    }))
    # C) Lucía (Andares): devoluciones de madrugada (00-01h, restaurante cerrado), sep-oct 2026
    luc = dim_emp[dim_emp.EMP_NOM == ANOM_EMP_ANDARES].EMP_ID.iloc[0]
    luc_tk = vta[(vta.TDA_ID == 4) & (vta.FEC_ID.between(20260825, 20261031))].sample(45, random_state=2)
    extras.append(pd.DataFrame({
        "FEC_ID": RNG.choice(dim_fec[dim_fec.FEC_ID.between(20260901, 20261031)].FEC_ID.values, 45),
        "HR_DEV": RNG.integers(0, 2, 45), "TDA_ID": 4, "PRD_ID": luc_tk.PRD_ID.values, "EMP_ID": luc,
        "TKT_ORIG": luc_tk.TKT_NUM.values, "CANT": luc_tk.CANT.values, "IMP_DEV": luc_tk.IMP_NTO.values,
        "MTV_CVE": "INS",
    }))
    # D) Puebla Centro (tienda) devuelve platillos de restaurante, sin ticket, mar-2026
    platos = dim_prod[dim_prod.LIN_ID == 2]
    extras.append(pd.DataFrame({
        "FEC_ID": RNG.choice(dim_fec[dim_fec.FEC_ID.between(20260301, 20260331)].FEC_ID.values, 15),
        "HR_DEV": RNG.integers(10, 20, 15), "TDA_ID": 10, "PRD_ID": RNG.choice(platos.PRD_ID.values, 15),
        "EMP_ID": RNG.choice(cajeros[10], 15), "TKT_ORIG": None, "CANT": 1,
        "IMP_DEV": np.round(RNG.choice(platos.PRC_LST.values, 15), 2), "MTV_CVE": "OTR",
    }))
    dev = pd.concat([dev] + extras, ignore_index=True)
    dev = dev.sort_values(["FEC_ID", "TDA_ID"]).reset_index(drop=True)
    dev.insert(0, "DEV_ID", np.arange(1, len(dev) + 1))

    # ---------------- inventario semanal (solo tiendas) ----------------
    domingos = dim_fec[dim_fec.DIA_SEM == 7].FEC_ID.values
    inv_parts = []
    for t in [t for t in TIENDAS if t[2] == "T"]:
        pool = dim_prod[dim_prod.LIN_ID.isin([4, 5, 6, 7, 8, 9])]
        g = np.array(np.meshgrid(domingos, pool.PRD_ID.values)).T.reshape(-1, 2)
        cst = pool.set_index("PRD_ID").PRC_LST.reindex(g[:, 1]).values * 0.62
        inv_parts.append(pd.DataFrame({"FEC_ID": g[:, 0], "TDA_ID": t[0], "PRD_ID": g[:, 1],
                                       "EXIST_CANT": RNG.poisson(40, len(g)), "CST_UNIT": np.round(cst, 2)}))
    inv = pd.concat(inv_parts, ignore_index=True)

    # ---------------- ruido: auditoría y staging ----------------
    aud = pd.DataFrame({
        "BIT_ID": np.arange(1, 501),
        "USR_CVE": RNG.choice(["ETL_USR", "ADMIN", "JPEREZ"], 500),
        "TBL_NOM": RNG.choice(["FCT_VTA", "FCT_DEV", "DIM_PROD"], 500),
        "ACC_CVE": RNG.choice(["INS", "UPD", "DEL"], 500),
        "TS_REG": pd.Timestamp("2026-01-01") + pd.to_timedelta(RNG.integers(0, 300 * 86400, 500), unit="s"),
    })
    tmp = vta.sample(2000, random_state=3)[["TKT_NUM", "FEC_ID", "TDA_ID", "PRD_ID", "IMP_NTO"]].copy()
    tmp["EST_CARGA"] = RNG.choice(["OK", "ERR", "PEND"], len(tmp))

    return dict(DIM_RGN=dim_rgn, DIM_TDA=dim_tda, DIM_LIN=dim_lin, DIM_PROD=dim_prod.drop(columns=["POP"]),
                DIM_EMP=dim_emp, DIM_CTE=dim_cte, DIM_FEC=dim_fec, DIM_MTV=dim_mtv, FCT_VTA=vta, FCT_DEV=dev,
                FCT_INV=inv, AUD_BITACORA=aud, TMP_CARGA_VTA=tmp)


DDL = """
CREATE TABLE DIM_RGN (RGN_ID INTEGER PRIMARY KEY, RGN_DESC VARCHAR(40));
CREATE TABLE DIM_TDA (TDA_ID INTEGER PRIMARY KEY, TDA_NOM VARCHAR(80), TDA_TP VARCHAR(1), CD_NOM VARCHAR(60),
                      RGN_ID INTEGER, FEC_APE DATE, M2_SUP INTEGER);
CREATE TABLE DIM_LIN (LIN_ID INTEGER PRIMARY KEY, LIN_DESC VARCHAR(60), LIN_TP VARCHAR(1));
CREATE TABLE DIM_PROD (PRD_ID INTEGER PRIMARY KEY, PRD_DESC VARCHAR(100), LIN_ID INTEGER REFERENCES DIM_LIN(LIN_ID),
                       PRC_LST DECIMAL(12,2), UOM_CVE VARCHAR(5));
CREATE TABLE DIM_EMP (EMP_ID INTEGER PRIMARY KEY, EMP_NOM VARCHAR(100), TDA_ID INTEGER, PTO_CVE VARCHAR(3));
CREATE TABLE DIM_CTE (CTE_ID INTEGER PRIMARY KEY, CTE_NOM VARCHAR(120), SEG_CVE VARCHAR(3));
CREATE TABLE DIM_FEC (FEC_ID INTEGER PRIMARY KEY, FEC DATE, ANIO INTEGER, MES INTEGER, NOM_MES VARCHAR(12),
                      DIA_SEM INTEGER, NOM_DIA VARCHAR(12), SEM_ANIO INTEGER, FES_FLG VARCHAR(1));
CREATE TABLE DIM_MTV (MTV_CVE VARCHAR(3) PRIMARY KEY, MTV_DESC VARCHAR(60));
CREATE TABLE FCT_VTA (TKT_NUM VARCHAR(12), TKT_LIN INTEGER, FEC_ID INTEGER, HR_VTA INTEGER, TDA_ID INTEGER,
                      PRD_ID INTEGER, EMP_ID INTEGER, CTE_ID INTEGER, CANT INTEGER, IMP_BRT DECIMAL(12,2),
                      IMP_DSC DECIMAL(12,2), IMP_NTO DECIMAL(12,2), IMP_IVA DECIMAL(12,2));
CREATE TABLE FCT_DEV (DEV_ID INTEGER, FEC_ID INTEGER, HR_DEV INTEGER, TDA_ID INTEGER, PRD_ID INTEGER, EMP_ID INTEGER,
                      TKT_ORIG VARCHAR(12), CANT INTEGER, IMP_DEV DECIMAL(12,2), MTV_CVE VARCHAR(3));
CREATE TABLE FCT_INV (FEC_ID INTEGER, TDA_ID INTEGER, PRD_ID INTEGER, EXIST_CANT INTEGER, CST_UNIT DECIMAL(12,2));
CREATE TABLE AUD_BITACORA (BIT_ID INTEGER, USR_CVE VARCHAR(20), TBL_NOM VARCHAR(30), ACC_CVE VARCHAR(3),
                           TS_REG TIMESTAMP);
CREATE TABLE TMP_CARGA_VTA (TKT_NUM VARCHAR(12), FEC_ID INTEGER, TDA_ID INTEGER, PRD_ID INTEGER,
                            IMP_NTO DECIMAL(12,2), EST_CARGA VARCHAR(4));
"""

# Documentación parcial (como en la vida real: algunas cosas sí, la mayoría no)
COMMENTS = """
COMMENT ON TABLE FCT_VTA IS 'Ventas detalle ticket';
COMMENT ON COLUMN FCT_VTA.IMP_NTO IS 'Importe neto sin IVA';
COMMENT ON TABLE DIM_MTV IS 'Catalogo motivos';
COMMENT ON COLUMN DIM_TDA.TDA_TP IS 'R=Rest, T=Tda';
COMMENT ON TABLE FCT_INV IS 'Foto semanal (domingo) de existencias';
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/test_dw.duckdb")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    tables = build()
    con = duckdb.connect(str(out))
    con.execute(DDL)
    for name, df in tables.items():
        con.register("_df", df)
        con.execute(f"INSERT INTO {name} SELECT * FROM _df")
        con.unregister("_df")
    con.execute(COMMENTS)
    con.close()
    for name, df in tables.items():
        print(f"{name:15s} {len(df):>9,d} filas")

    truth = Path(out.parent / "anomalias_sembradas.md")
    truth.write_text(f"""# Anomalías sembradas (verdad de referencia — el motor NO lee este archivo)

1. **Devoluciones sospechosas — {ANOM_EMP_SATELITE} (Tienda Satélite, cajero)**: desde 2026-06-01, ~260
   devoluciones extra, 85% sin ticket original, montos redondos (200/250/300/400/500), motivo OTR/INS.
2. **Pico de caducados — Tienda Zapopan, línea Lácteos, 2026-08-10 a 2026-08-16**: ~120 devoluciones motivo CAD.
3. **Devoluciones de madrugada — {ANOM_EMP_ANDARES} (Restaurante Andares)**: 45 devoluciones a las 00-01h
   (restaurante cerrado), sep–oct 2026.
4. **Tienda Puebla Centro devuelve platillos de restaurante** (línea Platos fuertes) sin ticket, marzo 2026 (15).
5. **Descuentos excesivos — {ANOM_EMP_CONDESA} (Restaurante Condesa)**: desde 2026-04-01, ~30% de sus líneas con
   50–100% de descuento.
6. **Cierre — Tienda Cumbres sin ventas 2026-02-10 a 2026-02-12.**
""", encoding="utf-8")
    print(f"\nDW en {out}  |  verdad en {truth}")


if __name__ == "__main__":
    main()
