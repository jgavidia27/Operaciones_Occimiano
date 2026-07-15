"""
send_weekly_indicadores.py — Envía Resumen mensual por email a cada senior.
============================================================================

Corre desde GitHub Actions cada Lunes a las 11:00 CLT.

Para cada uno de los 4 seniors (Juan Gallardo, Luis Pinto, Victor Bahamonde,
Carlos Avila) genera un Excel del mes ACTUAL con sus 3 indicadores
principales (Desempeño SLA + Efectividad MP + Precisión Fracttal) filtrado
por su equipo, y lo envía por email vía Gmail SMTP.

CC: jcaceres@occimiano.cl + wsoto@occimiano.cl en todos los correos.

Env vars requeridas (GitHub Secrets):
    GMAIL_APP_PASSWORD    — App password Gmail 16 chars (sin espacios)
    SUPABASE_URL, SUPABASE_KEY, FRACTTAL_CLIENT_ID, FRACTTAL_CLIENT_SECRET,
    BUK_API_TOKEN (para reutilizar loaders del proyecto)

Ejecución:
    python send_weekly_indicadores.py                # mes actual
    python send_weekly_indicadores.py --mes 2026-07  # mes específico
    python send_weekly_indicadores.py --dry-run      # no envía, solo genera
    python send_weekly_indicadores.py --solo Luis    # solo un senior
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional


# ── Cargar .env ─────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

GMAIL_SENDER   = "jgavidia@occimiano.cl"
GMAIL_DISPLAY  = "Operaciones Occimiano"
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

CC_FIJOS = ["jcaceres@occimiano.cl", "wsoto@occimiano.cl"]

# Configuración por senior (email → equipo(s) y label)
SENIORS = [
    {
        "nombre":       "Juan Gallardo",
        "email":        "jgallardo@occimiano.cl",
        "equipo_label": "Juan Gallardo",
    },
    {
        "nombre":       "Luis Pinto",
        "email":        "lpinto@occimiano.cl",
        "equipo_label": "Luis Pinto",
    },
    {
        "nombre":       "Victor Bahamonde",
        "email":        "vbahamonde@occimiano.cl",
        "equipo_label": "Victor Bahamonde",
    },
    # Carlos Avila lidera Norte + Sur; enviamos 2 correos (uno por equipo).
    {
        "nombre":       "Carlos Avila",
        "email":        "cavila@occimiano.cl",
        "equipo_label": "Carlos Avila Norte",
    },
    {
        "nombre":       "Carlos Avila",
        "email":        "cavila@occimiano.cl",
        "equipo_label": "Carlos Avila Sur",
    },
]

MESES_ES_LARGO = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre",
    11: "noviembre", 12: "diciembre",
}


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── SMTP ────────────────────────────────────────────────────────────────────
def enviar_email(destinatario: str, asunto: str, cuerpo_html: str,
                  attachment_bytes: bytes, attachment_filename: str,
                  cc: Optional[list[str]] = None,
                  dry_run: bool = False) -> None:
    """Envía email vía Gmail SMTP con adjunto Excel."""
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"]    = f"{GMAIL_DISPLAY} <{GMAIL_SENDER}>"
    msg["To"]      = destinatario
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content("Este correo requiere un cliente compatible con HTML.")
    msg.add_alternative(cuerpo_html, subtype="html")
    msg.add_attachment(attachment_bytes,
                       maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=attachment_filename)

    if dry_run:
        _log(f"  [DRY-RUN] Skip envío a {destinatario} (cc: {cc or []}). "
             f"Adjunto '{attachment_filename}' de {len(attachment_bytes)//1024} KB.")
        return

    if not GMAIL_PASSWORD:
        raise RuntimeError("Falta GMAIL_APP_PASSWORD en env vars.")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_SENDER, GMAIL_PASSWORD)
        # send_message toma msg["To"] y msg["Cc"] automáticamente
        smtp.send_message(msg)
    _log(f"  ✅ Enviado a {destinatario} (cc: {cc or []})")


# ── Cuerpo del email (HTML) ─────────────────────────────────────────────────
def render_email_html(senior_nombre: str, mes_label: str, equipo_label: str) -> str:
    """HTML del email. Basado en el formato original de Jesús."""
    return f"""\
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#1f2937;max-width:680px;line-height:1.55;">
<p>Hola {senior_nombre}, buenos días.</p>

<p>Te comparto el <strong>resumen semanal de indicadores</strong> del equipo
<strong>{equipo_label}</strong> correspondiente a <strong>{mes_label}</strong>. La información
está extraída directamente del dashboard, de forma que refleja fielmente
los datos de tu equipo.</p>

<p>El archivo adjunto contiene:</p>
<ul>
  <li><strong>Resumen</strong> — Cumplimiento SLA, Efectividad MP y Precisión Fracttal por técnico.</li>
  <li><strong>SLA</strong> — Detalle de cada llamado atendido con horas de resolución.</li>
  <li><strong>Efectividad MP</strong> — Fallas post-preventiva del equipo.</li>
  <li><strong>P. Fracttal</strong> — Detalle completo de OTs evaluadas (tiempo, causa raíz, numeral).</li>
