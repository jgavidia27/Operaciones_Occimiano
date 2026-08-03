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
        # Cuadro simple: Cód. Occim, EDS, Llamados. El detalle de origen/tipo
        # se muestra ya en el desglose por EDS más abajo.
        rows_html = ""
        for t in stats["top_eds"]:
            cod, nombre, n = t[0], t[1], t[2]
            rows_html += (
                f'<tr>'
                f'<td style="padding:4px 10px;border:1px solid #d1d5db;">{cod}</td>'
                f'<td style="padding:4px 10px;border:1px solid #d1d5db;">{nombre}</td>'
                f'<td style="padding:4px 10px;border:1px solid #d1d5db;text-align:center;'
                f'font-weight:bold;color:#b91c1c;">{n}</td>'
                f'</tr>'
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
<tbody>{rows_html}</tbody>
</table>
"""

    # ── Desglose OT-por-OT de cada EDS del Top 5 ──
    desglose_html = ""
    desglose = stats.get("desglose_por_eds") or {}
    top_ids = [t[0] for t in stats.get("top_eds", [])]
    # Nombres (para el título de cada tarjeta) desde top_eds
    nombre_map = {t[0]: t[1] for t in stats.get("top_eds", [])}
    for eds in top_ids:
        ots = desglose.get(eds) or []
        if not ots:
            continue
        _rows = ""
        # Detección de valores mal llenados por el técnico → resaltar en rojo
        def _color_mal(v: str) -> str:
            up = (v or "").upper()
            if "SIN CLASIFICAR" in up or "SIN INFORMACION" in up or "SIN INFORMACIÓN" in up:
                return "#dc2626"   # rojo alerta
            return "#0f172a"

        for i, ot in enumerate(ots, 1):
            fecha = ot["fecha_llam"] or "—"
            atn   = ot["fecha_atn"] or "—"
            tec   = ot["tecnico"] or "—"
            pri   = ot["prioridad"] or "—"
            org   = ot["origen"] or "—"
            tip   = ot["tipo"] or "—"
            obs   = (ot["obs"] or "—").replace("\r\n", " · ").replace("\n", " · ")
            c_org = _color_mal(org); c_tip = _color_mal(tip)
            # font-weight rojo cuando es mal-llenado
            w_org = "700" if c_org == "#dc2626" else "400"
            w_tip = "700" if c_tip == "#dc2626" else "400"
            _rows += (
                f'<tr>'
                f'<td style="padding:6px 6px;border:1px solid #e5e7eb;'
                f'color:#64748b;font-size:11px;text-align:center;'
                f'vertical-align:top;">#{i}</td>'
                f'<td style="padding:6px 8px;border:1px solid #e5e7eb;'
                f'font-weight:600;vertical-align:top;">{ot["os"]}</td>'
                f'<td style="padding:6px 6px;border:1px solid #e5e7eb;color:#334155;'
                f'font-size:11px;text-align:center;vertical-align:top;">{fecha}</td>'
                f'<td style="padding:6px 6px;border:1px solid #e5e7eb;color:#334155;'
                f'font-size:11px;text-align:center;vertical-align:top;">{atn}</td>'
                f'<td style="padding:6px 6px;border:1px solid #e5e7eb;color:#334155;'
                f'font-size:11px;text-align:center;vertical-align:top;">{pri}</td>'
                f'<td style="padding:6px 8px;border:1px solid #e5e7eb;color:#334155;'
                f'font-size:11px;vertical-align:top;">{tec}</td>'
                f'<td style="padding:6px 8px;border:1px solid #e5e7eb;background:#fefce8;'
                f'font-size:11px;color:{c_org};font-weight:{w_org};vertical-align:top;">{org}</td>'
                f'<td style="padding:6px 8px;border:1px solid #e5e7eb;background:#fefce8;'
                f'font-size:11px;color:{c_tip};font-weight:{w_tip};vertical-align:top;">{tip}</td>'
                f'<td style="padding:6px 10px;border:1px solid #e5e7eb;color:#0f172a;'
                f'font-size:12px;line-height:1.5;vertical-align:top;">{obs}</td>'
                f'</tr>'
            )
        _title_edss = nombre_map.get(eds, "")
        # Nuevos anchos: Observación 50%, resto ajustado.
        # Sin white-space:nowrap → el texto envuelve naturalmente.
        desglose_html += f"""
<h4 style="margin:18px 0 6px 0;color:#1F4E78;font-family:Arial,sans-serif;">
  📍 {eds} — {_title_edss}
  <span style="color:#dc2626;">({len(ots)} llamados)</span></h4>
<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px;
              margin-bottom:12px;width:100%;table-layout:fixed;">
<colgroup>
  <col style="width:3%">   <!-- # -->
  <col style="width:7%">   <!-- OS Fracttal -->
  <col style="width:5%">   <!-- F. llamado -->
  <col style="width:5%">   <!-- F. atención -->
  <col style="width:4%">   <!-- Prioridad -->
  <col style="width:7%">   <!-- Técnico -->
  <col style="width:8%">   <!-- Origen falla -->
  <col style="width:6%">   <!-- Tipo falla -->
  <col style="width:55%">  <!-- Observación (más de la mitad) -->
</colgroup>
<thead>
<tr style="background:#e5e7eb;color:#1f2937;">
  <th style="padding:6px 6px;border:1px solid #d1d5db;">#</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;">OS Fracttal</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;">F. llamado</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;">F. atención</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;">Prioridad</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;">Técnico</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;background:#fbbf24;">Origen falla</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;background:#fbbf24;">Tipo falla</th>
  <th style="padding:6px 6px;border:1px solid #d1d5db;">Observación del técnico</th>
</tr>
</thead>
<tbody>{_rows}</tbody>
</table>
"""
    if desglose_html:
        top_html += f"""
<p style="margin-top:18px;"><strong>Desglose por EDS (Top 5):</strong> —
qué pasó en cada llamado, causa/tipo y observaciones del técnico.</p>
{desglose_html}
"""

    return f"""\
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#1f2937;max-width:1100px;line-height:1.55;">
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

    # ── Gate por día: este correo solo se envía los LUNES ──────────────
    # El workflow corre a diario (los crons semanales de GitHub Actions
    # saltan disparos con frecuencia — best-effort, no garantía).
    # weekday(): 0=Lun, ..., 6=Dom
    if not args.dry_run and not args.test_email and _now_chile.weekday() != 0:
        _log(f"═══ SKIP: hoy es {_now_chile.strftime('%A')}, "
             "el correo se envía solo los lunes ═══")
        return 0

    # ── Dedupe idempotente (cron diario + múltiples horas) ──
    from email_dedupe import ya_enviado_hoy, marcar_enviado_hoy
    if not args.dry_run and not args.test_email and ya_enviado_hoy("weekly_reincidencias"):
        _log("═══ SKIP: ya se envió el resumen de hoy (dedupe) ═══")
        return 0

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

    if not args.dry_run and not args.test_email:
        marcar_enviado_hoy("weekly_reincidencias",
                           f"Resumen semanal reincidencias EDS — {mes_label} Sem. {sem_iso}")
    _log("═══ DONE ═══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
