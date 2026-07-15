"""
sync_buk_rrhh.py — Sincroniza nómina de Buk RRHH → tabla `tecnicos_hhee` en Supabase.
=====================================================================================

- Descarga TODOS los empleados de https://occimiano.buk.cl/api/v1/employees
  (paginado). Filtra los que son técnicos (mecánico, senior técnico,
  representante terreno) o que aparecen en el mapa manual de patentes.
- Adjunta la patente de cada técnico usando PATENTES_STO (mapa hardcodeado
  de la imagen "Patentes STO" enviada por operaciones).
- Adjunta el equipo (Juan Gallardo / Luis Pinto / Victor Bahamonde /
  Carlos Avila Norte / Carlos Avila Sur) a partir de mobile_auth.USERS.
- Upsert por RUT a `tecnicos_hhee`.

Ejecución local:
    python sync_buk_rrhh.py

Variables de entorno requeridas (via .env o Streamlit Secrets):
    BUK_API_TOKEN   # token de la API Buk RRHH (ya funciona)
    SUPABASE_URL
    SUPABASE_KEY
"""

import os
import sys
import time
import traceback
import unicodedata
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


BUK_TOKEN    = os.getenv("BUK_API_TOKEN", "")
BUK_BASE     = "https://occimiano.buk.cl/api/v1"
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")


# ── Mapa de patentes STO ────────────────────────────────────────────────────
# Fuente: imagen "Patentes STO" del 14-07-2026. Se matchea por nombre
# normalizado (sin acentos, mayúsculas) contra la nómina Buk.
PATENTES_STO: dict[str, str] = {
    # Nombre-key (normalizado) → patente
    "BREYANS TOLEDO":           "PSSX84",
    "CARLOS AVILA":             "SRRX29",
    "EDISON CARRASCO":          "VLZV44",
    "EDSON PEREZ":              "SRRX31",
    "ERWIN RIVERA":             "VDYY75",
    "GASTON FULLER":            "SKTF86",
    "IGNACIO FERRARI":          "PSSX82",   # Iván Ignacio Ferrari Vergara (Buk) = Ignacio Ferrari
    "JAVIER HEIN":              "RRRW21",
    "MARTIN FLORES":            "LWLF91",   # antes VCXZ49; ahora usa LWLF91 (ex Operaciones)
    "VICTOR BAHAMONDE":         "RRRW20",
    "JORGE RODRIGUEZ":          "VDBZ48",
    "JUAN FRANCISCO TORO":      "SYGK68",
    "JUAN GALLARDO":            "PKKH65",
    "LUIS LOPEZ":               "TFCR44",
    "LUIS PINTO":               "RRRW16",
}

# Vehículos que no corresponden a ningún técnico activo
VEHICULOS_AUXILIARES: dict[str, str] = {
    "SRRX18": "Vehículo Auxiliar (ex Eduardo Toro)",
    "STKX93": "Camión Occimiano",
    "VCXZ49": "Vehículo Auxiliar - Eléctrico",  # ex Martín Flores, ahora auxiliar
}

# Mapa email → equipo (fuente: mobile_auth.py USERS)
EMAIL_TO_EQUIPO: dict[str, str] = {
    "jgallardo@occimiano.cl":  "Juan Gallardo",
    "jhein@occimiano.cl":      "Juan Gallardo",
    "ecarrasco@occimiano.cl":  "Juan Gallardo",
    "ivergara@occimiano.cl":   "Juan Gallardo",
    "lpinto@occimiano.cl":     "Luis Pinto",
    "jtoro@occimiano.cl":      "Luis Pinto",
    "jrodriguez@occimiano.cl": "Luis Pinto",
    "btoledo@occimiano.cl":    "Luis Pinto",
    "vbahamonde@occimiano.cl": "Victor Bahamonde",
    "mflores@occimiano.cl":    "Victor Bahamonde",
    "cavila@occimiano.cl":     "Carlos Avila Norte",
    "eperez@occimiano.cl":     "Carlos Avila Norte",
    "erivera@occimiano.cl":    "Carlos Avila Norte",
    "llopez@occimiano.cl":     "Carlos Avila Sur",
    "gfuller@occimiano.cl":    "Carlos Avila Sur",
}


def _norm(s: str) -> str:
    """Normaliza texto: sin acentos, mayúsculas, espacios colapsados."""
    if not s:
        return ""
    s = ''.join(c for c in unicodedata.normalize('NFD', s)
                if unicodedata.category(c) != 'Mn')
    return ' '.join(s.upper().split())


def _match_patente(nombre_completo: str) -> str | None:
    """Retorna la patente asociada al técnico, o None si no matchea."""
    n = _norm(nombre_completo)
    words = set(n.split())
    for key, patente in PATENTES_STO.items():
        key_words = set(key.split())
        # Match si TODAS las palabras del key están en el nombre.
        # Ej: "IGNACIO FERRARI" ⊂ "IVAN IGNACIO FERRARI VERGARA" → OK
        if key_words.issubset(words):
            return patente
    return None


