# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  AUDITOR DASH 1.0  —  Robot de calidad de datos                             ║
║                                                                              ║
║  Corre de forma independiente (sin el dashboard abierto).                   ║
║  Programar en Windows Task Scheduler a las 9:30 y 14:00.                   ║
║                                                                              ║
║  Salidas:                                                                    ║
║    alertas_resultado.json  — leído por el sidebar del dashboard             ║
║    alertas_reporte.html    — reporte completo (se abre en el navegador)     ║
║    Notificación Windows    — si hay alertas CRÍTICAS                        ║
║                                                                              ║
║  Ejecución manual:  python alertizador.py                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import glob
import json
import os
import subprocess
import sys
import time
import unicodedata
import webbrowser
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    import pandas as pd
    _PANDAS_OK = True
except ImportError:
    _PANDAS_OK = False

# Forzar UTF-8 en stdout/stderr para evitar errores en consolas Windows (cp1252)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
JSON_OUT      = BASE_DIR / "alertas_resultado.json"
HTML_OUT      = BASE_DIR / "alertas_reporte.html"

SUPA_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPA_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
_H = {
    "apikey":        SUPA_KEY,
    "Authorization": "Bearer " + SUPA_KEY,
    "Prefer":        "count=none",
}

# Grupos de técnicos (espejo de data.py — actualizar aquí si cambia data.py)
GRUPOS_TERRENO = {
    "Luis Pinto":        ["Luis Pinto", "Juan Francisco", "Jorge Rodriguez", "Breyans Toledo"],
    "Victor Bahamonde":  ["Victor Bahamonde", "Martin Flores", "Eduardo Toro"],
    "Juan Gallardo":     ["Juan Gallardo", "Javier Hein", "Edison Carrasco", "Ignacio Ferrari"],
    "Carlos Avila Norte":["Carlos Avila", "Edson Perez", "Erwin Rivera"],
    "Carlos Avila Sur":  ["Luis Lopez", "Gaston Fuller"],
}
TODOS_TECNICOS = {m for ms in GRUPOS_TERRENO.values() for m in ms}

# Personal que aparece en OTs pero NO es técnico de terreno
# (espejo de TECNICOS_NO_APLICA en data.py — actualizar ambos si cambia)
PERSONAS_NO_TECNICO = frozenset({
    "Juan Valle", "Jaime Ocampo", "Walter Soto", "Ana Guzman",
    "AUTEC", "Autec", "AUTEC LTDA", "AUTEC IQUIQUE",
    "Alexis Ricardo Rojas Sanchez", "Alexis Ricardo Rojas Sánchez",
    "Roberto Carlos Muñoz Ordenes", "Eric Esteban Dayller Mesa",
    "Jorge Cáceres Hormaechea",
})
TIMEOUT_SEG = 40   # timeout por consulta a Supabase


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS DE CONSULTA
# ──────────────────────────────────────────────────────────────────────────────

def _get(tabla: str, params: dict) -> list:
    """GET paginado — devuelve todos los registros que cumplan los filtros."""
    PAGE   = 1000
    offset = 0
    total  = []
    while True:
        p = {**params, "limit": PAGE, "offset": offset}
        r = requests.get(f"{SUPA_URL}/rest/v1/{tabla}", headers=_H,
                         params=p, timeout=TIMEOUT_SEG)
        r.raise_for_status()
        batch = r.json()
        total.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return total


def _norm(s: str) -> str:
    """Normaliza texto: quita tildes y pasa a minúsculas."""
    return "".join(
        c for c in unicodedata.normalize("NFD", str(s or ""))
        if unicodedata.category(c) != "Mn"
    ).lower().strip()


# Set normalizado (sin tildes, minúsculas) para comparación eficiente en CHK-022
_PERSONAS_NO_TECNICO_NORM = frozenset(_norm(n) for n in PERSONAS_NO_TECNICO)


# ──────────────────────────────────────────────────────────────────────────────
# CONSTRUCTOR DE ALERTA
# ──────────────────────────────────────────────────────────────────────────────

def alerta(check_id: str, nivel: str, categoria: str,
           descripcion: str, n_afectados: int = 0,
           detalle: list = None) -> dict:
    return {
        "check_id":    check_id,
        "nivel":       nivel,       # CRÍTICO | ADVERTENCIA | INFO
        "categoria":   categoria,
        "descripcion": descripcion,
        "n_afectados": n_afectados,
        "detalle":     detalle or [],
    }


# ══════════════════════════════════════════════════════════════════════════════
# CHECKS — FASE 1  (13 verificaciones)
# ══════════════════════════════════════════════════════════════════════════════

def chk_090_ots_duplicadas():
    """CHK-090 · OTs duplicadas — mismo id_ot más de una vez."""
    filas = _get("ordenes_trabajo", {"select": "id_ot"})
    cnt   = Counter(r.get("id_ot") for r in filas if r.get("id_ot"))
    dups  = [(k, v) for k, v in cnt.items() if v > 1]
    if not dups:
        return None
    return alerta(
        "CHK-090", "CRÍTICO", "DUPLICADOS",
        f"{len(dups)} id_ot aparecen más de una vez en ordenes_trabajo — "
        "el KPI Llenado y el bono cuentan la misma OT dos veces.",
        n_afectados=len(dups),
        detalle=[{"id_ot": k, "repeticiones": v} for k, v in sorted(dups, key=lambda x: -x[1])[:15]],
    )


def chk_091_llamados_duplicados():
    """CHK-091 · Llamados duplicados — mismo n_llamado + cliente."""
    filas = _get("v_llamados_sla", {"select": "n_llamado,cliente,os_fracttal"})
    cnt   = Counter(
        (r.get("n_llamado"), r.get("cliente"))
        for r in filas if r.get("n_llamado")
    )
    dups = [(k, v) for k, v in cnt.items() if v > 1]
    if not dups:
        return None
    return alerta(
        "CHK-091", "CRÍTICO", "DUPLICADOS",
        f"{len(dups)} combinaciones (n_llamado + cliente) duplicadas en v_llamados_sla — "
        "distorsiona el % de cumplimiento SLA.",
        n_afectados=sum(v - 1 for _, v in dups),  # filas extra (sobrantes)
        detalle=[
            {"n_llamado": k[0], "cliente": k[1], "repeticiones": v}
            for k, v in sorted(dups, key=lambda x: -x[1])[:15]
        ],
    )


