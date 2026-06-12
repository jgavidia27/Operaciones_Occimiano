"""
actualizar_prioridades_excel.py
================================
Para cada OT en Supabase, busca la prioridad en el Excel correspondiente
y actualiza SOLO el campo prioridad_calc en ordenes_trabajo.

Reglas:
- COPEC:  Excel LLAMADOS DD  -> actualiza OTs del 01/01 al 02/06/2026
          (desde 03/06 en adelante el robot de email ya tiene la prioridad correcta)
- SHELL:  Excel Detalle      -> actualiza todas las OTs 2026 encontradas
- ESMAX:  Excel LLAMADOS DD  -> actualiza todas las OTs 2026 encontradas

Lo que NO cambia: folio, fechas, tecnico, EDS, T0, T1, cumplimiento.
Solo prioridad_calc.
"""
import pandas as pd
import requests
import json
import time

BASE_DRIVE = r"G:\.shortcut-targets-by-id\15zHnoU5VZlkOwYc6EBziNcnS-sAAtwtk\OPERACIONES\OPERACIONES\SLA OPERACIONES"
SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=minimal",
}

# ── PASO 1: Construir lookup folio -> prioridad desde los 3 Excels ───────────
print("Construyendo lookup de prioridades desde Excel...")
lookup = {}  # {folio: {"prio": "P1", "fuente": "excel_copec"}}

# COPEC — LLAMADOS DD, col 11=folio, col 12=P1-P2-P3-P4, col 5=fecha
# Solo actualizar OTs hasta 02/06/2026 (antes del robot)
COPEC_CORTE = pd.Timestamp("2026-06-03")
try:
    df_cp = pd.read_excel(
        BASE_DRIVE + r"\Llamados Correctivos COPEC 2024 V2.0.xlsx",
        sheet_name="LLAMADOS DD", header=3)
    df_cp.columns = [str(c).strip() for c in df_cp.columns]
    df_cp["folio"] = df_cp.iloc[:, 11].astype(str).str.strip()
    df_cp["prio"]  = df_cp.iloc[:, 12].astype(str).str.strip().str.upper()
    df_cp["fecha"] = pd.to_datetime(df_cp.iloc[:, 5], errors="coerce")
    df_cp = df_cp[
        (df_cp["fecha"].dt.year == 2026) &
        (df_cp["fecha"] < COPEC_CORTE) &
        df_cp["folio"].notna() &
        ~df_cp["folio"].isin(["nan","","S/OS","None"]) &
        df_cp["prio"].isin(["P1","P2","P3","P4"])
    ]
    for _, r in df_cp.iterrows():
        lookup[r["folio"]] = {"prio": r["prio"], "fuente": "excel_copec"}
    print(f"  COPEC (01/01-02/06): {len(df_cp)} OTs cargadas del Excel")
except Exception as e:
    print(f"  ERROR COPEC: {e}")

# SHELL — Detalle, col 12=folio, col 13=P1-P2-P3-P4, col 6=fecha
try:
    df_sh = pd.read_excel(
        BASE_DRIVE + r"\Llamados correctivos Shell.xlsx",
        sheet_name="Detalle", header=1)
    df_sh.columns = [str(c).strip() for c in df_sh.columns]
    df_sh["folio"] = df_sh.iloc[:, 12].astype(str).str.strip()
    df_sh["prio"]  = df_sh.iloc[:, 13].astype(str).str.strip().str.upper()
    df_sh["fecha"] = pd.to_datetime(df_sh.iloc[:, 6], errors="coerce")
    df_sh = df_sh[
        (df_sh["fecha"].dt.year == 2026) &
        df_sh["folio"].notna() &
        ~df_sh["folio"].isin(["nan","","S/OS","None","OS FRACTTAL"]) &
        df_sh["prio"].isin(["P1","P2","P3","P4"])
    ]
    for _, r in df_sh.iterrows():
        if r["folio"] not in lookup:  # no sobreescribir si ya esta
            lookup[r["folio"]] = {"prio": r["prio"], "fuente": "excel_shell"}
    print(f"  SHELL (2026):        {len(df_sh)} OTs cargadas del Excel")
except Exception as e:
    print(f"  ERROR SHELL: {e}")

