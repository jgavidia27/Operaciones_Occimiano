"""
sync_sto_export.py
==================
Genera sto_data.json y lo sube a Supabase (sto_data_export)
para que la app movil lea datos actualizados sin depender
de que alguien abra el dashboard.

Usa las mismas funciones de data.py y supabase_client.py
que el dashboard Streamlit.

Uso:
  python sync_sto_export.py          (export completo)

Programar en Render como cron job (2x dia):
  cron: "0 12,22 * * *"
  command: python sync_sto_export.py
"""

import json
import os
import sys
import time
import unicodedata
from datetime import datetime, date, timezone

import pandas as pd

from data import (
    build_work_orders_df,
    build_kpi_llenado_df,
    score_llenado_por_ot,
    aplicar_numerales_subtarea,
    build_reincidencias,
    GRUPOS_TERRENO,
    SENIORS,
    TECNICOS_NO_APLICA,
)
from gdrive import TECH_NAME_MAP
from supabase_client import (
    load_work_orders_supabase,
    load_all_llamados_supabase,
    load_numerales_subtarea_supabase,
    _query,
    _post,
)

BONO_TOTAL = 500_000
_CLIENTES_SLA = {"COPEC", "Aramco (Esmax)", "ESMAX (Aramco)", "SHELL (Enex)", "ABASTIBLE"}
_ESTADOS_NO_PUNTUAN = {
    "Canceladas", "Cancelado", "Cancelada",
    "ERROR DE INGRESO", "EQUIPO CON RECAMBIO",
    "DUPLICADO", "Duplicidad", "DE PRUEBA",
    "FUE REPETIDA EN OTRA OS", "PLAN INCOMPLETO",
}
_SLA_EXC_NUM = {"140785", "143926", "145331"}
# _SLA_EXC_OS se carga dinámicamente en runtime desde Supabase
# (tabla sla_excepciones). Antes era hardcoded; ahora operaciones
# puede dar de alta nuevas excepciones sin tocar código.
# La vista v_llamados_sla ya marca estas OTs como CUMPLE via JOIN,
# pero mantenemos este set como capa de defensa.
_SLA_EXC_OS: set = set()
_EQUIPO_LABEL = {k: k for k in GRUPOS_TERRENO}
_EQUIPO_LABEL["Carlos Avila Norte"] = "Carlos Avila (Norte)"
_EQUIPO_LABEL["Carlos Avila Sur"] = "Carlos Avila (Sur)"