def chk_001_ots_sin_tecnico():
    """CHK-001 · OTs sin técnico asignado."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,tipo_tarea,codigo_eds,fecha_creacion",
        "responsable": "is.null",
    })
    if not filas:
        return None
    return alerta(
        "CHK-001", "CRÍTICO", "NULOS",
        f"{len(filas)} OTs sin técnico asignado — no pueden calcularse KPI, bono ni SLA.",
        n_afectados=len(filas),
        detalle=[
            {"id_ot": r.get("id_ot"), "tipo": r.get("tipo_tarea"),
             "eds": r.get("codigo_eds"),
             "fecha": (r.get("fecha_creacion") or "")[:10]}
            for r in filas[:15]
        ],
    )


def chk_002_ots_sin_fecha_fin():
    """CHK-002 · OTs activas sin fecha_finalizacion con más de 30 días desde creación.
    Excluye Canceladas (5), Cerradas (6) y Finalizadas (4) — no tienen fecha_fin
    pero el trabajo ya está resuelto o descartado, no son OTs zombie reales.
    """
    # Excluir OTs Finalizadas y Canceladas — no son zombies, el trabajo ya terminó/descartó
    ESTADOS_CERRADOS = {"Finalizadas", "Cancelado"}
    limite = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    filas  = [
        r for r in _get("ordenes_trabajo", {
            "select":             "id_ot,responsable,tipo_tarea,fecha_creacion,estado",
            "fecha_finalizacion": "is.null",
            "fecha_creacion":     f"lt.{limite}",
        })
        if r.get("estado") not in ESTADOS_CERRADOS
    ]
    if not filas:
        return None
    return alerta(
        "CHK-002", "CRÍTICO", "NULOS",
        f"{len(filas)} OTs activas en Fracttal con más de 30 días sin cerrar — "
        "KPI Llenado incompleto para esas OTs; la duración real no puede calcularse. "
        "(El SLA usa fecha_atencion del llamado, no fecha_finalizacion de la OT.)",
        n_afectados=len(filas),
        detalle=[
            {"id_ot": r.get("id_ot"), "responsable": r.get("responsable"),
             "tipo": r.get("tipo_tarea"),
             "dias": str((datetime.now() - datetime.fromisoformat(
                 r["fecha_creacion"][:19])).days) + " días"
             if r.get("fecha_creacion") else "—"}
            for r in sorted(filas, key=lambda x: x.get("fecha_creacion") or "")[:15]
        ],
    )


def chk_004_llamados_sin_prioridad():
    """CHK-004 · Llamados sin prioridad válida (P1-P4)."""
    filas   = _get("v_llamados_sla", {"select": "os_fracttal,cliente,eds_occim,prioridad"})
    invalidos = [
        r for r in filas
        if not r.get("prioridad") or
           str(r.get("prioridad", "")).upper() not in ("P1", "P2", "P3", "P4")
    ]
    if not invalidos:
        return None
    por_cliente = Counter(r.get("cliente", "—") for r in invalidos)
    return alerta(
        "CHK-004", "CRÍTICO", "NULOS",
        f"{len(invalidos)} llamados sin prioridad válida — SLA no calculable para esos registros.",
        n_afectados=len(invalidos),
        detalle=[
            {"os_fracttal": r.get("os_fracttal"), "cliente": r.get("cliente"),
             "eds": r.get("eds_occim"), "prioridad_actual": r.get("prioridad") or "(vacío)"}
            for r in invalidos[:15]
        ] + [{"resumen_por_cliente": dict(por_cliente)}],
    )


def chk_006_eds_sin_zona():
    """CHK-006 · EDS activas sin zona — SLA usa 'Santiago' por defecto (puede ser incorrecto)."""
    filas = _get("estaciones_servicio", {
        "select": "eds_occim,cliente,nombre",
        "zona":   "is.null",
        "activa": "eq.true",
    })
    if not filas:
        return None
    return alerta(
        "CHK-006", "CRÍTICO", "NULOS",
        f"{len(filas)} EDS activas sin zona definida — "
        "el sistema aplica 'Santiago' como fallback; "
        "para COPEC P1 esto significa 18h en vez de 24h (más estricto).",
        n_afectados=len(filas),
        detalle=[
            {"eds_occim": r.get("eds_occim"), "cliente": r.get("cliente"),
             "nombre": r.get("nombre")}
            for r in filas[:20]
        ],
    )


def chk_050_fechas_imposibles():
    """CHK-050 · Fechas imposibles: finalización o inicio en fecha anterior a creación.

    Usa solo la parte de FECHA (YYYY-MM-DD) para evitar falsos positivos por
    diferencias de zona horaria entre Fracttal (UTC) y Chile (UTC-3/UTC-4).
    Solo alerta cuando el día calendario es anterior, no si es la misma fecha
    pero distinta hora.
    """
    filas = _get("ordenes_trabajo", {
        "select": "id_ot,responsable,fecha_creacion,fecha_finalizacion,fecha_inicio",
        "fecha_creacion": "not.is.null",
    })
    problemas = []
    for r in filas:
        fc = (r.get("fecha_creacion") or "")[:10]   # solo YYYY-MM-DD
        ff = (r.get("fecha_finalizacion") or "")[:10]
        fi = (r.get("fecha_inicio") or "")[:10]
        if ff and ff < fc:
            problemas.append({
                "id_ot": r.get("id_ot"), "responsable": r.get("responsable"),
                "problema": "finalización < creación",
                "creacion": fc, "finalizacion": ff,
            })
        elif fi and fi < fc:
            problemas.append({
                "id_ot": r.get("id_ot"), "responsable": r.get("responsable"),
                "problema": "inicio < creación",
                "creacion": fc, "inicio": fi,
            })
    if not problemas:
        return None
    return alerta(
        "CHK-050", "CRÍTICO", "TEMPORAL",
        f"{len(problemas)} OTs con fechas imposibles (fin o inicio anterior a creación) — "
        "produce horas_resolución negativas → SLA siempre aparece como CUMPLE.",
        n_afectados=len(problemas),
        detalle=problemas[:15],
    )


def chk_070_tecnicos_desincronizados():
    """CHK-070 · Técnicos en GRUPOS_TERRENO que no existen en tabla tecnicos de Supabase."""
    filas    = _get("tecnicos", {"select": "nombre_corto,aplica_bono"})
    en_supa  = {(r.get("nombre_corto") or "").strip() for r in filas}
    faltantes = TODOS_TECNICOS - en_supa
    # También verificar desincronía inversa: en Supabase pero no en código
    en_codigo   = TODOS_TECNICOS
    solo_en_supa = {n for n in en_supa if n and n not in en_codigo}
    # Solo alertar si faltan en Supabase (lo más crítico)
    if not faltantes:
        return None
    return alerta(
        "CHK-070", "CRÍTICO", "BONO",
        f"{len(faltantes)} técnico(s) definidos en GRUPOS_TERRENO (data.py) "
        "no existen en la tabla 'tecnicos' de Supabase — "
        "sus OTs no se asignan al equipo correcto y el bono es incorrecto.",
        n_afectados=len(faltantes),
        detalle=[{"nombre_corto_faltante": t} for t in sorted(faltantes)] +
                ([{"solo_en_supabase_no_en_codigo": sorted(solo_en_supa)}]
                 if solo_en_supa else []),
    )


def chk_011_correctivas_sin_tipo():
    """CHK-011 · OTs correctivas sin tipo de falla (INFO — el técnico debe llenarlo)."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,responsable,fecha_creacion",
        "tipo_falla": "is.null",
        "tipo_tarea": "ilike.*CORRECTIVA*",
    })
    if not filas:
        return None
    por_tec = Counter(r.get("responsable") or "Sin asignar" for r in filas)
    return alerta(
        "CHK-011", "INFO", "NULOS",
        f"{len(filas)} OTs correctivas sin tipo de falla (F.N.A.O / F.A.O / etc.) — "
        "afecta análisis de reincidencias y KPI Llenado. "
        "Solo el técnico puede completar este campo.",
        n_afectados=len(filas),
        detalle=[
            {"id_ot": r.get("id_ot"), "responsable": r.get("responsable"),
             "fecha": (r.get("fecha_creacion") or "")[:10]}
            for r in filas[:12]
        ] + [{"top_responsables": dict(por_tec.most_common(5))}],
    )


