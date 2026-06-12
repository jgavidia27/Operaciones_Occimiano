"""
Diagnóstico de velocidad del dashboard Occimiano.
Mide cada operación del pipeline exactamente como lo hace app.py al abrir una página.
"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'C:\Users\jgavi\Documents\occimiano_dashboard')

def t(label, fn):
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    n = len(result) if hasattr(result, '__len__') else '?'
    print(f"  {elapsed:6.2f}s  {label}  ({n} rows)")
    return result

print("=" * 60)
print("  DIAGNÓSTICO VELOCIDAD — SIN CACHÉ STREAMLIT")
print("=" * 60)

# ─── 1. Supabase: queries individuales ─────────────────────────────────
print()
print("1. SUPABASE QUERIES (tiempo de red + DB)")

from supabase_client import (
    load_work_orders_supabase, load_listado_eds_supabase,
    load_tecnicos_supabase, load_equipos_supabase,
    load_all_llamados_supabase, load_sla_umbrales_supabase
)

raw_wo       = t("load_work_orders_supabase()  [ordenes_trabajo]", load_work_orders_supabase)
eds_df       = t("load_listado_eds_supabase()  [estaciones_servicio]", load_listado_eds_supabase)
tecs_df      = t("load_tecnicos_supabase()     [tecnicos]", load_tecnicos_supabase)
grp_dict     = t("load_equipos_supabase()      [equipos]", load_equipos_supabase)
llamados_df  = t("load_all_llamados_supabase() [v_llamados_sla]", load_all_llamados_supabase)
sla_dict     = t("load_sla_umbrales_supabase() [sla_umbrales_horas]", load_sla_umbrales_supabase)

# ─── 2. Procesamiento Python ────────────────────────────────────────────
print()
print("2. PROCESAMIENTO PYTHON (build_* / score_*)")

from data import (
    build_work_orders_df, build_reincidencias,
    build_kpi_llenado_df, score_llenado_por_ot, score_llenado_por_tecnico
)
import unicodedata
from data import TECNICOS_NO_APLICA, GRUPOS_TERRENO

def _norm(s):
    return unicodedata.normalize('NFD', str(s)).encode('ascii','ignore').decode().strip().lower()
_no_aplica_norm = set()
for na in TECNICOS_NO_APLICA:
    _no_aplica_norm.add(_norm(na))
    pts = na.split()
    for i in range(len(pts)):
        for j in range(i+1,len(pts)):
            _no_aplica_norm.add(_norm(pts[i]+' '+pts[j]))
def es_excluido(t):
    if not isinstance(t,str) or not t.strip(): return False
    if _norm(t.strip()) in _no_aplica_norm: return True
    pts = t.strip().split()
    for i in range(len(pts)):
        for j in range(i+1,len(pts)):
            if _norm(pts[i]+' '+pts[j]) in _no_aplica_norm: return True
    return False
_grupos_norm = {}
for grp_k, grp_v in GRUPOS_TERRENO.items():
    for mb in grp_v['miembros']:
        _grupos_norm[_norm(mb)] = grp_k
        pts = mb.split()
        for i in range(len(pts)):
            for j in range(i+1,len(pts)):
                _grupos_norm[_norm(pts[i]+' '+pts[j])] = grp_k
def get_equipo(t):
    if not isinstance(t,str) or not t.strip(): return 'Sin equipo'
    n = _norm(t.strip())
    if n in _grupos_norm: return _grupos_norm[n]
    pts = t.strip().split()
    for i in range(len(pts)):
        for j in range(i+1,len(pts)):
            a = _norm(pts[i]+' '+pts[j])
            if a in _grupos_norm: return _grupos_norm[a]
    return 'Sin equipo'

df_wo       = t("build_work_orders_df(raw_wo)            [5792 recs]", lambda: build_work_orders_df(raw_wo))
df_reinc    = t("build_reincidencias(df_wo)              [merge_asof]", lambda: build_reincidencias(df_wo))
df_kpi_raw  = t("build_kpi_llenado_df(raw_wo)            [kpi rows]", lambda: build_kpi_llenado_df(raw_wo))
df_ot_all   = t("score_llenado_por_ot(df_kpi_raw)        [per OT]", lambda: score_llenado_por_ot(df_kpi_raw))
df_tec      = t("score_llenado_por_tecnico(df_ot_all)    [per tec]", lambda: score_llenado_por_tecnico(df_ot_all))

# get_equipo apply (el dashboard hace esto en cada tab)
import time, pandas as pd
t0 = time.perf_counter()
df_wo['equipo'] = df_wo['technician'].apply(get_equipo)
print(f"  {time.perf_counter()-t0:6.2f}s  df_wo['equipo'] = apply(get_equipo)  ({len(df_wo)} rows)")

t0 = time.perf_counter()
df_wo['es_excluido'] = df_wo['technician'].apply(es_excluido)
print(f"  {time.perf_counter()-t0:6.2f}s  df_wo['excluido'] = apply(es_excluido)  ({len(df_wo)} rows)")

# ─── 3. Resumen ─────────────────────────────────────────────────────────
print()
print("=" * 60)
print("  RESUMEN — bottlenecks (> 1 s)")
print("=" * 60)
print()
print("Tip: las queries Supabase > 1s = costosas para CADA pagina")
print("     sin caché en disco (primera carga o tras reinicio).")
