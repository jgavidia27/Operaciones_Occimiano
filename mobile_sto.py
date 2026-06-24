"""
mobile_sto.py — Vista móvil del Desempeño STO (Flask)
=====================================================
Espejo mobile-first de la pantalla "Desempeño STO" del dashboard Streamlit.
Lee de la MISMA fuente (Supabase) y usa la MISMA lógica de cálculo (data.py).

Ejecutar local:   python mobile_sto.py
Deploy Render:    gunicorn mobile_sto:app
"""

import os, re, unicodedata
from datetime import datetime, date
from functools import lru_cache

import pandas as pd
import requests as _requests
from flask import Flask, request, render_template_string

# ── Importar lógica compartida del dashboard ──────────────────────────────────
from data import (
    GRUPOS_TERRENO, TECNICOS_NO_APLICA, SENIORS,
    get_grupo_tecnico,
    build_work_orders_df, build_kpi_llenado_df, score_llenado_por_ot,
    build_reincidencias, classify_causa_raiz, classify_falla_type,
)
from gdrive import build_tech_name_maps, TECH_NAME_MAP

# ── Cargar .env local ─────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# DATA ACCESS (sin dependencia de Streamlit)
# ══════════════════════════════════════════════════════════════════════════════

def _query(tabla, params="", limit=10_000):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }
    results, offset, page = [], 0, 1000
    while offset < limit:
        url = f"{SUPABASE_URL}/rest/v1/{tabla}?{params}&limit={page}&offset={offset}"
        r = _requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return results[:limit]


def _load_llamados(desde="2026-01-01"):
    rows = _query(
        "v_llamados_sla",
        f"select=os_fracttal,n_llamado,cliente,eds_occim,eds_nombre,comuna,region,"
        f"fecha_llamado,hora_llamado,fecha_atencion,hora_fin,tecnico,tecnico_corto,"
        f"equipo,equipo_senior,prioridad,zona,tiempo_resp_horas,tiempo_resp_esp,"
        f"cumplimiento,estado_atencion,facturacion,fecha_creacion"
        f"&fecha_llamado=gte.{desde}",
        limit=10_000,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={"tiempo_resp_horas": "horas_resolucion"})

    def _safe_ts(x):
        if not x or str(x).strip() in ("", "None", "null"):
            return pd.NaT
        try:
            t = pd.Timestamp(str(x))
            return t.tz_convert(None) if t.tzinfo is not None else t
        except Exception:
            return pd.NaT

    df["fecha_llamado"] = df["fecha_llamado"].apply(_safe_ts)
    df["fecha_atencion"] = df["fecha_atencion"].apply(_safe_ts)
    df["fecha_llamado_dt"] = df["fecha_llamado"]
    if "cliente" in df.columns:
        df["cliente"] = df["cliente"].replace({"ESMAX (Aramco)": "Aramco (Esmax)"})
    df["cumplimiento"] = df["cumplimiento"].replace({
        "CUMPLE": "CUMPLE", "NO CUMPLE": "NO CUMPLE",
        "PENDIENTE": "SIN DATOS", "SIN UMBRAL": "SIN DATOS",
    })
    df["Año"] = df["fecha_llamado"].dt.year
    df["Mes"] = df["fecha_llamado"].dt.month
    return df


def _load_work_orders():
    _base = (
        "select=id_ot,estado,estado_tarea,codigo_activo,nombre_activo,"
        "ubicacion,cliente,estacion,codigo_eds,responsable,tipo_tarea,"
        "prioridad,prioridad_calc,fecha_creacion,fecha_inicio,"
        "fecha_finalizacion,causa_raiz,tipo_falla,modalidad_atencion,"
        "nota,nota_tarea,tiene_numeral,"
        "duracion_real_seg,duracion_estim_seg,"
        "tiene_recursos,completada"
    )
    rows = _query(
        "ordenes_trabajo",
        _base + ",numeral_inicial,numeral_final,comentario_tecnico,"
        "duracion_real_neta_seg,duracion_estim_neta_seg,form_tiene_numeral"
        "&fecha_creacion=gte.2026-01-01&order=fecha_creacion.desc",
        limit=20_000,
    )
    if not rows:
        rows = _query(
            "ordenes_trabajo",
            _base + "&fecha_creacion=gte.2026-01-01&order=fecha_creacion.desc",
            limit=20_000,
        )
    return rows


def _load_tecnicos():
    rows = _query("base_tecnicos", "select=full_name,short_name", limit=200)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# BUSINESS LOGIC (reutiliza data.py)
# ══════════════════════════════════════════════════════════════════════════════

def _norm_n(s):
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().strip().lower()

_GRUPOS_NORM = {}
for _gk, _gv in GRUPOS_TERRENO.items():
    for _mb in _gv["miembros"]:
        _GRUPOS_NORM[_norm_n(_mb)] = _gk
        _pts = _mb.split()
        for _i in range(len(_pts)):
            for _j in range(_i + 1, len(_pts)):
                _GRUPOS_NORM[_norm_n(f"{_pts[_i]} {_pts[_j]}")] = _gk

