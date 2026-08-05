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


def mes_actual(hoy: date | None = None) -> date:
    hoy = hoy or date.today()
    return date(hoy.year, hoy.month, 1)


def periodo_label(p: date, hoy: date | None = None) -> str:
    base = f"{MESES_ES[p.month]} {p.year}"
    if p == mes_actual(hoy):
        base += " (en curso)"
    return base


def periodo_rango(p: date) -> tuple[date, date]:
    """(inicio inclusivo, fin exclusivo) del mes."""
    if p.month == 12:
        return p, date(p.year + 1, 1, 1)
    return p, date(p.year, p.month + 1, 1)


def periodos_disponibles(desde: date = date(2026, 1, 1),
                         hoy: date | None = None) -> list[date]:
    """Lista de primeros-de-mes desde `desde` hasta el mes en curso,
    orden descendente (mes en curso primero, luego cerrados)."""
    actual = mes_actual(hoy)
    out: list[date] = [actual]
    cur = ultimo_mes_cerrado(hoy)
    while cur >= desde:
        out.append(cur)
        cur = date(cur.year - 1, 12, 1) if cur.month == 1 else date(cur.year, cur.month - 1, 1)
    return out
