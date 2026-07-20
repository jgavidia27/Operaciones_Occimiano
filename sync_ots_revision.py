"""sync_ots_revision.py
=====================================================================
Sincroniza OTs en estado "En Revision" (esperando validacion) de
Fracttal a Supabase (tabla `ots_en_revision`).

Definicion de "En Revision" (verificado empiricamente):
    task_status == "DONE"  AND
    done        == True    AND
    wo_final_date IS NULL  AND
    custom_status NO contiene ERROR/DUPLICADO/CANCEL/RECHAZ/PRUEBA

Semaforo (calculado aca, no en la UI):
    ROJO      : completed_pct < 100  OR  sin recursos registrados
    AMARILLO  : correctiva sin (tipo_falla + causa + deteccion)
    VERDE     : todo OK -> lista para cerrar

Corre cada 30 min (cron GitHub Actions).
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone

# UTF-8 en Windows (log messages con acentos, arrows)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Cargar .env solo si estamos en local (en GitHub Actions no existe)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────
FRACTTAL_BASE  = "https://app.fracttal.com"
FRACTTAL_TOKEN = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_WO    = f"{FRACTTAL_BASE}/api/work_orders"

CLIENT_ID     = os.environ.get("FRACTTAL_CLIENT_ID") or "KtHFO5pMskBbJ3lhPr"
CLIENT_SECRET = os.environ["FRACTTAL_CLIENT_SECRET"]

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SB_TABLE     = "ots_en_revision"

# Paginacion Fracttal: barrer hasta N paginas (max 5000 OTs recientes).
# Como filtramos por wo_final_date IS NULL, las OTs viejas ya deberian
# haber sido cerradas -> no necesitamos ir mas atras.
MAX_PAGES = 50   # 50 * 100 = 5000 OTs recientes

_BASURA = ("ERROR", "DUPLIC", "CANCEL", "RECHAZ", "PRUEBA")


def log(msg: str, tag: str = ""):
    now = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{tag}] " if tag else ""
    print(f"[{now}] {prefix}{msg}", flush=True)


def get_token() -> str:
    r = requests.post(FRACTTAL_TOKEN,
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_en_revision(token: str) -> list:
    """Barre paginacion de work_orders y filtra los que estan En Revision."""
    h = {"Authorization": f"Bearer {token}"}
    seen_folios = set()
    result = []
    start = 0
    pages = 0
    while pages < MAX_PAGES:
        r = requests.get(FRACTTAL_WO, headers=h,
                         params={"start": start, "limit": 100}, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", []) or []
        if not data:
            break
        for wo in data:
            # Filtro estricto: DONE + done=True + sin wo_final_date
            if wo.get("task_status") != "DONE":
                continue
            if wo.get("done") is not True:
                continue
            if wo.get("wo_final_date") is not None:
                continue
            cs = (wo.get("work_orders_status_custom_description") or "").upper()
            if any(x in cs for x in _BASURA):
                continue
            fol = wo.get("wo_folio")
            if not fol or fol in seen_folios:
                continue
            seen_folios.add(fol)
            result.append(wo)
        start += 100
        pages += 1
        if len(data) < 100:
            break
    log(f"Fracttal: {pages} paginas barridas, {len(result)} OTs En Revision", "OK")
    return result


def calcular_semaforo(wo: dict) -> tuple:
    """Devuelve (color, motivo)."""
    completed = wo.get("completed_percentage") or 0
    if completed < 100:
        return ("ROJO", f"Completitud={completed}%")

    has_recurso = any([
        wo.get("resources_inventory") is not None,
        wo.get("resources_human_resources") is not None,
        wo.get("resources_hours") is not None,
        wo.get("resources_services") is not None,
    ])
    if not has_recurso:
        return ("ROJO", "Sin recursos registrados (falta mano obra/repuestos/servicios)")

    tipo = (wo.get("tasks_log_task_type_main") or "").upper()
    is_correctivo = tipo.startswith("CORRECT")
    if is_correctivo:
        tiene_falla = bool(wo.get("types_description"))
        tiene_causa = bool(wo.get("causes_description"))
        tiene_det   = bool(wo.get("detection_method_description"))
        if not (tiene_falla and tiene_causa and tiene_det):
            missing = []
            if not tiene_falla: missing.append("tipo falla")
            if not tiene_causa: missing.append("causa")
            if not tiene_det:   missing.append("deteccion")
            return ("AMARILLO", f"Correctivo sin: {', '.join(missing)}")

    return ("VERDE", "Lista para cerrar")


def dias_espera(review_date: str) -> int:
    if not review_date:
        return 0
    try:
        # Normalizar formato ISO con timezone
        d = datetime.fromisoformat(review_date.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - d
        return max(0, delta.days)
    except Exception:
        return 0


def extraer_cliente(parent_desc: str) -> str:
    """Del parent_desc '// COPEC/ COPEC TALCA/' extrae 'COPEC'."""
    if not parent_desc:
        return ""
    parts = [p.strip() for p in parent_desc.replace("//", "").split("/") if p.strip()]
    return parts[0] if parts else ""


def extraer_eds(wo: dict) -> str:
    """Deriva codigo EDS Occimiano. Por ahora usa groups_2_description
    (a veces trae numeros de estacion). Se puede enriquecer despues
    cruzando con la tabla `estaciones` de Supabase."""
    g2 = wo.get("groups_2_description")
    return str(g2) if g2 else ""


def wo_to_row(wo: dict) -> dict:
    color, motivo = calcular_semaforo(wo)
    return {
        "folio":              wo.get("wo_folio"),
        "id_wo":              wo.get("id_work_order"),
        "tipo":               wo.get("tasks_log_task_type_main"),
        "activo":             wo.get("items_log_description"),
        "codigo_activo":      wo.get("code"),
        "parent_desc":        wo.get("parent_description"),
        "cliente":            extraer_cliente(wo.get("parent_description") or ""),
        "eds_occim":          extraer_eds(wo),
        "personnel":          wo.get("personnel_description"),
        "created_by":         wo.get("created_by"),
        "creation_date":      wo.get("creation_date"),
        "event_date":         wo.get("event_date"),
        "initial_date":       wo.get("initial_date"),
        "final_date":         wo.get("final_date"),
        "review_date":        wo.get("review_date"),
        "dias_en_revision":   dias_espera(wo.get("review_date") or wo.get("final_date") or ""),
        "completed_pct":      wo.get("completed_percentage") or 0,
        "tiene_recurso_inv":   wo.get("resources_inventory") is not None,
        "tiene_recurso_hh":    wo.get("resources_human_resources") is not None,
        "tiene_recurso_hours": wo.get("resources_hours") is not None,
        "tiene_recurso_serv":  wo.get("resources_services") is not None,
        "total_cost":         wo.get("total_cost_task") or 0,
        "resources_serv_desc":(wo.get("resources_services") or "")[:500],
        "tipo_falla":         wo.get("types_description"),
        "causa_raiz":         wo.get("causes_description"),
        "metodo_deteccion":   wo.get("detection_method_description"),
        "note":               (wo.get("note") or "")[:500],
        "task_note":          (wo.get("task_note") or "")[:500],
        "color_semaforo":     color,
        "motivo_semaforo":    motivo,
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }


def _sb_headers(extra=None):
    h = {"apikey": SUPABASE_KEY,
         "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def sb_delete_all():
    """Borra toda la tabla antes de repoblar. Estrategia SIMPLE:
    dado que la tabla siempre tiene < 500 filas y refleja estado presente,
    es mas confiable un refresh completo que un merge (que dejaria OTs
    ya cerradas por fuera del panel)."""
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{SB_TABLE}",
        params={"folio": "neq.__nada__"},   # match all rows
        headers=_sb_headers({"Prefer": "return=minimal"}),
        timeout=30)
    if r.status_code not in (200, 204):
        log(f"Delete previo devolvio {r.status_code}: {r.text[:200]}", "WARN")


def sb_upsert(rows: list):
    """Insert en tandas de 200 (Supabase acepta hasta ~1000 pero
    dejamos margen para timeouts)."""
    if not rows:
        return
    BATCH = 200
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{SB_TABLE}",
            json=chunk,
            headers=_sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            timeout=60)
        if r.status_code >= 400:
            log(f"Upsert {i}-{i+len(chunk)} FAIL {r.status_code}: {r.text[:400]}", "ERR")
            raise SystemExit(1)
        log(f"Upsert {i+1}-{i+len(chunk)} OK ({len(chunk)} filas)")


def main():
    t0 = time.time()
    log("═══ SYNC OTs EN REVISION → Supabase ═══")
    log("Obteniendo token Fracttal...")
    tok = get_token()

    log("Barriendo OTs En Revision...")
    ots = fetch_en_revision(tok)

    log("Calculando semaforo...")
    rows = [wo_to_row(wo) for wo in ots]
    counts = {"VERDE": 0, "AMARILLO": 0, "ROJO": 0}
    for r in rows:
        counts[r["color_semaforo"]] += 1
    log(f"   VERDES:    {counts['VERDE']:>3}", "OK")
    log(f"   AMARILLAS: {counts['AMARILLO']:>3}", "OK")
    log(f"   ROJAS:     {counts['ROJO']:>3}", "OK")

    log("Refrescando tabla Supabase (delete + upsert)...")
    sb_delete_all()
    sb_upsert(rows)

    log(f"COMPLETADO en {int(time.time()-t0)}s | {len(rows)} OTs sincronizadas", "OK")


if __name__ == "__main__":
    main()