def chk_012_correctivas_sin_causa():
    """CHK-012 · OTs correctivas sin causa raíz registrada (INFO — el técnico debe llenarlo)."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,responsable,tipo_tarea,fecha_creacion",
        "causa_raiz": "is.null",
        "tipo_tarea": "ilike.*CORRECTIVA*",
    })
    if not filas:
        return None
    por_tec = Counter(r.get("responsable") or "Sin asignar" for r in filas)
    return alerta(
        "CHK-012", "INFO", "NULOS",
        f"{len(filas)} OTs correctivas sin causa raíz — "
        "impacta KPI Llenado (score_causa = 0). "
        "Solo el técnico puede completar este campo.",
        n_afectados=len(filas),
        detalle=[
            {"id_ot": r.get("id_ot"), "responsable": r.get("responsable"),
             "fecha": (r.get("fecha_creacion") or "")[:10]}
            for r in filas[:12]
        ] + [{"top_responsables": dict(por_tec.most_common(5))}],
    )


def chk_022_tecnico_desconocido():
    """CHK-022 · Técnico en OTs que no existe en tabla tecnicos (nombre no mapeado).

    Excluye nombres que contengan 'AUTEC' (empresa subcontratada, no aplica bono)
    y nombres vacíos. Solo alerta cuando el nombre no mapeado pertenece a un
    técnico real que debería estar en la tabla.
    """
    ots      = _get("ordenes_trabajo", {"select": "responsable"})
    tecnicos = _get("tecnicos",        {"select": "nombre_completo,nombre_corto"})

    nombres_supa = {_norm(r.get("nombre_completo") or "") for r in tecnicos}
    nombres_supa |= {_norm(r.get("nombre_corto") or "") for r in tecnicos}
    nombres_supa.discard("")

    desconocidos: Counter = Counter()
    for r in ots:
        resp = (r.get("responsable") or "").strip()
        if not resp:
            continue
        resp_n = _norm(resp)
        # Excluir personas conocidas que no son técnicos de terreno
        # (AUTEC subcontratista + personal Occimiano no técnico)
        if "autec" in resp_n or resp_n in _PERSONAS_NO_TECNICO_NORM:
            continue
        if resp_n not in nombres_supa:
            desconocidos[resp] += 1

    if not desconocidos:
        return None
    return alerta(
        "CHK-022", "ADVERTENCIA", "INTEGRIDAD",
        f"{len(desconocidos)} nombre(s) de técnico en ordenes_trabajo sin mapeo en tabla tecnicos — "
        "sus OTs no se agrupan por equipo y quedan fuera del bono.",
        n_afectados=sum(desconocidos.values()),
        detalle=[
            {"responsable": n, "n_ots": c}
            for n, c in desconocidos.most_common(15)
        ],
    )


def chk_042_eds_sin_pm():
    """CHK-042 · EDS activas sin PM en los últimos 45 días."""
    limite_str = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")
    eds_activas = _get("estaciones_servicio", {
        "select": "eds_occim,nombre,cliente",
        "activa": "eq.true",
    })
    ots_pm      = _get("ordenes_trabajo", {
        "select":             "codigo_eds",
        "tipo_tarea":         "ilike.*PREVENTIVA*",
        "fecha_finalizacion": f"gte.{limite_str}",
    })
    con_pm_reciente = {(r.get("codigo_eds") or "").strip() for r in ots_pm}
    sin_pm = [
        e for e in eds_activas
        if (e.get("eds_occim") or "").strip() not in con_pm_reciente
    ]
    if not sin_pm:
        return None
    por_cliente = Counter(e.get("cliente", "—") for e in sin_pm)
    return alerta(
        "CHK-042", "ADVERTENCIA", "COBERTURA",
        f"{len(sin_pm)} EDS activas sin ningún PM en los últimos 45 días — "
        "posible incumplimiento contractual de frecuencia de mantenimiento.",
        n_afectados=len(sin_pm),
        detalle=[
            {"eds_occim": e.get("eds_occim"), "nombre": e.get("nombre"),
             "cliente": e.get("cliente")}
            for e in sorted(sin_pm, key=lambda x: x.get("cliente") or "")[:20]
        ] + [{"resumen_por_cliente": dict(por_cliente)}],
    )


def chk_043_eds_sin_llamados():
    """CHK-043 · EDS activas sin ningún llamado registrado en el año en curso."""
    año = str(datetime.now().year)
    eds_activas = _get("estaciones_servicio", {
        "select": "eds_occim,nombre,cliente",
        "activa": "eq.true",
    })
    llamados = _get("v_llamados_sla", {
        "select":         "eds_occim",
        "fecha_llamado":  f"gte.{año}-01-01",
    })
    con_llamado = {(r.get("eds_occim") or "").strip() for r in llamados}
    sin_llamado = [
        e for e in eds_activas
        if (e.get("eds_occim") or "").strip() not in con_llamado
    ]
    if not sin_llamado:
        return None
    return alerta(
        "CHK-043", "INFO", "COBERTURA",
        f"{len(sin_llamado)} EDS activas sin ningún llamado registrado en {año} — "
        "puede ser normal (muy poca actividad) o indicar que los llamados usan un código diferente.",
        n_afectados=len(sin_llamado),
        detalle=[
            {"eds_occim": e.get("eds_occim"), "nombre": e.get("nombre"),
             "cliente": e.get("cliente")}
            for e in sorted(sin_llamado, key=lambda x: x.get("cliente") or "")[:20]
        ],
    )


# ══════════════════════════════════════════════════════════════════════════════
# EJECUCIÓN DE TODOS LOS CHECKS
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# FASE 2 — VALIDACIÓN DEL PIPELINE DE EXTRACCIÓN
# ══════════════════════════════════════════════════════════════════════════════
#
#  GRUPO A (CHK-1xx): Frescura y volumen — ¿están llegando datos a Supabase?
#  GRUPO B (CHK-2xx): Fracttal vs Supabase — ¿el sync es completo?
#  GRUPO C (CHK-3xx): Excel vs Supabase   — ¿los llamados coinciden OT por OT?
#
# ══════════════════════════════════════════════════════════════════════════════

# ── Credenciales Fracttal (READ ONLY — espejo de api.py) ─────────────────────
_FRACT_BASE          = "https://app.fracttal.com"
_FRACT_TOKEN_URL     = f"{_FRACT_BASE}/oauth/token"
_FRACT_CLIENT_ID     = "KtHFO5pMskBbJ3lhPr"
_FRACT_CLIENT_SECRET = "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m"
_fract_tok: dict     = {"token": None, "exp": None}

# ── Rutas Excel (espejo de gdrive.py FILES) ───────────────────────────────────
_DRIVE_ROOT = "G:/.shortcut-targets-by-id/15zHnoU5VZlkOwYc6EBziNcnS-sAAtwtk/OPERACIONES/OPERACIONES"
_SLA_DIR    = f"{_DRIVE_ROOT}/SLA OPERACIONES"
_XLS = {
    "copec": f"{_SLA_DIR}/Llamados Correctivos COPEC 2024 V2.0.xlsx",
    "shell": f"{_SLA_DIR}/Llamados correctivos Shell.xlsx",
}

def _esmax_path() -> str:
    candidates = [
        f for f in glob.glob(os.path.join(_SLA_DIR, "*ESMAX*.xlsx"))
        if not os.path.basename(f).startswith("~$")
    ]
    return max(candidates, key=os.path.getmtime) if candidates else ""


# ── Fracttal helpers ──────────────────────────────────────────────────────────

def _fract_token() -> str:
    """Bearer token Fracttal (renueva si expiró). READ ONLY."""
    now = datetime.now()
    if _fract_tok["token"] and _fract_tok["exp"] and _fract_tok["exp"] > now:
        return _fract_tok["token"]
    r = requests.post(_FRACT_TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     _FRACT_CLIENT_ID,
        "client_secret": _FRACT_CLIENT_SECRET,
    }, timeout=20)
    r.raise_for_status()
    d = r.json()
    _fract_tok["token"] = d["access_token"]
    _fract_tok["exp"]   = now + timedelta(seconds=d.get("expires_in", 3600) - 60)
    return _fract_tok["token"]


def _fract_get(endpoint: str, params: dict, max_records: int = 2000) -> list:
    """GET paginado Fracttal (100 por página). READ ONLY."""
    all_data, start = [], 0
    while start < max_records:
        try:
            r = requests.get(
                f"{_FRACT_BASE}{endpoint}",
                headers={"Authorization": f"Bearer {_fract_token()}"},
                params={**params, "start": start, "limit": 100},
                timeout=TIMEOUT_SEG,
            )
            r.raise_for_status()
            batch = r.json().get("data") or []
        except Exception:
            break
        all_data.extend(batch)
        if len(batch) < 100:
            break
        start += 100
    return all_data


# ── Excel helpers ─────────────────────────────────────────────────────────────

def _read_excel_llamados(path: str, sheet: str, header_row: int) -> "pd.DataFrame":
    """Lee hoja de llamados desde Excel local (Google Drive sincronizado)."""
    if not _PANDAS_OK:
        raise ImportError("pandas no disponible")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Excel no encontrado: {path}")
    df = pd.read_excel(path, sheet_name=sheet, header=header_row)
    df.columns = [str(c).strip() for c in df.columns]
    # Normalizar columna clave os_fracttal
    for col_orig in ["OS FRACTTAL", "Os Fracttal", "os fracttal"]:
        if col_orig in df.columns:
            df = df.rename(columns={col_orig: "os_fracttal"})
            break
    # Normalizar n_llamado
    for col_orig in ["N° llamado", "N° Llamado", "N°llamado"]:
        if col_orig in df.columns:
            df = df.rename(columns={col_orig: "n_llamado"})
            break
    # Normalizar prioridad
    for col_orig in ["P1-P2-P3-P4", "PRIORIDAD", "Prioridad"]:
        if col_orig in df.columns:
            df = df.rename(columns={col_orig: "prioridad"})
            break
    # Normalizar fecha
    for col_orig in ["Fecha", "FECHA", "fecha_llamado"]:
        if col_orig in df.columns:
            df = df.rename(columns={col_orig: "fecha_llamado"})
            break
    if "fecha_llamado" in df.columns:
        df["fecha_llamado"] = pd.to_datetime(df["fecha_llamado"], errors="coerce")
    if "os_fracttal" in df.columns:
        df["os_fracttal"] = df["os_fracttal"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    return df.dropna(how="all")


def _excel_os_set(df: "pd.DataFrame", desde_año: int) -> set:
    """Set de os_fracttal del año en curso, sin nulos ni 'nan'."""
    if "os_fracttal" not in df.columns:
        return set()
    mask = df["os_fracttal"].notna() & (df["os_fracttal"] != "nan") & (df["os_fracttal"] != "")
    if "fecha_llamado" in df.columns:
        mask &= df["fecha_llamado"].dt.year >= desde_año
    return set(df.loc[mask, "os_fracttal"].astype(str).str.strip())


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO A — Frescura y volumen del pipeline Supabase
# ══════════════════════════════════════════════════════════════════════════════

def chk_100_ots_frescura():
    """CHK-100 · Frescura OTs — ¿cuánto hace que no llega una OT nueva a Supabase?"""
    filas = _get("ordenes_trabajo", {
        "select":         "id_ot,fecha_creacion",
        "order":          "fecha_creacion.desc",
        "limit":          1,
    })
    if not filas:
        return alerta(
            "CHK-100", "CRÍTICO", "PIPELINE",
            "No se encontró ninguna OT en Supabase — "
            "la tabla ordenes_trabajo está vacía o el sync falló completamente.",
            n_afectados=0,
        )
    ultima_fecha = (filas[0].get("fecha_creacion") or "")[:19]
    if not ultima_fecha:
        return None
    ultima_dt  = datetime.fromisoformat(ultima_fecha)
    horas_diff = (datetime.now() - ultima_dt).total_seconds() / 3600
    # No alertar en fines de semana (sábado=5, domingo=6) si horas < 60h
    es_finde = datetime.now().weekday() >= 5
    umbral   = 60 if es_finde else 26   # 26h en días hábiles, 60h en fin de semana
    if horas_diff <= umbral:
        return None
    return alerta(
        "CHK-100", "CRÍTICO", "PIPELINE",
        f"La última OT en Supabase tiene {horas_diff:.0f} horas de antigüedad "
        f"(última: {ultima_fecha[:10]}) — el sync Fracttal → Supabase puede estar detenido.",
        n_afectados=1,
        detalle=[{"ultima_ot": filas[0].get("id_ot"), "fecha": ultima_fecha[:16],
                  "horas_sin_nueva_ot": round(horas_diff, 1)}],
    )


def chk_101_volumen_ots():
    """CHK-101 · Volumen OTs — esta semana vs promedio histórico de 4 semanas."""
    hoy        = datetime.now()
    ini_semana = (hoy - timedelta(days=hoy.weekday())).strftime("%Y-%m-%dT00:00:00")
    ini_hist   = (hoy - timedelta(days=28 + hoy.weekday())).strftime("%Y-%m-%dT00:00:00")
    # Traer todas las OTs desde hace 5 semanas y filtrar en Python
    # (PostgREST no soporta dos filtros del mismo campo en un dict)
    todas = _get("ordenes_trabajo", {
        "select":         "id_ot,fecha_creacion",
        "fecha_creacion": f"gte.{ini_hist}",
    })
    semana_act = [r for r in todas if (r.get("fecha_creacion") or "") >= ini_semana]
    historico  = [r for r in todas if ini_hist <= (r.get("fecha_creacion") or "") < ini_semana]
    n_actual = len(semana_act)
    n_hist   = len(historico)
    if n_hist == 0:
        return None
    promedio_sem = n_hist / 4
    if promedio_sem < 5:
        return None    # muy pocas OTs históricas — check no aplica
    ratio = n_actual / promedio_sem
    if ratio >= 0.40:  # más del 40% del promedio → OK
        return None
    return alerta(
        "CHK-101", "CRÍTICO", "PIPELINE",
        f"Esta semana solo hay {n_actual} OTs nuevas en Supabase vs promedio "
        f"histórico de {promedio_sem:.0f}/semana ({ratio*100:.0f}% del normal) — "
        "posible fallo parcial del sync o semana de pocas operaciones.",
        n_afectados=n_actual,
        detalle=[{"ots_esta_semana": n_actual,
                  "promedio_4_semanas_anteriores": round(promedio_sem, 1),
                  "porcentaje_sobre_promedio": f"{ratio*100:.0f}%",
                  "inicio_semana": ini_semana[:10]}],
    )


def chk_120_llamados_por_cliente():
    """CHK-120 · Llamados pipeline — ¿llegaron datos de los 3 clientes esta semana?"""
    hoy      = datetime.now()
    ini      = (hoy - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
    filas    = _get("v_llamados_sla", {
        "select":        "cliente",
        "fecha_llamado": f"gte.{ini}",
    })
    clientes_esperados = {"COPEC", "SHELL (Enex)", "ESMAX (Aramco)"}
    clientes_presentes = {(r.get("cliente") or "").strip() for r in filas}
    faltantes = clientes_esperados - clientes_presentes
    if not faltantes:
        return None
    return alerta(
        "CHK-120", "CRÍTICO", "PIPELINE",
        f"Los siguientes clientes NO tienen llamados registrados en los últimos 7 días: "
        f"{', '.join(sorted(faltantes))} — "
        "el Excel de ese cliente no se está cargando a Supabase.",
        n_afectados=len(faltantes),
        detalle=[{"cliente_sin_datos": c} for c in sorted(faltantes)] +
                [{"clientes_con_datos_recientes": sorted(clientes_presentes & clientes_esperados)}],
    )


def chk_121_sla_sin_datos():
    """CHK-121 · SLA sin datos — % de llamados con cumplimiento = SIN DATOS."""
    año   = str(datetime.now().year)
    total = _get("v_llamados_sla", {
        "select":        "os_fracttal",
        "fecha_llamado": f"gte.{año}-01-01",
    })
    sin_datos = _get("v_llamados_sla", {
        "select":        "os_fracttal,cliente,prioridad",
        "fecha_llamado": f"gte.{año}-01-01",
        "cumplimiento":  "eq.SIN DATOS",
    })
    n_total = len(total)
    n_sd    = len(sin_datos)
    if n_total == 0:
        return None
    pct = n_sd / n_total * 100
    if pct < 15:
        return None
    por_cliente = Counter(r.get("cliente", "—") for r in sin_datos)
    return alerta(
        "CHK-121", "ADVERTENCIA", "PIPELINE",
        f"{pct:.1f}% de los llamados {año} tienen cumplimiento = SIN DATOS "
        f"({n_sd} de {n_total}) — no hay fecha_atencion o prioridad para calcular el SLA.",
        n_afectados=n_sd,
        detalle=[{"resumen_por_cliente": dict(por_cliente.most_common())}] +
                [{"os_fracttal": r.get("os_fracttal"), "cliente": r.get("cliente"),
                  "prioridad": r.get("prioridad")}
                 for r in sin_datos[:12]],
    )


def chk_122_horas_fuera_rango():
    """CHK-122 · Horas resolución inválidas — negativas o mayores a 720 h (30 días)."""
    año   = str(datetime.now().year)
    filas = _get("v_llamados_sla", {
        "select":        "os_fracttal,cliente,prioridad,tiempo_resp_horas",
        "fecha_llamado": f"gte.{año}-01-01",
    })
    invalidos = [
        r for r in filas
        if r.get("tiempo_resp_horas") is not None and
           (float(r["tiempo_resp_horas"]) < 0 or float(r["tiempo_resp_horas"]) > 720)
    ]
    if not invalidos:
        return None
    negativos = [r for r in invalidos if float(r["tiempo_resp_horas"]) < 0]
    extremos  = [r for r in invalidos if float(r["tiempo_resp_horas"]) > 720]
    return alerta(
        "CHK-122", "ADVERTENCIA", "PIPELINE",
        f"{len(invalidos)} llamados con horas_resolución fuera de rango "
        f"({len(negativos)} negativos, {len(extremos)} > 720h) — "
        "error en fechas de origen o en el cálculo del ETL.",
        n_afectados=len(invalidos),
        detalle=[
            {"os_fracttal": r.get("os_fracttal"), "cliente": r.get("cliente"),
             "prioridad": r.get("prioridad"),
             "horas": round(float(r["tiempo_resp_horas"]), 1)}
            for r in sorted(invalidos, key=lambda x: float(x["tiempo_resp_horas"]))[:15]
        ],
    )


def chk_130_eds_estabilidad():
    """CHK-130 · Estabilidad EDS — cambio brusco en EDS activas vs ejecución anterior."""
    activas_ahora = len(_get("estaciones_servicio", {
        "select": "eds_occim",
        "activa": "eq.true",
    }))
    prev_json = JSON_OUT
    if not prev_json.exists():
        return None
    try:
        prev = json.loads(prev_json.read_text(encoding="utf-8"))
        prev_count = (prev.get("metricas") or {}).get("total_eds_activas")
        if prev_count is None:
            return None
        diff = activas_ahora - int(prev_count)
        pct  = abs(diff) / max(int(prev_count), 1) * 100
        if pct < 5 and abs(diff) < 10:
            return None
        return alerta(
            "CHK-130", "ADVERTENCIA", "PIPELINE",
            f"El número de EDS activas cambió de {prev_count} → {activas_ahora} "
            f"({'+' if diff > 0 else ''}{diff}, {pct:.1f}%) desde la última ejecución — "
            "verifica si hubo alta/baja masiva de estaciones o error de carga.",
            n_afectados=abs(diff),
            detalle=[{"eds_activas_ahora": activas_ahora,
                      "eds_activas_anterior": prev_count,
                      "diferencia": diff}],
        )
    except Exception:
        return None


def chk_131_ots_sin_eds():
    """CHK-131 · OTs sin codigo_eds — OTs huérfanas invisibles en KPIs por estación."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,responsable,tipo_tarea,fecha_creacion",
        "codigo_eds": "is.null",
    })
    if not filas:
        return None
    por_tipo = Counter(r.get("tipo_tarea") or "—" for r in filas)
    return alerta(
        "CHK-131", "ADVERTENCIA", "INTEGRIDAD",
        f"{len(filas)} OTs sin codigo_eds — no aparecen en ningún KPI por Estación "
        "ni en el mapa de cobertura.",
        n_afectados=len(filas),
        detalle=[
            {"id_ot": r.get("id_ot"), "responsable": r.get("responsable"),
             "tipo": r.get("tipo_tarea"), "fecha": (r.get("fecha_creacion") or "")[:10]}
            for r in filas[:12]
        ] + [{"top_tipo_tarea": dict(por_tipo.most_common(5))}],
    )


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO B — Fracttal vs Supabase
# ══════════════════════════════════════════════════════════════════════════════

