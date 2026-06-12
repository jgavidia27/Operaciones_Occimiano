"""
poblar_eds_supabase.py
======================
Lee el listado de EDS desde el Excel de Google Drive y lo carga
en la tabla estaciones_servicio de Supabase.

Uso: python poblar_eds_supabase.py
"""
import json
import sys
import requests

SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)

def log(msg): print(f"  {msg}")

def main():
    # Cargar EDS desde el Excel via la funcion existente del dashboard
    import streamlit as _st  # necesario para cache_data
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))

    print("Cargando EDS desde Excel de Google Drive...")
    try:
        from gdrive import load_listado_eds
        df_eds = load_listado_eds()
    except Exception as e:
        print(f"Error cargando Excel: {e}")
        print("Asegurate de tener Google Drive montado en G:/")
        return

    if df_eds.empty:
        print("No se encontraron datos de EDS")
        return

    print(f"EDS encontradas: {len(df_eds)}")

    # Mapear columnas del Excel al esquema de Supabase
    records = []
    for _, row in df_eds.iterrows():
        eds = str(row.get("eds_occim") or "").strip()
        if not eds or eds.lower() in ("nan", "none", ""):
            continue

        cliente = str(row.get("cliente") or "").strip()
        records.append({
            "eds_occim":       eds,
            "cliente":         cliente or None,
            "nombre":          str(row.get("direccion") or "").strip() or None,
            "direccion":       str(row.get("direccion") or "").strip() or None,
            "comuna":          str(row.get("comuna") or "").strip() or None,
            "region":          str(row.get("region") or "").strip() or None,
            "zona":            str(row.get("zona_occim") or "").strip() or None,
            "activa":          bool(row.get("activa", True)),
        })

    if not records:
        print("Sin registros validos para cargar")
        return

    print(f"Cargando {len(records)} EDS a Supabase...")

    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }

    # Cargar en batches
    BATCH = 100
    total = 0
    for i in range(0, len(records), BATCH):
        batch = records[i:i+BATCH]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/estaciones_servicio",
            headers=headers,
            data=json.dumps(batch, ensure_ascii=False),
            timeout=30,
        )
        if r.status_code not in (200, 201, 204):
            print(f"Error {r.status_code}: {r.text[:200]}")
        else:
            total += len(batch)
            print(f"  Cargadas {total}/{len(records)} EDS...")

    print(f"\nCOMPLETADO: {total} estaciones en Supabase")

    # Verificar
    r2 = requests.head(
        f"{SUPABASE_URL}/rest/v1/estaciones_servicio",
        headers={**headers, "Prefer": "count=exact"},
        timeout=10,
    )
    print(f"Total en Supabase: {r2.headers.get('content-range','?').split('/')[-1]} estaciones")


if __name__ == "__main__":
    main()
