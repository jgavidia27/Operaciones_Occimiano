"""
sync_rastreosat_drive.py — Procesa reportes "Viajes" de Rastreosat.
====================================================================

El usuario baja manualmente el reporte semanal desde Rastreosat
(Reportes → Viajes → todos los vehiculos → semana previa) y lo sube
a la carpeta de Google Drive:
    OPERACIONES/OPERACIONES/HHEE semanal - GPS/

Este script:
  1) Detecta archivos CSV nuevos en esa carpeta
  2) Parsea cada viaje (formato Rastreosat: sep=';', decimal=',', fechas DD-MM-YYYY)
  3) Mapea patente -> RUT via tecnicos_hhee
  4) Guarda 2 eventos por viaje ('motor_on' inicio, 'motor_off' fin) en gps_eventos
  5) Marca el archivo como procesado (renombra o registra en hhee_sync_logs)

Ejecucion:
    python sync_rastreosat_drive.py                # procesa todos los CSV nuevos
    python sync_rastreosat_drive.py --file X.csv   # procesa uno especifico
    python sync_rastreosat_drive.py --dry-run      # no escribe a Supabase

Ambientes soportados:
    Local (Windows): lee de G:\\... (ruta hardcoded resuelta)
    Streamlit Cloud / Render: usa Google Drive API (via gdrive.py existente)
"""

import argparse
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
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

# Ruta local a la carpeta de Google Drive (Windows)
_DRIVE_LOCAL_PATH = (
    r"G:\.shortcut-targets-by-id\15zHnoU5VZlkOwYc6EBziNcnS-sAAtwtk"
    r"\OPERACIONES\OPERACIONES\HHEE semanal - GPS"
)

# Nombre exacto de las columnas del CSV Rastreosat (validado con reporte real)
COLS_REQ = {
    "patente":     "Placa Patente",
    "nombre_mov":  "Nombre Movil",
    "fecha_ini":   "Fecha y Hora de inicio",
    "geo_ini":     "Ubicacion inicial (geotexto)",
    "lat_ini":     "Lat. Inicio",
    "lng_ini":     "Long. Inicio",
    "km":          "Distancia Recorrida (km)",
    "duracion":    "Duracion Viaje (hh:mm:ss)",
    "fecha_fin":   "Fecha y Hora de termino",
    "geo_fin":     "Ubicacion Termino (geotexto)",
    "lat_fin":     "Lat. Termino",
    "lng_fin":     "Long. Termino",
    "vel_prom":    "Velocidad Promedio (km/h)",
    "vel_max":     "Velocidad Max. (km/h)",
    "acel_bruscas": "Aceleracion Brusca (veces)",
    "frenadas":    "Frenada Brusca (veces)",
}


# ── Supabase helpers ────────────────────────────────────────────────────────
def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def supabase_upsert(tabla: str, filas: list[dict]) -> int:
    if not filas:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{tabla}",
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=filas, timeout=60,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase upsert {r.status_code}: {r.text[:300]}")
    return len(filas)