def chk_200_fracttal_vs_supa_conteo():
    """CHK-200 · Conteo OTs — Fracttal vs Supabase en los últimos 30 días.

    NOTA: el parámetro 'since' de la API Fracttal no filtra con precisión
    (documentado en api.py). Se obtienen las primeras 2000 OTs (más recientes)
    y se filtra por fecha en Python para comparar manzanas con manzanas.
    """
    limite_dt   = datetime.now() - timedelta(days=30)
    limite_str  = limite_dt.strftime("%Y-%m-%dT00:00:00")
    limite_frac = limite_dt.strftime("%Y-%m-%dT00:00:00-00")

    # Fracttal: trae las 2000 más recientes y filtra por creation_date >= limite
    raw_fract   = _fract_get(
        "/api/work_orders",
        {"since": limite_frac, "type_date": "creation_date"},
        max_records=2000,
    )
    if not raw_fract:
        return alerta(
            "CHK-200", "ADVERTENCIA", "PIPELINE",
            "Fracttal devolvió 0 OTs — posible problema con la API o credenciales.",
            n_afectados=0,
        )
    # Filtrar client-side por fecha (la API no filtra de forma confiable)
    fract_recientes = [
        r for r in raw_fract
        if (r.get("creation_date") or "")[:19] >= limite_str
    ]
    n_fract = len(fract_recientes)

    # Supabase para el mismo período
    supa_rows = _get("ordenes_trabajo", {
        "select":         "id_ot",
        "fecha_creacion": f"gte.{limite_str}",
    })
    n_supa = len(supa_rows)

    if n_fract == 0 and n_supa == 0:
        return None
    diff = n_fract - n_supa
    pct  = abs(diff) / max(max(n_fract, n_supa), 1) * 100
    if pct <= 8 and abs(diff) <= 15:
        return None
    nivel = "CRÍTICO" if pct > 20 or abs(diff) > 50 else "ADVERTENCIA"
    return alerta(
        "CHK-200", nivel, "PIPELINE",
        f"Fracttal tiene {n_fract} OTs en los últimos 30 días, "
        f"Supabase tiene {n_supa} — diferencia de {diff:+d} OTs ({pct:.1f}%). "
        "El sync podría estar perdiendo o retrasando registros.",
        n_afectados=abs(diff),
        detalle=[{"fracttal_ultimos_30d": n_fract,
                  "supabase_ultimos_30d": n_supa,
                  "diferencia": diff,
                  "porcentaje": f"{pct:.1f}%",
                  "ventana_dias": 30}],
    )