# ── Cliente Buk RRHH ────────────────────────────────────────────────────────
def buk_fetch_all_employees() -> list[dict]:
    """Descarga TODOS los empleados de Buk RRHH (paginado)."""
    if not BUK_TOKEN:
        raise RuntimeError("Falta BUK_API_TOKEN en .env o Streamlit Secrets")
    all_emp: list[dict] = []
    url = f"{BUK_BASE}/employees?limit=100"
    while url:
        r = requests.get(url, headers={"auth_token": BUK_TOKEN}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Buk API {r.status_code}: {r.text[:200]}")
        j = r.json()
        all_emp.extend(j.get("data", []))
        url = j.get("pagination", {}).get("next")
        if url:
            time.sleep(0.3)  # cortesía anti-rate-limit
    return all_emp


# ── Cliente Supabase ────────────────────────────────────────────────────────
def supabase_upsert(tabla: str, filas: list[dict]) -> int:
    """Upsert por PK (Supabase lo maneja via Prefer merge-duplicates). Devuelve # filas escritas."""
    if not filas:
        return 0
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Faltan SUPABASE_URL / SUPABASE_KEY")
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{tabla}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates,return=minimal",
        },
        json=filas,
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase upsert {r.status_code}: {r.text[:300]}")
    return len(filas)


def supabase_log_start(script: str) -> int | None:
    """Inserta una fila 'running' en hhee_sync_logs, devuelve el id."""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=representation",
            },
            json={"script": script, "estado": "running"},
            timeout=15,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0].get("id")
    except Exception:
        pass
    return None


def supabase_log_end(log_id: int | None, estado: str, filas: int, mensaje: str = ""):
    if not log_id:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs?id=eq.{log_id}",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json={
                "estado":         estado,
                "filas_upserted": filas,
                "mensaje":        mensaje[:500] if mensaje else None,
                "fin":            datetime.now(timezone.utc).isoformat(),
            },
            timeout=15,
        )
    except Exception:
        pass


# ── Transformación Buk → tecnicos_hhee ──────────────────────────────────────
def _rut_upper_dv(rut: str) -> str:
    """Asegura que el DV (ultimo caracter tras '-') este en mayuscula.
    Necesario para matching con marcaciones (que usan mayuscula)."""
    if not rut or "-" not in rut:
        return rut
    return rut[:rut.rfind("-")+1] + rut[rut.rfind("-")+1:].upper()


def buk_to_tecnico_row(emp: dict) -> dict | None:
    """Convierte un empleado Buk en fila para tecnicos_hhee.
    Retorna None si NO es técnico ni vehículo asignable."""
    rut = _rut_upper_dv(emp.get("rut") or "")
    if not rut:
        return None
    nombre = emp.get("full_name") or ""
    email = (emp.get("email") or "").strip().lower()

    patente = _match_patente(nombre)

    # Incluir si es técnico conocido O si su email pertenece a un equipo STO
    equipo = EMAIL_TO_EQUIPO.get(email)
    if not patente and not equipo:
        return None  # empleado admin/gerencia — no evaluamos HHEE

    # Dirección
    direccion = (emp.get("address") or "").strip() or None
    comuna    = (emp.get("district") or emp.get("commune") or "").strip() or None
    region    = (emp.get("region") or "").strip() or None

    return {
        "rut":                 rut,
        "nombre_completo":     nombre,
        "email":               email or None,
        "equipo":              equipo,
        "patente":             patente,
        "tipo_vehiculo":       "staff" if patente else None,
        "domicilio_direccion": direccion,
        "domicilio_comuna":    comuna,
        "domicilio_region":    region,
        "activo":              str(emp.get("status", "")).lower() == "activo",
        "excluir_hhee":        False,
    }


def vehiculos_auxiliares_rows() -> list[dict]:
    """Filas ficticias para los vehículos sin técnico asignado."""
    filas = []
    for pat, desc in VEHICULOS_AUXILIARES.items():
        filas.append({
            "rut":                 f"AUX-{pat}",   # RUT ficticio para PK
            "nombre_completo":     desc,
            "email":               None,
            "equipo":              None,
            "patente":             pat,
            "tipo_vehiculo":       "auxiliar",
            "domicilio_direccion": None,
            "domicilio_comuna":    None,
            "domicilio_region":    None,
            "activo":              True,
            "excluir_hhee":        True,
        })
    return filas


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Windows: forzar stdout a utf-8 para que no reviente con acentos
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    log_id = supabase_log_start("sync_buk_rrhh")
    try:
        print("[1/3] Descargando empleados de Buk RRHH...")
        empleados = buk_fetch_all_employees()
        print(f"  {len(empleados)} empleados totales.")

        print("[2/3] Filtrando tecnicos + adjuntando patente/equipo...")
        filas = []
        for e in empleados:
            row = buk_to_tecnico_row(e)
            if row:
                filas.append(row)
        print(f"  {len(filas)} tecnicos matcheados.")

        # Agregar vehículos auxiliares (sin dueño)
        filas.extend(vehiculos_auxiliares_rows())
        print(f"  + {len(VEHICULOS_AUXILIARES)} vehiculos auxiliares.")

        print(f"[3/3] Upsert {len(filas)} filas -> tecnicos_hhee...")
        n = supabase_upsert("tecnicos_hhee", filas)
        print(f"  OK: {n} filas escritas.")

        # Resumen legible
        print("\n=== RESUMEN ===")
        for f in sorted(filas, key=lambda x: (x.get("equipo") or "ZZ", x.get("nombre_completo") or "")):
            eq = f.get("equipo") or "—"
            pat = f.get("patente") or "—"
            print(f"  {pat:<8} | {eq:<20} | {f['nombre_completo'][:35]:<35} | {f['rut']}")

        supabase_log_end(log_id, "success", n, f"{len(empleados)} totales, {n} upserted")
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n❌ ERROR: {e}\n{tb}", file=sys.stderr)
        supabase_log_end(log_id, "error", 0, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
