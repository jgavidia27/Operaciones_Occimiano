"""
robot_esmax.py
==============
Robot ESMAX / Aramco
Flujo:
  1. Lee Gmail → carpeta LLAMADOS/ESMAX
  2. Parsea asunto: N° Cotalker + código EDS + equipo
  3. Consulta Metabase (sin auth) → SLA esperado
  4. Omite Preventivas (sin SLA en Metabase)
  5. Mapea SLA → P1/P2/P3/P4
  6. Busca OT Fracttal en Supabase por EDS + equipo + fecha ±3 días
  7. Actualiza prioridad_calc en Supabase
  8. Registra emails procesados en robot_esmax_log.json

Ejecución: python robot_esmax.py
Primera vez: requiere autenticación Google → abre navegador automáticamente.
"""

import os
import re
import json
import requests
from datetime import datetime, timedelta

# ─── Google Gmail API ──────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════

GMAIL_LABEL_NAME   = "LLAMADOS/ESMAX"
GMAIL_SCOPES       = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS  = "credentials_esmax.json"   # descargar de Google Cloud Console
GMAIL_TOKEN        = "token_esmax.json"          # se genera automáticamente

METABASE_URL = (
    "https://bi.cotalker.com/api/public/card"
    "/56662edd-715d-4dbe-af9a-21891f4dbb97/query/json"
)

SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)

LOG_FILE = "robot_esmax_log.json"    # IDs de emails ya procesados

# SLA en horas → prioridad (100h = P4 por contrato especial)
SLA_MAP = {24: "P1", 48: "P2", 72: "P3"}

def sla_to_priority(sla_hours):
    if sla_hours is None:
        return None
    h = int(float(sla_hours))
    return SLA_MAP.get(h, "P4")   # todo lo que no sea 24/48/72 → P4


# ══════════════════════════════════════════════════════════════
# 1. GMAIL
# ══════════════════════════════════════════════════════════════

def get_gmail_service():
    """Autenticación OAuth2. Primera vez: abre navegador. Después usa token guardado."""
    creds = None
    if os.path.exists(GMAIL_TOKEN):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS):
                raise FileNotFoundError(
                    f"No se encontró {GMAIL_CREDENTIALS}.\n"
                    "Descárgalo desde Google Cloud Console → APIs → Gmail API → Credenciales → OAuth2."
                )
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_label_id(service, label_name):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == label_name:
            return lbl["id"]
    raise ValueError(f"Label '{label_name}' no encontrado en Gmail.")


