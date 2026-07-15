"""
cron_helpers.py — Construye los helpers/mapas que necesita excel_reports.

Extraídos de app.py (líneas ~7596-7800) para poder usar el generador
Excel fuera del contexto Streamlit.
"""

from __future__ import annotations

import unicodedata
from typing import Callable

import pandas as pd


def _norm_n(s) -> str:
    if not isinstance(s, str):
        s = str(s) if s is not None else ""
    return (unicodedata.normalize("NFD", s)
            .encode("ascii", "ignore").decode()
            .strip().lower())


def _strip_comentario_headers(txt) -> str:
    """Simplificación de la versión de app.py:109 — quita encabezados
    como 'DESCRIPCION DE LA FALLA:', 'TRABAJO REALIZADO:', 'OBSERVACIONES:'."""
    if not txt or not isinstance(txt, str):
        return "—"
    import re as _re
    _headers = _re.compile(
        r"(DESCRIPCI[OÓ]N\s+DE\s+LA\s+FALLA|TRABAJO\s+REALIZADO|OBSERVACIONES)\s*:?\s*",
        _re.IGNORECASE,
    )
    result = _headers.sub(" | ", txt).strip(" |\n\t")
    return result if result else "—"


def _build_eds_nombre_map(df_eds_) -> dict:
    """Mapa eds_occim → nombre estación. Copia mínima de app.py:123."""
    if df_eds_ is None or df_eds_.empty:
        return {}
    if "eds_occim" not in df_eds_.columns:
        return {}
    _cols_nombre = [c for c in ("nombre", "estacion", "direccion", "eds_nombre")
                    if c in df_eds_.columns]
    if not _cols_nombre:
        return {}
    _mapa = {}
    for _, r in df_eds_.iterrows():
        _key = str(r.get("eds_occim") or "").strip()
        if not _key:
            continue
        for _c in _cols_nombre:
            _v = r.get(_c)
            if _v and str(_v).strip():
                _mapa[_key] = str(_v).strip()
                break
    return _mapa


def build_context(data: dict) -> dict:
    """
    Construye el contexto de helpers/mapas para pasarle a build_excel_resumen.

    data: dict con {df_wo, df_llamados, df_eds, df_tecnicos, ...} — el output
          de cron_data_loader.load_dashboard_data()

    Retorna dict con: excel_to_full, label_to_grupo, equipo_label_map,
    numeral_motivo_label, es_excluido, get_equipo, norm_n, strip_headers,
    build_eds_nombre_map.
    """
    from data import GRUPOS_TERRENO, TECNICOS_NO_APLICA, NUMERAL_MOTIVO_LABEL
    from gdrive import build_tech_name_maps

    df_tecnicos = data.get("df_tecnicos", pd.DataFrame())

    # excel_to_full / full_to_excel (mapa short-name ↔ full-name Fracttal)
    excel_to_full, full_to_excel = build_tech_name_maps(df_tecnicos)

    # equipo_label_map (identity + labels bonitos para Carlos)
    equipo_label_map = {k: k for k in GRUPOS_TERRENO}
    equipo_label_map["Carlos Avila Norte"] = "Carlos Avila (Norte)"
    equipo_label_map["Carlos Avila Sur"] = "Carlos Avila (Sur)"
    label_to_grupo = {v: k for k, v in equipo_label_map.items()}

    # GRUPOS_NORM: mapa nombre-normalizado → grupo del senior
    _GRUPOS_NORM: dict[str, str] = {}
    for _grp_k, _grp_v in GRUPOS_TERRENO.items():
        for _mb in _grp_v["miembros"]:
            _GRUPOS_NORM[_norm_n(_mb)] = _grp_k
            _pts = _mb.split()
            for _i in range(len(_pts)):
                for _j in range(_i + 1, len(_pts)):
                    _GRUPOS_NORM[_norm_n(f"{_pts[_i]} {_pts[_j]}")] = _grp_k

    # NO_APLICA_NORM (técnicos excluidos)
    _NO_APLICA_NORM: set[str] = set()
    for _na in TECNICOS_NO_APLICA:
        _NO_APLICA_NORM.add(_norm_n(_na))
        _pts = _na.split()
        for _i in range(len(_pts)):
            for _j in range(_i + 1, len(_pts)):
                _NO_APLICA_NORM.add(_norm_n(f"{_pts[_i]} {_pts[_j]}"))

    def get_equipo(t) -> str:
        if not isinstance(t, str) or not t.strip():
            return "Sin equipo"
        norm = _norm_n(t.strip())
        if norm in _GRUPOS_NORM:
            return _GRUPOS_NORM[norm]
        parts = t.strip().split()
        for _i in range(len(parts)):
            for _j in range(_i + 1, len(parts)):
                alias = _norm_n(f"{parts[_i]} {parts[_j]}")
                if alias in _GRUPOS_NORM:
                    return _GRUPOS_NORM[alias]
        return "Sin equipo"

    def es_excluido(t) -> bool:
        if not isinstance(t, str) or not t.strip():
            return False
        norm = _norm_n(t.strip())
        if norm in _NO_APLICA_NORM:
            return True
        parts = t.strip().split()
        for _i in range(len(parts)):
            for _j in range(_i + 1, len(parts)):
                alias = _norm_n(f"{parts[_i]} {parts[_j]}")
                if alias in _NO_APLICA_NORM:
                    return True
        return False

    # Enriquecer df_wo con 'equipo' (necesario para el filtro de PMs en excel)
    df_wo = data.get("df_wo")
    if df_wo is not None and not df_wo.empty and "technician" in df_wo.columns and "equipo" not in df_wo.columns:
        df_wo["equipo"] = df_wo["technician"].apply(get_equipo)

    # Enriquecer df_ot_scores con 'equipo' (si no lo tiene)
    df_ot_scores = data.get("df_ot_scores")
    if df_ot_scores is not None and not df_ot_scores.empty and "tecnico" in df_ot_scores.columns and "equipo" not in df_ot_scores.columns:
        df_ot_scores["equipo"] = df_ot_scores["tecnico"].apply(get_equipo)

    return {
        "excel_to_full":         excel_to_full,
        "full_to_excel":         full_to_excel,
        "label_to_grupo":        label_to_grupo,
        "equipo_label_map":      equipo_label_map,
        "numeral_motivo_label":  NUMERAL_MOTIVO_LABEL,
        "es_excluido":           es_excluido,
        "get_equipo":            get_equipo,
        "norm_n":                _norm_n,
        "strip_headers":         _strip_comentario_headers,
        "build_eds_nombre_map":  _build_eds_nombre_map,
    }
