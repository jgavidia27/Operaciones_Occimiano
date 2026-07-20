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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN     = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_WO        = f"{FRACTTAL_BASE}/api/work_orders"
FRACTTAL_SUBTASKS  = f"{FRACTTAL_BASE}/api/work_orders_subtasks/"
FRACTTAL_RESOURCES = f"{FRACTTAL_BASE}/api/wo_resources/"
ID_COMPANY         = 1507
WORKERS            = 8

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


def fetch_subtareas_detalle(folio: str, token: str) -> dict:
    """Extrae campos de formulario adicionales: descripcion_falla,
    trabajo_realizado, entrega_repuestos, observaciones."""
    h = {"Authorization": f"Bearer {token}"}
    out = {"descripcion_falla": None, "trabajo_realizado": None,
           "entrega_repuestos": None, "observaciones": None}
    try:
        r = requests.get(FRACTTAL_SUBTASKS, headers=h,
            params={"wo_folio": folio, "id_company": ID_COMPANY, "limit": 500},
            timeout=30)
        items = r.json().get("data", []) or []
    except Exception:
        return out

    for it in items:
        desc = (it.get("description") or "").upper().strip()
        val = it.get("value")
        val_str = str(val).strip() if val is not None else ""
        if not val_str or val_str.lower() in ("none", "null", ""):
            continue

        if "DESCRIP" in desc and "FALLA" in desc:
            out["descripcion_falla"] = val_str[:500]
        elif "TRABAJO REALIZADO" in desc:
            out["trabajo_realizado"] = val_str[:1000]
        elif "ENTREGA" in desc and "REPUESTO" in desc:
            # Puede venir como 'true'/'false', 'SI'/'NO'/'N/A', o texto
            v = val_str.upper()
            if v in ("TRUE", "SI", "SÍ", "YES"):
                out["entrega_repuestos"] = "SI"
            elif v in ("FALSE", "NO"):
                out["entrega_repuestos"] = "NO"
            elif v in ("N/A", "NA", "NOT APPLICABLE"):
                out["entrega_repuestos"] = "N/A"
            else:
                out["entrega_repuestos"] = val_str[:20]
        elif "OBSERVACION" in desc:
            out["observaciones"] = val_str[:500]
    return out


def fetch_recursos_detalle(folio: str, token: str) -> dict:
    """Extrae detalle de recursos usados (repuestos, mano obra, servicios).
    type=1 inventario, type=2 mano obra, type=3 servicio."""
    h = {"Authorization": f"Bearer {token}"}
    out = {"repuestos_detalle": None, "servicios_detalle": None,
           "hh_detalle": None, "tiene_repuesto_real": False}
    try:
        r = requests.get(FRACTTAL_RESOURCES, headers=h,
            params={"wo_folio": folio, "id_company": ID_COMPANY, "limit": 100},
            timeout=30)
        recursos = r.json().get("data", []) or []
    except Exception:
        return out

    rep, hh, serv = [], [], []
    for rr in recursos:
        t = rr.get("type")
        d = rr.get("description", "")
        q = rr.get("qty", 0)
        if t == 1:  # inventario/repuesto
            rep.append(f"{q}x {d}")
            out["tiene_repuesto_real"] = True
        elif t == 2:  # mano obra
            hh.append(f"{q} {d}")
        elif t == 3:  # servicio
            serv.append(f"{q}x {d}")
    if rep:  out["repuestos_detalle"] = "; ".join(rep)[:500]
    if serv: out["servicios_detalle"] = "; ".join(serv)[:500]
    if hh:   out["hh_detalle"] = "; ".join(hh)[:500]
    return out


