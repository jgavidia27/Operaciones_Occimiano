"""
send_weekly_reincidencias_eds.py — Envío semanal (Miércoles 11 AM CLT).
=======================================================================

UN SOLO correo con el ranking EDS mes-a-la-fecha que superan 3 correctivos
en el mes en curso, + detalle de OTs por cada EDS. Va a los 4 seniors +
jcaceres + wsoto (todos como destinatarios directos).

Saludo genérico "Buenos días equipo".

Ejecución:
    python send_weekly_reincidencias_eds.py                       # envío real
    python send_weekly_reincidencias_eds.py --dry-run             # solo genera
    python send_weekly_reincidencias_eds.py --test-email X@Y.cl   # redirige
    python send_weekly_reincidencias_eds.py --mes 2026-07         # mes específico
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
import traceback
from datetime import datetime, timezone, timedelta
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

# Destinatarios FIJOS del correo grupal (todos como TO)
DESTINATARIOS = [
    "cavila@occimiano.cl",       # Carlos Avila
    "vbahamonde@occimiano.cl",   # Victor Bahamonde
    "jgallardo@occimiano.cl",    # Juan Gallardo
    "lpinto@occimiano.cl",       # Luis Pinto
    "jcaceres@occimiano.cl",     # Jesus Caceres
    "wsoto@occimiano.cl",        # Wilson Soto
]

MESES_ES_LARGO = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
                  "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── Envío ──────────────────────────────────────────────────────────────────
def enviar_email(destinatarios: list[str], asunto: str, cuerpo_html: str,
                 xlsx_bytes: bytes, xlsx_filename: str,
                 dry_run: bool = False):
    if dry_run:
        _log(f"  [DRY-RUN] TO: {destinatarios}  asunto: {asunto}")
        _log(f"  [DRY-RUN] adjunto: {xlsx_filename} ({len(xlsx_bytes)//1024} KB)")
        return

    if not GMAIL_PASSWORD:
        raise RuntimeError("Falta GMAIL_APP_PASSWORD en .env / Secrets.")

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = f"{GMAIL_DISPLAY} <{GMAIL_SENDER}>"
    msg["To"] = ", ".join(destinatarios)
    msg.set_content("Este correo contiene HTML. Actualiza tu cliente para verlo.")
    msg.add_alternative(cuerpo_html, subtype="html")
    msg.add_attachment(
        xlsx_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=xlsx_filename,
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_SENDER, GMAIL_PASSWORD)
        s.send_message(msg)

    _log(f"  ✅ Enviado a {destinatarios}")


# ── Cuerpo HTML ─────────────────────────────────────────────────────────────
def render_email_html(mes_label: str, sem_iso: int, stats: dict) -> str:
    top_html = ""
    if stats["top_eds"]:
        rows = "".join(
            f'<tr><td style="padding:4px 10px;border:1px solid #d1d5db;">{cod}</td>'
            f'<td style="padding:4px 10px;border:1px solid #d1d5db;">{nombre}</td>'
            f'<td style="padding:4px 10px;border:1px solid #d1d5db;text-align:center;'
            f'font-weight:bold;color:#b91c1c;">{n}</td></tr>'
            for cod, nombre, n in stats["top_eds"]
        )
        top_html = f"""
<p><strong>Top 5 EDS con más correctivos {mes_label}:</strong></p>
<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;margin-bottom:14px;">
<thead>
<tr style="background:#1F4E78;color:white;">
  <th style="padding:6px 10px;border:1px solid #1F4E78;">Cód. Occim</th>
  <th style="padding:6px 10px;border:1px solid #1F4E78;">EDS</th>
  <th style="padding:6px 10px;border:1px solid #1F4E78;">Llamados</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
"""

    return f"""\
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#1f2937;max-width:720px;line-height:1.55;">
<p>Buenos días equipo,</p>

<p>Se adjunta el <strong>resumen de reincidencias por EDS</strong> del mes
<strong>{mes_label}</strong>. <em>(Actualizado semana {sem_iso})</em></p>

<p>El archivo contiene todas las EDS que acumulan <strong>3 o más correctivos</strong>
en lo que va del mes, más el detalle de cada OT asociada.</p>

<ul>
  <li><strong>{stats['eds_reincidentes']}</strong> EDS reincidentes (≥3 correctivos)</li>
  <li><strong>{stats['ots_totales']}</strong> OTs correctivas asociadas</li>
</ul>

{top_html}

