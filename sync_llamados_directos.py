"""
sync_llamados_directos.py
=========================
Detecta OTs correctivas de COPEC/Shell/ESMAX creadas directamente
en Fracttal (sin correo ni robot) y las agrega a llamados_correctivos
con fuente = 'ot_directa'.

Se ejecuta diariamente. Complementa al robot de correos.
"""

import json
import time
import requests
from datetime import datetime, timezone

SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
CLIENTES_SLA = ("COPEC", "ESMAX (Aramco)", "SHELL (Enex)")
_ZONE_NAMES = {"CENTRO", "SUR", "SANTIAGO", "NORTE"}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def _build_loc_to_eds(h):
    """Mapea loc_fracttal -> (eds_occim, nombre) desde estaciones_servicio."""
    r = requests.get(
        SUPABASE_URL + "/rest/v1/estaciones_servicio"
        "?loc_fracttal=not.is.null&select=eds_occim,nombre,loc_fracttal",
        headers=h, timeout=15,
    )
    return {
        row["loc_fracttal"]: (row["eds_occim"], row.get("nombre") or "")
        for row in r.json()
        if row.get("loc_fracttal") and row.get("eds_occim")
    }

def main():
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    h_post = {**h, "Content-Type": "application/json",
              "Prefer": "resolution=merge-duplicates,return=minimal"}

    log("Buscando OTs directas de Fracttal no capturadas por robot...")
    loc_map = _build_loc_to_eds(h)

    # 1. Obtener folios que YA estan en llamados_correctivos
    r = requests.get(
        SUPABASE_URL + "/rest/v1/llamados_correctivos?select=os_fracttal&limit=5000",
        headers=h, timeout=15
    )
    ya_registrados = {
        row["os_fracttal"] for row in r.json()
        if row.get("os_fracttal")
    }
    log(f"  Ya registrados en llamados_correctivos: {len(ya_registrados)}")

    # 2. Obtener todas las OTs correctivas 2026 de los clientes SLA
    nuevos, revisados = [], 0
    for offset in range(0, 5000, 500):
        r2 = requests.get(
            SUPABASE_URL + "/rest/v1/ordenes_trabajo"
            "?tipo_tarea=eq.CORRECTIVA"
            "&select=id_ot,cliente,codigo_eds,estacion,responsable,"
            "prioridad_calc,fecha_creacion,fecha_finalizacion,estado,"
            "causa_raiz,nombre_activo,codigo_activo",
            headers={**h, "Range": f"{offset}-{offset+499}"},
            timeout=15
        )
        batch = r2.json()
        if not batch:
            break
        revisados += len(batch)

        for ot in batch:
            cliente = ot.get("cliente") or ""
            if cliente not in CLIENTES_SLA:
                continue
            folio = ot.get("id_ot", "")
            if folio in ya_registrados:
                continue  # ya lo tenemos

            # OT nueva no capturada por robot -> 'ot_directa'
            eds_cod = ot.get("codigo_eds") or ""
            eds_nom = ot.get("estacion") or ""

            if eds_cod.upper() in _ZONE_NAMES or not eds_cod:
                loc_code = ot.get("codigo_activo") or ""
                if not loc_code:
                    raw = ot.get("nombre_activo", "")
                    loc_code = raw.split("{ ")[-1].rstrip(" }") if "{ " in raw else ""
                if loc_code and loc_code in loc_map:
                    eds_cod, eds_nom = loc_map[loc_code]

            nuevos.append({
                "os_fracttal":     folio,
                "n_aviso":         None,
                "cliente":         cliente,
                "eds_codigo":      eds_cod or None,
                "eds_nombre":      eds_nom or None,
                "fecha_llamado":   ot.get("fecha_creacion"),
                "prioridad":       ot.get("prioridad_calc"),
                "tecnico":         ot.get("responsable"),
                "fecha_cierre":    ot.get("fecha_finalizacion"),
                "falla":           ot.get("causa_raiz"),
                "fuente":          "ot_directa",
            })

        if len(batch) < 500:
            break

    log(f"  OTs revisadas: {revisados} | Nuevas sin registrar: {len(nuevos)}")

    if not nuevos:
        log("Sin OTs nuevas para agregar.")
        return

    # 3. Insertar en llamados_correctivos
    BATCH = 200
    total = 0
    for i in range(0, len(nuevos), BATCH):
        batch = nuevos[i:i+BATCH]
        r3 = requests.post(
            SUPABASE_URL + "/rest/v1/llamados_correctivos",
            headers=h_post,
            data=json.dumps(batch, ensure_ascii=False),
            timeout=20
        )
        if r3.status_code in (200, 201, 204):
            total += len(batch)
        else:
            log(f"  Error {r3.status_code}: {r3.text[:150]}")

    log(f"  Agregadas {total} OTs directas con fuente='ot_directa'")

    # Resumen final
    r4 = requests.get(
        SUPABASE_URL + "/rest/v1/llamados_correctivos?select=fuente",
        headers={**h, "Prefer": "count=exact"},
        timeout=10
    )
    from collections import Counter
    fuentes = Counter(row["fuente"] for row in r4.json())
    log("Resumen llamados_correctivos:")
    for fuente, cnt in fuentes.items():
        icono = "[email]" if fuente == "robot_email" else ("[directa]" if fuente == "ot_directa" else "[manual]")
        log(f"  {icono} {fuente}: {cnt}")

if __name__ == "__main__":
    main()