def chk_201_fracttal_ots_faltantes():
    """CHK-201 · OTs recientes en Fracttal no presentes en Supabase (últimos 7 días)."""
    limite_fract = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00-00")
    raw_fract    = _fract_get(
        "/api/work_orders",
        {"since": limite_fract, "type_date": "creation_date"},
        max_records=1000,
    )
    if not raw_fract:
        return None
    folios_fract = {str(r.get("wo_folio", "")).strip() for r in raw_fract if r.get("wo_folio")}

    limite_supa  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
    supa_rows    = _get("ordenes_trabajo", {
        "select":         "id_ot",
        "fecha_creacion": f"gte.{limite_supa}",
    })
    folios_supa  = {str(r.get("id_ot", "")).strip() for r in supa_rows if r.get("id_ot")}

    faltantes = sorted(folios_fract - folios_supa)
    if not faltantes:
        return None
    return alerta(
        "CHK-201", "CRÍTICO", "PIPELINE",
        f"{len(faltantes)} OTs de los últimos 7 días existen en Fracttal "
        f"pero NO en Supabase — el sync tiene un retraso o está perdiendo registros.",
        n_afectados=len(faltantes),
        detalle=[{"folio_fracttal_faltante_en_supabase": f} for f in faltantes[:20]],
    )


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO C — Excel (Google Drive) vs Supabase  [READ ONLY — nunca escribe Excel]
# ══════════════════════════════════════════════════════════════════════════════

