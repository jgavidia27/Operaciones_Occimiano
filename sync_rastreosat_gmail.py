"""
sync_rastreosat_gmail.py — Ingesta automática del reporte diario Rastreosat.
============================================================================

Rastreosat no expone API para el reporte "Viajes", pero puede programarse
para que se envíe cada día por correo a jgavidia@occimiano.cl con un link
firmado a un CSV en S3.

Este script:
  1) Conecta por IMAP a Gmail con la app password ya configurada
  2) Busca correos de noreply@reddsystem.com con asunto "Reporte_viajes"
     que NO tengan la etiqueta "Rastreosat/Procesado"
  3) Extrae el URL S3 del HTML, descarga el ZIP, extrae el CSV
  4) Delega el parseo/upsert a sync_rastreosat_drive.procesar_csv
  5) Etiqueta el correo como "Rastreosat/Procesado" (dedupe)

Ejecución:
    python sync_rastreosat_gmail.py                # procesa todos los pendientes
    python sync_rastreosat_gmail.py --dry-run      # descarga y parsea sin escribir
    python sync_rastreosat_gmail.py --limit 1      # solo el más reciente
"""

from __future__ import annotations

import argparse
import email
import imaplib
import io
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from typing import Optional

import requests


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

GMAIL_USER      = os.getenv("GMAIL_USER", "jgavidia@occimiano.cl")
GMAIL_PW        = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_LABEL_OK  = os.getenv("RASTREOSAT_LABEL", "Rastreosat/Procesado")
FROM_ADDR       = "noreply@reddsystem.com"
SUBJECT_MATCH   = "Reporte_viajes"


def log(msg: str, tag: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{tag}] " if tag else ""
    print(f"[{ts}] {prefix}{msg}", flush=True)


# ── IMAP helpers ────────────────────────────────────────────────────────────
def _imap_connect() -> imaplib.IMAP4_SSL:
    if not GMAIL_PW:
        raise RuntimeError("Falta GMAIL_APP_PASSWORD en .env / secrets.")
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    m.login(GMAIL_USER, GMAIL_PW)
    return m