# ESMAX — LLAMADOS DD, col 11=folio, col 13=PRIORIDAD, col 5=fecha
try:
    df_em = pd.read_excel(
        BASE_DRIVE + r"\Llamados Correctivos 2025 ESMAX.xlsx",
        sheet_name="LLAMADOS DD", header=3)
    df_em.columns = [str(c).strip() for c in df_em.columns]
    df_em["folio"] = df_em.iloc[:, 11].astype(str).str.strip()
    df_em["prio"]  = df_em.iloc[:, 13].astype(str).str.strip().str.upper()
    df_em["fecha"] = pd.to_datetime(df_em.iloc[:, 5], errors="coerce")
    df_em = df_em[
        (df_em["fecha"].dt.year == 2026) &
        df_em["folio"].notna() &
        ~df_em["folio"].isin(["nan","","S/OS","None"]) &
        df_em["prio"].isin(["P1","P2","P3","P4"])
    ]
    for _, r in df_em.iterrows():
        if r["folio"] not in lookup:
            lookup[r["folio"]] = {"prio": r["prio"], "fuente": "excel_esmax"}
    print(f"  ESMAX (2026):        {len(df_em)} OTs cargadas del Excel")
except Exception as e:
    print(f"  ERROR ESMAX: {e}")

print(f"\nTotal OTs en lookup Excel: {len(lookup)}")

# ── PASO 2: Obtener OTs de Supabase ──────────────────────────────────────────
print("\nObteniendo OTs de Supabase...")
from supabase_client import _query
rows = _query("ordenes_trabajo",
    "select=id_ot,prioridad_calc,cliente"
    "&tipo_tarea=eq.CORRECTIVA&fecha_creacion=gte.2026-01-01",
    limit=10000)
df_supa = pd.DataFrame(rows)
df_supa["id_ot"] = df_supa["id_ot"].astype(str).str.strip()
print(f"  {len(df_supa)} OTs en Supabase")

# ── PASO 3: Identificar qué actualizar ───────────────────────────────────────
actualizar = []
sin_match  = []
ya_correcto = []

for _, row in df_supa.iterrows():
    folio = row["id_ot"]
    p_supa = str(row.get("prioridad_calc","")).strip()
    if folio in lookup:
        p_excel = lookup[folio]["prio"]
        if p_excel != p_supa:
            actualizar.append({
                "folio":    folio,
                "cliente":  row.get("cliente",""),
                "p_antes":  p_supa,
                "p_nuevo":  p_excel,
                "fuente":   lookup[folio]["fuente"],
            })
        else:
            ya_correcto.append(folio)
    else:
        sin_match.append(folio)

print(f"\nAnalisis:")
print(f"  Requieren actualizacion: {len(actualizar)}")
print(f"  Ya correctos (Excel=Supa): {len(ya_correcto)}")
print(f"  Sin match en Excel: {len(sin_match)} (se dejan como estan)")

# Distribucion de cambios
from collections import Counter
dist = Counter((x["p_antes"], x["p_nuevo"]) for x in actualizar)
print(f"\nCambios a realizar:")
umbral = {"P1":"24h","P2":"48h","P3":"72h","P4":"96h"}
for (pa, pn), cnt in sorted(dist.items(), key=lambda x: -x[1]):
    print(f"  {pa}({umbral.get(pa,'?')}) -> {pn}({umbral.get(pn,'?')}): {cnt} OTs")

# ── PASO 4: Ejecutar las actualizaciones ─────────────────────────────────────
print(f"\nActualizando {len(actualizar)} OTs en Supabase...")
ok = 0
err = 0

for item in actualizar:
    folio = item["folio"]
    r = requests.patch(
        SUPABASE_URL + f"/rest/v1/ordenes_trabajo?id_ot=eq.{folio}",
        headers=HEADERS,
        data=json.dumps({"prioridad_calc": item["p_nuevo"]}),
        timeout=10
    )
    if r.status_code in (200, 204):
        ok += 1
    else:
        err += 1
        print(f"  ERROR {folio}: {r.status_code} {r.text[:80]}")

    if ok % 100 == 0 and ok > 0:
        print(f"  {ok}/{len(actualizar)} actualizados...")

    time.sleep(0.05)  # respetar rate limit

print(f"\nActualizaciones completadas:")
print(f"  OK:    {ok}")
print(f"  ERROR: {err}")

# ── PASO 5: Verificacion rapida ───────────────────────────────────────────────
print("\nVerificando resultado...")
rows2 = _query("ordenes_trabajo",
    "select=id_ot,prioridad,prioridad_calc,cliente"
    "&tipo_tarea=eq.CORRECTIVA&fecha_creacion=gte.2026-01-01",
    limit=10000)
df_post = pd.DataFrame(rows2)

for cli in ["COPEC","ESMAX (Aramco)","SHELL (Enex)"]:
    sub = df_post[df_post["cliente"]==cli]
    dist_post = sub["prioridad_calc"].value_counts().sort_index()
    print(f"  {cli}: {dict(dist_post)}")

print("\nListo. La vista v_llamados_sla ya usa prioridad_calc actualizada.")
print("Los umbrales SLA ahora corresponden a las prioridades del Excel.")
