"""
sync_estaciones_from_ots.py — Detecta EDS nuevas desde OTs de Fracttal.
========================================================================

Consulta OTs recientes de Fracttal, extrae códigos EDS únicos (campo
groups_2_description del WO), detecta las EDS que NO están en la tabla
`estaciones_servicio` de Supabase, y las auto-registra con datos básicos.

Datos extraídos por EDS desde el WO:
  groups_2_description  -> eds_occim (ej. "SH_419")
  groups_1_description  -> cliente Fracttal (ENEX/COPEC/etc) → cliente_norm
  parent_description    -> nombre (ej. "// SHELL/ SHELL AZOLAS/" → "Shell Azolas")
  items_log_description -> pista de comuna/ciudad (a completar manual después)

Ejecución:
    python sync_estaciones_from_ots.py             # último mes, upsert
    python sync_estaciones_from_ots.py --dias 90   # ultimos 90 dias
    python sync_estaciones_from_ots.py --dry-run   # no escribe, solo reporta

Env vars (.env):
    FRACTTAL_CLIENT_ID, FRACTTAL_CLIENT_SECRET
    SUPABASE_URL, SUPABASE_KEY
"""

import argparse
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

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

FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN_URL = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_WO_URL    = f"{FRACTTAL_BASE}/api/work_orders"
CLIENT_ID          = os.getenv("FRACTTAL_CLIENT_ID", "")
CLIENT_SECRET      = os.getenv("FRACTTAL_CLIENT_SECRET", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

PAGE_SIZE = 100   # Fracttal tope real: 100 items/pagina (limit>=150 devuelve 99)

# ── Normalización cliente Fracttal → nombre estándar en `estaciones_servicio`
# Alineado con lo que ya está en Supabase (validar con SELECT DISTINCT cliente).
_CLIENTE_MAP = {
    "ENEX":            "SHELL (Enex)",
    "SHELL":           "SHELL (Enex)",
    "SHELL (ENEX)":    "SHELL (Enex)",
    "COPEC":           "COPEC",
    "ESMAX":           "ESMAX (Aramco)",
    "ARAMCO":          "ESMAX (Aramco)",
    "ESMAX (ARAMCO)":  "ESMAX (Aramco)",
    "ABASTIBLE":       "ABASTIBLE",
    "PETROBRAS":       "PETROBRAS",
}


def _norm_cliente(g1: Optional[str]) -> Optional[str]:
    if not g1:
        return None
    key = str(g1).strip().upper()
    return _CLIENTE_MAP.get(key, g1.strip())


_NOMBRES_GENERICOS = {"LAVADORA", "ASPIRADORA", "ASPIRADO", "LAVADO",
                       "BOMBA", "EQUIPO", "MAX", "MSELF"}


def _extraer_nombre(parent_desc: Optional[str]) -> Optional[str]:
    """'// SHELL/ SHELL AZOLAS/ ' -> 'Shell Azolas'.
    Si el ultimo segmento es generico (LAVADORA, ASPIRADORA), sube 1 nivel."""
    if not parent_desc:
        return None
    parts = [p.strip() for p in parent_desc.replace("//", "").split("/") if p.strip()]
    if not parts:
        return None
    # Descartar segmentos genericos desde el final
    while parts and parts[-1].upper() in _NOMBRES_GENERICOS:
        parts.pop()
    if not parts:
        return None
    return parts[-1].title()


# Patrones validos de EDS Occimiano
_EDS_VALIDA = re.compile(r"""^(
    SH_\d+           |   # Shell (Enex): SH_419
    EE_S\d+          |   # Esmax/Aramco: EE_S268
    \d{4,6}          |   # Copec (codigo numerico 4-6 digitos)
    ABAS-\d+         |   # Abastible
    PBR-\d+              # Petrobras (si aplica)
)$""", re.VERBOSE)


def _es_eds_valida(codigo: str) -> bool:
    if not codigo:
        return False
    return bool(_EDS_VALIDA.match(str(codigo).strip().upper()))


def _extraer_comuna_pista(items_log: Optional[str]) -> Optional[str]:
    """'LAVADORA PENDIENTE SHELL AZOLAS ARICA' → 'ARICA'. Heurística: última
    palabra en MAYÚSCULAS que no sea el cliente ni un tipo de equipo."""
    if not items_log:
        return None
    words = str(items_log).upper().split()
    ignorar = {"LAVADORA","ASPIRADORA","ASPIRADO","LAVADO","BOMBA","MSELF","MAX",
               "SHELL","COPEC","ESMAX","ARAMCO","ENEX","PENDIENTE","MANTENCION","SIN"}
    # Retornar la última palabra sola con >=4 letras que no esté en la lista
    for w in reversed(words):
        w = w.strip(".,;:")
        if len(w) >= 4 and w not in ignorar and w.isalpha():
            return w.title()
    return None


# ── Fracttal ────────────────────────────────────────────────────────────────
_token_cache = {"token": None, "expires": datetime.min}


def get_token() -> str:
    if _token_cache["token"] and datetime.now() < _token_cache["expires"]:
        return _token_cache["token"]
    r = requests.post(
        FRACTTAL_TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    _token_cache.update({
        "token":   d["access_token"],
        "expires": datetime.now() + timedelta(seconds=d.get("expires_in", 3600) - 60),
    })
    return _token_cache["token"]


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fetch_wos_recientes(dias: int, verbose: bool = False) -> list[dict]:
    """Trae WOs paginado. Fracttal devuelve DESC por creation_date
    (más recientes primero). Sale cuando la ÚLTIMA fila del batch queda
    fuera del rango — así garantizamos que no cortemos por un solo WO
    con fecha rara en el medio."""
    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    todos = []
    headers = {"Authorization": f"Bearer {get_token()}"}
    start = 0
    max_paginas = 200  # safety: 200 * 200 = 40k WOs max
    for _pagina in range(max_paginas):
        r = requests.get(FRACTTAL_WO_URL, headers=headers,
                          params={"start": start, "limit": PAGE_SIZE}, timeout=45)
        if r.status_code == 429:
            time.sleep(5); continue
        if r.status_code in (401, 403):
            headers["Authorization"] = f"Bearer {get_token()}"; continue
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            break

        # Filtrar los del rango
        pagina_util = [wo for wo in data
                       if (dt := _parse_dt(wo.get("creation_date"))) and dt >= corte]
        todos.extend(pagina_util)

        # Ver la fecha del último WO del batch (el más antiguo por ordenamiento DESC)
        ultima_dt = _parse_dt(data[-1].get("creation_date"))
        if verbose:
            print(f"  fetch start={start} data={len(data)} útil={len(pagina_util)}  "
                  f"ultima_fecha={ultima_dt}  acum={len(todos)}")

        # Salir si el último WO ya está fuera del rango, o si el batch es corto (fin)
        if len(data) < PAGE_SIZE:
            break
        if ultima_dt and ultima_dt < corte:
            break
        start += PAGE_SIZE
        time.sleep(0.3)
    return todos


# ── Supabase ────────────────────────────────────────────────────────────────
def _sb_headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def get_eds_existentes() -> set[str]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/estaciones_servicio?select=eds_occim&limit=3000",
        headers=_sb_headers(), timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Supabase get estaciones {r.status_code}: {r.text[:200]}")
    return {row["eds_occim"] for row in r.json() if row.get("eds_occim")}


def insertar_estaciones(filas: list[dict]) -> int:
    if not filas:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/estaciones_servicio",
        headers={**_sb_headers(),
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=filas, timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase insert {r.status_code}: {r.text[:400]}")
    return len(filas)


def log_start(script: str) -> Optional[int]:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs",
            headers={**_sb_headers(), "Prefer": "return=representation"},
            json={"script": script, "estado": "running"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0].get("id")
    except Exception:
        pass
    return None


def log_end(log_id, estado, filas, mensaje=""):
    if not log_id: return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs?id=eq.{log_id}",
            headers={**_sb_headers(), "Prefer": "return=minimal"},
            json={"estado": estado, "filas_upserted": filas,
                  "mensaje": (mensaje or "")[:500] or None,
                  "fin": datetime.now(timezone.utc).isoformat()},
            timeout=10,
        )
    except Exception:
        pass


# ── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30,
                    help="Cuantos dias hacia atras revisar OTs (default 30)")
    ap.add_argument("--dry-run", action="store_true",
                    help="No escribe a Supabase, solo reporta")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap.parse_args()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args()
    log_id = log_start("sync_estaciones_from_ots") if not args.dry_run else None

    try:
        print(f"[1/4] Cargando EDS existentes desde Supabase...")
        existentes = get_eds_existentes()
        print(f"      {len(existentes)} EDS ya registradas.")

        print(f"[2/4] Descargando OTs de Fracttal (ultimos {args.dias} dias)...")
        wos = fetch_wos_recientes(args.dias, verbose=args.verbose)
        print(f"      {len(wos)} OTs consultadas.")

        print(f"[3/4] Extrayendo EDS unicas...")
        # Agrupar por groups_2_description; guardar el primer WO para completar metadata
        eds_map: dict[str, dict] = {}
        descartadas: set[str] = set()
        for wo in wos:
            eds = wo.get("groups_2_description")
            if not eds or eds in ("None", "null", "?"):
                continue
            eds = str(eds).strip()
            if not _es_eds_valida(eds):
                descartadas.add(eds)
                continue
            if eds in eds_map:
                continue
            eds_map[eds] = {
                "eds_occim":  eds,
                "cliente":    _norm_cliente(wo.get("groups_1_description")),
                "nombre":     _extraer_nombre(wo.get("parent_description")),
                "direccion":  None,  # Fracttal no lo trae limpio en WO
                "comuna":     _extraer_comuna_pista(wo.get("items_log_description")),
                "region":     None,
                "zona":       None,
                "activa":     True,
                "zona_sla":   None,   # Requiere completar manualmente
                "loc_fracttal":       None,
                "cod_occim_fracttal": None,
                "barcode_cliente":    None,
            }
        print(f"      {len(eds_map)} EDS distintas encontradas (validas).")
        if descartadas:
            print(f"      {len(descartadas)} codigos descartados (no matchean patron EDS): "
                  f"{sorted(descartadas)[:10]}{'...' if len(descartadas)>10 else ''}")

        # Identificar NUEVAS
        nuevas = {k: v for k, v in eds_map.items() if k not in existentes}
        print(f"[4/4] {len(nuevas)} EDS NUEVAS (no estaban en Supabase).")

        if not nuevas:
            print("       Todo al día — no hay EDS nuevas.")
            log_end(log_id, "success", 0, "sin nuevas")
            return 0

        print("\n=== EDS NUEVAS DETECTADAS ===")
        for eds, row in sorted(nuevas.items()):
            print(f"  + {eds:<12} | cliente={row['cliente']!s:<15} | "
                  f"nombre={row['nombre']!s:<25} | comuna_pista={row['comuna']}")

        if args.dry_run:
            print(f"\n[DRY-RUN] No se escribio a Supabase.")
            return 0

        n = insertar_estaciones(list(nuevas.values()))
        print(f"\n[OK] {n} EDS nuevas insertadas en estaciones_servicio.")
        print("     Recordatorio: completar manualmente 'comuna', 'region', 'zona_sla'")
        print("     y 'direccion' para las EDS nuevas — el sync solo trae datos basicos.")
        log_end(log_id, "success", n, f"{n} nuevas de {len(eds_map)} totales")
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\nERROR: {e}\n{tb}", file=sys.stderr)
        log_end(log_id, "error", 0, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