def fetch_esmax_emails(service, label_id, max_results=100):
    """Devuelve lista de mensajes en LLAMADOS/ESMAX enviados por Cotalker."""
    result = service.users().messages().list(
        userId="me",
        labelIds=[label_id],
        q="from:no-responder@cotalker.com",
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_subject(service, msg_id):
    """Obtiene solo el asunto del email (header Subject)."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    return headers.get("Subject", ""), headers.get("Date", "")


# ══════════════════════════════════════════════════════════════
# 2. PARSER DE ASUNTO
# ══════════════════════════════════════════════════════════════

# Formato: "Se ha creado la OT: 149762 - 167546 -  ee_s048 - EDS: IQUIQUE/KAMIKAZE - Hidrolavadora"
_RE_COTALKER = re.compile(r"OT:\s*(\d+)", re.IGNORECASE)
_RE_EDS      = re.compile(r"\b(ee_s\d+)\b", re.IGNORECASE)

def parse_subject(subject):
    """
    Retorna (n_cotalker: int, eds_code: str, equipment: str) o None si no parsea.
    """
    m_ot  = _RE_COTALKER.search(subject)
    m_eds = _RE_EDS.search(subject)
    if not m_ot or not m_eds:
        return None

    n_cotalker = int(m_ot.group(1))
    eds_code   = m_eds.group(1).lower()

    # Equipo: último segmento separado por " - "
    parts     = [p.strip() for p in subject.split(" - ")]
    equipment = parts[-1] if parts else ""

    return n_cotalker, eds_code, equipment


# ══════════════════════════════════════════════════════════════
# 3. METABASE (sin autenticación)
# ══════════════════════════════════════════════════════════════

def fetch_metabase_data():
    """
    Descarga todas las OTs del panel Cotalker (Metabase público).
    Retorna dict { n_cotalker (int) : fila (dict) }
    """
    print("  Descargando panel Cotalker/Metabase...")
    r = requests.get(METABASE_URL, headers={"Accept": "application/json"}, timeout=40)
    r.raise_for_status()
    data = r.json()
    index = {int(row["N° Cotalker"]): row for row in data if row.get("N° Cotalker")}
    print(f"  {len(index)} OTs cargadas ({len([r for r in data if r.get('SLA esperado')])} con SLA)")
    return index


# ══════════════════════════════════════════════════════════════
# 4. SUPABASE — buscar OT Fracttal y actualizar prioridad
# ══════════════════════════════════════════════════════════════

_SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}


def find_fracttal_ot(n_cotalker, eds_code, equipment, fecha_str, window_days=7):
    """
    Busca OT Correctiva en Supabase MATCHEANDO POR N° COTALKER en nota_tarea.

    El N° Cotalker aparece SIEMPRE al inicio de nota_tarea (formato
    '149542 - 167751 - ee_s058 - EDS: MELIPILLA - ...'), por lo que
    es un identificador único y confiable. Antes se usaba matching
    laxo por EDS+equipo+fecha, que dio ~22% de asignaciones erróneas
    (documentado en fix_aramco_priorities.py). Ahora usamos nota_tarea
    como primary key implícita.

    Retorna la fila o None. Los args eds_code/equipment/fecha_str
    quedan como fallback en caso de no encontrar por nota_tarea.
    """
    # 1) Matching primario: nota_tarea empieza con el N° Cotalker
    params = "&".join([
        "select=id_ot,nombre_activo,fecha_creacion,prioridad_calc,nota_tarea",
        f"nota_tarea=like.{n_cotalker}*",
        "tipo_tarea=eq.CORRECTIVA",
        "cliente=eq.ESMAX (Aramco)",
        "limit=5",
    ])
    url = f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?{params}"
    r = requests.get(url, headers=_SB_HEADERS, timeout=10)
    rows = r.json() if r.ok else []
    if isinstance(rows, list) and rows:
        # Confirmar que efectivamente empieza con el N° (no que lo contenga)
        for row in rows:
            nota = str(row.get("nota_tarea") or "").strip()
            m = re.match(r"^(\d{5,8})(?:\s*-|\s*$)", nota)
            if m and int(m.group(1)) == n_cotalker:
                return row

    # 2) Fallback matching laxo (por si nota_tarea aún no está poblada)
    try:
        fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d")
    except ValueError:
        fecha = datetime.utcnow()
    f_min = (fecha - timedelta(days=window_days)).strftime("%Y-%m-%dT00:00:00")
    f_max = (fecha + timedelta(days=window_days)).strftime("%Y-%m-%dT23:59:59")
    equip_kw = equipment.split()[0] if equipment else equipment
    params = "&".join([
        "select=id_ot,nombre_activo,fecha_creacion,prioridad_calc,nota_tarea",
        f"codigo_eds=eq.{eds_code}",
        "tipo_tarea=eq.CORRECTIVA",
        "cliente=eq.ESMAX (Aramco)",
        f"fecha_creacion=gte.{f_min}",
        f"fecha_creacion=lte.{f_max}",
        f"nombre_activo=ilike.%25{equip_kw}%25",
        "order=fecha_creacion.desc",
        "limit=1",
    ])
    r = requests.get(f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?{params}",
                     headers=_SB_HEADERS, timeout=10)
    rows = r.json() if r.ok else []
    if isinstance(rows, list) and rows:
        print(f"    [FALLBACK] Match por EDS+equipo+fecha (nota_tarea vacía). Verificar manualmente.")
        return rows[0]
    return None


# ARQUITECTURA IMPORTANTE (fn_proteger_prioridad_robot):
#   ordenes_trabajo tiene un trigger BEFORE UPDATE que fuerza
#   prioridad_calc = llamados_correctivos.prioridad para esa OT.
#   Actualizar ordenes_trabajo.prioridad_calc directo NO funciona
#   (el trigger lo pisa). La fuente de verdad es llamados_correctivos.
#
#   El robot escribe en llamados_correctivos, y hace un touch a
#   ordenes_trabajo para que el trigger propague prioridad_calc.

_SLA_UMBRAL = {"P1": 24, "P2": 48, "P3": 72, "P4": 100}   # Aramco: mismo umbral santiago/regiones


def update_prioridad_calc(id_ot, prioridad, n_cotalker=None):
    """
    Escribe prioridad en llamados_correctivos (fuente de verdad),
    luego toca ordenes_trabajo para que el trigger propague a prioridad_calc.
    Si no existe en llamados_correctivos, hace INSERT.
    """
    from datetime import timezone
    umbral = _SLA_UMBRAL.get(prioridad, 100)
    n_aviso = str(n_cotalker) if n_cotalker else None

    # 1) UPSERT en llamados_correctivos (fuente de verdad)
    payload = {
        "os_fracttal":   id_ot,
        "cliente":       "ESMAX (Aramco)",
        "prioridad":     prioridad,
        "umbral_horas":  umbral,
        "fuente":        "robot_esmax",
    }
    if n_aviso:
        payload["n_aviso"] = n_aviso

    # Intentar PATCH primero (más común: OT ya existe)
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/llamados_correctivos?os_fracttal=eq.{id_ot}",
        headers={**_SB_HEADERS, "Prefer": "return=minimal"},
        json={"prioridad": prioridad, "umbral_horas": umbral,
              "n_aviso": n_aviso, "fuente": "robot_esmax"},
        timeout=10,
    )
    # Si Content-Range = 0-0/0 no había fila. Insertar.
    if r.headers.get("Content-Range", "").endswith("/0"):
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/llamados_correctivos",
            headers={**_SB_HEADERS,
                     "Prefer": "return=minimal,resolution=merge-duplicates"},
            json=payload, timeout=10,
        )
    if r.status_code not in (200, 201, 204):
        return False

    # 2) Touch ordenes_trabajo para que el trigger propague prioridad_calc
    r2 = requests.patch(
        f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?id_ot=eq.{id_ot}",
        headers={**_SB_HEADERS, "Prefer": "return=minimal"},
        json={"updated_at": datetime.now(timezone.utc).isoformat(),
              "n_cotalker": int(n_cotalker) if n_cotalker else None},
        timeout=10,
    )
    return r2.status_code in (200, 204)


# ══════════════════════════════════════════════════════════════
# 5. LOG DE PROCESADOS
# ══════════════════════════════════════════════════════════════

def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed_ids": [], "processed_ots": []}


def save_log(log):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════
# 6. MAIN
# ══════════════════════════════════════════════════════════════

def main():
    print("\n" + "="*60)
    print("  ROBOT ESMAX / ARAMCO")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    # ── Cargar log de procesados ──────────────────────────────
    log = load_log()
    ya_procesados = set(log.get("processed_ids", []))
    print(f"  Emails ya procesados anteriormente: {len(ya_procesados)}")

    # ── Descargar datos Metabase ──────────────────────────────
    cotalker = fetch_metabase_data()

    # ── Gmail ─────────────────────────────────────────────────
    print("  Conectando a Gmail...")
    svc      = get_gmail_service()
    label_id = get_label_id(svc, GMAIL_LABEL_NAME)
    emails   = fetch_esmax_emails(svc, label_id)
    nuevos   = [e for e in emails if e["id"] not in ya_procesados]
    print(f"  Emails en {GMAIL_LABEL_NAME}: {len(emails)}  |  Nuevos: {len(nuevos)}")

    if not nuevos:
        print("  Nada nuevo que procesar.")
        return

    # ── Procesar emails ────────────────────────────────────────
    stats = {"procesados": 0, "prioridad_ok": 0, "preventivas": 0,
             "sin_sla": 0, "sin_match_sb": 0, "error": 0}

    for msg in nuevos:
        msg_id = msg["id"]
        subject, date_str = get_message_subject(svc, msg_id)
        print(f"\n  [{msg_id[:8]}] {subject}")

        # Parsear asunto
        parsed = parse_subject(subject)
        if parsed is None:
            print(f"    [!] No se pudo parsear el asunto")
            stats["error"] += 1
            ya_procesados.add(msg_id)
            continue

        n_cotalker, eds_code, equipment = parsed
        print(f"    N°={n_cotalker}  EDS={eds_code}  Equipo={equipment}")

        # Buscar en Metabase
        ot_info = cotalker.get(n_cotalker)
        if ot_info is None:
            print(f"    [!] N° {n_cotalker} no encontrado en Metabase")
            stats["error"] += 1
            ya_procesados.add(msg_id)
            continue

        nombre_orden = str(ot_info.get("Nombre orden", ""))

        # Omitir Preventivas
        if nombre_orden.startswith("PREV"):
            print(f"    [skip] Preventiva — {nombre_orden[:50]}")
            stats["preventivas"] += 1
            ya_procesados.add(msg_id)
            continue

        # Obtener SLA y prioridad
        sla      = ot_info.get("SLA esperado")
        prioridad = sla_to_priority(sla)
        fecha_ot  = str(ot_info.get("Fecha creación", ""))[:10]

        if prioridad is None:
            print(f"    [!] SLA nulo en Metabase para OT {n_cotalker}")
            stats["sin_sla"] += 1
            ya_procesados.add(msg_id)
            continue

        print(f"    SLA={sla}h  →  Prioridad={prioridad}  Fecha={fecha_ot}")

        # Buscar OT Fracttal en Supabase (matching primario por N° Cotalker en nota_tarea)
        fracttal = find_fracttal_ot(n_cotalker, eds_code, equipment, fecha_ot)
        if fracttal is None:
            print(f"    [!] Sin match en Supabase (EDS={eds_code}, equipo={equipment}, fecha±3d)")
            stats["sin_match_sb"] += 1
            # NO marcar como procesado → reintentará en próxima ejecución
            continue

        id_ot = fracttal["id_ot"]
        prev_prior = fracttal.get("prioridad_calc")
        print(f"    Match Fracttal: {id_ot}  (prioridad actual: {prev_prior})")

        # Actualizar prioridad y n_cotalker
        ok = update_prioridad_calc(id_ot, prioridad, n_cotalker)
        if ok:
            print(f"    [OK] {id_ot} → prioridad_calc={prioridad}, n_cotalker={n_cotalker}")
            stats["prioridad_ok"] += 1
        else:
            print(f"    [ERROR] No se pudo actualizar {id_ot}")
            stats["error"] += 1

        ya_procesados.add(msg_id)
        stats["procesados"] += 1

    # ── Guardar log ───────────────────────────────────────────
    log["processed_ids"] = list(ya_procesados)
    log["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_log(log)

    # ── Resumen ───────────────────────────────────────────────
    print("\n" + "="*60)
    print("  RESUMEN")
    print("="*60)
    print(f"  Emails nuevos procesados : {stats['procesados']}")
    print(f"  Prioridades asignadas    : {stats['prioridad_ok']}")
    print(f"  Preventivas omitidas     : {stats['preventivas']}")
    print(f"  Sin SLA en Metabase      : {stats['sin_sla']}")
    print(f"  Sin match en Fracttal    : {stats['sin_match_sb']}  ← reintentarán próxima ejecución")
    print(f"  Errores                  : {stats['error']}")
    print("="*60)


if __name__ == "__main__":
    main()