def _decode(h: Optional[str]) -> str:
    if not h:
        return ""
    try:
        parts = decode_header(h)
        out = []
        for v, enc in parts:
            if isinstance(v, bytes):
                out.append(v.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(v)
        return "".join(out)
    except Exception:
        return str(h)


def _ensure_label(m: imaplib.IMAP4_SSL, label: str) -> None:
    """Crea la etiqueta Gmail si no existe (usa jerarquía con '/')."""
    typ, data = m.list()
    if typ != "OK":
        return
    labels = []
    for row in data:
        if not row:
            continue
        s = row.decode("utf-8", errors="ignore") if isinstance(row, bytes) else str(row)
        # formato: (\HasNoChildren) "/" "Nombre"
        m2 = re.search(r'"([^"]+)"$', s.strip())
        if m2:
            labels.append(m2.group(1))
    if label not in labels:
        m.create(f'"{label}"')


def _buscar_pendientes(m: imaplib.IMAP4_SSL, limit: Optional[int] = None) -> list[bytes]:
    """Devuelve UIDs de correos Rastreosat sin la etiqueta 'Procesado'."""
    m.select("INBOX")
    # Filtro Gmail X-GM-RAW: usa la sintaxis de búsqueda de Gmail (labels, etc.)
    query = (f'(X-GM-RAW "from:{FROM_ADDR} subject:{SUBJECT_MATCH} '
             f'-label:\\"{GMAIL_LABEL_OK}\\"")')
    typ, data = m.search(None, query)
    if typ != "OK" or not data or not data[0]:
        return []
    ids = data[0].split()
    if limit is not None:
        ids = ids[-limit:]
    return ids


def _extraer_url_s3(msg: email.message.Message) -> Optional[str]:
    """Extrae el URL firmado de S3 del HTML del correo."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="ignore"
            )
            m2 = re.search(r'href="(https://s3\.amazonaws\.com/reportes_rslite/[^"]+)"', html)
            if m2:
                return m2.group(1).replace("&amp;", "&")
    return None


def _descargar_y_extraer(url: str) -> Optional[Path]:
    """Descarga el ZIP y extrae el .csv que contiene. Devuelve el path del CSV."""
    r = requests.get(url, timeout=120, allow_redirects=True)
    if r.status_code != 200:
        log(f"HTTP {r.status_code} al descargar. Body: {r.text[:200]}", "ERR")
        return None
    if not r.content:
        log("Cuerpo vacío en la descarga.", "ERR")
        return None

    tmpdir = Path(tempfile.mkdtemp(prefix="rastreosat_"))
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        log("El archivo descargado no es ZIP.", "ERR")
        return None
    csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
    if not csvs:
        log(f"El ZIP no trae CSV. Contenido: {z.namelist()}", "ERR")
        return None
    csv_path = tmpdir / csvs[0]
    with z.open(csvs[0]) as src, open(csv_path, "wb") as dst:
        dst.write(src.read())
    log(f"CSV extraído: {csv_path.name}  ({csv_path.stat().st_size:,} bytes)")
    return csv_path


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Descarga y parsea, pero no escribe a Supabase ni etiqueta")
    ap.add_argument("--limit", type=int, default=None,
                    help="Procesar solo los últimos N correos")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    log("═══ SYNC Rastreosat Gmail → Supabase ═══")

    # Import diferido — reutilizamos el parser del sync de Drive
    from sync_rastreosat_drive import (
        get_patente_to_rut, procesar_csv, log_start, log_end,
        supabase_upsert,
    )

    log_id = log_start("sync_rastreosat_gmail") if not args.dry_run else None
    total_filas = 0
    total_correos = 0
    errores = []

    try:
        m = _imap_connect()
        log(f"OK login Gmail ({GMAIL_USER})")

        if not args.dry_run:
            _ensure_label(m, GMAIL_LABEL_OK)

        patente_to_rut = get_patente_to_rut()
        log(f"Mapa patente→RUT: {len(patente_to_rut)} vehículos")

        ids = _buscar_pendientes(m, limit=args.limit)
        log(f"Correos pendientes: {len(ids)}")
        if not ids:
            log("Nada por procesar.", "OK")
            if log_id:
                log_end(log_id, "ok", 0, "sin correos pendientes")
            m.logout()
            return 0

        for uid in ids:
            typ, msg_data = m.fetch(uid, "(RFC822)")
            if typ != "OK":
                errores.append(f"UID {uid!r}: fetch fallo")
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            fecha_correo = msg.get("Date", "?")
            log(f"─ Correo UID={uid.decode()} · {fecha_correo}")

            url = _extraer_url_s3(msg)
            if not url:
                log("  Sin URL S3. Salto.", "WARN")
                continue

            csv_path = _descargar_y_extraer(url)
            if csv_path is None:
                errores.append(f"UID {uid!r}: descarga/extracción falló")
                continue

            try:
                eventos = procesar_csv(str(csv_path), patente_to_rut)
                log(f"  → {len(eventos)} eventos generados")
                if not args.dry_run and eventos:
                    for i in range(0, len(eventos), 500):
                        batch = eventos[i:i + 500]
                        supabase_upsert("gps_eventos", batch)
                    log(f"  Upsert OK: {len(eventos)} eventos → gps_eventos")
                total_filas += len(eventos)
                total_correos += 1
            except Exception as e:
                errores.append(f"UID {uid!r}: parse error: {e}")
                log(f"  ERR parseando CSV: {e}", "ERR")
                continue

            # Etiquetar como procesado (solo en modo real)
            if not args.dry_run:
                try:
                    typ2, _ = m.store(uid, "+X-GM-LABELS", f'"{GMAIL_LABEL_OK}"')
                    if typ2 == "OK":
                        log(f"  Etiquetado como '{GMAIL_LABEL_OK}'")
                    else:
                        log(f"  ⚠ No se pudo etiquetar (typ={typ2})", "WARN")
                except Exception as e:
                    log(f"  ⚠ Error etiquetando: {e}", "WARN")

        m.logout()

        msg_final = (f"{total_correos} correos procesados · {total_filas} filas"
                     + (f" · {len(errores)} errores" if errores else ""))
        log(f"COMPLETADO. {msg_final}", "OK" if not errores else "WARN")
        if log_id:
            log_end(log_id, "ok" if not errores else "warn",
                    total_filas, msg_final + (" | " + "; ".join(errores[:3]) if errores else ""))
        return 0 if not errores else 2

    except Exception as e:
        import traceback
        tb = traceback.format_exc()[:600]
        log(f"FATAL: {e}\n{tb}", "ERR")
        if log_id:
            log_end(log_id, "error", total_filas, f"{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