_NO_APLICA_NORM = set()
for _na in TECNICOS_NO_APLICA:
    _NO_APLICA_NORM.add(_norm_n(_na))
    _pts = _na.split()
    for _i in range(len(_pts)):
        for _j in range(_i + 1, len(_pts)):
            _NO_APLICA_NORM.add(_norm_n(f"{_pts[_i]} {_pts[_j]}"))


def _get_equipo(t):
    if not isinstance(t, str) or not t.strip():
        return "Sin equipo"
    norm = _norm_n(t.strip())
    if norm in _GRUPOS_NORM:
        return _GRUPOS_NORM[norm]
    parts = t.strip().split()
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            alias = _norm_n(f"{parts[i]} {parts[j]}")
            if alias in _GRUPOS_NORM:
                return _GRUPOS_NORM[alias]
    return "Sin equipo"


def _es_excluido(t):
    if not isinstance(t, str) or not t.strip():
        return False
    norm = _norm_n(t.strip())
    if norm in _NO_APLICA_NORM:
        return True
    parts = t.strip().split()
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if _norm_n(f"{parts[i]} {parts[j]}") in _NO_APLICA_NORM:
                return True
    return False


EQUIPO_LABEL = {k: k for k in GRUPOS_TERRENO}
EQUIPO_LABEL["Carlos Avila Norte"] = "Carlos Avila (Norte)"
EQUIPO_LABEL["Carlos Avila Sur"] = "Carlos Avila (Sur)"

BONO_TOTAL = 500_000
W_SLA, W_CAL, W_PREC = 0.40, 0.30, 0.30
MAX_SLA = int(BONO_TOTAL * W_SLA)
MAX_CAL = int(BONO_TOTAL * W_CAL)
MAX_PREC = int(BONO_TOTAL * W_PREC)


def _bono_sla(pct):
    m = MAX_SLA
    if pct >= 95: return 100, m
    if pct >= 93: return 90, int(m * .90)
    if pct >= 90: return 80, int(m * .80)
    if pct >= 85: return 50, int(m * .50)
    return 0, 0


def _bono_calidad(n_fallas, n_pms):
    exactitud = (1 - n_fallas / n_pms) * 100 if n_pms > 0 else (100.0 if n_fallas == 0 else 0.0)
    m = MAX_CAL
    if exactitud >= 98: return 100, m, exactitud
    if exactitud >= 96: return 90, int(m * .90), exactitud
    if exactitud >= 94: return 80, int(m * .80), exactitud
    if exactitud >= 92: return 70, int(m * .70), exactitud
    if exactitud >= 90: return 60, int(m * .60), exactitud
    return 0, 0, exactitud


def _bono_prec(pct):
    m = MAX_PREC
    if pct >= 95: return 100, m
    if pct >= 90: return 90, int(m * .90)
    if pct >= 85: return 80, int(m * .80)
    if pct >= 80: return 70, int(m * .70)
    if pct >= 75: return 60, int(m * .60)
    if pct >= 70: return 50, int(m * .50)
    return 0, 0


def _color_pct(pct):
    if pct >= 95: return "#22c55e"
    if pct >= 85: return "#f59e0b"
    return "#ef4444"


TRIMESTRES = {
    "T1": {"label": "T1 · Ene–Mar", "meses": [1, 2, 3]},
    "T2": {"label": "T2 · Abr–Jun", "meses": [4, 5, 6]},
    "T3": {"label": "T3 · Jul–Sep", "meses": [7, 8, 9]},
    "T4": {"label": "T4 · Oct–Dic", "meses": [10, 11, 12]},
}

