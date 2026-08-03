"""
periodo.py — Helpers de mes cerrado para Análisis STO Occim.

Un "periodo" es siempre el primer día del mes (date(YYYY, MM, 1)).
El "último mes cerrado" es el mes anterior al actual: si hoy es 2026-08-03,
el último cerrado es 2026-07-01 (julio 2026).
"""
from datetime import date

MESES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def ultimo_mes_cerrado(hoy: date | None = None) -> date:
    hoy = hoy or date.today()
    if hoy.month == 1:
        return date(hoy.year - 1, 12, 1)
    return date(hoy.year, hoy.month - 1, 1)


def periodo_label(p: date) -> str:
    return f"{MESES_ES[p.month]} {p.year}"


def periodo_rango(p: date) -> tuple[date, date]:
    """(inicio inclusivo, fin exclusivo) del mes."""
    if p.month == 12:
        return p, date(p.year + 1, 1, 1)
    return p, date(p.year, p.month + 1, 1)


def periodos_disponibles(desde: date = date(2026, 1, 1),
                         hoy: date | None = None) -> list[date]:
    """Lista de primeros-de-mes desde `desde` hasta el último mes cerrado,
    orden descendente (más reciente primero)."""
    ult = ultimo_mes_cerrado(hoy)
    out: list[date] = []
    cur = ult
    while cur >= desde:
        out.append(cur)
        cur = date(cur.year - 1, 12, 1) if cur.month == 1 else date(cur.year, cur.month - 1, 1)
    return out
