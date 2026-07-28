"""sync_enlace.py
=====================================================================
Sincroniza el pool de avisos del Portal Enlace Copec a Supabase.

Auth: OAuth2 refresh_token flow (FusionAuth OIDC).
  - El refresh_token vive en la tabla enlace_auth (una fila, id=1).
  - Cada corrida obtiene un access_token nuevo con ese refresh_token.
  - FusionAuth rota el refresh_token en cada llamada → persistimos el
    nuevo de vuelta en Supabase.

API descubierta:
  GET https://aviso.apigw.us-west-2.prd.mantenimiento.git.copec.cl/avisos
      Authorization: Bearer <access_token>
      → { data: { avisos: [...], responsables: [...] } }

Bootstrap: la primera vez, insertar manualmente en enlace_auth el
refresh_token extraído del localStorage del navegador logueado en
https://portalenlace.copec.cl. Ver bootstrap_enlace_auth.py.

Corre cada 15 min (cron GitHub Actions).
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────
FUSIONAUTH_TOKEN = "https://copec-sa.fusionauth.io/oauth2/token"
ENLACE_CLIENT_ID = "a95848f7-750f-4547-b081-2be98c98c0c9"
ENLACE_API_BASE  = "https://aviso.apigw.us-west-2.prd.mantenimiento.git.copec.cl"
ENLACE_AVISOS    = f"{ENLACE_API_BASE}/avisos"
ENLACE_LIMIT     = 5000   # el servidor devuelve todo lo del usuario; usamos un limit alto

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SB_AUTH      = "enlace_auth"
SB_AVISOS    = "enlace_avisos"


def log(msg: str, tag: str = ""):
    now = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{tag}] " if tag else ""
    print(f"[{now}] {prefix}{msg}", flush=True)


# ── Supabase helpers ──────────────────────────────────────────────────
def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def sb_get_auth() -> dict:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SB_AUTH}",
        params={"id": "eq.1", "select": "*"},
        headers=_sb_headers(), timeout=20,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError(
            "Tabla enlace_auth está vacía. Corre bootstrap_enlace_auth.py "
            "primero para inyectar el refresh_token inicial."
        )
    return rows[0]


def sb_update_auth(refresh_token: str, access_token: str, expires_at: str, error: str = None):
    body = {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "expires_at": expires_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_error": error,
    }
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SB_AUTH}",
        params={"id": "eq.1"},
        headers={**_sb_headers(), "Prefer": "return=minimal"},
        json=body, timeout=20,
    )
    r.raise_for_status()


def sb_flag_auth_error(error: str):
    """Marca last_error sin tocar los tokens (para que el panel muestre alerta)."""
    body = {"last_error": error, "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/{SB_AUTH}",
            params={"id": "eq.1"},
            headers={**_sb_headers(), "Prefer": "return=minimal"},
            json=body, timeout=10,
        )
    except Exception:
        pass


def sb_upsert_avisos(rows: list[dict]):
    if not rows:
        return
    for i in range(0, len(rows), 500):
        chunk = rows[i:i+500]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{SB_AVISOS}",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=chunk, timeout=60,
        )
        if r.status_code >= 400:
            log(f"upsert error {r.status_code}: {r.text[:400]}", "ERR")
            r.raise_for_status()


# ── OAuth ─────────────────────────────────────────────────────────────
def refresh_access_token(refresh_token: str) -> dict:
    r = requests.post(
        FUSIONAUTH_TOKEN,
        data={
            "grant_type": "refresh_token",
            "client_id": ENLACE_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"FusionAuth refresh falló ({r.status_code}): {r.text[:300]}")
    j = r.json()
    return {
        "access_token": j["access_token"],
        "refresh_token": j.get("refresh_token", refresh_token),
        "expires_in": j.get("expires_in", 28800),
    }


# ── Fetch avisos ──────────────────────────────────────────────────────
def fetch_avisos(access_token: str) -> list[dict]:
    r = requests.get(
        ENLACE_AVISOS,
        params={"limit": ENLACE_LIMIT},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"API Enlace devolvió error: {j.get('message')}")
    return (j.get("data") or {}).get("avisos") or []


# ── Transform ─────────────────────────────────────────────────────────
def _extraer_eds(id_instalacion: str | None) -> str | None:
    """id_instalacion viene como 'C-E-13-CE-60066' → devolvemos '60066'."""
    if not id_instalacion:
        return None
    parts = id_instalacion.strip().split("-")
    tail = parts[-1] if parts else ""
    return tail if tail.isdigit() else None


def _parse_ts(s: str | None) -> str | None:
    """API devuelve ISO con microsegundos → dejamos tal cual (Postgres lo parsea)."""
    return s if s else None


def transform_aviso(a: dict) -> dict:
    return {
        "id_sap":                       a.get("id_sap"),
        "numero_aviso":                 a.get("numero_aviso"),
        "numero_orden":                 a.get("numero_orden"),
        "tipo_aviso":                   a.get("tipo_aviso"),
        "tipo_atencion_mantenimiento":  a.get("tipo_atencion_mantenimiento"),
        "estado":                       a.get("estado"),
        "prioridad":                    a.get("prioridad"),
        "descripcion":                  a.get("descripcion"),
        "descripcion_falla":            a.get("descripcion_falla"),
        "titulo_aviso":                 a.get("titulo_aviso"),
        "descripcion_equipo":           a.get("descripcion_equipo"),
        "descripcion_producto":         a.get("descripcion_producto"),
        "descripcion_ubicacion":        a.get("descripcion_ubicacion"),
        "descripcion_instalacion":      a.get("descripcion_instalacion"),
        "descripcion_componente":       a.get("descripcion_componente"),
        "id_instalacion":               a.get("id_instalacion"),
        "eds_codigo":                   _extraer_eds(a.get("id_instalacion")),
        "id_equipo":                    a.get("id_equipo"),
        "id_producto":                  a.get("id_producto"),
        "id_ubicacion":                 a.get("id_ubicacion"),
        "id_falla":                     a.get("id_falla"),
        "id_grupo_falla":               a.get("id_grupo_falla"),
        "puesto_trabajo":               a.get("puesto_trabajo"),
        "ubicacion_tecnica":            a.get("ubicacion_tecnica"),
        "nombre_usuario_asignado":      a.get("nombre_usuario_asignado"),
        "id_usuario_asignado":          a.get("id_usuario_asignado"),
        "razon_social_empresa":         a.get("razon_social_empresa"),
        "rut_empresa":                  a.get("rut_empresa"),
        "responsable":                  a.get("responsable"),
        "nombre_contacto":              a.get("nombre_contacto"),
        "telefono_contacto":            a.get("telefono_contacto"),
        "sla":                          a.get("sla"),
        "fecha_creacion":               _parse_ts(a.get("fecha_creacion")),
        "fecha_planificada":            _parse_ts(a.get("fecha_planificada")),
        "fecha_ultimo_cambio":          _parse_ts(a.get("fecha_ultimo_cambio")),
        "multimedia":                   a.get("multimedia"),
        "campos_adicionales":           a.get("campos_adicionales"),
        "raw":                          a,
        "sync_at":                      datetime.now(timezone.utc).isoformat(),
    }


# ── Main ──────────────────────────────────────────────────────────────
def main():
    log("Cargando estado OAuth desde Supabase...", "AUTH")
    auth = sb_get_auth()

    try:
        tokens = refresh_access_token(auth["refresh_token"])
    except Exception as e:
        msg = f"Refresh falló: {e}"
        log(msg, "AUTH")
        sb_flag_auth_error(msg)
        sys.exit(1)

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"] - 60)).isoformat()
    sb_update_auth(tokens["refresh_token"], tokens["access_token"], expires_at, error=None)
    log(f"access_token renovado (expira en {tokens['expires_in']}s)", "AUTH")

    log("Consultando /avisos...", "API")
    avisos = fetch_avisos(tokens["access_token"])
    log(f"{len(avisos)} avisos recibidos", "API")

    rows = [transform_aviso(a) for a in avisos if a.get("id_sap")]
    log(f"Upserteando {len(rows)} filas en Supabase...", "DB")
    sb_upsert_avisos(rows)
    log("OK", "DONE")


if __name__ == "__main__":
    main()
