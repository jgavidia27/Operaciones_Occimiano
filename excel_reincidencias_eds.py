"""
excel_reincidencias_eds.py — Genera Excel de reincidencias EDS del mes.

Replica el formato del archivo modelo:
    HHEE semanal - GPS / Resumen reincidencias por EDS (3).xlsx

- Hoja "Ranking EDS": una fila por EDS con >=3 correctivos MTD.
    Columnas: Cód. Occim | Nombre / Dirección | Cliente | Comuna |
              Llamados | % Cumpl. SLA | Último Llamado | Último Técnico
- Hoja "Detalle OTs": todas las OTs correctivas de esas EDS.
    Columnas: OS Fracttal | N° Aviso | Fecha llamado | Fecha atención |
              Cód. EDS | EDS | Cliente | Técnico | Prioridad
"""

from __future__ import annotations

import io
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


UMBRAL_REINCIDENCIA = 3   # EDS con "Llamados" >= 3 aparecen en el resumen


# ── Estilos ──────────────────────────────────────────────────────────────
_HDR_FILL   = PatternFill("solid", fgColor="1F4E78")
_HDR_FONT   = Font(name="Arial", size=11, bold=True, color="FFFFFF")
_HDR_ALIGN  = Alignment(horizontal="center", vertical="center", wrap_text=True)
_CELL_FONT  = Font(name="Arial", size=10)
_CELL_ALIGN = Alignment(vertical="center")
_ALERT_FILL = PatternFill("solid", fgColor="FFF3CD")  # amarillo suave (>=5 llamados)
_BORDER_THIN = Border(
    left=Side(style="thin",  color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin",   color="D9D9D9"),
    bottom=Side(style="thin",color="D9D9D9"),
)


def _clean_str(v) -> str:
    """Convierte a str seguro, sin 'nan'/'None'/'NaT'."""
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    if s.lower() in {"nan", "none", "nat", "<na>"}:
        return ""
    return s


def _fecha_str(v) -> str:
    """Formato yyyy-mm-dd, o '' si nulo."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return ""
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _write_headers(ws, headers: list[str], widths: list[int]):
    for c, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = _HDR_ALIGN
        cell.border = _BORDER_THIN
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "A2"


def _write_row(ws, row_idx: int, values: list, alert: bool = False):
    for c, v in enumerate(values, start=1):
        cell = ws.cell(row=row_idx, column=c, value=v)
        cell.font = _CELL_FONT
        cell.alignment = _CELL_ALIGN
        cell.border = _BORDER_THIN
        if alert:
            cell.fill = _ALERT_FILL


def build_excel_reincidencias(
    df_llamados: pd.DataFrame,
    mes_yyyy_mm: str,
    umbral: int = UMBRAL_REINCIDENCIA,
) -> tuple[bytes, dict]:
    """
    Genera el Excel de reincidencias del mes indicado.

    Retorna (bytes_xlsx, stats) donde stats = {
        "eds_reincidentes":  int,
        "ots_totales":       int,
        "top_eds":           list[(eds_cod, eds_nombre, count)]  # top 5 para el correo
    }
    """
    if df_llamados is None or df_llamados.empty:
        raise ValueError("df_llamados está vacío — no hay datos para generar el reporte.")

    # ── Filtrar mes objetivo ──
    df = df_llamados.copy()
    df["fecha_llamado"] = pd.to_datetime(df["fecha_llamado"], errors="coerce")
    df = df[df["fecha_llamado"].dt.strftime("%Y-%m") == mes_yyyy_mm]

    if df.empty:
        # Excel vacío pero válido (con encabezados y un aviso)
        wb = Workbook()
        ws = wb.active
        ws.title = "Ranking EDS"
        ws["A1"] = f"Sin datos para el mes {mes_yyyy_mm}."
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), {"eds_reincidentes": 0, "ots_totales": 0, "top_eds": []}

    # ── Agrupar por EDS ──
    df["eds_occim"] = df["eds_occim"].fillna("").astype(str)
    df["eds_nombre"] = df["eds_nombre"].fillna("").astype(str)

    grp = df.groupby("eds_occim", dropna=False)
    ranking_rows = []
    for eds_cod, gg in grp:
        if not eds_cod:
            continue
        n_llamados = len(gg)
        if n_llamados < umbral:
            continue
        # % Cumplimiento SLA: 'CUMPLE' y 'EXCEPCION' cuentan como OK. Ignora 'SIN DATOS'.
        cumpl_col = gg.get("cumplimiento", pd.Series([], dtype=str)).fillna("").astype(str)
        cumpl_up = cumpl_col.str.strip().str.upper()
        ok_mask = cumpl_up.isin({"CUMPLE", "EXCEPCION", "EXCEPCIÓN"})
        eval_mask = cumpl_up.isin({"CUMPLE", "NO CUMPLE", "EXCEPCION", "EXCEPCIÓN"})
        n_eval = int(eval_mask.sum())
        pct_sla = round(100.0 * ok_mask.sum() / n_eval, 1) if n_eval else 0.0
        # Último llamado y último técnico
        ult = gg.sort_values("fecha_llamado", ascending=False).iloc[0]
        ranking_rows.append({
            "cod":      _clean_str(eds_cod),
            "nombre":   _clean_str(ult.get("eds_nombre")),
            "cliente":  _clean_str(ult.get("cliente")),
            "comuna":   _clean_str(ult.get("comuna")),
            "llamados": n_llamados,
            "pct_sla":  pct_sla,
            "ultimo":   _fecha_str(ult.get("fecha_llamado")),
            "tecnico":  _clean_str(ult.get("tecnico")),
        })

    # Ordenar por llamados desc, luego por último llamado desc
    ranking_rows.sort(key=lambda r: (-r["llamados"], r["ultimo"]), reverse=False)
    ranking_rows.sort(key=lambda r: (-r["llamados"]))

    eds_reincidentes_codes = {r["cod"] for r in ranking_rows}

    # ── Detalle de OTs (solo de EDS reincidentes) ──
    df_det = df[df["eds_occim"].isin(eds_reincidentes_codes)].copy()
    df_det = df_det.sort_values(["eds_occim", "fecha_llamado"], ascending=[True, True])

    # ── Escribir Excel ──
    wb = Workbook()

    # Hoja 1: Ranking EDS
    ws1 = wb.active
    ws1.title = "Ranking EDS"
    headers1 = ["Cód. Occim", "Nombre / Dirección", "Cliente", "Comuna",
                "Llamados", "% Cumpl. SLA", "Último Llamado", "Último Técnico"]
    widths1 = [14, 45, 16, 20, 10, 14, 15, 32]
    _write_headers(ws1, headers1, widths1)

    for i, r in enumerate(ranking_rows, start=2):
        alert = r["llamados"] >= 5   # amarillo suave para casos críticos
        _write_row(ws1, i, [
            r["cod"], r["nombre"], r["cliente"], r["comuna"],
            r["llamados"], r["pct_sla"], r["ultimo"], r["tecnico"],
        ], alert=alert)
        # Formato numérico columna E (Llamados) y F (% SLA)
        ws1.cell(row=i, column=5).number_format = "0"
        ws1.cell(row=i, column=6).number_format = '0.0"%"'

    # Hoja 2: Detalle OTs
    ws2 = wb.create_sheet("Detalle OTs")
    headers2 = ["OS Fracttal", "N° Aviso", "Fecha llamado", "Fecha atención",
                "Cód. EDS", "EDS", "Cliente", "Técnico", "Prioridad"]
    widths2 = [14, 15, 15, 15, 14, 45, 16, 32, 10]
    _write_headers(ws2, headers2, widths2)

    for i, (_, r) in enumerate(df_det.iterrows(), start=2):
        _write_row(ws2, i, [
            _clean_str(r.get("os_fracttal")),
            _clean_str(r.get("n_llamado")),
            _fecha_str(r.get("fecha_llamado")),
            _fecha_str(r.get("fecha_atencion")),
            _clean_str(r.get("eds_occim")),
            _clean_str(r.get("eds_nombre")),
            _clean_str(r.get("cliente")),
            _clean_str(r.get("tecnico")),
            _clean_str(r.get("prioridad")),
        ])

    buf = io.BytesIO()
    wb.save(buf)

    top_eds = [(r["cod"], r["nombre"], r["llamados"]) for r in ranking_rows[:5]]
    stats = {
        "eds_reincidentes": len(ranking_rows),
        "ots_totales":      len(df_det),
        "top_eds":          top_eds,
    }
    return buf.getvalue(), stats