<p>El archivo adjunto contiene dos hojas:</p>
<ul>
  <li><strong>Ranking EDS</strong> — Cód. Occim, nombre, cliente, comuna, N° llamados,
      % cumplimiento SLA, último llamado y último técnico.</li>
  <li><strong>Detalle OTs</strong> — Todas las OTs correctivas de esas EDS, con
      OS Fracttal, N° Aviso, fecha llamado, fecha atención, técnico y prioridad.</li>
</ul>

<p>Cualquier observación o inconsistencia que detecten la pueden realizar para
validarlo en interno en el transcurso de la semana.</p>

<p>Saludos.</p>
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
    ap.add_argument("--mes", default=None, help="YYYY-MM (default: mes actual Chile)")
    ap.add_argument("--dry-run", action="store_true", help="No envía; solo genera Excel")
    ap.add_argument("--test-email", default=None,
                    help="Redirige el correo a este email (para pruebas)")
    args = ap.parse_args()

    # Mes objetivo + semana ISO (siempre calculada desde HOY en Chile)
    _now_chile = datetime.now(timezone.utc) - timedelta(hours=4)
    if args.mes:
        mes_yyyy_mm = args.mes
    else:
        mes_yyyy_mm = _now_chile.strftime("%Y-%m")
    _year, _mes_num = int(mes_yyyy_mm.split("-")[0]), int(mes_yyyy_mm.split("-")[1])
    mes_label = f"{MESES_ES_LARGO[_mes_num].capitalize()} {_year}"
    sem_iso = _now_chile.isocalendar().week

    _log(f"═══ REINCIDENCIAS EDS — {mes_label} (Sem. {sem_iso}) ═══")

    # ── Cargar data ──
    _log("Cargando df_llamados desde Supabase...")
    from cron_data_loader import load_dashboard_data
    data = load_dashboard_data()
    df_llamados = data["df_llamados"]
    _log(f"  {len(df_llamados)} llamados totales")

    # ── Generar Excel ──
    _log("Generando Excel de reincidencias...")
    from excel_reincidencias_eds import build_excel_reincidencias
    try:
        xlsx_bytes, stats = build_excel_reincidencias(df_llamados, mes_yyyy_mm)
    except ValueError as e:
        _log(f"❌ ERROR: {e}")
        return 1
    _log(f"  Excel: {len(xlsx_bytes)//1024} KB — "
         f"{stats['eds_reincidentes']} EDS reincidentes, {stats['ots_totales']} OTs")

    if stats["eds_reincidentes"] == 0:
        _log("⚠️  No hay EDS reincidentes este mes — no se envía correo.")
        return 0

    filename = f"Resumen_Reincidencias_EDS_{mes_yyyy_mm} Sem. {sem_iso}.xlsx"
    asunto = f"Resumen semanal reincidencias EDS ({MESES_ES_LARGO[_mes_num].capitalize()} — Sem. {sem_iso})"
    cuerpo = render_email_html(mes_label, sem_iso, stats)

    # Dry-run: guardar copia local
    if args.dry_run:
        _dry_dir = Path(__file__).parent / "_dry_run_excels"
        _dry_dir.mkdir(exist_ok=True)
        _dry_path = _dry_dir / filename
        _dry_path.write_bytes(xlsx_bytes)
        _log(f"  💾 Guardado local: {_dry_path}")

    # Modo prueba: redirigir a UN solo destinatario
    _to = DESTINATARIOS
    _asunto = asunto
    _cuerpo = cuerpo
    if args.test_email:
        _to = [args.test_email]
        _asunto = f"[PRUEBA] {asunto}  →  original: {', '.join(DESTINATARIOS)}"
        _cuerpo = (
            f'<div style="background:#fef3c7;border-left:4px solid #f59e0b;'
            f'padding:10px 14px;margin-bottom:14px;color:#78350f;'
            f'font-family:Arial,sans-serif;font-size:14px;">'
            f'<b>⚠️ MODO PRUEBA</b><br>'
            f'En envío real este correo iría a: <b>{", ".join(DESTINATARIOS)}</b>'
            f'</div>' + cuerpo
        )
        _log(f"  🔀 Redirigido a {args.test_email}")

    try:
        enviar_email(_to, _asunto, _cuerpo, xlsx_bytes, filename, dry_run=args.dry_run)
    except Exception as e:
        _log(f"❌ ERROR envío: {e}")
        _log(traceback.format_exc()[:600])
        return 1

    _log("═══ DONE ═══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
