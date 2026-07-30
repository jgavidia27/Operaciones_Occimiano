"""
sync_planes_fracttal.py
=======================
Refleja en un Google Sheet la configuración vigente de los Planes de Tareas
de Fracttal (lo que el técnico debe completar en cada mantenimiento
preventivo), para que Operaciones lo consulte sin entrar a Fracttal y sin
tener que rellenar el desglose a mano.

Modelo de datos en Fracttal:
    groups_tasks   → "Plan de Tareas"  (ej. "10 PLAN MTTO SKID")
      └─ tasks     → Tareas del plan   (ej. "PLAN MTTO GENERAL MSELF")
           └─ tasks_subtasks → checklist configurado (acción, tipo, orden,
                               obligatorio, iteraciones)

Salida: un Google Sheet con 2 pestañas
    • "Planes"   — 1 fila por plan (nombre, #tareas, #subtareas, #activos)
    • "Desglose" — 1 fila por subtarea (Plan · Tarea · Secuencia · Acción ·
                    Registro · Obligatorio · Iteraciones)

Idempotente: reescribe siempre el Sheet completo con la foto actual. Si en
Fracttal se agrega/cambia/elimina una subtarea, al siguiente sync se refleja.

Setup (una sola vez):
    1. Crear un Google Sheet en blanco (desde operaciones@occimiano.cl).
    2. Compartirlo como EDITOR con el correo del service account (client_email
       del GOOGLE_SERVICE_ACCOUNT_JSON). Si no lo tienes, corré el script sin
       PLANES_SHEET_ID: imprime el correo exacto a compartir.
    3. Copiar el ID del Sheet (lo que va entre /d/ y /edit en la URL) al
       GitHub Secret PLANES_SHEET_ID.
    (Este es el mismo patrón que GDRIVE_HHEE_FOLDER_ID: el usuario comparte, el
     service account solo escribe. Los service accounts no pueden crear/poseer
     archivos propios en Drive — por eso NO creamos el Sheet nosotros.)

Requiere env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON   (JSON completo del service account)
    PLANES_SHEET_ID               (ID del Sheet ya creado y compartido con el SA)

Uso:
    python sync_planes_fracttal.py                 (sync normal)
    python sync_planes_fracttal.py --dry-run       (solo extrae, no escribe;
                                                     vuelca CSVs de control)
"""

import argparse
import json
import os
import sys
import unicodedata
from datetime import datetime, timezone

import requests

# ── Config Fracttal (reutiliza credenciales del proyecto) ────────────────────
FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN_URL = f"{FRACTTAL_BASE}/oauth/token"
CLIENT_ID          = os.getenv("FRACTTAL_CLIENT_ID", "KtHFO5pMskBbJ3lhPr")
CLIENT_SECRET      = os.getenv("FRACTTAL_CLIENT_SECRET", "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m")
ID_COMPANY         = 1507

# Tipos de registro (id_task_form_item_type → etiqueta legible).
# Confirmado contra la pantalla de Fracttal y el Excel de Operaciones.
TIPO_REGISTRO = {
    1: "Texto",
    2: "Cambio de repuesto",
    3: "Número",
    4: "Verificación",
    5: "Una lectura del medidor",
    7: "Selección",   # campos de lista (ej. "TIPO TERMO", "MATERIAL TERMO")
}

# Planes que interesan a Operaciones (columna "¿me interesa?" = Si).
# Se comparan por nombre normalizado contra groups_tasks.description.
PLANES_INTERES = [
    "10 Plan Mtto Skid",
    "4 Plan Mtto Lavado Shell",
    "4 Plan Mtto Mself Con Mousse Y Lavallantas",
    "4 Plan Mtto Mself Con Mousse Y Sanitizado",
    "4 Plan Mtto Mself Con Sanitizado",
    "4 Plan Mtto Mself General",
    "4 Plan Mtto Mself Mousse 6 Progr",
    "4 Plan Mtto Mself Vapor Mousse Bp",
    "4 Plan Mtto Smart 2 Pro",
    "5 Plan De Mtto Aspirado Shell",
    "5 Plan Mtto Twister M2 And Dry (Tambor Plástico)",
    "5 Plan Mtto Twister S2 And Dry (Tambor Metálico)",
    "5 Plan Mtto Twister S2 (Tambor Metálico Sin Soplado)",
    "6 Plan Mtto Dispensador De Paños",
    "6 Plan Mtto Dispensador Renovador Y Perfume",
    "6 Plan Mtto Equipo De Secado",
    "6 Plan Mtto Lavatapette",
    "7 Plan Mtto Lavainterior Multifunción",
    "7 Plan Mtto Lavatapiz",
    "9 Plan Mtto Lavabike",
    "Plan Mantenimiento Mself Breve",
    "Plan Mtto Pf2",
    "Plan Mtto Pf2 Pro",
    "Plan Mtto Pulse 2S Pro",
]