def log(msg, lvl="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "    ", "OK": "[OK]", "WARN": "[!] ", "ERR": "[X] ", "PROG": "--> "}
    print(f"[{ts}] {tags.get(lvl, '    ')} {msg}", flush=True)


def _norm_n(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().strip().lower()


_GRUPOS_NORM: dict = {}
for _grp_k, _grp_v in GRUPOS_TERRENO.items():
    for _mb in _grp_v["miembros"]:
        _GRUPOS_NORM[_norm_n(_mb)] = _grp_k
        _pts = _mb.split()
        for _i in range(len(_pts)):
            for _j in range(_i + 1, len(_pts)):
                _GRUPOS_NORM[_norm_n(f"{_pts[_i]} {_pts[_j]}")] = _grp_k

_NO_APLICA_NORM: set = set()
for _na in TECNICOS_NO_APLICA:
    _NO_APLICA_NORM.add(_norm_n(_na))
    _na_pts = _na.split()
    for _i in range(len(_na_pts)):
        for _j in range(_i + 1, len(_na_pts)):
            _NO_APLICA_NORM.add(_norm_n(f"{_na_pts[_i]} {_na_pts[_j]}"))


def _get_equipo(t) -> str:
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


def _es_excluido(t) -> bool:
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


def _bono_sla(pct):
    if pct is None:
        return 0, 0
    m = int(BONO_TOTAL * 0.40)
    if pct >= 95: return 100, m
    if pct >= 93: return 90, int(m * .90)
    if pct >= 90: return 80, int(m * .80)
    if pct >= 85: return 50, int(m * .50)
    return 0, 0


def _bono_calidad(n_fallas, n_pms):
    if n_pms <= 0:
        return 0, 0, 0.0
    exactitud = (1 - n_fallas / n_pms) * 100
    m = int(BONO_TOTAL * 0.30)
    if exactitud >= 98: return 100, m, exactitud
    if exactitud >= 96: return 90, int(m * .90), exactitud
    if exactitud >= 94: return 80, int(m * .80), exactitud
    if exactitud >= 92: return 70, int(m * .70), exactitud
    if exactitud >= 90: return 60, int(m * .60), exactitud
    return 0, 0, exactitud


def _bono_prec(pct):
    if pct is None:
        return 0, 0
    m = int(BONO_TOTAL * 0.30)
    if pct >= 95: return 100, m
    if pct >= 90: return 90, int(m * .90)
    if pct >= 85: return 80, int(m * .80)
    if pct >= 80: return 70, int(m * .70)
    if pct >= 75: return 60, int(m * .60)
    if pct >= 70: return 50, int(m * .50)
    return 0, 0


def main():
    t0 = time.time()
    log("Inicio sync STO export")

    # ── 0. Cargar excepciones SLA desde Supabase ────────────────────────────
    global _SLA_EXC_OS
    try:
        _rows_exc = _query("sla_excepciones", "select=os_fracttal", limit=5000)
        _SLA_EXC_OS = {str(r.get("os_fracttal") or "").strip().upper()
                       for r in _rows_exc if r.get("os_fracttal")}
        log(f"  Excepciones SLA cargadas: {len(_SLA_EXC_OS)}", "OK")
    except Exception as _e_exc:
        log(f"No se pudieron cargar excepciones SLA: {_e_exc}", "WARN")
        _SLA_EXC_OS = set()

    # ── 1. Cargar datos desde Supabase ──────────────────────────────────────
    log("Cargando work orders...")
    raw_wo = load_work_orders_supabase()
    log(f"  {len(raw_wo)} work orders", "OK")

    log("Cargando llamados SLA...")
    df_llamados = load_all_llamados_supabase("2026-01-01")
    log(f"  {len(df_llamados)} llamados", "OK")

    log("Cargando numerales subtarea...")
    df_num_sub = load_numerales_subtarea_supabase()
    log(f"  {len(df_num_sub)} registros", "OK")

    if not raw_wo:
        log("Sin work orders, abortando", "ERR")
        return

    # ── 2. Build work orders DF ─────────────────────────────────────────────
    log("Construyendo DataFrames...")
    df_wo = build_work_orders_df(raw_wo)
    df_wo["equipo"] = df_wo["technician"].apply(_get_equipo)

    # ── 3. Aplicar excepciones SLA ──────────────────────────────────────────
    if not df_llamados.empty and "cumplimiento" in df_llamados.columns:
        _exc_mask = pd.Series(False, index=df_llamados.index)
        if "os_fracttal" in df_llamados.columns:
            _exc_mask |= df_llamados["os_fracttal"].astype(str).str.strip().str.upper().isin(
                {s.upper() for s in _SLA_EXC_OS}
            )
        for _col in ["n_llamado", "LLAMADO"]:
            if _col in df_llamados.columns:
                _exc_mask |= df_llamados[_col].astype(str).str.strip().isin(_SLA_EXC_NUM)
                break
        _nc_mask = df_llamados["cumplimiento"] == "NO CUMPLE"
        df_llamados.loc[_exc_mask & _nc_mask, "cumplimiento"] = "CUMPLE"

    # ── 4. SLA records ──────────────────────────────────────────────────────
    sla_records = []
    if not df_llamados.empty and "cumplimiento" in df_llamados.columns:
        _src = df_llamados.copy()
        _src["cumple_sla"] = _src["cumplimiento"].map({"CUMPLE": True, "NO CUMPLE": False})
        _src = _src[_src["cumple_sla"].notna()].copy()
        if "tecnico" in _src.columns:
            _src["tecnico"] = _src["tecnico"].apply(
                lambda t: TECH_NAME_MAP.get(str(t).strip(), str(t).strip())
                if isinstance(t, str) and t.strip() else t
            )
        if "equipo" not in _src.columns or _src["equipo"].isna().all():
            _src["equipo"] = _src["tecnico"].apply(_get_equipo)
        _src = _src[~_src["tecnico"].apply(_es_excluido)].copy()
        _src["mes_num"] = pd.to_datetime(_src["fecha_llamado"], errors="coerce").dt.month
        _sla_g = _src.groupby(["tecnico", "equipo", "mes_num"]).agg(
            cumple=("cumple_sla", "sum"), total=("cumple_sla", "count"),
        ).reset_index()
        _sla_g["tecnico"] = _sla_g["tecnico"].str.replace(r'\s+', ' ', regex=True).str.strip()
        sla_records = _sla_g.to_dict(orient="records")
    log(f"  SLA: {len(sla_records)} registros", "OK")

    # ── 5. Precision records ────────────────────────────────────────────────
    prec_records = []
    log("Calculando precision (KPI llenado)...")
    df_kpi = build_kpi_llenado_df(raw_wo)
    if not df_kpi.empty:
        df_kpi["equipo"] = df_kpi["tecnico"].apply(_get_equipo)
        df_kpi = df_kpi[~df_kpi["tecnico"].apply(_es_excluido)].copy()
        _tipo_upper = df_kpi["maint_type"].str.upper()
        df_kpi = df_kpi[
            _tipo_upper.str.contains("CORRECTIVA", na=False) |
            _tipo_upper.str.contains("PREVENTIVA", na=False)
        ].copy()
        df_kpi = df_kpi[
            df_kpi["creation_date"].dt.tz_convert(None).dt.year >= 2026
        ].copy()
        df_kpi["mes"] = df_kpi["creation_date"].dt.tz_convert(None).dt.to_period("M").astype(str)

        df_scored = score_llenado_por_ot(df_kpi)
        if not df_scored.empty:
            ot_estados = {
                r["id_ot"]: r.get("estado", "")
                for r in _query("ordenes_trabajo", "select=id_ot,estado&fecha_creacion=gte.2026-01-01", limit=20_000)
                if r.get("id_ot")
            }
            folios_excluir = {f for f, e in ot_estados.items() if e in _ESTADOS_NO_PUNTUAN}
            if folios_excluir and "folio" in df_scored.columns:
                df_scored = df_scored[~df_scored["folio"].isin(folios_excluir)].copy()
            df_scored["wo_status"] = df_scored["folio"].map(ot_estados).fillna("")
            df_scored = aplicar_numerales_subtarea(df_scored, df_num_sub)
            df_scored["score_numeral"] = df_scored["numeral_ok"].apply(lambda ok: 25 if ok else 0)
            df_scored["score_total"] = (
                df_scored["score_tiempo"] + df_scored["score_causa"] + df_scored["score_numeral"]
            ).clip(upper=75).round(1)
            df_scored["equipo"] = df_scored["tecnico"].apply(_get_equipo)
            df_scored["mes"] = df_scored["creation_date"].dt.tz_convert(None).dt.to_period("M").astype(str)

            _prec_exp = df_scored[["tecnico", "equipo", "score_total", "mes"]].copy()
            _prec_exp["mes_num"] = _prec_exp["mes"].apply(
                lambda m: int(str(m).split("-")[1]) if "-" in str(m) else 0
            )
            _prec_exp["buena"] = _prec_exp["score_total"] >= 75
            _prec_g = _prec_exp.groupby(["tecnico", "equipo", "mes_num"]).agg(
                buenas=("buena", "sum"), total=("buena", "count"),
            ).reset_index()
            _prec_g["tecnico"] = _prec_g["tecnico"].str.replace(r'\s+', ' ', regex=True).str.strip()
            prec_records = _prec_g.to_dict(orient="records")
    log(f"  Precision: {len(prec_records)} registros", "OK")

    # ── 6. Reincidencias ────────────────────────────────────────────────────
    reinc_records = []
    log("Calculando reincidencias...")
    _df_wo_reinc = df_wo[df_wo["client"].isin(_CLIENTES_SLA)].copy() \
                   if "client" in df_wo.columns else df_wo.copy()
    try:
        df_reinc = build_reincidencias(_df_wo_reinc, dict(TECH_NAME_MAP))
        if not df_reinc.empty:
            if "fecha_cm" in df_reinc.columns:
                df_reinc = df_reinc[
                    pd.to_datetime(df_reinc["fecha_cm"], errors="coerce") >= pd.Timestamp("2026-01-01")
                ].copy()
            elif "fecha2" in df_reinc.columns:
                df_reinc = df_reinc[
                    pd.to_datetime(df_reinc["fecha2"], errors="coerce") >= pd.Timestamp("2026-01-01")
                ].copy()
                df_reinc = df_reinc.rename(columns={
                    "folio1": "folio_pm", "fecha1": "fecha_pm",
                    "folio2": "folio_cm", "fecha2": "fecha_cm",
                    "tecnico2": "tecnico_cm",
                })
            if not df_reinc.empty and "falla_tipo" in df_reinc.columns:
                _reinc_exp = df_reinc[df_reinc["falla_tipo"] == "fao"].copy()
                if "fecha_cm" in _reinc_exp.columns:
                    _reinc_exp["mes_num"] = pd.to_datetime(
                        _reinc_exp["fecha_cm"], errors="coerce"
                    ).dt.month
                if "tecnico_resp_short" in _reinc_exp.columns and "grupo_responsable" in _reinc_exp.columns:
                    _agg_col = "folio_cm" if "folio_cm" in _reinc_exp.columns else "tecnico_resp_short"
                    _reinc_g = _reinc_exp.groupby(
                        ["tecnico_resp_short", "grupo_responsable", "mes_num"]
                    ).agg(fallas=(_agg_col, "nunique")).reset_index()
                    reinc_records = _reinc_g.rename(columns={
                        "tecnico_resp_short": "tecnico_short",
                        "grupo_responsable": "equipo",
                    }).to_dict(orient="records")
    except Exception as e:
        log(f"Error en reincidencias: {e}", "ERR")
    log(f"  Reincidencias: {len(reinc_records)} registros", "OK")

    # ── 7. PMs ──────────────────────────────────────────────────────────────
    pm_records = []
    if not df_wo.empty:
        _pm_exp = df_wo[
            (df_wo["maint_type"] == "Preventiva") &
            (~df_wo["technician"].apply(_es_excluido)) &
            (df_wo["equipo"] != "Sin equipo") &
            (df_wo["client"].isin(_CLIENTES_SLA) if "client" in df_wo.columns else True)
        ].copy()
        if not _pm_exp.empty:
            _pm_dates = _pm_exp["creation_date"].dt.tz_convert(None) \
                        if _pm_exp["creation_date"].dt.tz is not None \
                        else _pm_exp["creation_date"]
            _pm_exp["mes_num"] = _pm_dates.dt.month
            _pm_g = _pm_exp.groupby(["technician", "equipo", "mes_num"]).agg(
                pms=("folio", "nunique") if "folio" in _pm_exp.columns else ("technician", "count"),
            ).reset_index()
            _pm_g["technician"] = _pm_g["technician"].str.replace(r'\s+', ' ', regex=True).str.strip()
            pm_records = _pm_g.rename(columns={"technician": "tecnico"}).to_dict(orient="records")
    log(f"  PMs: {len(pm_records)} registros", "OK")

    # ── 8. Equipos y tech maps ──────────────────────────────────────────────
    equipos_exp = {}
    for gk, gv in GRUPOS_TERRENO.items():
        miembros = [TECH_NAME_MAP.get(m, m) for m in gv.get("miembros", [])]
        equipos_exp[gk] = {
            "label": _EQUIPO_LABEL.get(gk, gk),
            "senior": gv.get("senior", ""),
            "miembros": miembros,
        }

    # ── 9. Bono table ───────────────────────────────────────────────────────
    log("Calculando bono table...")
    yr = datetime.now().year
    TRIMS = {"T1": [1, 2, 3], "T2": [4, 5, 6], "T3": [7, 8, 9], "T4": [10, 11, 12]}
    CC_STGO = ["Juan Gallardo", "Luis Pinto", "Victor Bahamonde"]
    CC_REF = date(2026, 3, 16)

    bono_tbl = {}
    for tk, tm in TRIMS.items():
        cc_w = {}
        for mm in tm:
            ps = f"{yr}-{mm:02d}"
            for lun in pd.date_range(
                pd.Period(ps, "M").start_time, pd.Period(ps, "M").end_time, freq="W-MON"
            ):
                eq_r = CC_STGO[(lun.date() - CC_REF).days // 7 % 3]
                cc_w[eq_r] = cc_w.get(eq_r, 0) + 1

        sf = [r for r in sla_records if r.get("mes_num") in tm]
        pf = [r for r in prec_records if r.get("mes_num") in tm]
        mfr = [r for r in pm_records if r.get("mes_num") in tm]
        rfr = [r for r in reinc_records if r.get("mes_num") in tm]

        bt = []
        for gk, gv in GRUPOS_TERRENO.items():
            ms = [m for m in gv.get("miembros", []) if not _es_excluido(TECH_NAME_MAP.get(m, m))]
            mfl = [TECH_NAME_MAP.get(m, m) for m in ms]
            n = len(mfl)
            if not n:
                continue

            eso = sum(r.get("cumple", 0) for r in sf if r.get("equipo") == gk)
            est = sum(r.get("total", 0) for r in sf if r.get("equipo") == gk)
            epb = sum(r.get("buenas", 0) for r in pf if r.get("equipo") == gk)
            ept = sum(r.get("total", 0) for r in pf if r.get("equipo") == gk)
            epm = sum(r.get("pms", 0) for r in mfr if r.get("equipo") == gk)
            efl = sum(r.get("fallas", 0) for r in rfr if r.get("equipo") == gk)

            esp = round(eso / est * 100, 1) if est else None
            emp = round((1 - efl / epm) * 100, 1) if epm else None
            epp = round(epb / ept * 100, 1) if ept else None

            ens = _bono_sla(esp)[0]
            enm = _bono_calidad(efl, epm)[0]
            enp = _bono_prec(epp)[0]
            ec = round(.40 * ens + .30 * enm + .30 * enp, 1)

            ppi = int(int(BONO_TOTAL / n) * .50)
            ppe = ppi
            ncc = cc_w.get(gk, 0)
            bcc = int(100000 * ncc / n) if n else 0
            be = int(ppe * .40 * ens / 100 + ppe * .30 * enm / 100 + ppe * .30 * enp / 100)

            trs = []
            for tf in mfl:
                ts = next((k for k, v in TECH_NAME_MAP.items() if v == tf), tf)
                iss = ts in SENIORS
                if iss:
                    so, st2, pb, pt, fl, pm = eso, est, epb, ept, efl, epm
                else:
                    so = sum(r.get("cumple", 0) for r in sf if r.get("tecnico") == tf)
                    st2 = sum(r.get("total", 0) for r in sf if r.get("tecnico") == tf)
                    pb = sum(r.get("buenas", 0) for r in pf if r.get("tecnico") == tf)
                    pt = sum(r.get("total", 0) for r in pf if r.get("tecnico") == tf)
                    pm = sum(r.get("pms", 0) for r in mfr if r.get("tecnico") == tf)
                    fl = sum(r.get("fallas", 0) for r in rfr if r.get("tecnico_short") == ts)

                sp = round(so / st2 * 100, 1) if st2 else None
                mp2 = round((1 - fl / pm) * 100, 1) if pm else None
                pp2 = round(pb / pt * 100, 1) if pt else None

                ns = _bono_sla(sp)[0]
                nm = _bono_calidad(fl, pm)[0]
                np2 = _bono_prec(pp2)[0]
                c = round(.40 * ns + .30 * nm + .30 * np2, 1)
                bi = int(ppi * .40 * ns / 100 + ppi * .30 * nm / 100 + ppi * .30 * np2 / 100)
                tot = bi + be + bcc

                trs.append({
                    "short": ts, "is_senior": iss,
                    "sla_pct": sp, "sla_ok": so, "sla_tot": st2, "sla_niv": ns,
                    "mp_pct": mp2, "mp_f": fl, "mp_pm": pm, "mp_niv": nm,
                    "prec_pct": pp2, "prec_b": pb, "prec_t": pt, "prec_niv": np2,
                    "cumpl": c, "bono_ind": bi, "bono_eq": be, "bono_cc": bcc,
                    "total_trim": tot, "prom_mensual": tot // 3,
                })

            bt.append({
                "key": gk, "label": _EQUIPO_LABEL.get(gk, gk),
                "senior": gv.get("senior", ""), "n_eq": n,
                "pp_ind": ppi, "pp_eq": ppe, "n_semanas_cc": ncc,
                "bono_cc_eq": 100000 * ncc,
                "tecs": trs,
                "eq": {
                    "sla_pct": esp, "sla_ok": eso, "sla_tot": est, "sla_niv": ens,
                    "mp_pct": emp, "mp_f": efl, "mp_pm": epm, "mp_niv": enm,
                    "prec_pct": epp, "prec_b": epb, "prec_t": ept, "prec_niv": enp,
                    "cumpl": ec,
                },
            })
        bono_tbl[tk] = bt
    log("  Bono table completa", "OK")

    # ── 10. Export ──────────────────────────────────────────────────────────
    export_obj = {
        "updated_at": datetime.now().isoformat(),
        "sla": sla_records,
        "precision": prec_records,
        "reincidencias": reinc_records,
        "pms": pm_records,
        "tech_name_map": dict(TECH_NAME_MAP),
        "full_to_short": {v: k for k, v in TECH_NAME_MAP.items()},
        "equipos": equipos_exp,
        "seniors": list(SENIORS),
        "bono_table": bono_tbl,
    }

    sto_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sto_data.json")
    with open(sto_path, "w", encoding="utf-8") as f:
        json.dump(export_obj, f, ensure_ascii=False, default=str)
    log(f"  sto_data.json escrito ({os.path.getsize(sto_path) / 1024:.0f} KB)", "OK")

    ok = _post("sto_data_export", {
        "id": "latest",
        "data": export_obj,
        "updated_at": datetime.now().isoformat(),
    })
    if ok:
        log("Subido a Supabase (sto_data_export)", "OK")
    else:
        log("Error subiendo a Supabase", "ERR")

    log(f"COMPLETADO en {time.time() - t0:.0f}s", "OK")


if __name__ == "__main__":
    main()
