"""
setup_asignaciones_vehiculo.py — Crea y siembra la tabla de asignaciones
patente <-> tecnico con VIGENCIA POR FECHAS.
=========================================================================

Problema que resuelve:
  Los tecnicos se intercambian las camionetas. Si asumimos que la patente
  X siempre pertenece a la persona Y, atribuimos mal los KM/viajes GPS.
  Esta tabla registra "de tal fecha a tal fecha, la patente P la ocupo el
  tecnico T", permitiendo que el dashboard mire la fecha del viaje y sepa
  quien la conducia realmente ese dia.

Uso:
  python setup_asignaciones_vehiculo.py --print-sql   # imprime el DDL para
                                                       # correr en Supabase SQL editor
  python setup_asignaciones_vehiculo.py --seed        # siembra asignaciones
                                                       # actuales desde tecnicos_hhee
"""
import argparse
import os
import sys
import requests


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

# ── DDL de la tabla ──────────────────────────────────────────────────────
SQL = """
-- Tabla de asignaciones patente <-> tecnico con vigencia por fechas.
CREATE TABLE IF NOT EXISTS asignaciones_vehiculo (
    id             BIGSERIAL PRIMARY KEY,
    patente        TEXT NOT NULL,
    rut            TEXT NOT NULL,
    nombre_tecnico TEXT NOT NULL,
    equipo         TEXT,
    fecha_desde    DATE NOT NULL,
    fecha_hasta    DATE,               -- NULL = asignacion vigente (actual)
    nota           TEXT,
    creado_por     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indices para busqueda por patente y por rango de fechas
CREATE INDEX IF NOT EXISTS idx_asigveh_patente ON asignaciones_vehiculo (patente);
CREATE INDEX IF NOT EXISTS idx_asigveh_fechas  ON asignaciones_vehiculo (patente, fecha_desde, fecha_hasta);

GRANT SELECT, INSERT, UPDATE, DELETE ON asignaciones_vehiculo TO anon, service_role, authenticated;
GRANT USAGE, SELECT ON SEQUENCE asignaciones_vehiculo_id_seq TO anon, service_role, authenticated;
"""


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def seed():
    """Siembra una asignacion vigente por cada tecnico desde tecnicos_hhee.
    fecha_desde = 2026-01-01, fecha_hasta = NULL (vigente).
    El encargado luego corrige los periodos historicos reales."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Faltan credenciales SUPABASE_URL / SUPABASE_KEY")
        sys.exit(1)
    # Verificar que la tabla exista
    r = requests.get(f"{SUPABASE_URL}/rest/v1/asignaciones_vehiculo?select=id&limit=1",
                     headers=_headers(), timeout=15)
    if r.status_code != 200:
        print(f"La tabla asignaciones_vehiculo no existe aun (HTTP {r.status_code}).")
        print("Corre primero el SQL:  python setup_asignaciones_vehiculo.py --print-sql")
        sys.exit(1)
    # Si ya hay datos, no re-sembrar
    existing = r.json()
    if existing:
        print(f"La tabla ya tiene datos ({len(existing)}+ filas). Seed omitido.")
        return
    # Traer tecnicos con patente
    rt = requests.get(f"{SUPABASE_URL}/rest/v1/tecnicos_hhee"
                      f"?select=rut,nombre_completo,patente,equipo"
                      f"&patente=not.is.null",
                      headers=_headers(), timeout=20)
    tecs = rt.json()
    filas = []
    for t in tecs:
        pat = str(t.get("patente") or "").strip()
        if not pat:
            continue
        filas.append({
            "patente": pat,
            "rut": str(t.get("rut") or "").strip(),
            "nombre_tecnico": str(t.get("nombre_completo") or "").strip(),
            "equipo": t.get("equipo"),
            "fecha_desde": "2026-01-01",
            "fecha_hasta": None,
            "nota": "Asignacion inicial (seed automatico desde tecnicos_hhee). "
                    "Corregir periodos historicos segun corresponda.",
            "creado_por": "seed",
        })
    if not filas:
        print("Sin tecnicos con patente para sembrar.")
        return
    ins = requests.post(f"{SUPABASE_URL}/rest/v1/asignaciones_vehiculo",
                        headers={**_headers(), "Prefer": "return=minimal"},
                        json=filas, timeout=30)
    if ins.status_code in (200, 201, 204):
        print(f"OK — sembradas {len(filas)} asignaciones vigentes.")
    else:
        print(f"Error al sembrar: HTTP {ins.status_code} — {ins.text[:300]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--print-sql", action="store_true", help="Imprime el DDL para Supabase SQL editor")
    ap.add_argument("--seed", action="store_true", help="Siembra asignaciones actuales")
    args = ap.parse_args()
    if args.print_sql:
        print(SQL)
    elif args.seed:
        seed()
    else:
        ap.print_help()