def log(msg, lvl="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "    ", "OK": "[OK]", "WARN": "[!] ", "ERR": "[X] ", "PROG": "--> "}
    print(f"[{ts}] {tags.get(lvl,'    ')} {msg}")


def _norm(s: str) -> str:
    s = ''.join(c for c in unicodedata.normalize('NFD', str(s or ''))
                if unicodedata.category(c) != 'Mn')
    return ' '.join(s.upper().split())


# ── Fracttal ─────────────────────────────────────────────────────────────────
_token = {"v": None}

def get_token() -> str:
    if _token["v"]:
        return _token["v"]
    r = requests.post(FRACTTAL_TOKEN_URL,
                      data={"grant_type": "client_credentials",
                            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
                      timeout=20)
    r.raise_for_status()
    _token["v"] = r.json()["access_token"]
    return _token["v"]


def _fx_get(path: str, params: dict) -> list:
    h = {"Authorization": f"Bearer {get_token()}"}
    r = requests.get(f"{FRACTTAL_BASE}/api/{path}", headers=h, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("data", []) or []


def fetch_grupos() -> dict:
    """Devuelve {nombre_normalizado: grupo_dict} de todos los groups_tasks."""
    data = _fx_get("groups_tasks", {"limit": 200})
    return {_norm(g.get("description")): g for g in data}


def fetch_tareas(id_group: int) -> list:
    return _fx_get("tasks", {"id_group_task": id_group, "limit": 100})


def fetch_subtareas(id_task: int) -> list:
    subs = _fx_get("tasks_subtasks", {"id_task": id_task, "limit": 500})
    return sorted(subs, key=lambda x: (x.get("order_number") or 0))


def _iter_label(it) -> str:
    """Normaliza el campo iterations (dict/str/None) a texto legible."""
    if not it:
        return ""
    if isinstance(it, dict):
        if not it:
            return ""
        return json.dumps(it, ensure_ascii=False)
    return str(it)


def extraer_planes() -> tuple:
    """Recorre los planes de interés y arma (planes_rows, desglose_rows)."""
    grupos = fetch_grupos()
    planes_rows, desglose_rows = [], []
    faltantes = []

    for nombre in PLANES_INTERES:
        g = grupos.get(_norm(nombre))
        if not g:
            faltantes.append(nombre)
            continue
        gid = g["id"]
        tareas = fetch_tareas(gid)
        n_subs_plan = 0
        for t in tareas:
            tid = t["id"]
            subs = fetch_subtareas(tid)
            n_subs_plan += len(subs)
            for s in subs:
                tipo = TIPO_REGISTRO.get(s.get("id_task_form_item_type"),
                                         f"Tipo {s.get('id_task_form_item_type')}")
                desglose_rows.append({
                    "Plan":        g.get("description", "").strip(),
                    "Tarea":       (t.get("description") or "").strip(),
                    "Secuencia":   s.get("order_number"),
                    "Acción":      (s.get("description") or "").strip(),
                    "Registro":    tipo,
                    "Obligatorio": "Sí" if s.get("is_required") else "No",
                    "Iteraciones": _iter_label(s.get("iterations")),
                })
        planes_rows.append({
            "Plan":       g.get("description", "").strip(),
            "N° Tareas":  len(tareas),
            "N° Subtareas": n_subs_plan,
            "N° Activos": g.get("assets_number"),
            "ID Grupo":   gid,
        })
        log(f"{g.get('description','')[:45]:45} | tareas={len(tareas)} subtareas={n_subs_plan}", "PROG")

    if faltantes:
        log(f"Planes sin match en Fracttal ({len(faltantes)}): {faltantes}", "WARN")
    return planes_rows, desglose_rows


# ── Google Sheets ────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

def _google_services():
    """Devuelve (sheets_service, drive_service) o (None, None)."""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        log("Falta GOOGLE_SERVICE_ACCOUNT_JSON — no puedo escribir el Sheet.", "ERR")
        return None, None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive  = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive


def _sa_email() -> str:
    """Devuelve el client_email del service account (para compartir el Sheet)."""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return ""
    try:
        return json.loads(sa_json).get("client_email", "")
    except Exception:
        return ""


def _rows_to_values(rows: list, cols: list) -> list:
    """Convierte lista de dicts a matriz [encabezado] + filas para Sheets."""
    out = [cols]
    for r in rows:
        out.append([("" if r.get(c) is None else r.get(c)) for c in cols])
    return out


def escribir_sheet(sheets, sid: str, planes_rows: list, desglose_rows: list):
    """Reescribe las 2 pestañas con la foto actual (clear + update)."""
    # Asegurar que existen ambas pestañas
    meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
    existentes = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
    reqs = []
    for title in ("Planes", "Desglose"):
        if title not in existentes:
            reqs.append({"addSheet": {"properties": {"title": title}}})
    if reqs:
        sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()

    sello = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    planes_cols   = ["Plan", "N° Tareas", "N° Subtareas", "N° Activos", "ID Grupo"]
    desglose_cols = ["Plan", "Tarea", "Secuencia", "Acción", "Registro", "Obligatorio", "Iteraciones"]

    planes_vals = _rows_to_values(planes_rows, planes_cols)
    planes_vals.append([])
    planes_vals.append([f"Última actualización: {sello} (hora Chile) — fuente: Fracttal One"])

    desglose_vals = _rows_to_values(desglose_rows, desglose_cols)

    for title, vals in (("Planes", planes_vals), ("Desglose", desglose_vals)):
        sheets.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"'{title}'!A1:Z10000").execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{title}'!A1",
            valueInputOption="RAW", body={"values": vals}).execute()
    log(f"Sheet actualizado: {len(planes_rows)} planes, {len(desglose_rows)} subtareas.", "OK")


def _dump_csv(planes_rows, desglose_rows):
    import csv
    with open("_planes_desglose.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["Plan","Tarea","Secuencia","Acción","Registro","Obligatorio","Iteraciones"])
        w.writeheader(); w.writerows(desglose_rows)
    with open("_planes_resumen.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["Plan","N° Tareas","N° Subtareas","N° Activos","ID Grupo"])
        w.writeheader(); w.writerows(planes_rows)
    log("Volcados _planes_resumen.csv y _planes_desglose.csv", "OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo extrae de Fracttal y vuelca CSV; no escribe el Sheet.")
    args = ap.parse_args()

    log(f"Extrayendo {len(PLANES_INTERES)} planes de interés desde Fracttal...")
    planes_rows, desglose_rows = extraer_planes()
    log(f"Extraídos: {len(planes_rows)} planes | {len(desglose_rows)} subtareas", "OK")

    if args.dry_run:
        _dump_csv(planes_rows, desglose_rows)
        return

    sid = os.getenv("PLANES_SHEET_ID", "").strip()
    if not sid:
        # Setup pendiente: el usuario debe crear el Sheet y compartirlo con el SA.
        # (Los service accounts no pueden crear/poseer archivos propios en Drive.)
        email = _sa_email()
        print("\n" + "=" * 72)
        log("Falta PLANES_SHEET_ID. Setup en 3 pasos:", "WARN")
        log("  1) Crea un Google Sheet en blanco (desde operaciones@occimiano.cl).", "WARN")
        log(f"  2) Compártelo como EDITOR con el service account:", "WARN")
        log(f"       {email or '(no pude leer client_email del JSON)'}", "WARN")
        log("  3) Copia el ID del Sheet (lo que va entre /d/ y /edit en la URL)", "WARN")
        log("     al GitHub Secret 'PLANES_SHEET_ID' y vuelve a correr el workflow.", "WARN")
        log("  (Requisito previo: habilitar la Google Sheets API en el proyecto GCP", "WARN")
        log("   del service account — ver instrucciones del asistente.)", "WARN")
        print("=" * 72)
        _dump_csv(planes_rows, desglose_rows)
        sys.exit(1)

    sheets, drive = _google_services()
    if not sheets:
        log("Sin credenciales Google — vuelco CSV como respaldo.", "WARN")
        _dump_csv(planes_rows, desglose_rows)
        sys.exit(1)

    escribir_sheet(sheets, sid, planes_rows, desglose_rows)
    log(f"URL: https://docs.google.com/spreadsheets/d/{sid}/edit", "OK")


if __name__ == "__main__":
    main()
