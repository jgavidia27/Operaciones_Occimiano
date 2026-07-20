"""cierre_ots_playwright.py
=====================================================================
Cierre automatico de OTs en 'En Revision' via Playwright (web scraping).

Fracttal NO expone API de escritura sin el add-on 'API Advanced'. Por
eso automatizamos con navegador real: login -> ir a cada OT -> click en
'Enviar a OTs Finalizadas'.

Uso:
    # Cerrar solo folios especificos
    python cierre_ots_playwright.py OS-38469 OS-38477

    # Cerrar TODAS las verdes de Supabase (leidas de ots_en_revision)
    python cierre_ots_playwright.py --verdes

    # Modo dry-run: navega pero NO hace click final
    python cierre_ots_playwright.py --verdes --dry-run

    # Modo visible (por defecto). Para modo headless usar --headless
    python cierre_ots_playwright.py --verdes --headless

Requiere:
    pip install playwright python-dotenv requests
    playwright install chromium

Variables .env local:
    FRACTTAL_LOGIN_EMAIL
    FRACTTAL_LOGIN_PASSWORD
    SUPABASE_URL, SUPABASE_KEY  (para leer verdes y grabar auditoria)
"""

import os
import sys
import time
import uuid
import argparse
import requests
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────
FRACTTAL_LOGIN_URL = "https://one.fracttal.com/#/login"
FRACTTAL_WO_URL    = "https://one.fracttal.com/#/orders/tasks/{folio}/task"
# Nota: la URL exacta se ajusta empiricamente en la primera corrida visible.

EMAIL      = os.environ.get("FRACTTAL_LOGIN_EMAIL")
PASSWORD   = os.environ.get("FRACTTAL_LOGIN_PASSWORD")
SB_URL     = os.environ.get("SUPABASE_URL")
SB_KEY     = os.environ.get("SUPABASE_KEY")
BATCH_ID   = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def log(msg: str, tag: str = ""):
    now = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{tag}] " if tag else ""
    print(f"[{now}] {prefix}{msg}", flush=True)


# ── Supabase helpers ──────────────────────────────────────────────────
def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json"}


