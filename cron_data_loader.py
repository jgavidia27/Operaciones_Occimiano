"""
cron_data_loader.py — Carga toda la data que necesitan los scripts de cron.
=============================================================================

Reutiliza los loaders del proyecto (supabase_client.py, data.py) pero sin
depender de Streamlit ni de session_state.

Retorna un dict con: df_wo, df_llamados, df_eds, df_ot_scores, df_tecnicos,
df_num_sub, ot_estados.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()


def load_dashboard_data() -> dict:
    """Carga TODA la data del dashboard (~40s en primer llamado)."""
    # Imports diferidos para no forzar dependencias si el módulo se importa
    from data import (
        build_work_orders_df, build_kpi_llenado_df, score_llenado_por_ot,
        aplicar_numerales_subtarea,
    )
    from supabase_client import (
        load_work_orders_supabase, load_listado_eds_supabase,
        load_tecnicos_supabase, load_all_llamados_supabase,
        load_numerales_subtarea_supabase,
    )
    import requests

    print("[loader] Descargando work orders de Supabase...", flush=True)
    wo_list = load_work_orders_supabase()
    print(f"[loader]   {len(wo_list)} OTs.", flush=True)

    print("[loader] Construyendo df_wo...", flush=True)
    df_wo = build_work_orders_df(wo_list)
    print(f"[loader]   df_wo shape: {df_wo.shape}", flush=True)

    print("[loader] Descargando estaciones...", flush=True)
    df_eds = load_listado_eds_supabase()
    print(f"[loader]   df_eds shape: {df_eds.shape}", flush=True)

    print("[loader] Descargando técnicos...", flush=True)
    df_tecnicos = load_tecnicos_supabase()
    print(f"[loader]   df_tecnicos shape: {df_tecnicos.shape}", flush=True)

    print("[loader] Descargando llamados SLA...", flush=True)
    df_llamados = load_all_llamados_supabase("2026-01-01")
    print(f"[loader]   df_llamados shape: {df_llamados.shape}", flush=True)

    print("[loader] Descargando numerales subtarea...", flush=True)
    df_num_sub = load_numerales_subtarea_supabase()
    print(f"[loader]   df_num_sub shape: {df_num_sub.shape}", flush=True)

    print("[loader] Construyendo df_kpi + scores por OT...", flush=True)
    df_kpi = build_kpi_llenado_df(wo_list)
    df_ot_scores = score_llenado_por_ot(df_kpi)
    # Enriquecer scores con datos que se agregan en app.py después:
    if not df_ot_scores.empty:
        # Recalcular numeral_ok/motivo con subtareas cuando estén
        df_ot_scores = aplicar_numerales_subtarea(df_ot_scores, df_num_sub)
        # Agregar 'equipo' (viene de _get_equipo aplicado a 'tecnico')
        # → lo hacemos afuera con el ctx (cron_helpers).
        # Aquí solo aseguramos que existan las columnas 'mes' y 'creation_date_local'
        if "creation_date" in df_ot_scores.columns:
            import pandas as pd
            _cd = df_ot_scores["creation_date"]
            if hasattr(_cd, "dt") and _cd.dt.tz is not None:
                _cd_naive = _cd.dt.tz_convert(None)
            else:
                _cd_naive = _cd
            df_ot_scores["mes"] = _cd_naive.dt.to_period("M").astype(str)
            df_ot_scores["creation_date_local"] = _cd_naive.dt.date
    print(f"[loader]   df_ot_scores shape: {df_ot_scores.shape}", flush=True)

    return {
        "df_wo":        df_wo,
        "df_llamados":  df_llamados,
        "df_eds":       df_eds,
        "df_ot_scores": df_ot_scores,
        "df_tecnicos":  df_tecnicos,
        "df_num_sub":   df_num_sub,
        "wo_list":      wo_list,
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    d = load_dashboard_data()
    print("\n=== LOADER OK ===")
    for k, v in d.items():
        if hasattr(v, "shape"):
            print(f"  {k:<15} : {v.shape}")
        else:
            print(f"  {k:<15} : {type(v).__name__} (len={len(v) if hasattr(v,'__len__') else '?'})")