</ul>

<p>Esto se enviará automáticamente todos los lunes con el avance mensual acumulado.
Si algo no te hace sentido o detectas alguna inconsistencia, cualquier observación o
ajuste que quieras hacer es bienvenida — así podemos ajustar el formato y
mantener un seguimiento cercano de tu expertise.</p>

<p>Saludos,</p>
<p style="margin-bottom:0"><strong>Operaciones Occimiano</strong><br>
<a href="https://ops-occimiano-dashboard.streamlit.app/" style="color:#2563eb;">Dashboard de Operaciones</a></p>
</body></html>"""


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--mes", help="YYYY-MM (default: mes actual CLT)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Genera Excels pero NO envía emails")
    ap.add_argument("--solo", help="Filtrar por nombre senior (subcadena)")
    args = ap.parse_args()

    # Mes objetivo
    if args.mes:
        mes_yyyy_mm = args.mes
    else:
        from datetime import timedelta
        _now_chile = datetime.now(timezone.utc) - timedelta(hours=4)
        mes_yyyy_mm = _now_chile.strftime("%Y-%m")
    _year, _mes_num = int(mes_yyyy_mm.split("-")[0]), int(mes_yyyy_mm.split("-")[1])
    mes_label = f"{MESES_ES_LARGO[_mes_num].capitalize()} {_year}"

    _log(f"═══ ENVÍO SEMANAL INDICADORES — {mes_label} ═══")

    # ── Cargar data (imports pesados solo cuando ejecutamos) ──
    _log("Cargando data (df_wo, df_llamados, df_eds, scores)...")
    from cron_data_loader import load_dashboard_data
    data = load_dashboard_data()

    # ── Construir helpers/mapas ──
    _log("Construyendo helpers y mapas...")
    from cron_helpers import build_context
    ctx = build_context(data)

    # ── Importar el generador Excel + funciones necesarias del proyecto ──
    from excel_reports import build_excel_resumen
    from data import build_reincidencias, score_llenado_por_tecnico, aplicar_transferencias
    from supabase_client import load_cotalker_index_supabase

    seniors_a_procesar = SENIORS
    if args.solo:
        f = args.solo.lower()
        seniors_a_procesar = [s for s in SENIORS if f in s["nombre"].lower()]
        if not seniors_a_procesar:
            _log(f"ERROR: No hay senior que matchee '{args.solo}'")
            return 1

    ok, err = 0, 0
    for s in seniors_a_procesar:
        nombre = s["nombre"]
        email = s["email"]
        eq_lbl = s["equipo_label"]
        _log(f"\n→ Procesando {nombre} · equipo {eq_lbl}")
        try:
            xlsx_bytes = build_excel_resumen(
                dl_mes=mes_yyyy_mm,
                dl_quien=nombre,
                equipo_label=eq_lbl,
                tec_sel="Todos",
                sem_match=None,
                df_wo=data["df_wo"],
                df_llamados=data["df_llamados"],
                df_eds=data["df_eds"],
                df_ot_scores=data["df_ot_scores"],
                excel_to_full=ctx["excel_to_full"],
                label_to_grupo=ctx["label_to_grupo"],
                equipo_label_map=ctx["equipo_label_map"],
                numeral_motivo_label=ctx["numeral_motivo_label"],
                es_excluido_fn=ctx["es_excluido"],
                get_equipo_fn=ctx["get_equipo"],
                norm_n_fn=ctx["norm_n"],
                strip_headers_fn=ctx["strip_headers"],
                build_eds_nombre_map_fn=ctx["build_eds_nombre_map"],
                build_reincidencias_fn=build_reincidencias,
                score_llenado_por_tecnico_fn=score_llenado_por_tecnico,
                aplicar_transferencias_fn=aplicar_transferencias,
                load_cotalker_index_fn=load_cotalker_index_supabase,
            )
            _log(f"  Excel generado: {len(xlsx_bytes)//1024} KB")

            eq_slug = eq_lbl.replace(" ", "_").replace("(", "").replace(")", "")
            filename = f"Resumen_{eq_slug}_{mes_yyyy_mm}.xlsx"
            asunto = f"Resumen semanal indicadores ({MESES_ES_LARGO[_mes_num].capitalize()}). Eq. {nombre}"
            cuerpo = render_email_html(nombre, mes_label, eq_lbl)

            # En dry-run guardar los Excel a disco para poder verificarlos
            if args.dry_run:
                _dry_dir = Path(__file__).parent / "_dry_run_excels"
                _dry_dir.mkdir(exist_ok=True)
                _dry_path = _dry_dir / filename
                _dry_path.write_bytes(xlsx_bytes)
                _log(f"  💾 Guardado local: {_dry_path}")

            enviar_email(email, asunto, cuerpo, xlsx_bytes, filename,
                          cc=CC_FIJOS, dry_run=args.dry_run)
            ok += 1
        except Exception as e:
            tb = traceback.format_exc()
            _log(f"  ❌ ERROR: {e}")
            _log(tb[:600])
            err += 1

    _log(f"\n═══ RESUMEN: {ok} OK, {err} errores ═══")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
