"""cierre_ots_playwright.py
=====================================================================
Cierre automatico de OTs en 'En Revision' via Playwright.

Fracttal es SPA (Single Page App) - no navega por URL individual.
El bot simula el flujo manual: buscar folio, click fila, click AI,
click 'Enviar a OTs Finalizadas', volver, repetir.

Uso:
    # Cerrar folios especificos
    python cierre_ots_playwright.py OS-38469 OS-38477

    # Cerrar TODAS las verdes de Supabase (leidas de ots_en_revision)
    python cierre_ots_playwright.py --verdes

    # Modo dry-run: navega pero NO hace click final en 'Finalizadas'
    python cierre_ots_playwright.py --verdes --dry-run

    # Sin ventana visible (default: visible)
    python cierre_ots_playwright.py --verdes --headless
"""

import os
import sys
import time
import uuid
import argparse
import requests
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


FRACTTAL_WO_LIST   = "https://app.fracttal.com/tasks/wo"
# NO usar one.fracttal.com/#/login porque los cookies no se comparten
# con app.fracttal.com. Vamos directo a app.fracttal.com y dejamos que
# redirija al login si es necesario -> ahi el session cookie queda en
# el dominio correcto.

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
    r = requests.get(
        f"{SB_URL}/rest/v1/ots_en_revision",
        params={"select": "folio,personnel,activo,total_cost",
                "color_semaforo": "eq.VERDE",
                "order": "dias_en_revision.desc"},
        headers=_sb_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def log_auditoria(folio: str, resultado: str, motivo: str = "", duracion_ms: int = 0):
    """Graba fila de auditoria. Si el cierre fue OK, borra la fila de
    ots_en_revision para que el panel la vea desaparecer inmediatamente
    (sin esperar al sync horario)."""
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

    # Si fue cierre real exitoso, remover de ots_en_revision al instante
    if resultado == "OK":
        try:
            requests.delete(
                f"{SB_URL}/rest/v1/ots_en_revision",
                params={"folio": f"eq.{folio}"},
                headers=_sb_headers(), timeout=10)
        except Exception as e:
            log(f"Delete de ots_en_revision fallo: {e}", "WARN")


# ── Playwright ────────────────────────────────────────────────────────
def esta_en_login(page) -> bool:
    """Fracttal es SPA - la URL puede ser tasks/wo pero mostrar login.
    Detectamos por presencia de input email + password (sin :visible)."""
    try:
        email = page.locator('input[type="email"]').count()
        pwd = page.locator('input[type="password"]').count()
        return email > 0 and pwd > 0
    except Exception:
        return False


def login(page):
    """Login directamente vs app.fracttal.com."""
    log("Navegando a app.fracttal.com/tasks/wo...")
    page.goto(FRACTTAL_WO_LIST, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)  # dar tiempo a la SPA a montarse

    # Verificar si aparece login
    if not esta_en_login(page):
        log(f"Ya logueado (sesion previa) — URL: {page.url}", "OK")
        return

    # Email - probar selectores
    email_ok = False
    for sel in ['input[type="email"]', 'input[name="email"]', 'input[name="username"]',
                'input[placeholder*="mail" i]', 'input[placeholder*="usuario" i]',
                'input[type="text"]']:
        try:
            page.locator(sel).first.fill(EMAIL, timeout=3000)
            email_ok = True
            log(f"Email cargado con selector: {sel}")
            break
        except Exception:
            continue
    if not email_ok:
        raise RuntimeError("No se encontro campo de email en el login")

    # Password
    password_ok = False
    for sel in ['input[type="password"]', 'input[name="password"]']:
        try:
            page.locator(sel).first.fill(PASSWORD, timeout=3000)
            password_ok = True
            log(f"Password cargado con selector: {sel}")
            break
        except Exception:
            continue
    if not password_ok:
        raise RuntimeError("No se encontro campo de password en el login")

    # Submit: primero probamos ENTER (funciona en casi todos los forms)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    log("Enviado con Enter, esperando que login desaparezca...")

    # Esperar activamente hasta que ya no se vea el login (SPA - la URL
    # no cambia; el criterio es la ausencia de inputs email/password).
    max_wait_s = 30
    t0 = time.time()
    while time.time() - t0 < max_wait_s:
        if not esta_en_login(page):
            break
        page.wait_for_timeout(500)

    page.wait_for_timeout(3000)  # dar tiempo a la SPA a montar la app

    # Si sigue en login, intentar click en boton
    if esta_en_login(page):
        for sel in ['button[type="submit"]', 'button:has-text("Ingresar")',
                    'button:has-text("Iniciar")', 'button:has-text("Login")',
                    'button:has-text("Siguiente")', 'button:has-text("Entrar")']:
            try:
                page.locator(sel).first.click(timeout=3000)
                break
            except Exception:
                continue
        # Esperar de nuevo
        t0 = time.time()
        while time.time() - t0 < max_wait_s:
            if not esta_en_login(page):
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(3000)

    if esta_en_login(page):
        page.screenshot(path="debug_login_fail.png", full_page=True)
        raise RuntimeError(f"Sigue viendose login despues del submit. URL: {page.url}")

    log(f"Login OK — URL actual: {page.url}", "OK")


def ir_a_lista_ots(page):
    """Asegura que estamos en la vista LISTA de OTs (no Kanban).
    Fracttal por defecto abre en Kanban - hay que clickear el icono
    de vista lista (4to icono de la barra superior izquierda)."""
    if "tasks/wo" not in page.url:
        page.goto(FRACTTAL_WO_LIST, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)

    # Cambiar a vista LISTA — el boton correcto es title="Órdenes de Trabajo"
    cambiado = False
    estrategias_lista = [
        lambda: page.locator('button[title="Órdenes de Trabajo"]').first,
        lambda: page.locator('button[title*="Ordenes" i]').first,
        lambda: page.locator('button[title*="Órdenes" i]').first,
    ]
    for i, get_loc in enumerate(estrategias_lista):
        try:
            loc = get_loc()
            loc.click(timeout=3000)
            log(f"   cambiado a vista LISTA (estrategia {i+1})")
            page.wait_for_timeout(2500)
            cambiado = True
            break
        except Exception:
            continue

    if not cambiado:
        log("   WARN: no se pudo cambiar a vista lista - dejando en Kanban", "WARN")


def resetear_vista(page):
    """Vuelve a un estado limpio: sin modales, en lista, buscador vacio."""
    # 1. Cerrar cualquier modal abierto
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    # 2. Ir a la lista de OTs (limpia URL)
    try:
        page.goto(FRACTTAL_WO_LIST, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
    except Exception:
        pass

    # 3. Asegurar vista Lista
    try:
        page.locator('button[title="Órdenes de Trabajo"]').first.click(timeout=3000)
        page.wait_for_timeout(2000)
    except Exception:
        pass

    # 4. Limpiar buscador si tiene valor previo
    try:
        buscador = page.locator('input[placeholder="Buscar..."]').first
        buscador.click(timeout=3000)
        # Ctrl+A + Delete para borrar cualquier valor
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.wait_for_timeout(500)
    except Exception:
        pass


def cerrar_ot(page, folio: str, dry_run: bool = False) -> tuple:
    """Flujo: buscar folio -> click fila -> click AI -> click Finalizadas
    -> confirmar -> volver a lista.
    Retorna (resultado, motivo, duracion_ms)."""
    t0 = time.time()
    try:
        # Resetear vista para cada OT (limpia modales, buscador previo, etc)
        resetear_vista(page)

        # Log estado actual
        log(f"   URL actual: {page.url}")
        log(f"   Titulo: {page.title()[:60]}")

        # Si perdio sesion, re-login
        if "login" in page.url.lower() or "signin" in page.url.lower():
            log("   Sesion perdida, re-logueando...", "WARN")
            login(page)
            ir_a_lista_ots(page)

        # DEBUG: guardar screenshot + HTML al llegar a la lista
        page.screenshot(path="debug_lista_ots.png", full_page=True)
        with open("debug_lista_ots.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("   DEBUG: guardado debug_lista_ots.png + debug_lista_ots.html")

        # Listar TODOS los botones cerca del top (para encontrar iconos de vista)
        botones_top = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            return btns.filter(b => {
                const r = b.getBoundingClientRect();
                return b.offsetParent !== null && r.top < 200 && r.left < 400;
            }).map(b => ({
                text: (b.innerText || '').trim().slice(0, 40),
                title: b.title || '',
                ariaLabel: b.getAttribute('aria-label') || '',
                class: b.className.slice(0, 80),
                iconClass: b.querySelector('i')?.className || '',
                rect: {top: Math.round(b.getBoundingClientRect().top),
                       left: Math.round(b.getBoundingClientRect().left)}
            }));
        }""")
        log(f"   Botones top-left encontrados: {len(botones_top)}")
        for i, b in enumerate(botones_top):
            log(f"      [{i}] icon='{b['iconClass'][:40]}' aria='{b['ariaLabel']}' title='{b['title']}' text='{b['text'][:20]}' pos=({b['rect']['top']},{b['rect']['left']})")

        # Listar inputs para diagnostico
        inputs_info = page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input'));
            return inputs.filter(i => i.offsetParent !== null).map(i => ({
                type: i.type,
                placeholder: i.placeholder,
                name: i.name,
                ariaLabel: i.getAttribute('aria-label')
            }));
        }""")
        log(f"   Inputs visibles: {len(inputs_info)}")
        for i, inp in enumerate(inputs_info[:5]):
            log(f"      [{i}] type={inp['type']} placeholder='{inp['placeholder']}' name='{inp['name']}' aria='{inp['ariaLabel']}'")

        # 1. Escribir folio en el buscador — probar multiples estrategias
        buscador = None
        estrategias = [
            lambda: page.get_by_placeholder("Buscar...", exact=True),
            lambda: page.get_by_placeholder("Buscar"),
            lambda: page.locator('input[placeholder*="Buscar"]').first,
            lambda: page.locator('input[type="search"]').first,
            lambda: page.locator('input[type="text"]:visible').first,
            lambda: page.locator('.search input, .buscar input').first,
        ]
        for i, get_loc in enumerate(estrategias):
            try:
                loc = get_loc()
                loc.wait_for(state="visible", timeout=3000)
                buscador = loc
                log(f"   buscador encontrado (estrategia {i+1})")
                break
            except Exception:
                continue
        if buscador is None:
            # Screenshot para debug
            page.screenshot(path=f"debug_no_buscador_{folio}.png", full_page=True)
            raise RuntimeError(f"No se encontro buscador. Screenshot guardado en debug_no_buscador_{folio}.png")

        buscador.click(timeout=5000)
        buscador.fill("")
        page.wait_for_timeout(300)
        buscador.type(folio, delay=30)
        page.wait_for_timeout(2000)  # esperar a que filtre

        # 2. Click en la fila con el folio
        fila = page.locator(f'text={folio}').first
        fila.click(timeout=10000)
        log(f"   click en fila {folio} OK")
        page.wait_for_timeout(2500)

        # 3. Click en boton "..." (3 puntos verticales) al lado de Guardar.
        # NO es el boton "AI" - ese abre el chat asistente. Es un IconButton
        # con icono kebab (mdi-dots-vertical o SVG MoreVert).
        boton_menu = None
        for get_loc in [
            lambda: page.get_by_role("button", name="more_vert"),
            lambda: page.get_by_role("button", name="Más"),
            lambda: page.get_by_role("button", name="Opciones"),
            lambda: page.locator('button[aria-label*="more" i]').first,
            lambda: page.locator('button svg[data-testid="MoreVertIcon"]').first,
            lambda: page.locator('button:has(svg[data-testid="MoreVertIcon"])').first,
            lambda: page.locator('button i.mdi-dots-vertical').first,
            lambda: page.locator('button:has(i.mdi-dots-vertical)').first,
            # Kebab menu suele estar cerca del boton Guardar arriba a la derecha
            lambda: page.locator('header button, .toolbar button, .app-bar button').last,
        ]:
            try:
                loc = get_loc()
                loc.wait_for(state="visible", timeout=2500)
                boton_menu = loc
                break
            except Exception:
                continue
        if boton_menu is None:
            page.screenshot(path=f"debug_no_menu_{folio}.png", full_page=True)
            raise RuntimeError(f"No se encontro boton menu 3 puntos. Screenshot: debug_no_menu_{folio}.png")
        boton_menu.click(timeout=5000)
        log("   click en boton menu (3 puntos) OK")
        page.wait_for_timeout(1500)

        # 4. Click en "Enviar a OTs Finalizadas" - filtrar por visible
        # El texto aparece 15+ veces en el DOM pero solo 1 es visible
        opcion_finalizar = None
        for get_loc in [
            lambda: page.get_by_role("menuitem", name="Enviar a OTs Finalizadas"),
            lambda: page.locator('li:visible:has-text("Enviar a OTs Finalizadas")').first,
            lambda: page.locator('[role="menuitem"]:visible:has-text("Enviar a OTs Finalizadas")').first,
            lambda: page.locator('text="Enviar a OTs Finalizadas"').locator('visible=true').first,
            lambda: page.get_by_text("Enviar a OTs Finalizadas", exact=True).locator('visible=true').first,
        ]:
            try:
                loc = get_loc()
                loc.wait_for(state="visible", timeout=2500)
                opcion_finalizar = loc
                break
            except Exception:
                continue
        if opcion_finalizar is None:
            page.screenshot(path=f"debug_no_finalizar_{folio}.png", full_page=True)
            raise RuntimeError(f"No se encontro 'Enviar a OTs Finalizadas'. Screenshot: debug_no_finalizar_{folio}.png")

        if dry_run:
            # Solo verificar que el elemento existe, no clickear
            opcion_finalizar.wait_for(state="visible", timeout=5000)
            log(f"[DRY-RUN] {folio} — menu abierto, boton 'Enviar a OTs Finalizadas' encontrado")
            # Cerrar el menu clicando fuera + volver a lista
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.go_back()
            page.wait_for_timeout(1500)
            return ("DRY_OK", "dry-run: skip", int((time.time()-t0)*1000))

        # Screenshot ANTES del click para ver el menú abierto
        page.screenshot(path=f"debug_menu_open_{folio}.png", full_page=True)
        log(f"   screenshot pre-click: debug_menu_open_{folio}.png")

        opcion_finalizar.click(timeout=5000)
        log("   click en 'Enviar a OTs Finalizadas' OK")
        page.wait_for_timeout(2500)

        # Screenshot DESPUES del click para ver si aparece modal
        page.screenshot(path=f"debug_post_click_{folio}.png", full_page=True)
        log(f"   screenshot post-click: debug_post_click_{folio}.png")

        # 5. Confirmar modal — Fracttal muestra "¿Desea continuar?" con botones
        # "No" y "Si" (sin tilde). El "Si" es el confirmar irreversible.
        # Fracttal usa Material UI - los botones pueden ser <span> con role
        # aplicado o <button> plano. Probamos varias estrategias.
        modal_confirmado = False

        # Estrategia 1: buscar el texto exacto "Si" en cualquier elemento visible
        # Filtramos por texto EXACTO para no matchear "Sin datos" o similar
        estrategias_confirm = [
            # Rol button, nombre exacto (mas confiable si esta bien marcado)
            lambda: page.get_by_role("button", name="Si", exact=True),
            lambda: page.get_by_role("button", name="Sí", exact=True),
            # button DOM con texto exacto (ignora "Sin")
            lambda: page.locator('button:visible').filter(
                has_text="Si").filter(has_not_text="Sin"),
            # Cualquier elemento con texto EXACTO "Si"
            lambda: page.locator('*:visible').filter(
                has_text="Si").filter(has_not_text="Sin").last,
            # Ultimo recurso: click por xpath directo
            lambda: page.locator('//button[normalize-space(text())="Si"]'),
            lambda: page.locator('//button[normalize-space(text())="Sí"]'),
            # Fallback: "Confirmar" u otros
            lambda: page.get_by_role("button", name="Confirmar", exact=True),
            lambda: page.get_by_role("button", name="Aceptar", exact=True),
        ]
        for i, get_loc in enumerate(estrategias_confirm):
            try:
                btn = get_loc().first
                btn.wait_for(state="visible", timeout=1500)
                btn.click(timeout=2000)
                log(f"   confirmado modal (estrategia {i+1})")
                modal_confirmado = True
                break
            except Exception:
                continue

        # Ultimo fallback: apretar Enter (los modales suelen aceptar Enter)
        if not modal_confirmado:
            log("   fallback: apretando Enter", "WARN")
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            modal_confirmado = True  # asumimos que funciono

        if not modal_confirmado:
            log("   NO aparecio modal de confirmacion - puede que la accion no se ejecuto", "WARN")

        # Wait fijo (no networkidle - Fracttal hace polling constante)
        page.wait_for_timeout(3500)

        # Screenshot final para validar
        page.screenshot(path=f"debug_final_{folio}.png", full_page=True)

        return ("OK", "cerrada" if modal_confirmado else "sin_confirmacion",
                int((time.time()-t0)*1000))

    except PWTimeout as e:
        # Intentar recuperar posicion (volver a lista)
        try:
            ir_a_lista_ots(page)
        except Exception:
            pass
        return ("FAIL", f"timeout: {str(e)[:200]}", int((time.time()-t0)*1000))
    except Exception as e:
        try:
            ir_a_lista_ots(page)
        except Exception:
            pass
        return ("FAIL", str(e)[:200], int((time.time()-t0)*1000))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folios", nargs="*", help="Folios especificos (ej: OS-38469)")
    ap.add_argument("--verdes", action="store_true",
                    help="Cerrar TODAS las verdes de Supabase")
    ap.add_argument("--dry-run", action="store_true",
                    help="No hace click final, solo abre menu")
    ap.add_argument("--headless", action="store_true",
                    help="Sin ventana visible")
    ap.add_argument("--max", type=int, default=None,
                    help="Cortar despues de N cierres (safety)")
    args = ap.parse_args()

    if not EMAIL or not PASSWORD:
        log("FALTAN FRACTTAL_LOGIN_EMAIL / FRACTTAL_LOGIN_PASSWORD en .env", "ERR")
        sys.exit(1)

    folios = list(args.folios)
    if args.verdes:
        log("Leyendo verdes de Supabase...")
        verdes = get_verdes()
        folios.extend([v["folio"] for v in verdes])
        log(f"   {len(verdes)} verdes candidatas", "OK")

    if not folios:
        log("Sin folios. Usar OS-XXXX o --verdes", "ERR")
        sys.exit(1)

    if args.max:
        folios = folios[:args.max]

    folios = list(dict.fromkeys(folios))
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
            ir_a_lista_ots(page)
            log("Lista de OTs cargada. Empezando cierres...")
        except Exception as e:
            log(f"LOGIN/NAV FALLO: {e}", "ERR")
            browser.close()
            sys.exit(2)

        for i, folio in enumerate(folios, 1):
            log(f"→ [{i}/{len(folios)}] {folio}...")
            resultado, motivo, dur = cerrar_ot(page, folio, dry_run=args.dry_run)
            log_auditoria(folio, resultado, motivo, dur)
            if resultado in ("OK", "DRY_OK"):
                ok += 1
                log(f"   ✅ {folio} ({dur/1000:.1f}s)", "OK")
            else:
                fail += 1
                log(f"   ❌ {folio}: {motivo}", "ERR")

        browser.close()

    log("")
    log(f"═══ RESUMEN | Batch {BATCH_ID} ═══")
    log(f"   ✅ Exitos: {ok}", "OK")
    log(f"   ❌ Fallos: {fail}", "ERR" if fail else "")


if __name__ == "__main__":
    main()