def _chk_excel_vs_supa(cliente_label: str, xls_path: str,
                        sheet: str, header_row: int) -> dict | None:
    """
    Comparación EXPLORATORIA entre Excel operacional y Supabase.

    IMPORTANTE — lectura únicamente:
      • El Excel es un registro operacional del equipo. NUNCA se modifica.
      • El dato válido es Fracttal → Supabase. El Excel es solo referencia.
      • Las discrepancias son informativas: indican que algo vale la pena
        revisar manualmente. No implican que Supabase esté equivocado.

    Detecta:
      a) OS presentes en el Excel del año en curso que NO aparecen en Supabase
         → podría indicar llamados registrados en el Excel pero no en Fracttal,
           o que Supabase aún no los tiene (retraso de sync).
      b) Misma OS con prioridad distinta entre Excel y Supabase
         → si el Excel dice P1 y Supabase dice P2, el umbral SLA es diferente;
           vale la pena revisar cuál es el correcto en Fracttal.
    """
    if not _PANDAS_OK:
        return None
    año = datetime.now().year

    # ── Leer Excel (READ ONLY — solo pandas.read_excel, sin escribir nada) ────
    df_xls = _read_excel_llamados(xls_path, sheet, header_row)
    os_xls = _excel_os_set(df_xls, año)
    if not os_xls:
        return alerta(
            "CHK-3xx", "INFO", "EXPLORATORIO",
            f"Excel de {cliente_label}: sin columna 'OS FRACTTAL' o sin registros "
            f"de {año} — comparación no disponible.",
            n_afectados=0,
        )

    # ── Leer Supabase (solo para comparar) ───────────────────────────────────
    supa_rows = _get("v_llamados_sla", {
        "select":        "os_fracttal,prioridad",
        "cliente":       f"eq.{cliente_label}",
        "fecha_llamado": f"gte.{año}-01-01",
    })
    os_supa   = {str(r.get("os_fracttal") or "").strip() for r in supa_rows if r.get("os_fracttal")}
    supa_prio = {str(r.get("os_fracttal") or "").strip(): r.get("prioridad") for r in supa_rows}

    # a) En Excel pero no en Supabase (puede ser normal si aún no se sincronizaron)
    solo_en_excel = sorted(os_xls - os_supa - {""})

    # b) Discrepancias de prioridad (ESTO sí puede afectar el cálculo de SLA)
    discrepancias = []
    if "prioridad" in df_xls.columns and "os_fracttal" in df_xls.columns:
        for _, row in df_xls.iterrows():
            os_id  = str(row.get("os_fracttal") or "").strip()
            prio_x = str(row.get("prioridad") or "").strip().upper()
            if not os_id or os_id == "nan" or prio_x not in ("P1","P2","P3","P4"):
                continue
            prio_s = supa_prio.get(os_id)
            if prio_s and prio_s != prio_x:
                discrepancias.append({
                    "os_fracttal":       os_id,
                    "prioridad_excel":   prio_x,
                    "prioridad_supabase": prio_s,
                    "nota": "verificar en Fracttal cuál es la correcta",
                })

    if not solo_en_excel and not discrepancias:
        return None

    # Nivel: solo ADVERTENCIA si hay discrepancias de prioridad (afectan SLA)
    # Solo INFO si son solo OS que no cuadran (pueden estar pendientes de sync)
    nivel = "ADVERTENCIA" if discrepancias else "INFO"

    partes, detalle = [], []
    if solo_en_excel:
        partes.append(f"{len(solo_en_excel)} OS del Excel {año} no encontradas en Supabase")
        detalle.append({"solo_en_excel_count": len(solo_en_excel),
                        "nota": "puede ser retraso de sync o llamados no ingresados en Fracttal"})
        detalle += [{"os_solo_en_excel": f} for f in solo_en_excel[:12]]
    if discrepancias:
        partes.append(f"{len(discrepancias)} OS con prioridad distinta entre Excel y Supabase")
        detalle.append({"discrepancias_prioridad_count": len(discrepancias),
                        "nota": "verificar en Fracttal — la prioridad incorrecta cambia el umbral SLA"})
        detalle += discrepancias[:10]

    return alerta(
        "CHK-3xx", nivel, "EXPLORATORIO",
        f"{cliente_label} — comparación Excel vs Supabase (referencia, no acción): "
        + " · ".join(partes) + ".",
        n_afectados=len(solo_en_excel) + len(discrepancias),
        detalle=detalle,
    )


def chk_300_copec_excel_vs_supa():
    """CHK-300 · COPEC Excel vs Supabase — registros faltantes o prioridad incorrecta."""
    path = _XLS["copec"]
    r = _chk_excel_vs_supa("COPEC", path, "LLAMADOS DD", 3)
    if r:
        r["check_id"] = "CHK-300"
    return r


def chk_301_shell_excel_vs_supa():
    """CHK-301 · SHELL Excel vs Supabase — registros faltantes o prioridad incorrecta."""
    path = _XLS["shell"]
    r = _chk_excel_vs_supa("SHELL (Enex)", path, "Detalle", 1)
    if r:
        r["check_id"] = "CHK-301"
    return r


def chk_302_esmax_excel_vs_supa():
    """CHK-302 · ESMAX/Aramco Excel vs Supabase — registros faltantes o prioridad incorrecta."""
    path = _esmax_path()
    if not path:
        return alerta(
            "CHK-302", "ADVERTENCIA", "PIPELINE",
            "No se encontró ningún archivo ESMAX/Aramco en Google Drive — "
            "verifica que el sync de Drive esté activo.",
            n_afectados=0,
        )
    r = _chk_excel_vs_supa("ESMAX (Aramco)", path, "LLAMADOS DD", 3)
    if r:
        r["check_id"] = "CHK-302"
    return r


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO D — Mantenciones Preventivas (CHK-4xx)
#
#  CHK-400 · Frescura preventivas     — ¿está llegando a Supabase la última OT?
#  CHK-401 · Campos nuevos nulos      — activador / nombre_tarea / clasificacion_2
#  CHK-402 · Activadores sin parsear  — valores raw "DATE$EVERY$…" sin traducir
#  CHK-403 · Estado_tarea sin mapear  — valores fuera del set esperado
# ══════════════════════════════════════════════════════════════════════════════

def chk_400_preventivas_frescura():
    """CHK-400 · Frescura preventivas — última OT preventiva en Supabase."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,fecha_creacion",
        "tipo_tarea": "ilike.*PREVENTIV*",
        "order":      "fecha_creacion.desc",
        "limit":      1,
    })
    if not filas:
        return alerta(
            "CHK-400", "CRÍTICO", "PREVENTIVAS",
            "No se encontró ninguna OT preventiva en Supabase — "
            "la tabla no tiene registros con tipo_tarea PREVENTIVA o el sync falló.",
            n_afectados=0,
        )
    ultima_fecha = (filas[0].get("fecha_creacion") or "")[:19]
    if not ultima_fecha:
        return None
    ultima_dt  = datetime.fromisoformat(ultima_fecha)
    horas_diff = (datetime.now() - ultima_dt).total_seconds() / 3600
    es_finde   = datetime.now().weekday() >= 5
    umbral     = 72 if es_finde else 48   # preventivas son menos frecuentes que correctivas
    if horas_diff <= umbral:
        return None
    return alerta(
        "CHK-400", "ADVERTENCIA", "PREVENTIVAS",
        f"La última OT preventiva en Supabase tiene {horas_diff:.0f} horas de antigüedad "
        f"(última: {ultima_fecha[:10]}) — puede indicar que el sync no está capturando "
        "mantenciones recientes o que la semana no tuvo actividad preventiva.",
        n_afectados=1,
        detalle=[{"ultima_ot_preventiva": filas[0].get("id_ot"),
                  "fecha": ultima_fecha[:16],
                  "horas_sin_nueva": round(horas_diff, 1)}],
    )


def chk_401_preventivas_campos_nulos():
    """CHK-401 · Campos nuevos nulos — activador / nombre_tarea / clasificacion_2 en preventivas."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,activador,nombre_tarea,clasificacion_2,estado_tarea",
        "tipo_tarea": "ilike.*PREVENTIV*",
    })
    if not filas:
        return None
    total = len(filas)

    sin_activador     = [r for r in filas if not r.get("activador")]
    sin_nombre_tarea  = [r for r in filas if not r.get("nombre_tarea")]
    sin_clasificacion = [r for r in filas if not r.get("clasificacion_2")]
    sin_estado_tarea  = [r for r in filas if not r.get("estado_tarea")]

    # Umbrales: alertar si el % nulo supera el umbral esperado
    problemas = []
    detalle   = [{"total_preventivas_revisadas": total}]

    pct_activ = len(sin_activador) / total * 100
    pct_nom   = len(sin_nombre_tarea) / total * 100
    pct_cla   = len(sin_clasificacion) / total * 100
    pct_est   = len(sin_estado_tarea) / total * 100

    detalle.append({
        "activador_nulo_%":      f"{pct_activ:.1f}%",
        "nombre_tarea_nulo_%":   f"{pct_nom:.1f}%",
        "clasificacion_2_nulo_%":f"{pct_cla:.1f}%",
        "estado_tarea_nulo_%":   f"{pct_est:.1f}%",
    })

    if pct_activ > 30:
        problemas.append(f"activador nulo en {pct_activ:.1f}% ({len(sin_activador)} OTs)")
        detalle += [{"id_ot_sin_activador": r.get("id_ot")} for r in sin_activador[:8]]
    if pct_nom > 20:
        problemas.append(f"nombre_tarea nulo en {pct_nom:.1f}% ({len(sin_nombre_tarea)} OTs)")
        detalle += [{"id_ot_sin_nombre_tarea": r.get("id_ot")} for r in sin_nombre_tarea[:8]]
    if pct_cla > 40:
        problemas.append(f"clasificacion_2 nulo en {pct_cla:.1f}% ({len(sin_clasificacion)} OTs)")
    if pct_est > 10:
        problemas.append(f"estado_tarea nulo en {pct_est:.1f}% ({len(sin_estado_tarea)} OTs)")
        detalle += [{"id_ot_sin_estado_tarea": r.get("id_ot")} for r in sin_estado_tarea[:8]]

    if not problemas:
        return None
    return alerta(
        "CHK-401", "ADVERTENCIA", "PREVENTIVAS",
        "Campos nuevos con alto % de nulos en ordenes preventivas: "
        + "; ".join(problemas) + ". "
        "Puede indicar que el sync no está capturando esos campos de la API Fracttal.",
        n_afectados=max(len(sin_activador), len(sin_nombre_tarea),
                        len(sin_clasificacion), len(sin_estado_tarea)),
        detalle=detalle,
    )


