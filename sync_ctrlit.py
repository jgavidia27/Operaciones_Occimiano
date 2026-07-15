"""
sync_ctrlit.py — Scraper de app.ctrlit.cl (Buk Asistencia) con Playwright.
==========================================================================

Descarga las marcaciones de entrada/salida del dia anterior desde el
reporte "Vista del mandante" y las inserta en `buk_marcaciones` de Supabase.

Uso:
    python sync_ctrlit.py                # dia anterior (default)
    python sync_ctrlit.py --fecha 14-07-2026
    python sync_ctrlit.py --desde 01-07-2026 --hasta 14-07-2026
    python sync_ctrlit.py --dry-run      # NO escribe a Supabase, solo imprime

Variables .env / Streamlit Secrets:
    CTRLIT_USER, CTRLIT_PASS
    SUPABASE_URL, SUPABASE_KEY
"""

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


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

CTRLIT_USER  = os.getenv("CTRLIT_USER", "")
CTRLIT_PASS  = os.getenv("CTRLIT_PASS", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Mandantes Occimiano — 1 por oficina/recinto:
#   36279 = Casa Matriz (Santiago)
#   36736 = La Serena (Norte)
#   36737 = Concepción (Sur)
# Overridable via env var CTRLIT_MANDANTES="36279,36736,36737"
_MANDANTES_DEFAULT = "36279,36736,36737"
MANDANTES: list[int] = [
    int(x.strip()) for x in os.getenv("CTRLIT_MANDANTES", _MANDANTES_DEFAULT).split(",")
    if x.strip()
]

# Archivo local con el storage state (cookies/localStorage) de la sesión ctrlit.
# NO commitear a git. En modo cloud (cron) el state vive en Supabase
# tabla hhee_sync_state — ver funciones _load_state/_save_state.
_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_ctrlit_state.json")
_STATE_KEY_SUPABASE = "sync_ctrlit"   # clave en hhee_sync_state


def _load_state_from_supabase() -> bool:
    """Descarga el state JSON desde hhee_sync_state y lo escribe a _STATE_PATH.
    Retorna True si tuvo éxito. En modo local, si el archivo ya existe, no
    hace nada (prioridad al disco)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_state"
            f"?script=eq.{_STATE_KEY_SUPABASE}&select=state_json",
            headers=_sb_headers(), timeout=10,
        )
        if r.status_code != 200 or not r.json():
            return False
        state = r.json()[0].get("state_json")
        if not state:
            return False
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
        return True
    except Exception:
        return False


def _save_state_to_supabase() -> bool:
    """Sube el _STATE_PATH actual a hhee_sync_state (upsert por script key)."""
    if not SUPABASE_URL or not SUPABASE_KEY or not os.path.exists(_STATE_PATH):
        return False
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_state",
            headers={**_sb_headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"script": _STATE_KEY_SUPABASE, "state_json": state},
            timeout=15,
        )
        return r.status_code in (200, 201, 204)
    except Exception:
        return False


# ── Supabase helpers ────────────────────────────────────────────────────────
def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def supabase_upsert(tabla: str, filas: list[dict]) -> int:
    if not filas:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{tabla}",
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=filas, timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase upsert {r.status_code}: {r.text[:300]}")
    return len(filas)


def log_start(script: str) -> int | None:
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


def log_end(log_id: int | None, estado: str, filas: int, mensaje: str = ""):
    if not log_id:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/hhee_sync_logs?id=eq.{log_id}",
            headers={**_sb_headers(), "Prefer": "return=minimal"},
            json={
                "estado":         estado,
                "filas_upserted": filas,
                "mensaje":        (mensaje or "")[:500] or None,
                "fin":            datetime.now(timezone.utc).isoformat(),
            }, timeout=10,
        )
    except Exception:
        pass


# ── Utilidades ──────────────────────────────────────────────────────────────
def _norm_rut(rut_raw: str) -> str | None:
    """Convierte '18211653K', '18.211.653-K', '18211653-K' -> '18.211.653-K'."""
    if not rut_raw:
        return None
    s = re.sub(r'[^\dkK]', '', rut_raw).upper()
    if len(s) < 2:
        return None
    dv = s[-1]
    num = s[:-1]
    if not num.isdigit():
        return None
    # Formatear numero con puntos
    with_dots = ''
    for i, c in enumerate(reversed(num)):
        if i > 0 and i % 3 == 0:
            with_dots = '.' + with_dots
        with_dots = c + with_dots
    return f"{with_dots}-{dv}"


def _parse_ts_ctrlit(s: str, fecha_dia: str | None = None) -> str | None:
    """Parsea la celda de entrada/salida.
    Formatos soportados:
      - '2026/07/14 09:11:30'    (fecha completa)
      - '2026-07-14 09:11:30'
      - '20:18:04'               (solo hora — usa fecha_dia como YYYY-MM-DD)
      - '20:18'                  (solo hora sin segundos)
    Retorna ISO '2026-07-14T09:11:30-04:00' o None si vacio/invalido.
    fecha_dia: 'YYYY-MM-DD' del dia del reporte, para completar timestamps 'solo hora'.
    """
    if not s or s.strip() in ('-', '—', '--', ''):
        return None
    s = s.strip()
    # 1) Fecha + hora completa
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S") + "-04:00"
        except ValueError:
            continue
    # 2) Solo hora — necesita fecha_dia
    if fecha_dia:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(s, fmt)
                return f"{fecha_dia}T{t.strftime('%H:%M:%S')}-04:00"
            except ValueError:
                continue
    return None


# ── Playwright: login manual + persistencia de sesion ──────────────────────
def login_manual_y_guardar_sesion():
    """Abre browser visible para que el usuario se loguee y resuelva captcha.
    Guarda cookies/localStorage en _STATE_PATH para usos futuros."""
    print("=" * 68)
    print(" LOGIN MANUAL — sesion ctrlit.cl")
    print("=" * 68)
    print(" Se va a abrir un navegador Chromium.")
    print(" 1) Loguéate en ctrlit con tu usuario y contraseña.")
    print(" 2) Resuelve el reCAPTCHA 'No soy un robot'.")
    print(" 3) Cuando ya estés dentro (viendo el dashboard), vuelve a esta")
    print("    ventana de terminal y presiona ENTER para guardar la sesión.")
    print(" La sesión guardada dura aprox 30 días — solo hay que repetir")
    print(" este paso cuando expire.")
    print("=" * 68)
    input(" Presiona ENTER para abrir el navegador... ")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="es-CL", viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto("https://app.ctrlit.cl/ctrl/login/auth")
        # Prefill si tenemos las credenciales — el usuario solo hace click al captcha + botón
        try:
            page.wait_for_selector("input[name=username]", timeout=10000)
            if CTRLIT_USER:
                page.fill("input[name=username]", CTRLIT_USER)
            if CTRLIT_PASS:
                page.fill("input[name=password]", CTRLIT_PASS)
        except Exception:
            pass

        print("\n --> Navegador abierto. Loguéate ahí ahora.")
        input(" Cuando ya estés dentro, presiona ENTER para guardar sesión... ")

        # Validar que efectivamente estamos logueados
        if "login" in page.url.lower():
            print(" [ADVERTENCIA] La URL sigue siendo la de login — parece que aún")
            print(" no completaste el ingreso. La sesión se guardará igual pero")
            print(" puede que no sirva. Ejecuta este comando de nuevo si falla.")

        ctx.storage_state(path=_STATE_PATH)
        print(f"\n Sesión guardada localmente en: {_STATE_PATH}")

        # Subir a Supabase para que el cron GitHub Actions pueda usarla
        if _save_state_to_supabase():
            print(" Sesión también subida a Supabase (hhee_sync_state).")
            print(" El cron diario ya puede autenticarse automáticamente por ~30 días.")
        else:
            print(" [WARN] No se pudo subir a Supabase — el cron cloud NO podrá autenticarse.")
            print("        Verifica SUPABASE_URL/SUPABASE_KEY en .env.")

        print(" Ya puedes correr:  python sync_ctrlit.py --dry-run --verbose")
        browser.close()


def fetch_marcaciones(fecha_desde: str, fecha_hasta: str,
                      mandante_id: int,
                      verbose: bool = False,
                      screenshot_dir: str | None = None) -> list[dict]:
    """Carga sesion persistente y descarga la tabla de marcaciones para 1 mandante.
    Retorna lista de dicts. Fechas en formato DD-MM-YYYY."""
    # Si no hay estado local, intentar bajar de Supabase (modo cron cloud)
    if not os.path.exists(_STATE_PATH):
        if _load_state_from_supabase():
            if verbose:
                print("  [ctrlit] Sesión descargada desde Supabase.")
        else:
            raise RuntimeError(
                f"No existe {_STATE_PATH} ni hay sesión en Supabase.\n"
                f"Corre PRIMERO:  python sync_ctrlit.py --login-manual\n"
                f"para generar la sesión ctrlit (login + captcha manual, 1 sola vez cada ~30d).")

    filas: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="es-CL",
            viewport={"width": 1400, "height": 900},
            storage_state=_STATE_PATH,   # <-- carga cookies/localStorage guardados
        )
        page = ctx.new_page()

        # 1) Verificar sesión: ir a home; si redirige a login, la sesión expiró
        if verbose:
            print(f"  [ctrlit] Verificando sesion persistente...")
        # Retry con backoff — ctrlit a veces tarda en responder
        _last_err = None
        for _intento in range(3):
            try:
                page.goto("https://app.ctrlit.cl/ctrl/", timeout=60000,
                          wait_until="domcontentloaded")
                _last_err = None
                break
            except Exception as _e:
                _last_err = _e
                if verbose:
                    print(f"  [ctrlit] intento {_intento+1}/3 fallo: {_e}. Reintentando en {2**_intento}s...")
                import time as _t
                _t.sleep(2 ** _intento)
        if _last_err:
            browser.close()
            raise RuntimeError(f"ctrlit no responde tras 3 intentos: {_last_err}")

        if "login" in page.url.lower():
            if screenshot_dir:
                page.screenshot(path=os.path.join(screenshot_dir, "ctrlit_sesion_expirada.png"))
            browser.close()
            raise RuntimeError(
                "La sesión ctrlit expiró. Ejecuta:\n"
                "    python sync_ctrlit.py --login-manual\n"
                "para renovarla.")

        if verbose:
            print(f"  [ctrlit] Sesion OK. Landing: {page.url}")

        # 2) Ir al reporte de asistencia del mandante
        # URL patron: /ctrl/mandante/registro/<ID>?d=DD-MM-YYYY&h=DD-MM-YYYY
        url_reporte = (f"https://app.ctrlit.cl/ctrl/mandante/registro/{mandante_id}"
                       f"?f=&d={fecha_desde}&h={fecha_hasta}&espe=&contrato=")
        if verbose:
            print(f"  [ctrlit] Cargando reporte: {url_reporte}")
        _last_err = None
        for _intento in range(3):
            try:
                page.goto(url_reporte, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                _last_err = None
                break
            except Exception as _e:
                _last_err = _e
                if verbose:
                    print(f"  [ctrlit] intento {_intento+1}/3 fallo: {_e}. Reintentando en {2**_intento}s...")
                import time as _t
                _t.sleep(2 ** _intento)
        if _last_err:
            browser.close()
            raise RuntimeError(f"No se pudo cargar reporte tras 3 intentos: {_last_err}")

        # Mostrar los 500 registros por pagina (default 50) para no paginar
        try:
            page.select_option("select[name*='length'], select[name*='pageSize']", "500", timeout=3000)
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # si no existe el select, seguimos con 50

        if screenshot_dir:
            page.screenshot(path=os.path.join(screenshot_dir, "ctrlit_reporte.png"),
                            full_page=True)

        # 3) Parsear la tabla HTML
        # La tabla del reporte tiene columnas: Codigo | RUT | Primer Apellido | Nombre |
        # Especialidad | Area | Contrato | Supervisor | Turno | Entrada | Salida
        # Buscamos filas <tr> con >= 10 celdas
        rows = page.query_selector_all("table tbody tr")
        if verbose:
            print(f"  [ctrlit] Filas encontradas en la tabla: {len(rows)}")

        for i_tr, tr in enumerate(rows):
            tds = tr.query_selector_all("td")
            if len(tds) < 10:
                continue
            def _txt(idx):
                if idx >= len(tds):
                    return ""
                return (tds[idx].text_content() or "").strip()

            codigo         = _txt(0)
            rut_raw        = _txt(1)
            apellido       = _txt(2)
            nombre         = _txt(3)
            especialidad   = _txt(4)
            area           = _txt(5)
            contrato       = _txt(6)
            supervisor     = _txt(7)
            turno          = _txt(8)
            entrada        = _txt(9)
            salida         = _txt(10) if len(tds) > 10 else ""

            # DEBUG en verbose: mostrar celdas 8-10 de primeras 3 filas
            if verbose and i_tr < 3:
                print(f"    [debug tr={i_tr}] len(tds)={len(tds)}  "
                      f"turno={turno!r:<25} entrada={entrada!r:<35} salida={salida!r}")

            rut = _norm_rut(rut_raw)
            if not rut:
                continue

            filas.append({
                "codigo":       codigo,
                "rut":          rut,
                "nombre":       f"{nombre} {apellido}".strip(),
                "especialidad": especialidad,
                "area":         area,
                "contrato":     contrato,
                "supervisor":   supervisor,
                "turno":        turno,
                "entrada_str":  entrada,
                "salida_str":   salida,
            })

        browser.close()

    return filas


# ── Transformar a filas buk_marcaciones ─────────────────────────────────────
def transformar_a_marcaciones(filas: list[dict], fecha: str) -> list[dict]:
    """Convierte filas del reporte (una por tecnico/dia) en filas de buk_marcaciones
    (una por evento: entrada y/o salida)."""
    ymd = datetime.strptime(fecha, "%d-%m-%Y").strftime("%Y-%m-%d")
    marcaciones = []

    for f in filas:
        raw = {k: v for k, v in f.items() if k not in ("entrada_str", "salida_str")}
        for tipo, campo in (("entrada", "entrada_str"), ("salida", "salida_str")):
            ts = _parse_ts_ctrlit(f.get(campo, ""), fecha_dia=ymd)
            if not ts:
                continue
            marcaciones.append({
                "rut":      f["rut"],
                "fecha":    ymd,
                "tipo":     tipo,
                "hora":     ts,
                "fuente":   "ctrlit_scrape",
                "raw_data": raw,
            })
    return marcaciones


# ── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fecha", help="Fecha unica DD-MM-YYYY (default: ayer)")
    ap.add_argument("--desde", help="Fecha desde DD-MM-YYYY")
    ap.add_argument("--hasta", help="Fecha hasta DD-MM-YYYY")
    ap.add_argument("--dry-run", action="store_true",
                    help="No escribe a Supabase, solo imprime")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--screenshots", action="store_true",
                    help="Guardar capturas de pantalla para debug")
    ap.add_argument("--login-manual", action="store_true",
                    help="Abre browser visible para login manual (resolver captcha) "
                         "y guarda la sesion. Ejecutar cuando la sesion expire (~30d).")
    return ap.parse_args()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args()

    # Modo login manual: no toca Supabase, solo abre browser para persistir sesion
    if args.login_manual:
        try:
            login_manual_y_guardar_sesion()
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    if args.desde and args.hasta:
        desde, hasta = args.desde, args.hasta
    elif args.fecha:
        desde = hasta = args.fecha
    else:
        ayer = datetime.now() - timedelta(days=1)
        desde = hasta = ayer.strftime("%d-%m-%Y")

    print(f"[sync_ctrlit] Descargando marcaciones {desde} -> {hasta}")
    if args.dry_run:
        print("  (DRY RUN — no se escribira a Supabase)")

    screenshot_dir = None
    if args.screenshots:
        screenshot_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_ctrlit_screenshots")
        os.makedirs(screenshot_dir, exist_ok=True)
        print(f"  Screenshots -> {screenshot_dir}")

    log_id = log_start("sync_ctrlit") if not args.dry_run else None

    try:
        # Iterar por dia (si es un rango)
        d0 = datetime.strptime(desde, "%d-%m-%Y").date()
        d1 = datetime.strptime(hasta, "%d-%m-%Y").date()
        total_upserted = 0
        dias_ok = 0
        dias_error = 0
        errores_por_dia: list[str] = []

        print(f"[sync_ctrlit] Mandantes a consultar: {MANDANTES}")

        cur = d0
        while cur <= d1:
            fecha_str = cur.strftime("%d-%m-%Y")
            for m_id in MANDANTES:
                print(f"\n  [{fecha_str} · mandante {m_id}] Fetching...")
                try:
                    filas = fetch_marcaciones(fecha_str, fecha_str,
                                               mandante_id=m_id,
                                               verbose=args.verbose,
                                               screenshot_dir=screenshot_dir)
                    print(f"  [{fecha_str} · {m_id}] {len(filas)} personas.")

                    marcaciones = transformar_a_marcaciones(filas, fecha_str)
                    print(f"  [{fecha_str} · {m_id}] {len(marcaciones)} marcaciones.")

                    if args.verbose:
                        for m in marcaciones[:3]:
                            print(f"    {m['rut']} | {m['tipo']:<8} | {m['hora']}")
                        if len(marcaciones) > 3:
                            print(f"    ... y {len(marcaciones)-3} mas")

                    if args.dry_run:
                        total_upserted += len(marcaciones)
                    elif marcaciones:
                        n = supabase_upsert("buk_marcaciones", marcaciones)
                        total_upserted += n
                        print(f"  [{fecha_str} · {m_id}] Upsert OK: {n}.")

                    dias_ok += 1
                except Exception as _e:
                    print(f"  [{fecha_str} · {m_id}] ERROR: {_e}", file=sys.stderr)
                    dias_error += 1
                    errores_por_dia.append(f"{fecha_str}·{m_id}: {str(_e)[:120]}")

            cur += timedelta(days=1)

        if args.dry_run:
            print(f"\nTotal (dry-run): {total_upserted} marcaciones en {dias_ok} dias OK, {dias_error} dias con error.")
            return 0

        print(f"\n[RESUMEN] {total_upserted} marcaciones upserted en {dias_ok} dias OK, {dias_error} dias con error.")
        if errores_por_dia:
            print("Dias con error:")
            for e in errores_por_dia:
                print(f"  - {e}")
        estado = "success" if dias_error == 0 else "partial"
        log_end(log_id, estado, total_upserted,
                f"{desde}..{hasta}: {dias_ok} OK, {dias_error} err")
        return 0 if dias_error == 0 else 2

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\nERROR: {e}\n{tb}", file=sys.stderr)
        log_end(log_id, "error", 0, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