def calcular_semaforo(wo: dict, extras: dict) -> tuple:
    """Devuelve (color, motivo, incongruencias).
    extras incluye trabajo_realizado, entrega_repuestos, tiene_repuesto_real,
    repuestos_detalle, servicios_detalle, hh_detalle."""
    completed = wo.get("completed_percentage") or 0
    if completed < 100:
        return ("ROJO", f"Completitud={completed}%", None)

    # Chequear recursos usando /wo_resources (fuente autoritativa) primero,
    # y como fallback los flags de work_orders (que pueden estar en None para
    # OTs compuestas con multiples activos - Fracttal devuelve 1 fila por
    # activo y solo la fila con recursos los muestra en resources_*).
    has_recurso_real = any([
        extras.get("tiene_repuesto_real"),
        bool(extras.get("servicios_detalle")),
        bool(extras.get("hh_detalle")),
    ])
    has_recurso_flag = any([
        wo.get("resources_inventory") is not None,
        wo.get("resources_human_resources") is not None,
        wo.get("resources_hours") is not None,
        wo.get("resources_services") is not None,
    ])
    if not (has_recurso_real or has_recurso_flag):
        return ("ROJO", "Sin recursos registrados (falta mano obra/repuestos/servicios)", None)

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
            return ("AMARILLO", f"Correctivo sin: {', '.join(missing)}", None)

    # Congruencia entrega_repuestos vs recursos reales
    incong = []
    entrega = extras.get("entrega_repuestos")
    tiene_rep = extras.get("tiene_repuesto_real", False)
    if entrega == "SI" and not tiene_rep:
        incong.append("Dice 'SI' a entrega repuestos pero no hay repuesto registrado en recursos")
    elif entrega == "NO" and tiene_rep:
        incong.append("Dice 'NO' a entrega repuestos pero HAY repuestos en recursos")

    # Congruencia trabajo_realizado (keyword de cambio) vs repuestos
    trabajo = (extras.get("trabajo_realizado") or "").lower()
    if trabajo and any(k in trabajo for k in ("cambio de", "cambio ", "reemplaz", "reemplaz")):
        if not tiene_rep:
            incong.append(f"Trabajo menciona 'cambio/reemplazo' pero no hay repuesto en recursos")

    if incong:
        return ("AMARILLO", "; ".join(incong), "; ".join(incong))

    return ("VERDE", "Lista para cerrar", None)


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


def wo_to_row(wo: dict, extras: dict) -> dict:
    color, motivo, incong = calcular_semaforo(wo, extras)
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
        "incongruencias":     incong,
        # Enriquecimiento con datos de subtareas + recursos
        "descripcion_falla":  extras.get("descripcion_falla"),
        "trabajo_realizado":  extras.get("trabajo_realizado"),
        "entrega_repuestos":  extras.get("entrega_repuestos"),
        "observaciones":      extras.get("observaciones"),
        "repuestos_detalle":  extras.get("repuestos_detalle"),
        "servicios_detalle":  extras.get("servicios_detalle"),
        "hh_detalle":         extras.get("hh_detalle"),
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


def enriquecer_ot(wo: dict, tok: str) -> dict:
    """Combina subtareas + recursos en un solo dict de extras."""
    folio = wo.get("wo_folio")
    extras = {}
    extras.update(fetch_subtareas_detalle(folio, tok))
    extras.update(fetch_recursos_detalle(folio, tok))
    return extras


def main():
    t0 = time.time()
    log("═══ SYNC OTs EN REVISION → Supabase ═══")
    log("Obteniendo token Fracttal...")
    tok = get_token()

    log("Barriendo OTs En Revision...")
    ots = fetch_en_revision(tok)

    log(f"Enriqueciendo {len(ots)} OTs con subtareas + recursos (paralelo x{WORKERS})...")
    extras_por_folio = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(enriquecer_ot, wo, tok): wo.get("wo_folio") for wo in ots}
        done = 0
        for fut in as_completed(futs):
            folio = futs[fut]
            try:
                extras_por_folio[folio] = fut.result()
            except Exception as e:
                extras_por_folio[folio] = {}
                log(f"   error enriqueciendo {folio}: {e}", "WARN")
            done += 1
            if done % 25 == 0:
                log(f"   {done}/{len(ots)} enriquecidas")

    log("Calculando semaforo...")
    rows = [wo_to_row(wo, extras_por_folio.get(wo.get("wo_folio"), {})) for wo in ots]
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