def chk_402_activadores_sin_parsear():
    """CHK-402 · Activadores sin parsear — valores raw 'DATE$EVERY$…' sin traducir."""
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,activador",
        "tipo_tarea": "ilike.*PREVENTIV*",
        "activador":  "ilike.*$*",   # contiene "$" → no fue parseado
    })
    if not filas:
        return None
    por_valor = Counter(r.get("activador") for r in filas)
    return alerta(
        "CHK-402", "ADVERTENCIA", "PREVENTIVAS",
        f"{len(filas)} OTs preventivas tienen el campo 'activador' en formato raw "
        f"(contiene '$', ej: 'DATE$EVERY$1$MONTHS') — parse_trigger() no reconoció "
        "el formato. Actualizar sync_supabase.py y re-sincronizar.",
        n_afectados=len(filas),
        detalle=[{"activador_raw": v, "n_ots": c}
                 for v, c in por_valor.most_common(10)],
    )


def chk_403_estado_tarea_sin_mapear():
    """CHK-403 · Estado_tarea sin mapear — valores fuera del set esperado."""
    ESTADOS_VALIDOS = {"Finalizada", "En Proceso", "No Iniciada", "En Espera", "En Revisión"}
    filas = _get("ordenes_trabajo", {
        "select":     "id_ot,estado_tarea",
        "tipo_tarea": "ilike.*PREVENTIV*",
    })
    sin_mapear = [
        r for r in filas
        if r.get("estado_tarea") and r.get("estado_tarea") not in ESTADOS_VALIDOS
    ]
    if not sin_mapear:
        return None
    por_valor = Counter(r.get("estado_tarea") for r in sin_mapear)
    return alerta(
        "CHK-403", "ADVERTENCIA", "PREVENTIVAS",
        f"{len(sin_mapear)} OTs preventivas con estado_tarea no reconocido "
        f"({', '.join(por_valor.keys())}) — "
        "agregar al mapeo en map_task_status() de sync_supabase.py.",
        n_afectados=len(sin_mapear),
        detalle=[{"estado_tarea_sin_mapear": v, "n_ots": c}
                 for v, c in por_valor.most_common(10)],
    )


# ══════════════════════════════════════════════════════════════════════════════
# REGISTRO COMPLETO DE CHECKS (Fase 1 + Fase 2)
# ══════════════════════════════════════════════════════════════════════════════

_CHECKS = [
    # ── FASE 1: Calidad del dato en Supabase ──────────────────────────────────
    ("CHK-090", chk_090_ots_duplicadas),
    ("CHK-091", chk_091_llamados_duplicados),
    ("CHK-001", chk_001_ots_sin_tecnico),
    ("CHK-002", chk_002_ots_sin_fecha_fin),
    ("CHK-004", chk_004_llamados_sin_prioridad),
    ("CHK-006", chk_006_eds_sin_zona),
    ("CHK-050", chk_050_fechas_imposibles),
    ("CHK-070", chk_070_tecnicos_desincronizados),
    ("CHK-011", chk_011_correctivas_sin_tipo),
    ("CHK-012", chk_012_correctivas_sin_causa),
    ("CHK-022", chk_022_tecnico_desconocido),
    ("CHK-042", chk_042_eds_sin_pm),
    ("CHK-043", chk_043_eds_sin_llamados),
    # ── FASE 2-A: Frescura y volumen del pipeline ─────────────────────────────
    ("CHK-100", chk_100_ots_frescura),
    ("CHK-101", chk_101_volumen_ots),
    ("CHK-120", chk_120_llamados_por_cliente),
    ("CHK-121", chk_121_sla_sin_datos),
    ("CHK-122", chk_122_horas_fuera_rango),
    ("CHK-130", chk_130_eds_estabilidad),
    ("CHK-131", chk_131_ots_sin_eds),
    # ── FASE 2-B: Fracttal vs Supabase ───────────────────────────────────────
    ("CHK-200", chk_200_fracttal_vs_supa_conteo),
    ("CHK-201", chk_201_fracttal_ots_faltantes),
    # ── FASE 2-C: Excel (Google Drive) vs Supabase ───────────────────────────
    ("CHK-300", chk_300_copec_excel_vs_supa),
    ("CHK-301", chk_301_shell_excel_vs_supa),
    ("CHK-302", chk_302_esmax_excel_vs_supa),
    # ── FASE 2-D: Mantenciones Preventivas ───────────────────────────────────
    ("CHK-400", chk_400_preventivas_frescura),
    ("CHK-401", chk_401_preventivas_campos_nulos),
    ("CHK-402", chk_402_activadores_sin_parsear),
    ("CHK-403", chk_403_estado_tarea_sin_mapear),
]


