"""
geocodificar_domicilios.py — Convierte direccion textual a lat/lng.
==================================================================

- Lee de `tecnicos_hhee` los registros con domicilio_direccion pero sin
  domicilio_lat/lng.
- Consulta OpenStreetMap Nominatim (gratis, sin API key).
- Actualiza las filas con lat/lng.
- Respeta el rate limit de 1 req/s de Nominatim (courtesy).

Ejecucion:
    python geocodificar_domicilios.py

Notas:
- Nominatim exige User-Agent identificable. Usamos "Occimiano-HHEE/1.0".
- Cachea resultados en scratchpad/geocode_cache.json para no re-consultar.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests


# ── Cargar .env ─────────────────────────────────────────────────────────────
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT    = "Occimiano-HHEE/1.0 (operaciones@occimiano.cl)"
CACHE_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_geocode_cache.json")


# ── Cache local ─────────────────────────────────────────────────────────────
def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


import re as _re

# ── Geocoder Nominatim ──────────────────────────────────────────────────────
def _nominatim_query(query: str) -> tuple[float, float] | None:
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        return None


def _simplificar_direccion(direccion: str) -> str:
    """Remueve depto/lote/pasaje/casa que confunden a Nominatim."""
    d = direccion
    # Quitar "depto. XXXX", "casa X", "lote X"
    d = _re.sub(r',?\s*(depto|dpto|departamento|casa|lote|of\.?|oficina)[\s\.]*[A-Z0-9\-]+',
                '', d, flags=_re.IGNORECASE)
    # Quitar "pasaje" y "calle" al inicio
    d = _re.sub(r'^\s*(pasaje|pje\.?|calle|cll)\s+', '', d, flags=_re.IGNORECASE)
    # Quitar "N°" antes del numero
    d = _re.sub(r'N[°º\.]\s*', '', d)
    return d.strip(' ,')


def geocode(direccion: str, comuna: str | None = None,
            region: str | None = None, pais: str = "Chile") -> tuple[float, float] | None:
    """Consulta Nominatim con hasta 3 estrategias progresivamente mas laxas.
    Retorna (lat, lng) o None."""
    clean_region = region.split(":", 1)[-1].strip() if region and ":" in region else region

    def _q(*parts):
        return ", ".join(p for p in parts if p)

    # Estrategia 1: dirección completa
    for intento in [
        _q(direccion, comuna, clean_region, pais),
        _q(direccion, clean_region, pais),
        _q(_simplificar_direccion(direccion), comuna, clean_region, pais),
        _q(_simplificar_direccion(direccion), clean_region, pais),
    ]:
        if not intento:
            continue
        time.sleep(1.1)  # rate limit
        r = _nominatim_query(intento)
        if r:
            return r

    return None


# ── Cliente Supabase ────────────────────────────────────────────────────────
def supabase_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def fetch_pendientes() -> list[dict]:
    """Trae técnicos con domicilio_direccion pero sin lat/lng."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/tecnicos_hhee"
        "?select=rut,nombre_completo,domicilio_direccion,domicilio_comuna,domicilio_region,domicilio_lat,domicilio_lng"
        "&domicilio_direccion=not.is.null&domicilio_lat=is.null",
        headers=supabase_headers(),
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Supabase fetch {r.status_code}: {r.text[:200]}")
    return r.json()


def patch_coords(rut: str, lat: float, lng: float) -> bool:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/tecnicos_hhee?rut=eq.{requests.utils.quote(rut)}",
        headers={**supabase_headers(), "Prefer": "return=minimal"},
        json={"domicilio_lat": lat, "domicilio_lng": lng},
        timeout=15,
    )
    return r.status_code in (200, 204)


# ── Log ─────────────────────────────────────────────────────────────────────
def log_start(script: str) -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs",
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json={"script": script, "estado": "running"},
            timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0].get("id")
    except Exception:
        pass
    return None


def log_end(log_id: int | None, estado: str, filas: int, mensaje: str = ""):
    if not log_id:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs?id=eq.{log_id}",
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            json={
                "estado":         estado,
                "filas_upserted": filas,
                "mensaje":        mensaje[:500] if mensaje else None,
                "fin":            datetime.now(timezone.utc).isoformat(),
            },
            timeout=10,
        )
    except Exception:
        pass


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Falta SUPABASE_URL o SUPABASE_KEY en .env", file=sys.stderr)
        return 1

    log_id = log_start("geocodificar_domicilios")
    cache = load_cache()
    n_ok, n_fail = 0, 0

    try:
        pendientes = fetch_pendientes()
        print(f"[1/2] {len(pendientes)} tecnicos sin coordenadas.")
        if not pendientes:
            print("  Nada que geocodificar.")
            log_end(log_id, "success", 0, "sin pendientes")
            return 0

        print("[2/2] Consultando Nominatim (1 req/s)...")
        for p in pendientes:
            direccion = p.get("domicilio_direccion")
            comuna    = p.get("domicilio_comuna")
            region    = p.get("domicilio_region")
            nombre    = p.get("nombre_completo", "?")
            rut       = p.get("rut")
            cache_key = f"{direccion}|{comuna}|{region}"

            if cache_key in cache and cache[cache_key]:
                lat, lng = cache[cache_key]
                origen = "cache"
            else:
                # geocode() internamente respeta rate limit por cada intento
                coords = geocode(direccion, comuna, region)
                if not coords:
                    print(f"  [FAIL] {nombre[:30]:<30} -> no encontro '{direccion}'")
                    n_fail += 1
                    cache[cache_key] = None
                    save_cache(cache)
                    continue
                lat, lng = coords
                cache[cache_key] = [lat, lng]
                save_cache(cache)
                origen = "nominatim"

            if patch_coords(rut, lat, lng):
                print(f"  [OK]   {nombre[:30]:<30} -> ({lat:.5f}, {lng:.5f}) [{origen}]")
                n_ok += 1
            else:
                print(f"  [ERR]  {nombre[:30]:<30} -> patch fallo")
                n_fail += 1

        print(f"\nResumen: {n_ok} OK, {n_fail} fallidos.")
        log_end(log_id, "success", n_ok, f"{n_ok} ok, {n_fail} fail")
        return 0 if n_fail == 0 else 2

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\nERROR: {e}\n{tb}", file=sys.stderr)
        log_end(log_id, "error", n_ok, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