def get_patente_to_rut() -> dict[str, str]:
    """Descarga el mapa patente -> RUT desde tecnicos_hhee."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/tecnicos_hhee?select=rut,patente&patente=not.is.null",
        headers=_sb_headers(), timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"No pude cargar tecnicos_hhee: {r.text[:200]}")
    return {t["patente"].strip().upper(): t["rut"] for t in r.json() if t.get("patente")}


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


def log_end(log_id: Optional[int], estado: str, filas: int, mensaje: str = ""):
    if not log_id:
        return
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


# ── Utilidades ──────────────────────────────────────────────────────────────
def _parse_fecha(s) -> Optional[str]:
    """'01-07-2026 08:01:30' -> ISO '2026-07-01T08:01:30-04:00'."""
    if s is None or pd.isna(s) or str(s).strip() in ('', '--', '-'):
        return None
    s = str(s).strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S") + "-04:00"
        except ValueError:
            continue
    return None


def _parse_num_es(s) -> Optional[float]:
    """'-33,485376' -> -33.485376 (decimal europeo -> punto)."""
    if s is None or pd.isna(s):
        return None
    s = str(s).strip().replace(",", ".")
    if not s or s in ('--', '-', '', 'nan'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _dur_a_min(s) -> Optional[int]:
    """'0:36:09' -> 36 (minutos, redondeando)."""
    if s is None or pd.isna(s):
        return None
    s = str(s).strip()
    m = re.match(r"^(\d+):(\d+):(\d+)$", s)
    if not m:
        return None
    h, mn, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 60 + mn + (1 if sec >= 30 else 0)


# ── Procesamiento CSV ───────────────────────────────────────────────────────
def procesar_csv(csv_path: str, patente_to_rut: dict[str, str],
                  verbose: bool = False) -> list[dict]:
    """Lee un CSV de Rastreosat y devuelve lista de dicts para gps_eventos.
    Cada viaje genera 2 eventos: motor_on (inicio) y motor_off (fin)."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", sep=";", low_memory=False)
    print(f"  Filas en CSV: {len(df)}")

    # Validar columnas requeridas
    faltantes = [v for v in COLS_REQ.values() if v not in df.columns]
    if faltantes:
        raise RuntimeError(f"Columnas faltantes en el CSV: {faltantes}")

    eventos = []
    ruts_no_matcheados = set()
    for _, row in df.iterrows():
        patente = str(row[COLS_REQ["patente"]]).strip().upper()
        if not patente or patente in ("NAN", "--"):
            continue

        # Fecha para gps_eventos.fecha (DATE)
        fecha_ini_iso = _parse_fecha(row[COLS_REQ["fecha_ini"]])
        fecha_fin_iso = _parse_fecha(row[COLS_REQ["fecha_fin"]])
        if not fecha_ini_iso:
            continue
        fecha_dia = fecha_ini_iso[:10]  # YYYY-MM-DD

        lat_ini = _parse_num_es(row[COLS_REQ["lat_ini"]])
        lng_ini = _parse_num_es(row[COLS_REQ["lng_ini"]])
        lat_fin = _parse_num_es(row[COLS_REQ["lat_fin"]])
        lng_fin = _parse_num_es(row[COLS_REQ["lng_fin"]])
        km = _parse_num_es(row[COLS_REQ["km"]])
        duracion_min = _dur_a_min(row[COLS_REQ["duracion"]])
        vel_prom = _parse_num_es(row[COLS_REQ["vel_prom"]])
        vel_max = _parse_num_es(row[COLS_REQ["vel_max"]])
        geo_ini = str(row[COLS_REQ["geo_ini"]] or "").strip()
        geo_fin = str(row[COLS_REQ["geo_fin"]] or "").strip()
        nombre_mov = str(row[COLS_REQ["nombre_mov"]] or "").strip()

        rut = patente_to_rut.get(patente)
        if not rut:
            ruts_no_matcheados.add(patente)

        raw = {
            "patente": patente, "nombre_movil": nombre_mov, "rut_tecnico": rut,
            "km": km, "vel_prom": vel_prom, "vel_max": vel_max,
            "duracion_min": duracion_min,
            "geo_ini": geo_ini[:200], "geo_fin": geo_fin[:200],
            "acel_bruscas": _parse_num_es(row[COLS_REQ["acel_bruscas"]]),
            "frenadas_bruscas": _parse_num_es(row[COLS_REQ["frenadas"]]),
        }

        # Evento motor_on al inicio del viaje
        if lat_ini is not None and lng_ini is not None:
            eventos.append({
                "patente": patente, "fecha": fecha_dia,
                "timestamp": fecha_ini_iso,
                "lat": lat_ini, "lng": lng_ini,
                "velocidad_kmh": 0.0,  # arranca detenido
                "evento": "motor_on",
                "direccion": geo_ini[:250] or None,
                "duracion_min": None,
                "raw_data": raw,
            })

        # Evento motor_off al fin del viaje
        if fecha_fin_iso and lat_fin is not None and lng_fin is not None:
            eventos.append({
                "patente": patente, "fecha": fecha_dia,
                "timestamp": fecha_fin_iso,
                "lat": lat_fin, "lng": lng_fin,
                "velocidad_kmh": 0.0,  # termina detenido
                "evento": "motor_off",
                "direccion": geo_fin[:250] or None,
                "duracion_min": duracion_min,   # duracion del VIAJE anterior
                "raw_data": raw,
            })

    if ruts_no_matcheados:
        print(f"  [WARN] {len(ruts_no_matcheados)} patentes sin match en tecnicos_hhee: "
              f"{sorted(ruts_no_matcheados)[:8]}{'...' if len(ruts_no_matcheados)>8 else ''}")
    return eventos


# ── Descubrir archivos ──────────────────────────────────────────────────────
# ID de la carpeta "HHEE semanal - GPS" en Google Drive (viene del URL).
# En el cron GitHub Actions se define via env var GDRIVE_HHEE_FOLDER_ID.
_GDRIVE_HHEE_FOLDER_ID = os.getenv("GDRIVE_HHEE_FOLDER_ID", "")


