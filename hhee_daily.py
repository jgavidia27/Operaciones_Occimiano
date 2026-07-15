"""
hhee_daily.py — Wrapper que corre los syncs HHEE en secuencia.
==============================================================

Diseñado para ejecutarse desde GitHub Actions (cron diario 09:30 CLT).

Orden:
  1. sync_estaciones_from_ots  (EDS nuevas de Fracttal → estaciones_servicio)
  2. sync_buk_rrhh             (nómina Buk → tecnicos_hhee)
  3. sync_ctrlit               (scraper marcaciones Buk Asistencia)
  4. sync_rastreosat_drive     (procesa CSV nuevo de Google Drive)
  5. he_evaluator              (recalcula veredictos con data fresca)

Si un paso falla, continúa con los siguientes (evita bloquear todo por 1 error).
Al final reporta a hhee_sync_logs con el resumen.

Ejecución:
    python hhee_daily.py                # ultimos 14 dias
    python hhee_daily.py --dias 30
    python hhee_daily.py --skip ctrlit  # saltar un script específico

Env vars requeridas (GitHub Secrets):
    SUPABASE_URL, SUPABASE_KEY
    BUK_API_TOKEN
    CTRLIT_USER, CTRLIT_PASS
    GOOGLE_SERVICE_ACCOUNT_JSON   (JSON completo del service account)
    GDRIVE_HHEE_FOLDER_ID         (ID de la carpeta "HHEE semanal - GPS")
"""

import argparse
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone

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
TZ_CHILE = timezone(timedelta(hours=-4))


# ── Log al inicio y fin ─────────────────────────────────────────────────────
def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def log_start(script: str) -> int | None:
    if not SUPABASE_URL: return None
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/hhee_sync_logs",
                          headers={**_sb_headers(), "Prefer": "return=representation"},
                          json={"script": script, "estado": "running"}, timeout=10)
        if r.status_code in (200, 201) and r.json():
            return r.json()[0].get("id")
    except Exception:
        pass
    return None


def log_end(log_id, estado, mensaje=""):
    if not log_id or not SUPABASE_URL: return
    try:
        requests.patch(f"{SUPABASE_URL}/rest/v1/hhee_sync_logs?id=eq.{log_id}",
                       headers={**_sb_headers(), "Prefer": "return=minimal"},
                       json={"estado": estado,
                             "mensaje": (mensaje or "")[:500] or None,
                             "fin": datetime.now(timezone.utc).isoformat()},
                       timeout=10)
    except Exception:
        pass


# ── Runner de subprocesos ───────────────────────────────────────────────────
def correr(nombre: str, cmd: list[str], timeout_s: int = 900) -> dict:
    """Corre un script hijo. Retorna {ok, salida, error}."""
    print(f"\n{'='*70}\n[{nombre}] {' '.join(cmd)}\n{'='*70}", flush=True)
    inicio = datetime.now()
    try:
        # PYTHONIOENCODING=utf-8 para evitar cp1252 issues en Windows
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        res = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout_s, env=env)
        dur = (datetime.now() - inicio).total_seconds()
        # Stream output
        if res.stdout: print(res.stdout, flush=True)
        if res.stderr: print(res.stderr, file=sys.stderr, flush=True)
        return {
            "nombre":  nombre,
            "ok":      res.returncode == 0,
            "code":    res.returncode,
            "dur_s":   int(dur),
            "resumen": (res.stdout or "").splitlines()[-1][:200] if res.stdout else "",
            "error":   (res.stderr or "")[:500] if res.returncode else "",
        }
    except subprocess.TimeoutExpired:
        return {"nombre": nombre, "ok": False, "code": -1, "dur_s": timeout_s,
                "resumen": "", "error": f"Timeout tras {timeout_s}s"}
    except Exception as e:
        return {"nombre": nombre, "ok": False, "code": -1, "dur_s": 0,
                "resumen": "", "error": f"{type(e).__name__}: {e}"}


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=14,
                    help="Rango hacia atrás para el motor HHEE (default 14)")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Scripts a saltar: estaciones/buk/ctrlit/rastreosat/motor")
    args = ap.parse_args()

    skip = set((s.lower() for s in args.skip))
    log_id = log_start("hhee_daily_cron")
    inicio_all = datetime.now(TZ_CHILE)
    print(f"╔══════════════════════════════════════════════════════════════╗")
    print(f"║ HHEE Daily Cron — {inicio_all.strftime('%Y-%m-%d %H:%M')} CLT{' ' * 25}║")
    print(f"╚══════════════════════════════════════════════════════════════╝")

    py = sys.executable
    aquí = os.path.dirname(os.path.abspath(__file__))
    def _s(name):  # helper para path del script
        return os.path.join(aquí, name)

    # Rango para motor: últimos N días hasta hoy
    hoy = inicio_all.date()
    desde_iso = (hoy - timedelta(days=args.dias)).isoformat()
    hasta_iso = hoy.isoformat()

    pasos = []

    # 1) EDS nuevas
    if "estaciones" not in skip:
        pasos.append(correr("EDS nuevas", [py, _s("sync_estaciones_from_ots.py"), "--dias", "30"]))

    # 2) Nómina Buk RRHH
    if "buk" not in skip:
        pasos.append(correr("Buk RRHH", [py, _s("sync_buk_rrhh.py")]))

    # 3) Marcaciones ctrlit (día anterior)
    if "ctrlit" not in skip:
        ayer = (hoy - timedelta(days=1)).strftime("%d-%m-%Y")
        pasos.append(correr("ctrlit marcaciones",
                             [py, _s("sync_ctrlit.py"), "--fecha", ayer]))

    # 4) Rastreosat GPS (CSV en Drive)
    if "rastreosat" not in skip:
        pasos.append(correr("Rastreosat GPS", [py, _s("sync_rastreosat_drive.py")]))

    # 5) Motor HHEE (recalcula veredictos con data fresca)
    if "motor" not in skip:
        pasos.append(correr("Motor HHEE",
                             [py, _s("he_evaluator.py"),
                              "--desde", desde_iso, "--hasta", hasta_iso]))

    # Resumen
    dur_total = (datetime.now(TZ_CHILE) - inicio_all).total_seconds()
    ok_count = sum(1 for p in pasos if p["ok"])
    err_count = sum(1 for p in pasos if not p["ok"])

    print(f"\n{'='*70}")
    print(f"RESUMEN — {ok_count}/{len(pasos)} pasos OK, {err_count} errores. "
          f"Duración: {int(dur_total)}s\n")
    for p in pasos:
        emoji = "✅" if p["ok"] else "❌"
        print(f"  {emoji} {p['nombre']:<25} ({p['dur_s']:>4}s) "
              f"code={p['code']}  {p['resumen']}")
        if not p["ok"] and p["error"]:
            print(f"      ↳ {p['error'][:200]}")

    resumen_txt = " | ".join(f"{p['nombre']}={p['code']}" for p in pasos)
    estado_final = "success" if err_count == 0 else ("partial" if ok_count > 0 else "error")
    log_end(log_id, estado_final, resumen_txt)

    # Exit code: 0 si todo OK, 2 si hubo errores parciales, 1 si todo falló
    if err_count == 0: return 0
    if ok_count > 0: return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