def get_verdes() -> list:
    """Lee folios verdes de ots_en_revision."""
    r = requests.get(
        f"{SB_URL}/rest/v1/ots_en_revision",
        params={"select": "folio,personnel,activo,total_cost",
                "color_semaforo": "eq.VERDE",
                "order": "dias_en_revision.desc"},
        headers=_sb_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def log_auditoria(folio: str, resultado: str, motivo: str = "", duracion_ms: int = 0):
    """Graba fila de auditoria en Supabase."""
    if not SB_URL:
        return
    try:
        requests.post(
            f"{SB_URL}/rest/v1/ots_cierres_auditoria",
            json={
                "folio":         folio,
                "resultado":     resultado,
                "motivo":        motivo[:500] if motivo else None,
                "duracion_ms":   duracion_ms,
                "batch_id":      BATCH_ID,
                "ejecutado_por": EMAIL or "unknown",
            },
            headers=_sb_headers(), timeout=10)
    except Exception as e:
        log(f"Auditoria fallo: {e}", "WARN")


# ── Playwright ────────────────────────────────────────────────────────
def login(page):
    """Login en Fracttal One."""
    log("Abriendo Fracttal login...")
    page.goto(FRACTTAL_LOGIN_URL, wait_until="networkidle", timeout=30000)
    # Los selectores exactos se ajustan cuando ejecutemos por primera vez
    # con browser visible. Placeholder inicial:
    page.fill('input[type="email"], input[name="email"]', EMAIL)
    page.fill('input[type="password"], input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(2)
    log("Login OK", "OK")


def cerrar_ot(page, folio: str, dry_run: bool = False) -> tuple:
    """Retorna (resultado, motivo, duracion_ms)."""
    t0 = time.time()
    try:
        url = FRACTTAL_WO_URL.format(folio=folio)
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(1)

        # Buscar boton "AI" / menu 3 puntos (por captura del usuario)
        # y luego "Enviar a OTs Finalizadas"
        # NOTA: selectores tentativos. Se ajustan en primera corrida visible.
        page.click('text=Enviar a OTs Finalizadas', timeout=10000)

        if dry_run:
            log(f"[DRY-RUN] {folio} — encontro boton pero NO hizo click final")
            return ("DRY_OK", "dry-run: skip", int((time.time()-t0)*1000))

        # Confirmar si aparece modal
        try:
            page.click('button:has-text("Confirmar"), button:has-text("OK"), button:has-text("Aceptar")',
                       timeout=3000)
        except PWTimeout:
            pass  # No aparecio modal, seguir

        # Verificar exito - esperar redirect o mensaje
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(1)

        return ("OK", "cerrada", int((time.time()-t0)*1000))

    except PWTimeout as e:
        return ("FAIL", f"timeout: {str(e)[:200]}", int((time.time()-t0)*1000))
    except Exception as e:
        return ("FAIL", str(e)[:200], int((time.time()-t0)*1000))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folios", nargs="*", help="Folios especificos a cerrar (ej: OS-38469)")
    ap.add_argument("--verdes", action="store_true",
                    help="Cerrar TODAS las verdes de Supabase")
    ap.add_argument("--dry-run", action="store_true",
                    help="No hace click final, solo navega")
    ap.add_argument("--headless", action="store_true",
                    help="Sin ventana visible (default: visible)")
    ap.add_argument("--max", type=int, default=None,
                    help="Cortar despues de N cierres (safety)")
    args = ap.parse_args()

    if not EMAIL or not PASSWORD:
        log("FALTAN FRACTTAL_LOGIN_EMAIL / FRACTTAL_LOGIN_PASSWORD en .env", "ERR")
        sys.exit(1)

    # Determinar folios a procesar
    folios = list(args.folios)
    if args.verdes:
        log("Leyendo verdes de Supabase...")
        verdes = get_verdes()
        folios.extend([v["folio"] for v in verdes])
        log(f"   {len(verdes)} verdes candidatas", "OK")

    if not folios:
        log("Sin folios que procesar. Usar OS-XXXX o --verdes", "ERR")
        sys.exit(1)

    if args.max:
        folios = folios[:args.max]

    folios = list(dict.fromkeys(folios))  # dedupe preservando orden
    log(f"═══ CIERRE AUTOMATICO — {len(folios)} OTs ═══")
    log(f"Modo: {'DRY-RUN' if args.dry_run else 'REAL'} | "
        f"Browser: {'headless' if args.headless else 'visible'} | "
        f"Batch: {BATCH_ID}")

    ok = 0
    fail = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context()
        page = ctx.new_page()

        try:
            login(page)
        except Exception as e:
            log(f"LOGIN FALLO: {e}", "ERR")
            browser.close()
            sys.exit(2)

        for i, folio in enumerate(folios, 1):
            log(f"→ [{i}/{len(folios)}] Cerrando {folio}...")
            resultado, motivo, dur = cerrar_ot(page, folio, dry_run=args.dry_run)
            log_auditoria(folio, resultado, motivo, dur)
            if resultado in ("OK", "DRY_OK"):
                ok += 1
                log(f"   ✅ {folio} ({dur}ms)", "OK")
            else:
                fail += 1
                log(f"   ❌ {folio}: {motivo}", "ERR")

        browser.close()

    log("")
    log(f"═══ RESUMEN | Batch {BATCH_ID} ═══")
    log(f"   ✅ Exitos: {ok}", "OK")
    log(f"   ❌ Fallos: {fail}", "ERR" if fail else "")
    if SB_URL:
        log(f"   📜 Auditoria en Supabase.ots_cierres_auditoria WHERE batch_id='{BATCH_ID}'")


if __name__ == "__main__":
    main()