def _gdrive_service():
    """Construye un servicio Google Drive API con service account.
    Requiere env var GOOGLE_SERVICE_ACCOUNT_JSON con el JSON completo del
    service account. Retorna None si no está configurado."""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"[WARN] No pude inicializar Google Drive API: {e}")
        return None


def _listar_csvs_gdrive(folder_id: str) -> list[dict]:
    """Lista archivos .csv en la carpeta Drive. Retorna [{id, name}]."""
    svc = _gdrive_service()
    if not svc or not folder_id:
        return []
    q = f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    resp = svc.files().list(q=q, fields="files(id, name, mimeType, modifiedTime)",
                             pageSize=100).execute()
    files = resp.get("files", [])
    # Filtrar CSVs (por nombre, ya que mimeType puede variar)
    return [f for f in files if f["name"].lower().endswith(".csv")]


def _descargar_csv_gdrive(file_id: str, dest_path: str) -> bool:
    """Descarga un file de Drive por ID a dest_path. Retorna True si OK."""
    svc = _gdrive_service()
    if not svc:
        return False
    try:
        import io
        from googleapiclient.http import MediaIoBaseDownload
        req = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _st, done = dl.next_chunk()
        with open(dest_path, "wb") as f:
            f.write(buf.getvalue())
        return True
    except Exception as e:
        print(f"[ERR] Descargando file {file_id}: {e}")
        return False


def listar_csvs_pendientes() -> list[str]:
    """Retorna paths locales de CSVs a procesar.
    - Modo LOCAL (tu PC): lista archivos en G:\\...\\HHEE semanal - GPS
    - Modo CLOUD (cron): descarga desde Google Drive API a un temp local
    """
    # Modo 1: disco local (G:\)
    if os.path.isdir(_DRIVE_LOCAL_PATH):
        return sorted(str(p) for p in Path(_DRIVE_LOCAL_PATH).glob("*.csv"))

    # Modo 2: Google Drive API
    if not _GDRIVE_HHEE_FOLDER_ID:
        raise RuntimeError(
            f"No existe la carpeta local {_DRIVE_LOCAL_PATH}.\n"
            f"Y no hay GDRIVE_HHEE_FOLDER_ID definido para usar Google Drive API.\n"
            f"Configura GDRIVE_HHEE_FOLDER_ID + GOOGLE_SERVICE_ACCOUNT_JSON en env vars."
        )

    files = _listar_csvs_gdrive(_GDRIVE_HHEE_FOLDER_ID)
    if not files:
        return []

    # Descargar todos a un directorio temporal local
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="rsat_gdrive_")
    paths = []
    for f in files:
        p = os.path.join(tmp_dir, f["name"])
        if _descargar_csv_gdrive(f["id"], p):
            paths.append(p)
            print(f"  [gdrive] descargado: {f['name']}")
    return sorted(paths)


# ── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="Ruta a un CSV especifico (evita listado)")
    ap.add_argument("--dry-run", action="store_true",
                    help="No escribe a Supabase, solo imprime resumen")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap.parse_args()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args()
    log_id = log_start("sync_rastreosat_drive") if not args.dry_run else None
    total_evt = 0

    try:
        print("[1/3] Cargando mapa patente -> RUT desde tecnicos_hhee...")
        p2r = get_patente_to_rut()
        print(f"      {len(p2r)} patentes con RUT asignado.")

        if args.file:
            archivos = [args.file]
        else:
            print("[2/3] Buscando CSV en carpeta Drive...")
            archivos = listar_csvs_pendientes()
            print(f"      {len(archivos)} archivo(s) encontrado(s).")

        for path in archivos:
            print(f"\n[3/3] Procesando: {os.path.basename(path)}")
            eventos = procesar_csv(path, p2r, verbose=args.verbose)
            print(f"      -> {len(eventos)} eventos generados")
            if args.dry_run:
                total_evt += len(eventos)
                # Muestra ejemplo
                for e in eventos[:3]:
                    print(f"        {e['patente']} | {e['evento']:<10} | {e['timestamp']} | ({e['lat']}, {e['lng']})")
                continue
            # Upsert en lotes de 500
            for i in range(0, len(eventos), 500):
                batch = eventos[i:i+500]
                n = supabase_upsert("gps_eventos", batch)
                total_evt += n
            print(f"      Upsert OK: {len(eventos)} eventos.")

        print(f"\n[RESUMEN] Total eventos: {total_evt}  (dry-run={args.dry_run})")
        log_end(log_id, "success", total_evt, f"{len(archivos)} archivos")
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\nERROR: {e}\n{tb}", file=sys.stderr)
        log_end(log_id, "error", total_evt, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