def ejecutar_checks() -> dict:
    alertas   = []
    errores   = []
    inicio    = time.time()

    for cid, fn in _CHECKS:
        try:
            _primera_linea = (fn.__doc__ or "").splitlines()[0]
            _lbl = _primera_linea.split(" · ")[1].split(" —")[0].strip() if " · " in _primera_linea else cid
            print(f"  ▸ {cid} {_lbl}...", end=" ", flush=True)
            resultado = fn()
            if resultado:
                alertas.append(resultado)
                nivel = resultado["nivel"]
                print(f"{'🔴' if nivel=='CRÍTICO' else '🟡' if nivel=='ADVERTENCIA' else '🔵'} "
                      f"{resultado['n_afectados']} afectados")
            else:
                print("✅ OK")
        except Exception as exc:
            errores.append({"check_id": cid, "error": str(exc)})
            print(f"⚠️  Error: {exc}")

    duracion = round(time.time() - inicio, 1)
    criticos     = [a for a in alertas if a["nivel"] == "CRÍTICO"]
    advertencias = [a for a in alertas if a["nivel"] == "ADVERTENCIA"]
    infos        = [a for a in alertas if a["nivel"] == "INFO"]

    if criticos:
        estado = "CRÍTICO"
    elif advertencias:
        estado = "ADVERTENCIA"
    else:
        estado = "OK"

    # Métricas de referencia para comparación entre ejecuciones (CHK-130, etc.)
    try:
        _eds_count = len(_get("estaciones_servicio", {"select": "eds_occim", "activa": "eq.true"}))
    except Exception:
        _eds_count = None
    try:
        _ot_count = len(_get("ordenes_trabajo", {"select": "id_ot",
                                                  "fecha_creacion": f"gte.{datetime.now().year}-01-01"}))
    except Exception:
        _ot_count = None
    try:
        _ll_count = len(_get("v_llamados_sla", {"select": "os_fracttal",
                                                 "fecha_llamado": f"gte.{datetime.now().year}-01-01"}))
    except Exception:
        _ll_count = None

    return {
        "fecha_ejecucion":     datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "duracion_seg":        duracion,
        "estado":              estado,
        "total_criticos":      len(criticos),
        "total_advertencias":  len(advertencias),
        "total_info":          len(infos),
        "total_errores_check": len(errores),
        "metricas": {
            "total_eds_activas":        _eds_count,
            "total_ots_año":            _ot_count,
            "total_llamados_año":       _ll_count,
            "total_checks_ejecutados":  len(_CHECKS),
        },
        "alertas":             alertas,
        "errores_check":       errores,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GENERACIÓN DEL REPORTE HTML
# ══════════════════════════════════════════════════════════════════════════════

def _color_nivel(nivel: str) -> str:
    return {"CRÍTICO": "#ef4444", "ADVERTENCIA": "#f59e0b", "INFO": "#3b82f6"}.get(nivel, "#94a3b8")


def _icon_nivel(nivel: str) -> str:
    return {"CRÍTICO": "🔴", "ADVERTENCIA": "🟡", "INFO": "🔵"}.get(nivel, "⚪")


def generar_html(resultado: dict) -> str:
    fecha  = resultado["fecha_ejecucion"].replace("T", " ")
    estado = resultado["estado"]
    nc     = resultado["total_criticos"]
    na     = resultado["total_advertencias"]
    ni     = resultado["total_info"]
    dur    = resultado["duracion_seg"]

    color_estado = {"CRÍTICO": "#ef4444", "ADVERTENCIA": "#f59e0b", "OK": "#22c55e"}.get(estado, "#94a3b8")
    checks_ok    = len(_CHECKS) - nc - na - ni - resultado["total_errores_check"]

    # Bloques de alertas
    bloques = ""
    for a in resultado["alertas"]:
        col   = _color_nivel(a["nivel"])
        ico   = _icon_nivel(a["nivel"])
        det_html = ""
        for d in a.get("detalle", []):
            if isinstance(d, dict):
                cells = " ".join(
                    f'<span style="margin-right:12px;"><b style="color:#94a3b8;">'
                    f'{k}:</b> {v}</span>'
                    for k, v in d.items()
                )
                det_html += f'<div style="padding:3px 0;font-size:0.78rem;">{cells}</div>'

        bloques += f"""
        <div style="border-left:4px solid {col};background:#111f38;
                    border-radius:6px;padding:14px 18px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:700;font-size:0.95rem;color:#e2e8f0;">
              {ico} <span style="color:{col};">[{a['check_id']}]</span>
              &nbsp;{a['descripcion'].split('—')[0].strip() if '—' in a['descripcion']
                    else a['descripcion'][:60]}
            </span>
            <span style="font-size:0.8rem;color:{col};font-weight:700;">
              {a['n_afectados']} afectado(s)
            </span>
          </div>
          <div style="font-size:0.82rem;color:#94a3b8;margin:6px 0 10px 0;">
            {a['descripcion']}
          </div>
          {f'<details style="cursor:pointer;"><summary style="font-size:0.78rem;color:#60a5fa;">Ver detalle ({len(a["detalle"])} registros de muestra)</summary><div style="margin-top:8px;max-height:200px;overflow-y:auto;font-family:monospace;">{det_html}</div></details>'
           if a.get('detalle') else ''}
        </div>"""

    if not bloques:
        bloques = """<div style="text-align:center;padding:40px;color:#22c55e;font-size:1.1rem;">
            ✅ No se encontraron problemas en esta ejecución.</div>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Auditor Dash 1.0 — {fecha}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', sans-serif; background: #0a1226; color: #e2e8f0;
            min-height: 100vh; padding: 0; }}
    .header {{ background: linear-gradient(135deg,#0C2540,#01798A);
               padding: 24px 40px; display: flex; justify-content: space-between;
               align-items: center; }}
    .header h1 {{ font-size: 1.4rem; font-weight: 800; letter-spacing: 0.02em; }}
    .header .fecha {{ font-size: 0.85rem; color: rgba(255,255,255,0.7); }}
    .container {{ max-width: 960px; margin: 0 auto; padding: 28px 24px; }}
    .summary {{ display: flex; gap: 16px; margin-bottom: 28px; }}
    .card {{ flex: 1; background: #111f38; border-radius: 10px; padding: 16px 20px;
             text-align: center; border: 1px solid #1e3356; }}
    .card .num {{ font-size: 2rem; font-weight: 800; }}
    .card .lbl {{ font-size: 0.78rem; color: #94a3b8; margin-top: 4px; }}
    .section-title {{ font-size: 0.72rem; font-weight: 700; color: #94a3b8;
                      letter-spacing: 0.08em; margin: 20px 0 10px 0; }}
    details summary {{ outline: none; }}
    details summary::-webkit-details-marker {{ display: none; }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>📊 Auditor Dash 1.0</h1>
      <div class="fecha">Ejecución: {fecha} · Duración: {dur}s · {len(_CHECKS)} checks ejecutados</div>
    </div>
    <div style="background:{color_estado};color:#fff;font-weight:800;font-size:1.1rem;
                padding:10px 24px;border-radius:8px;">
      {estado}
    </div>
  </div>

  <div class="container">
    <div class="summary">
      <div class="card" style="border-color:#ef444466;">
        <div class="num" style="color:#ef4444;">{nc}</div>
        <div class="lbl">🔴 CRÍTICOS</div>
      </div>
      <div class="card" style="border-color:#f59e0b66;">
        <div class="num" style="color:#f59e0b;">{na}</div>
        <div class="lbl">🟡 ADVERTENCIAS</div>
      </div>
      <div class="card" style="border-color:#3b82f666;">
        <div class="num" style="color:#3b82f6;">{ni}</div>
        <div class="lbl">🔵 INFORMATIVAS</div>
      </div>
      <div class="card" style="border-color:#22c55e66;">
        <div class="num" style="color:#22c55e;">{checks_ok}</div>
        <div class="lbl">✅ CHECKS OK</div>
      </div>
    </div>

    <div class="section-title">ALERTAS DETECTADAS</div>
    {bloques}

    <div style="margin-top:32px;font-size:0.72rem;color:#475569;text-align:center;">
      Occimiano Dashboard · Auditor Dash 1.0 · {fecha}
    </div>
  </div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICACIÓN WINDOWS
# ══════════════════════════════════════════════════════════════════════════════

def notificar_windows(titulo: str, mensaje: str) -> None:
    """Muestra una notificación tipo globo en la bandeja del sistema (Windows 10/11)."""
    try:
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon    = [System.Drawing.SystemIcons]::Warning
$n.Visible = $true
$n.ShowBalloonTip(9000, "{titulo}", "{mensaje}", [System.Windows.Forms.ToolTipIcon]::Warning)
Start-Sleep -Seconds 9
$n.Dispose()
"""
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=0x08000000,   # CREATE_NO_WINDOW
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # La notificación es un extra — nunca rompe la ejecución


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("═" * 60)
    print("  📊 AUDITOR DASH 1.0")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print("═" * 60)
    print()

    # ── Ejecutar checks ───────────────────────────────────────────────────────
    resultado = ejecutar_checks()

    # ── Guardar JSON (leído por el sidebar del dashboard) ─────────────────────
    JSON_OUT.write_text(json.dumps(resultado, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n  💾 JSON guardado → {JSON_OUT.name}")

    # ── Generar reporte HTML ──────────────────────────────────────────────────
    html = generar_html(resultado)
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"  📄 Reporte HTML  → {HTML_OUT.name}")

    # ── Resumen en consola ────────────────────────────────────────────────────
    nc = resultado["total_criticos"]
    na = resultado["total_advertencias"]
    ni = resultado["total_info"]
    print()
    print(f"  Estado: {resultado['estado']}")
    print(f"  🔴 Críticos:     {nc}")
    print(f"  🟡 Advertencias: {na}")
    print(f"  🔵 Info:         {ni}")
    print(f"  ⏱  Duración:     {resultado['duracion_seg']}s")
    print()

    # ── Notificación Windows si hay críticos ──────────────────────────────────
    if nc > 0:
        msg = f"{nc} alerta(s) crítica(s) encontrada(s). Ver reporte."
        notificar_windows("📊 Auditor Dash 1.0", msg)
        print(f"  🔔 Notificación Windows enviada")

    # ── Abrir HTML automáticamente solo si se ejecuta manualmente ────────────
    # (no abrir cuando lo lanza el Task Scheduler en background)
    if len(sys.argv) > 1 and sys.argv[1] == "--abrir":
        webbrowser.open(HTML_OUT.as_uri())
        print(f"  🌐 Reporte abierto en el navegador")

    print()
    print("═" * 60)
    print("  Listo.")
    print("═" * 60)
    print()


if __name__ == "__main__":
    main()
