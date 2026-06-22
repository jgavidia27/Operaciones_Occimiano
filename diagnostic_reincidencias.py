"""
diagnostic_reincidencias.py
============================
Diagnóstico exhaustivo del algoritmo de Efectividad MP (build_reincidencias).

Responde a la pregunta:
  "¿Es real el 100% de efectividad para todos los equipos?"

Cómo usar:
  cd C:\\Users\\jgavi\\Documents\\occimiano_dashboard
  python diagnostic_reincidencias.py

Salida: resumen por equipo + tabla de matches potenciales.
"""

import os
import sys
import pandas as pd

# Forzar UTF-8 en la consola Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Importar módulos del dashboard ───────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase_client import load_work_orders_supabase, load_tecnicos_supabase
from data import build_work_orders_df, GRUPOS_TERRENO
from gdrive import build_tech_name_maps

# ── Clientes evaluados ────────────────────────────────────────────────────────
_CLIENTES_SLA = {"COPEC", "Aramco (Esmax)", "ESMAX (Aramco)", "SHELL (Enex)"}

SEP = "=" * 72


def fmt_pct(n, total):
    if total == 0:
        return "0/0 (N/A)"
    return f"{n}/{total} ({100*n/total:.0f}%)"


def main():
    print(SEP)
    print("  DIAGNÓSTICO EFECTIVIDAD MP — build_reincidencias")
    print(SEP)

    # 1. Cargar datos
    print("\n[1] Cargando órdenes de trabajo desde Supabase...")
    raw = load_work_orders_supabase()
    print(f"    {len(raw)} OTs cargadas")
    if not raw:
        print("ERROR: No se cargaron OTs. Verificar credenciales Supabase.")
        return

    # 2. Construir DataFrame
    print("\n[2] Construyendo DataFrame de OTs...")
    df_tec = load_tecnicos_supabase()
    _excel_to_full, _full_to_excel = build_tech_name_maps(df_tec)
    df_wo = build_work_orders_df(raw)
    print(f"    {len(df_wo)} filas en df_wo")
    print(f"    Columnas: {list(df_wo.columns)}")

    # Mostrar clientes únicos presentes
    if "client" in df_wo.columns:
        print(f"\n    Clientes únicos en df_wo:")
        for c, n in df_wo["client"].value_counts().items():
            flag = " ✅ (evaluado)" if c in _CLIENTES_SLA else ""
            print(f"      {c:<30} {n:>5} OTs{flag}")

    # 3. Filtrar a COPEC/ESMAX/SHELL (mismo filtro que la app)
    if "client" in df_wo.columns:
        df_sla = df_wo[df_wo["client"].isin(_CLIENTES_SLA)].copy()
    else:
        df_sla = df_wo.copy()
    print(f"\n[3] OTs en clientes evaluados (COPEC/ESMAX/SHELL): {len(df_sla)}")

    # 4. Separar PMs y CMs
    prev_all = df_sla[
        (df_sla["maint_type"] == "Preventiva") &
        df_sla["final_date"].notna()
    ].copy()
    corr_all = df_sla[
        (df_sla["maint_type"] == "Correctiva") &
        (df_sla["creation_date"].notna() | df_sla["final_date"].notna())
    ].copy()

    print(f"\n[4] En clientes evaluados:")
    print(f"    PMs con final_date :  {len(prev_all)}")
    print(f"    CMs con fecha      :  {len(corr_all)}")

    if prev_all.empty:
        print("\n⚠️  SIN PMs en COPEC/ESMAX/SHELL → build_reincidencias retorna vacío → 100% trivial")
        return
    if corr_all.empty:
        print("\n⚠️  SIN CMs en COPEC/ESMAX/SHELL → build_reincidencias retorna vacío → 100% genuino")
        return

    # 5. equipment_code: ¿está poblado?
    print(f"\n[5] Campo equipment_code (codigo_activo):")
    prev_code_ok = (prev_all["equipment_code"].str.strip() != "").sum()
    corr_code_ok = (corr_all["equipment_code"].str.strip() != "").sum()
    print(f"    PMs con código no-vacío  : {fmt_pct(prev_code_ok, len(prev_all))}")
    print(f"    CMs con código no-vacío  : {fmt_pct(corr_code_ok, len(corr_all))}")

    # Distribución de códigos únicos
    pm_codes  = set(prev_all["equipment_code"].dropna().unique())
    cm_codes  = set(corr_all["equipment_code"].dropna().unique())
    comunes   = pm_codes & cm_codes
    print(f"\n    Códigos únicos en PMs    : {len(pm_codes)}")
    print(f"    Códigos únicos en CMs    : {len(cm_codes)}")
    print(f"    Códigos COMUNES          : {len(comunes)}")
    if len(comunes) == 0:
        print("\n  ⚠️  NO HAY CÓDIGOS COMUNES ENTRE PMs Y CMs.")
        print("     merge_asof(by='equipment_code') no encontrará ningún par.")
        print("     → El 100% ES UN ARTEFACTO DEL ALGORITMO (no datos reales).")
    else:
        print(f"\n  ✅ Hay {len(comunes)} equipos con tanto PMs como CMs → matching posible.")

    # 6. Breakdown por equipo (grupo_responsable)
    print(f"\n[6] PMs y CMs por equipo (clientes COPEC/ESMAX/SHELL):")
    print(f"    {'Equipo':<35} {'PMs':>6} {'PMs c/cod':>10} {'CMs':>6} {'CMs c/cod':>10} {'Match posible':>14}")
    print("    " + "-" * 85)

    for gk, ginfo in GRUPOS_TERRENO.items():
        miembros = ginfo.get("miembros", [])
        # PMs de este equipo
        pm_eq = prev_all[prev_all["equipo"] == gk] if "equipo" in prev_all.columns else pd.DataFrame()
        cm_eq = corr_all  # los CMs no tienen grupo asignado a priori
        # Contar CMs donde el técnico responsable pertenece a este equipo
        # (esto no aplica aquí porque los CMs no tienen grupo — el grupo va por el técnico del PM)
        pm_codes_eq = set(pm_eq["equipment_code"].dropna().unique()) if not pm_eq.empty else set()
        # Verificar cuántos CMs de CUALQUIER técnico tienen códigos que coinciden con PMs de este equipo
        if not pm_eq.empty and not corr_all.empty:
            matching_cms = corr_all[corr_all["equipment_code"].isin(pm_codes_eq)]
        else:
            matching_cms = pd.DataFrame()

        pm_ok = (pm_eq["equipment_code"].str.strip() != "").sum() if not pm_eq.empty else 0
        cm_ok_total = corr_code_ok  # global (los CMs no tienen equipo)
        print(f"    {gk:<35} {len(pm_eq):>6} {fmt_pct(pm_ok, len(pm_eq)):>10} "
              f"{'N/A':>6} {'N/A':>10} "
              f"{len(matching_cms):>14}")

    # 7. Muestra de códigos de equipo en PMs
    print(f"\n[7] Ejemplos de equipment_code en PMs COPEC/ESMAX/SHELL:")
    sample_pm = prev_all[["folio","client","station","equipment","equipment_code","maint_type","technician"]].head(20)
    print(sample_pm.to_string(index=False))

    # 8. Muestra de códigos de equipo en CMs
    print(f"\n[8] Ejemplos de equipment_code en CMs COPEC/ESMAX/SHELL:")
    sample_cm = corr_all[["folio","client","station","equipment","equipment_code","maint_type","technician"]].head(20)
    print(sample_cm.to_string(index=False))

    # 9. Intentar merge_asof manual para ver qué pasaría
    print(f"\n[9] Simulando merge_asof (muestra top-20 matches potenciales):")
    try:
        import unicodedata
        prev_s = prev_all.copy()
        corr_s = corr_all.copy()
        prev_s["fecha_dt"] = pd.to_datetime(prev_s["final_date"].dt.tz_convert(None).dt.date)
        _cd = corr_s["creation_date"].where(corr_s["creation_date"].notna(), corr_s["final_date"])
        corr_s["fecha_dt"] = pd.to_datetime(_cd.dt.tz_convert(None).dt.date)

        prev_s = prev_s.sort_values("fecha_dt").reset_index(drop=True)
        corr_s = corr_s.sort_values("fecha_dt").reset_index(drop=True)

        merged = pd.merge_asof(
            corr_s[["folio","equipment_code","fecha_dt","client","station","equipment","technician"]],
            prev_s[["folio","equipment_code","fecha_dt","client","station","equipment","technician"]],
            by="equipment_code",
            on="fecha_dt",
            direction="backward",
            suffixes=("_cm","_pm"),
        )
        found = merged[merged["folio_pm"].notna()].copy()
        found["dias"] = (found["fecha_dt"] - found["fecha_dt"].map(
            prev_s.set_index("folio")["fecha_dt"].to_dict()
        )).dt.days if False else 0  # simplificado

        # Calcular días manualmente
        prev_map = prev_s.set_index("folio")["fecha_dt"].to_dict()
        found["fecha_pm"] = found["folio_pm"].map(prev_map)
        found["dias_entre"] = (found["fecha_dt"] - found["fecha_pm"]).dt.days
        en_ventana = found[(found["dias_entre"] >= 1) & (found["dias_entre"] <= 5)]

        print(f"    Total CMs con PM encontrado : {len(found)}")
        print(f"    CMs dentro de ventana 1-5d  : {len(en_ventana)}")

        if not en_ventana.empty:
            print(f"\n    → REINCIDENCIAS DETECTADAS ({len(en_ventana)}):")
            print(en_ventana[["equipment_code","station_cm","folio_pm","folio_cm","dias_entre"]].head(20).to_string(index=False))
        else:
            print(f"\n    → NO SE DETECTARON REINCIDENCIAS en ventana 1-5 días")
            # Investigar por qué
            if len(found) > 0:
                print(f"\n    Distribución días entre PM y CM (encontrados):")
                print(found["dias_entre"].describe())
                print(f"\n    Primeros 10 matches (fuera de ventana):")
                print(found[["equipment_code","station_cm","folio_pm","folio_cm","dias_entre"]].head(10).to_string(index=False))
            elif len(comunes) == 0:
                print("    ← Confirmado: sin códigos comunes → merge_asof sin matches")
            else:
                print("    ← Revisar fechas o filtros de tipo de OT")
    except Exception as e:
        print(f"    Error en simulación: {e}")

    # 10. Verificación por mes
    print(f"\n[10] ¿En qué meses hay PMs y CMs de COPEC/ESMAX/SHELL?")
    if not prev_all.empty:
        prev_all["mes"] = prev_all["final_date"].dt.tz_convert(None).dt.to_period("M").astype(str)
        print("     PMs por mes:")
        print("     " + prev_all["mes"].value_counts().sort_index().to_string())
    if not corr_all.empty:
        _cd2 = corr_all["creation_date"].where(corr_all["creation_date"].notna(), corr_all["final_date"])
        corr_all["mes"] = _cd2.dt.tz_convert(None).dt.to_period("M").astype(str)
        print("     CMs por mes:")
        print("     " + corr_all["mes"].value_counts().sort_index().to_string())

    print(f"\n{SEP}")
    print("  FIN DIAGNÓSTICO")
    print(SEP)


if __name__ == "__main__":
    main()