SLA_OVERRIDE_CUMPLE = {"OS-37055", "OS-37448", "OS-37547"}


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    # ── Parámetros ────────────────────────────────────────────────────────────
    trim_key = request.args.get("trim", "T2")
    mes_sel = request.args.get("mes", "")
    tecnico_sel = request.args.get("tecnico", "")
    equipo_sel = request.args.get("equipo", "")

    trim = TRIMESTRES.get(trim_key, TRIMESTRES["T2"])
    meses_filtro = trim["meses"]
    if mes_sel:
        try:
            mes_num = int(mes_sel)
            if mes_num in meses_filtro:
                meses_filtro = [mes_num]
        except ValueError:
            pass

    # ── Cargar datos ──────────────────────────────────────────────────────────
    df_tec = _load_tecnicos()
    excel_to_full, full_to_excel = build_tech_name_maps(df_tec)

    df_llamados = _load_llamados()
    raw_wo = _load_work_orders()
    df_wo = build_work_orders_df(raw_wo)

    # ── Mapear técnico → equipo ───────────────────────────────────────────────
    if not df_wo.empty:
        _unique_techs = df_wo["technician"].dropna().unique()
        _tech_eq = {t: _get_equipo(t) for t in _unique_techs}
        df_wo["equipo"] = df_wo["technician"].map(_tech_eq).fillna("Sin equipo")

    # ── Lista de técnicos para el selector ────────────────────────────────────
    todos_tecnicos = sorted(set(
        t for t in df_wo["technician"].dropna().unique()
        if not _es_excluido(t) and _get_equipo(t) != "Sin equipo"
    )) if not df_wo.empty else []

    # Nombres cortos para display
    tec_display = []
    for t in todos_tecnicos:
        short = full_to_excel.get(t, t)
        eq = EQUIPO_LABEL.get(_get_equipo(t), _get_equipo(t))
        tec_display.append({"full": t, "short": short, "equipo": eq})

    # ══════════════════════════════════════════════════════════════════════════
    # KPI 1: PRODUCTIVIDAD SLA
    # ══════════════════════════════════════════════════════════════════════════
    sla_data = {"pct": 0, "cumple": 0, "total": 0, "bono_pct": 0, "bono_clp": 0,
                "equipos": [], "tecnicos": []}

    if not df_llamados.empty:
        df_sla = df_llamados[
            df_llamados["fecha_atencion"].notna() &
            df_llamados["fecha_llamado"].notna()
        ].copy()

        if not df_sla.empty:
            # Normalizar nombre técnico
            df_sla["tecnico"] = df_sla["tecnico"].apply(
                lambda t: excel_to_full.get(str(t).strip(), str(t).strip())
                if isinstance(t, str) and t.strip() else t
            )
            df_sla["equipo"] = df_sla["tecnico"].apply(_get_equipo)
            df_sla = df_sla[~df_sla["tecnico"].apply(_es_excluido)].copy()

            # Cumplimiento SLA
            if "cumplimiento" in df_sla.columns:
                df_sla["cumple_sla"] = df_sla["cumplimiento"].map(
                    {"CUMPLE": True, "NO CUMPLE": False}
                )
                _ov_mask = pd.Series(False, index=df_sla.index)
                if "os_fracttal" in df_sla.columns:
                    _ov_mask |= df_sla["os_fracttal"].astype(str).str.strip().str.upper().isin(
                        {f.replace(" ", "").upper() for f in SLA_OVERRIDE_CUMPLE}
                    )
                df_sla.loc[_ov_mask & (df_sla["cumple_sla"] == False), "cumple_sla"] = True

            # Filtrar por mes
            df_sla["mes_num"] = df_sla["fecha_llamado"].dt.month
            df_sla = df_sla[df_sla["mes_num"].isin(meses_filtro)]

            # Filtrar por equipo
            if equipo_sel:
                df_sla = df_sla[df_sla["equipo"] == equipo_sel]

            # Filtrar por técnico
            if tecnico_sel:
                df_sla = df_sla[df_sla["tecnico"] == tecnico_sel]

            df_con_pri = df_sla[df_sla["cumple_sla"].notna()].copy()
            if not df_con_pri.empty:
                total = len(df_con_pri)
                cumple = int(df_con_pri["cumple_sla"].sum())
                pct = round(cumple / total * 100, 1) if total > 0 else 0
                bp, bc = _bono_sla(pct)
                sla_data = {"pct": pct, "cumple": cumple, "total": total,
                            "bono_pct": bp, "bono_clp": bc,
                            "equipos": [], "tecnicos": []}

                # Por equipo
                eq_sum = df_con_pri[df_con_pri["equipo"] != "Sin equipo"].groupby("equipo").agg(
                    total=("cumple_sla", "count"), cumple=("cumple_sla", "sum"),
                ).reset_index()
                eq_sum["pct"] = (eq_sum["cumple"] / eq_sum["total"] * 100).round(1)
                for _, r in eq_sum.iterrows():
                    bp2, bc2 = _bono_sla(r["pct"])
                    sla_data["equipos"].append({
                        "nombre": EQUIPO_LABEL.get(r["equipo"], r["equipo"]),
                        "senior": GRUPOS_TERRENO.get(r["equipo"], {}).get("senior", ""),
                        "pct": r["pct"], "cumple": int(r["cumple"]), "total": int(r["total"]),
                        "bono_pct": bp2, "bono_clp": bc2,
                    })

                # Por técnico (ranking)
                tec_sum = df_con_pri[df_con_pri["equipo"] != "Sin equipo"].groupby(
                    ["tecnico", "equipo"]
                ).agg(
                    total=("cumple_sla", "count"), cumple=("cumple_sla", "sum"),
                ).reset_index()

                for snr in SENIORS:
                    snr_full = TECH_NAME_MAP.get(snr, snr)
                    snr_idx = tec_sum.index[tec_sum["tecnico"] == snr_full]
                    if len(snr_idx) == 0:
                        continue
                    snr_data = df_con_pri[df_con_pri["equipo"] == snr]
                    if snr_data.empty:
                        continue
                    tec_sum.at[snr_idx[0], "total"] = len(snr_data)
                    tec_sum.at[snr_idx[0], "cumple"] = int(snr_data["cumple_sla"].sum())

                tec_sum["pct"] = (tec_sum["cumple"] / tec_sum["total"] * 100).round(1)
                tec_sum = tec_sum.sort_values("pct", ascending=False)
                for _, r in tec_sum.iterrows():
                    short = full_to_excel.get(r["tecnico"], r["tecnico"])
                    bp3, bc3 = _bono_sla(r["pct"])
                    sla_data["tecnicos"].append({
                        "nombre": short, "equipo": EQUIPO_LABEL.get(r["equipo"], r["equipo"]),
                        "pct": r["pct"], "cumple": int(r["cumple"]), "total": int(r["total"]),
                        "bono_pct": bp3, "bono_clp": bc3,
                    })

    # ══════════════════════════════════════════════════════════════════════════
    # KPI 2: EFECTIVIDAD MP (Reincidencias)
    # ══════════════════════════════════════════════════════════════════════════
    cal_data = {"exactitud": 100.0, "fallas": 0, "pms_total": 0,
                "bono_pct": 100, "bono_clp": MAX_CAL, "tecnicos": []}

    if not df_wo.empty:
        _CLIENTES_SLA = {"COPEC", "Aramco (Esmax)", "ESMAX (Aramco)", "SHELL (Enex)", "ABASTIBLE"}
        df_wo_cli = df_wo[df_wo["client"].isin(_CLIENTES_SLA)].copy() if "client" in df_wo.columns else df_wo
        df_reinc = build_reincidencias(df_wo_cli, excel_to_full)

        if not df_reinc.empty and "fecha_cm" in df_reinc.columns:
            df_reinc = df_reinc[
                pd.to_datetime(df_reinc["fecha_cm"], errors="coerce") >= pd.Timestamp("2026-01-01")
            ].copy()
            if "falla_tipo" in df_reinc.columns and "causa_clasif" in df_reinc.columns:
                df_reinc["es_reincidencia_tecnico"] = (
                    (df_reinc["falla_tipo"] != "especial") &
                    (df_reinc["causa_clasif"] != "cliente")
                )

            # Filtrar por mes
            df_reinc["mes_num"] = pd.to_datetime(df_reinc["fecha_cm"]).dt.month
            df_reinc = df_reinc[df_reinc["mes_num"].isin(meses_filtro)]

            # PMs del período
            df_pm_per = df_wo_cli[df_wo_cli["maint_type"] == "Preventiva"].copy()
            if "final_date" in df_pm_per.columns:
                df_pm_per["mes_num"] = df_pm_per["final_date"].dt.tz_convert(None).dt.month
                df_pm_per = df_pm_per[df_pm_per["mes_num"].isin(meses_filtro)]
                df_pm_per["equipo"] = df_pm_per["technician"].apply(_get_equipo)
                df_pm_per = df_pm_per[~df_pm_per["technician"].apply(_es_excluido)]

            # Mapear equipo y excluir
            if not df_reinc.empty and "tecnico_resp_short" in df_reinc.columns:
                df_reinc["equipo_resp"] = df_reinc["tecnico_resp_short"].apply(
                    lambda t: _get_equipo(t)
                )

            # Filtrar por equipo/técnico
            if equipo_sel and not df_reinc.empty:
                df_reinc = df_reinc[df_reinc.get("equipo_resp", pd.Series()) == equipo_sel]
                df_pm_per = df_pm_per[df_pm_per["equipo"] == equipo_sel]
            if tecnico_sel and not df_reinc.empty:
                tec_short = full_to_excel.get(tecnico_sel, tecnico_sel)
                df_reinc = df_reinc[df_reinc["tecnico_resp_short"] == tec_short]
                df_pm_per = df_pm_per[df_pm_per["technician"] == tecnico_sel]

            fallas_tec = int(df_reinc[df_reinc.get("es_reincidencia_tecnico", False)].shape[0]) if not df_reinc.empty else 0
            pms_total = len(df_pm_per) if not df_pm_per.empty else 0
            bp_c, bc_c, exactitud = _bono_calidad(fallas_tec, pms_total)
            cal_data = {
                "exactitud": round(exactitud, 1), "fallas": fallas_tec,
                "pms_total": pms_total, "bono_pct": bp_c, "bono_clp": bc_c,
                "tecnicos": [],
            }

            # Por técnico
            if not df_reinc.empty and "tecnico_resp_short" in df_reinc.columns:
                fallas_g = df_reinc[df_reinc["es_reincidencia_tecnico"]].groupby(
                    "tecnico_resp_short"
                ).size().reset_index(name="fallas")

                pm_g = df_pm_per.groupby("technician").size().reset_index(name="pms") if not df_pm_per.empty else pd.DataFrame(columns=["technician", "pms"])
                pm_g["short"] = pm_g["technician"].apply(lambda t: full_to_excel.get(t, t))

                for _, rp in pm_g.iterrows():
                    f_row = fallas_g[fallas_g["tecnico_resp_short"] == rp["short"]]
                    n_f = int(f_row["fallas"].iloc[0]) if not f_row.empty else 0
                    n_pm = int(rp["pms"])
                    _, bc_t, ex_t = _bono_calidad(n_f, n_pm)
                    eq = _get_equipo(rp["technician"])
                    if eq == "Sin equipo" or _es_excluido(rp["technician"]):
                        continue
                    cal_data["tecnicos"].append({
                        "nombre": rp["short"],
                        "equipo": EQUIPO_LABEL.get(eq, eq),
                        "exactitud": round(ex_t, 1),
                        "fallas": n_f, "pms": n_pm, "bono_clp": bc_t,
                    })
                cal_data["tecnicos"].sort(key=lambda x: x["exactitud"], reverse=True)

    # ══════════════════════════════════════════════════════════════════════════
    # KPI 3: PRECISIÓN FRACTTAL
    # ══════════════════════════════════════════════════════════════════════════
    prec_data = {"pct": 100.0, "buenas": 0, "total": 0, "bono_pct": 100,
                 "bono_clp": MAX_PREC, "tecnicos": []}

    if raw_wo:
        df_kpi_raw = build_kpi_llenado_df(raw_wo)
        if not df_kpi_raw.empty:
            df_kpi_raw["equipo"] = df_kpi_raw["tecnico"].apply(_get_equipo)
            df_kpi_raw = df_kpi_raw[~df_kpi_raw["tecnico"].apply(_es_excluido)].copy()
            _tipo_upper = df_kpi_raw["maint_type"].str.upper()
            df_kpi_raw = df_kpi_raw[
                _tipo_upper.str.contains("CORRECTIVA", na=False) |
                _tipo_upper.str.contains("PREVENTIVA", na=False)
            ].copy()
            _cd = df_kpi_raw["creation_date"]
            if _cd.dt.tz is not None:
                _cd = _cd.dt.tz_convert(None)
            df_kpi_raw = df_kpi_raw[_cd.dt.year >= 2026].copy()
            _cd2 = df_kpi_raw["creation_date"]
            if _cd2.dt.tz is not None:
                _cd2 = _cd2.dt.tz_convert(None)
            df_kpi_raw["mes_num"] = _cd2.dt.month

            # Filtrar por mes
            df_kpi_raw = df_kpi_raw[df_kpi_raw["mes_num"].isin(meses_filtro)]

            # Filtrar por equipo
            if equipo_sel:
                df_kpi_raw = df_kpi_raw[df_kpi_raw["equipo"] == equipo_sel]

            # Filtrar por técnico
            if tecnico_sel:
                tec_short_p = full_to_excel.get(tecnico_sel, tecnico_sel)
                df_kpi_raw = df_kpi_raw[
                    (df_kpi_raw["tecnico"] == tecnico_sel) |
                    (df_kpi_raw["tecnico"] == tec_short_p)
                ]

            df_kpi_ot = score_llenado_por_ot(df_kpi_raw)
            if not df_kpi_ot.empty:
                df_kpi_ot["equipo"] = df_kpi_ot["tecnico"].apply(_get_equipo)
                total_k = len(df_kpi_ot)
                buenas_k = int((df_kpi_ot["score_total"] >= 75).sum())
                pct_k = round(buenas_k / total_k * 100, 1) if total_k > 0 else 0
                bp_p, bc_p = _bono_prec(pct_k)
                prec_data = {"pct": pct_k, "buenas": buenas_k, "total": total_k,
                             "bono_pct": bp_p, "bono_clp": bc_p, "tecnicos": []}

                # Por técnico
                for snr in SENIORS:
                    snr_full = TECH_NAME_MAP.get(snr, snr)
                    snr_idx = df_kpi_ot.index[df_kpi_ot["tecnico"] == snr_full]
                    if len(snr_idx) == 0:
                        continue
                    snr_d = df_kpi_ot[df_kpi_ot["equipo"] == snr]
                    if snr_d.empty:
                        continue

                tec_kpi = df_kpi_ot.groupby(["tecnico", "equipo"]).agg(
                    total=("score_total", "count"),
                    buenas=("score_total", lambda x: (x >= 75).sum()),
                ).reset_index()
                tec_kpi["pct"] = (tec_kpi["buenas"] / tec_kpi["total"] * 100).round(1)
                tec_kpi = tec_kpi.sort_values("pct", ascending=False)
                for _, r in tec_kpi.iterrows():
                    eq = r["equipo"]
                    if eq == "Sin equipo":
                        continue
                    short = full_to_excel.get(r["tecnico"], r["tecnico"])
                    bp_t, bc_t = _bono_prec(r["pct"])
                    prec_data["tecnicos"].append({
                        "nombre": short, "equipo": EQUIPO_LABEL.get(eq, eq),
                        "pct": r["pct"], "buenas": int(r["buenas"]),
                        "total": int(r["total"]), "bono_clp": bc_t,
                    })

    # ══════════════════════════════════════════════════════════════════════════
    # KPI 4: RESUMEN BONOS
    # ══════════════════════════════════════════════════════════════════════════
    bono_total = sla_data["bono_clp"] + cal_data["bono_clp"] + prec_data["bono_clp"]

    # ── Datos trimestre actual ────────────────────────────────────────────────
    hoy = date.today()
    mes_actual = hoy.month
    trim_actual = next((k for k, v in TRIMESTRES.items() if mes_actual in v["meses"]), "T2")
    if trim_key not in TRIMESTRES:
        trim_key = trim_actual

    return render_template_string(
        HTML_TEMPLATE,
        sla=sla_data,
        cal=cal_data,
        prec=prec_data,
        bono_total=bono_total,
        max_sla=MAX_SLA, max_cal=MAX_CAL, max_prec=MAX_PREC,
        bono_pool=BONO_TOTAL,
        trim_key=trim_key,
        trim_label=trim["label"],
        trimestres=TRIMESTRES,
        mes_sel=mes_sel,
        meses_filtro=meses_filtro,
        tecnico_sel=tecnico_sel,
        equipo_sel=equipo_sel,
        tecnicos=tec_display,
        equipos=EQUIPO_LABEL,
        grupos=GRUPOS_TERRENO,
        color_pct=_color_pct,
        now=datetime.now(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# HTML TEMPLATE (mobile-first)
# ══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Desempeño STO — Occimiano</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8;
    --green: #22c55e; --yellow: #f59e0b; --red: #ef4444;
    --blue: #3b82f6; --teal: #14b8a6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 12px; max-width: 480px; margin: 0 auto;
    -webkit-font-smoothing: antialiased;
  }
  h1 { font-size: 1.3rem; text-align: center; margin: 8px 0 4px; }
  h2 { font-size: 1.05rem; margin: 16px 0 8px; color: var(--muted); letter-spacing: .03em; }
  .subtitle { text-align: center; font-size: .78rem; color: var(--muted); margin-bottom: 12px; }

  /* Filtros */
  .filters { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
  .filters select {
    flex: 1; min-width: 0; padding: 8px 6px; border-radius: 8px;
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    font-size: .82rem; appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%2394a3b8'%3E%3Cpath d='M2 4l4 4 4-4'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 8px center;
  }

  /* Cards principales */
  .kpi-card {
    background: var(--card); border-radius: 12px; padding: 16px;
    margin-bottom: 10px; border-left: 4px solid var(--blue);
  }
  .kpi-card.sla   { border-left-color: var(--blue); }
  .kpi-card.cal   { border-left-color: var(--teal); }
  .kpi-card.prec  { border-left-color: var(--yellow); }
  .kpi-card.bono  { border-left-color: var(--green); }

  .kpi-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
  .kpi-title { font-size: .82rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .kpi-big { font-size: 2rem; font-weight: 800; line-height: 1.1; }
  .kpi-detail { font-size: .78rem; color: var(--muted); margin-top: 4px; }
  .kpi-bono { display: inline-block; padding: 2px 10px; border-radius: 6px; font-size: .78rem; font-weight: 700; color: #fff; margin-top: 6px; }

  /* Mini ranking */
  .ranking { margin-top: 10px; }
  .rank-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid var(--border);
    font-size: .82rem;
  }
  .rank-row:last-child { border-bottom: none; }
  .rank-name { flex: 1; }
  .rank-eq { color: var(--muted); font-size: .72rem; margin-left: 4px; }
  .rank-pct { font-weight: 700; min-width: 55px; text-align: right; }
  .rank-clp { color: var(--muted); font-size: .72rem; min-width: 70px; text-align: right; }

  /* Bono resumen */
  .bono-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 8px; }
  .bono-item {
    background: rgba(148,163,184,.08); border-radius: 8px; padding: 10px;
    text-align: center;
  }
  .bono-item .label { font-size: .7rem; color: var(--muted); text-transform: uppercase; }
  .bono-item .value { font-size: 1.1rem; font-weight: 800; margin-top: 2px; }
  .bono-item.total { grid-column: span 2; }
  .bono-item.total .value { font-size: 1.5rem; color: var(--green); }

  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 10px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .tab-btn {
    padding: 6px 12px; border-radius: 8px; font-size: .78rem; font-weight: 600;
    background: var(--card); color: var(--muted); border: 1px solid var(--border);
    white-space: nowrap; cursor: pointer; flex-shrink: 0;
  }
  .tab-btn.active { background: var(--blue); color: #fff; border-color: var(--blue); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* Equipo cards */
  .eq-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .eq-card {
    background: rgba(148,163,184,.06); border-radius: 8px; padding: 10px; text-align: center;
  }
  .eq-card .name { font-weight: 700; font-size: .82rem; }
  .eq-card .senior { font-size: .7rem; color: var(--muted); }
  .eq-card .pct { font-size: 1.3rem; font-weight: 800; margin: 4px 0; }
  .eq-card .detail { font-size: .7rem; color: var(--muted); }
  .eq-card .bono-badge { font-size: .7rem; padding: 2px 6px; border-radius: 4px; color: #fff; font-weight: 700; }

  .footer { text-align: center; font-size: .7rem; color: var(--muted); margin-top: 20px; padding: 10px 0; }
  .clear-filter { font-size: .72rem; color: var(--blue); text-decoration: none; margin-left: 6px; }
</style>
</head>
<body>

<h1>Desempeño STO</h1>
<p class="subtitle">Occimiano Operaciones · {{ now.strftime('%d/%m/%Y %H:%M') }}</p>

<!-- Filtros -->
<form class="filters" id="filterForm">
  <select name="trim" onchange="this.form.submit()">
    {% for k, v in trimestres.items() %}
    <option value="{{ k }}" {{ 'selected' if k == trim_key }}>{{ v.label }}</option>
    {% endfor %}
  </select>
  <select name="mes" onchange="this.form.submit()">
    <option value="">Todo el trim.</option>
    {% for m in trimestres[trim_key].meses %}
    <option value="{{ m }}" {{ 'selected' if mes_sel == m|string }}>
      {{ ['','Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'][m] }}
    </option>
    {% endfor %}
  </select>
  <select name="equipo" onchange="document.querySelector('[name=tecnico]').value=''; this.form.submit()">
    <option value="">Todos los eq.</option>
    {% for k, lbl in equipos.items() %}
    <option value="{{ k }}" {{ 'selected' if equipo_sel == k }}>{{ lbl }}</option>
    {% endfor %}
  </select>
  <select name="tecnico" onchange="this.form.submit()">
    <option value="">Todos los téc.</option>
    {% for t in tecnicos %}
    {% if not equipo_sel or t.equipo == equipos.get(equipo_sel, equipo_sel) %}
    <option value="{{ t.full }}" {{ 'selected' if tecnico_sel == t.full }}>{{ t.short }}</option>
    {% endif %}
    {% endfor %}
  </select>
</form>

{% if tecnico_sel or equipo_sel %}
<div style="text-align:center;margin-bottom:10px;">
  <a href="/?trim={{ trim_key }}&mes={{ mes_sel }}" class="clear-filter">Limpiar filtros de equipo/técnico</a>
</div>
{% endif %}

<!-- Tabs -->
<div class="tabs">
  <div class="tab-btn active" onclick="showTab('sla')">SLA</div>
  <div class="tab-btn" onclick="showTab('cal')">Efectividad</div>
  <div class="tab-btn" onclick="showTab('prec')">Precisión</div>
  <div class="tab-btn" onclick="showTab('bono')">Bonos</div>
</div>

<!-- ═══ TAB SLA ═══ -->
<div class="tab-content active" id="tab-sla">
  <div class="kpi-card sla">
    <div class="kpi-header">
      <span class="kpi-title">Productividad SLA (40%)</span>
    </div>
    <div class="kpi-big" style="color:{{ color_pct(sla.pct) }}">{{ sla.pct }}%</div>
    <div class="kpi-detail">{{ sla.cumple }} / {{ sla.total }} llamados cumplen SLA</div>
    <div class="kpi-bono" style="background:{{ color_pct(sla.pct) }}">
      {{ sla.bono_pct }}% bono → ${{ '{:,.0f}'.format(sla.bono_clp) }}
    </div>
  </div>

  {% if sla.equipos %}
  <h2>Por equipo</h2>
  <div class="eq-cards">
    {% for eq in sla.equipos %}
    <div class="eq-card">
      <div class="name">{{ eq.nombre }}</div>
      <div class="senior">{{ eq.senior }}</div>
      <div class="pct" style="color:{{ color_pct(eq.pct) }}">{{ eq.pct }}%</div>
      <div class="detail">{{ eq.cumple }}/{{ eq.total }}</div>
      <div class="bono-badge" style="background:{{ color_pct(eq.pct) }}">${{ '{:,.0f}'.format(eq.bono_clp) }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if sla.tecnicos %}
  <h2>Ranking técnicos</h2>
  <div class="ranking">
    {% for t in sla.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span></span>
      <span class="rank-pct" style="color:{{ color_pct(t.pct) }}">{{ t.pct }}%</span>
      <span class="rank-clp">${{ '{:,.0f}'.format(t.bono_clp) }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- ═══ TAB EFECTIVIDAD ═══ -->
<div class="tab-content" id="tab-cal">
  <div class="kpi-card cal">
    <div class="kpi-header">
      <span class="kpi-title">Efectividad MP (30%)</span>
    </div>
    <div class="kpi-big" style="color:{{ color_pct(cal.exactitud) }}">{{ cal.exactitud }}%</div>
    <div class="kpi-detail">
      {{ cal.fallas }} falla(s) post-preventiva · {{ cal.pms_total }} PMs evaluados
    </div>
    <div class="kpi-bono" style="background:{{ color_pct(cal.exactitud) }}">
      {{ cal.bono_pct }}% bono → ${{ '{:,.0f}'.format(cal.bono_clp) }}
    </div>
  </div>

  {% if cal.tecnicos %}
  <h2>Ranking técnicos</h2>
  <div class="ranking">
    {% for t in cal.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span></span>
      <span class="rank-pct" style="color:{{ color_pct(t.exactitud) }}">{{ t.exactitud }}%</span>
      <span class="rank-clp">{{ t.fallas }}F / {{ t.pms }}PM</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- ═══ TAB PRECISIÓN ═══ -->
<div class="tab-content" id="tab-prec">
  <div class="kpi-card prec">
    <div class="kpi-header">
      <span class="kpi-title">Precisión Fracttal (30%)</span>
    </div>
    <div class="kpi-big" style="color:{{ color_pct(prec.pct) }}">{{ prec.pct }}%</div>
    <div class="kpi-detail">
      {{ prec.buenas }} / {{ prec.total }} OTs correctas (score 75/75)
    </div>
    <div class="kpi-bono" style="background:{{ color_pct(prec.pct) }}">
      {{ prec.bono_pct }}% bono → ${{ '{:,.0f}'.format(prec.bono_clp) }}
    </div>
  </div>

  {% if prec.tecnicos %}
  <h2>Ranking técnicos</h2>
  <div class="ranking">
    {% for t in prec.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span></span>
      <span class="rank-pct" style="color:{{ color_pct(t.pct) }}">{{ t.pct }}%</span>
      <span class="rank-clp">{{ t.buenas }}/{{ t.total }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- ═══ TAB BONOS ═══ -->
<div class="tab-content" id="tab-bono">
  <div class="kpi-card bono">
    <div class="kpi-header">
      <span class="kpi-title">Resumen Bono · {{ trim_label }}</span>
    </div>
    <div class="bono-grid">
      <div class="bono-item">
        <div class="label">SLA (40%)</div>
        <div class="value" style="color:{{ color_pct(sla.pct) }}">${{ '{:,.0f}'.format(sla.bono_clp) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">{{ sla.pct }}% → {{ sla.bono_pct }}%</div>
      </div>
      <div class="bono-item">
        <div class="label">Efectividad (30%)</div>
        <div class="value" style="color:{{ color_pct(cal.exactitud) }}">${{ '{:,.0f}'.format(cal.bono_clp) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">{{ cal.exactitud }}% → {{ cal.bono_pct }}%</div>
      </div>
      <div class="bono-item">
        <div class="label">Precisión (30%)</div>
        <div class="value" style="color:{{ color_pct(prec.pct) }}">${{ '{:,.0f}'.format(prec.bono_clp) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">{{ prec.pct }}% → {{ prec.bono_pct }}%</div>
      </div>
      <div class="bono-item total">
        <div class="label">Bono estimado total</div>
        <div class="value">${{ '{:,.0f}'.format(bono_total) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">de ${{ '{:,.0f}'.format(bono_pool) }} pool/trim</div>
      </div>
    </div>
  </div>

  <div style="margin-top:12px;padding:12px;background:var(--card);border-radius:8px;font-size:.78rem;">
    <div style="font-weight:700;color:var(--muted);margin-bottom:6px;">ESCALA BONOS</div>
    <div style="margin-bottom:6px;">
      <b>SLA (40% · $200K pool)</b><br>
      <span style="color:#22c55e">≥95%→100%</span> ·
      <span style="color:#16a34a">93→90%</span> ·
      <span style="color:#4ade80">90→80%</span> ·
      <span style="color:#f59e0b">85→50%</span> ·
      <span style="color:#ef4444">&lt;85%→0%</span>
    </div>
    <div style="margin-bottom:6px;">
      <b>Calidad MP (30% · $150K pool)</b><br>
      <span style="color:#22c55e">≥98%→100%</span> ·
      <span style="color:#16a34a">96→90%</span> ·
      <span style="color:#4ade80">94→80%</span> ·
      <span style="color:#65a30d">92→70%</span> ·
      <span style="color:#f59e0b">90→60%</span> ·
      <span style="color:#ef4444">&lt;90%→0%</span>
    </div>
    <div>
      <b>Precisión (30% · $150K pool)</b><br>
      <span style="color:#22c55e">≥95%→100%</span> ·
      <span style="color:#16a34a">90→90%</span> ·
      <span style="color:#4ade80">85→80%</span> ·
      <span style="color:#65a30d">80→70%</span> ·
      <span style="color:#f59e0b">75→60%</span> ·
      <span style="color:#f97316">70→50%</span> ·
      <span style="color:#ef4444">&lt;70%→0%</span>
    </div>
  </div>
</div>

<div class="footer">
  Occimiano Operaciones · Datos de Supabase en tiempo real<br>
  Misma fuente que el dashboard principal
</div>

<script>
function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  event.target.classList.add('active');
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
