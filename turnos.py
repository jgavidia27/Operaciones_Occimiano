"""
Turnos STO — quién está de turno cada día.

"De turno" significa **atendiendo correctivas**: ese técnico no está disponible
para mantenciones preventivas. Por eso el planificador de MP necesita esta
lectura — la capacidad real de MP de una semana es el roster del equipo MENOS
los que están de turno.

Fuente: turnos_data.json (lo que publica Planificación Turnos STO). El archivo
sólo cubre las semanas ya cargadas; hacia adelante se proyecta el ciclo de
rotación de 3 equipos (Gallardo → Pinto → Bahamonde), igual que hace la vista
de turnos.

NOTA: app.py mantiene hoy una copia inline de esta proyección (en la vista
"Planificación Turnos STO"). Unificar ambas está pendiente; mientras tanto, si
se cambia la regla de rotación hay que tocar los dos lados.
"""

from __future__ import annotations

import copy
import json
import os
import unicodedata
from datetime import date, timedelta

_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turnos_data.json")

# Suplencias con fecha de vigencia: se aplican a las semanas cuyo jueves cae a
# partir de 'desde' (regla del jueves), respetando el histórico real.
SUPLENCIAS = [
    {"sale": "Javier Hein Pacheco",
     "entra": "Juan Francisco Toro Jimenez",
     "desde": "2026-09-01"},
]


def _proyectar(reales: list, hasta: date) -> list:
    """Extiende las semanas reales hacia adelante replicando la rotación.

    El ciclo es de 3 semanas (un equipo por semana) y dentro de cada equipo se
    alternan las variantes que ya existen en el histórico. `_src` apunta a la
    semana real que se copia.
    """
    if not reales or len(reales[0].get("dates", [])) < 7:
        return []
    lun0 = date.fromisoformat(reales[0]["dates"][0])
    lun_last = date.fromisoformat(reales[-1]["dates"][0])
    k = round((lun_last - lun0).days / 7) + 1
    out = []
    while True:
        lun = lun0 + timedelta(weeks=k)
        if lun > hasta:
            break
        pos = k % 3                      # 0=Gallardo, 1=Pinto, 2=Bahamonde
        if pos == 0:
            src = 0 if ((k // 3) % 2 == 0) else 3
        elif pos == 1:
            src = [1, 4, 7][((k - 1) // 3) % 3]
        else:
            src = [2, 5][((k - 2) // 3) % 2]
        if src >= len(reales):
            break
        out.append({"dates": [(lun + timedelta(days=d)).isoformat() for d in range(7)],
                    "zones": copy.deepcopy(reales[src].get("zones", {})),
                    "_estimado": True})
        k += 1
    return out


def cargar_semanas(hasta: date | None = None) -> list:
    """Semanas reales + proyectadas, con las suplencias ya aplicadas."""
    if not os.path.exists(_JSON):
        return []
    try:
        data = json.loads(open(_JSON, encoding="utf-8").read())
    except Exception:
        return []
    reales = data.get("weeks", []) or []
    hasta = hasta or (date.today() + timedelta(weeks=12))
    semanas = list(reales) + _proyectar(reales, hasta)
    for wk in semanas:
        ds = wk.get("dates", [])
        if len(ds) < 4:
            continue
        jueves = ds[3]
        for sup in SUPLENCIAS:
            if jueves >= sup["desde"]:
                for zona in wk.get("zones", {}).values():
                    for t in zona.get("turnos", []):
                        if (t.get("tecnico") or "").strip() == sup["sale"]:
                            t["tecnico"] = sup["entra"]
    return semanas


def de_turno_por_dia(hasta: date | None = None) -> dict:
    """{fecha_iso: {nombre_completo, ...}} de quienes están de turno ese día.

    Un horario vacío o "Libre" no cuenta como turno.
    """
    out: dict[str, set] = {}
    for wk in cargar_semanas(hasta):
        fechas = wk.get("dates", [])
        for zona in wk.get("zones", {}).values():
            for t in zona.get("turnos", []):
                nom = (t.get("tecnico") or "").strip()
                if not nom or nom.upper() in ("N/A", "-", ""):
                    continue
                for i, hr in enumerate(t.get("horarios", [])):
                    if i >= len(fechas):
                        break
                    h = str(hr or "").strip()
                    if not h or h.upper() == "LIBRE":
                        continue
                    out.setdefault(fechas[i], set()).add(nom)
    return out


def _tokens(nombre: str) -> set:
    """Palabras significativas del nombre, sin tildes.

    El roster escribe 'Martin Flores' y el turno 'Martín Ignacio Flores Galaz':
    sin quitar la tilde el cruce falla y el técnico aparecería disponible para
    MP estando de turno.
    """
    s = unicodedata.normalize("NFKD", str(nombre or "").lower())
    s = s.encode("ascii", "ignore").decode()
    return {p for p in s.replace(".", " ").split() if len(p) > 2}


def disponibles_para_mp(roster: list[str], dia: date, mapa: dict) -> list[str]:
    """Del `roster` (nombres cortos del equipo), los que NO están de turno ese día.

    Los turnos traen el nombre completo ("Juan Francisco Toro Jimenez") y el
    roster los nombres cortos ("Juan Francisco"), así que el cruce es por
    coincidencia de tokens: todos los del nombre corto deben estar en el largo.
    """
    ocupados = mapa.get(dia.isoformat(), set())
    tok_ocupados = [_tokens(o) for o in ocupados]
    libres = []
    for r in roster:
        tr = _tokens(r)
        if tr and any(tr <= to for to in tok_ocupados):
            continue
        libres.append(r)
    return libres
