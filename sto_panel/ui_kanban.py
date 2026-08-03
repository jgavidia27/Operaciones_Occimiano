"""
ui_kanban.py — Kanban de N+2 columnas: Por clasificar + grupos coincidentes + Sin coincidencia.

Modelo:
    asignaciones: {id_ot: "gN" | "sin_coincidencia"}  # ausente = por clasificar
    grupos:       [{"id": "g1", "nombre": "Error 001", ...}]

La columna "Sin coincidencia" se renderiza más angosta (~55% del ancho de una columna normal).
"""
from __future__ import annotations

import streamlit as st

COL_PENDIENTE = "Por clasificar"
COL_SIN       = "Sin coincidencia"
LABEL_PENDIENTE = f"⚪ {COL_PENDIENTE}"
LABEL_SIN       = f"❌ {COL_SIN}"


CUSTOM_STYLE = """
.sortable-component {
    display: flex !important;
    flex-direction: row !important;
    gap: 10px;
    background-color: transparent;
    padding: 0;
    align-items: stretch;
}
.sortable-container {
    flex: 1 1 0;
    min-width: 0;
    background-color: #f4f5f8;
    padding: 10px;
    border-radius: 10px;
    min-height: 520px;
    border: 1px solid rgba(120,130,150,0.25);
    display: flex;
    flex-direction: column;
}
.sortable-container:first-child {
    background-color: #eef1f6;
    border-color: rgba(120,130,150,0.35);
}
.sortable-container:last-child {
    flex: 0.55 1 0;
    background-color: #fbeeee;
    border-color: rgba(200,120,120,0.35);
}
.sortable-container-header {
    font-weight: 700 !important;
    font-size: 0.98em;
    padding: 10px 8px;
    text-align: center;
    background: #e2e6ed !important;
    color: #1a1a1a !important;
    border-radius: 8px;
    margin-bottom: 12px;
    border: 1px solid rgba(120,130,150,0.2);
    white-space: normal;
    line-height: 1.25;
    min-height: 42px;
}
.sortable-container:last-child .sortable-container-header {
    background: #f4d6d6 !important;
}
.sortable-container-body {
    flex: 1;
    min-height: 400px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.sortable-item, .sortable-item:hover {
    background-color: #ffffff !important;
    color: #1a1a1a !important;
    padding: 10px 12px !important;
    margin-bottom: 0 !important;
    border-radius: 8px !important;
    border: 1px solid rgba(0,0,0,0.1) !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08) !important;
    white-space: pre-line !important;
    line-height: 1.4 !important;
    cursor: grab !important;
    font-size: 0.86em !important;
    font-weight: 400 !important;
    font-family: inherit !important;
    text-align: left !important;
}
.sortable-item:hover {
    box-shadow: 0 3px 10px rgba(0,0,0,0.18) !important;
}
.sortable-item:active { cursor: grabbing !important; }
.sortable-item.dragging { opacity: 0.6; }
"""


def _distribuir(items: list[dict], asignaciones: dict,
                grupo_ids: list[str]) -> dict[str, list[str]]:
    """Devuelve {container_key: [label, label, ...]} listo para sort_items."""
    buckets: dict[str, list[str]] = {"pendiente": [], "sin_coincidencia": []}
    for gid in grupo_ids:
        buckets[gid] = []
    for it in items:
        dest = asignaciones.get(it["id_ot"])
        if dest in grupo_ids:
            buckets[dest].append(it["label"])
        elif dest == "sin_coincidencia":
            buckets["sin_coincidencia"].append(it["label"])
        else:
            buckets["pendiente"].append(it["label"])
    return buckets


def _label_to_id(items: list[dict]) -> dict[str, str]:
    return {it["label"]: it["id_ot"] for it in items}


def _header_grupo(g: dict) -> str:
    nombre = (g.get("nombre") or "").strip()
    return f"🗂 {nombre}" if nombre else "🗂 (sin motivo)"


def render_kanban(items: list[dict],
                  asignaciones: dict,
                  grupos: list[dict],
                  read_only: bool = False,
                  key_prefix: str = "kanban") -> dict:
    """
    Devuelve {"asignaciones": {id_ot: "gN"|"sin_coincidencia"|None}}.
    Las OTs no asignadas (columna Pendiente) no aparecen en el dict.
    """
    if not items:
        st.info("No hay correctivos para clasificar en este periodo.")
        return {"asignaciones": {}}

    try:
        from streamlit_sortables import sort_items
        _HAS_SORTABLES = True
    except Exception:
        _HAS_SORTABLES = False

    grupo_ids = [g["id"] for g in grupos]

    if not _HAS_SORTABLES or read_only:
        return _render_fallback(items, asignaciones, grupos, read_only, key_prefix)

    buckets = _distribuir(items, asignaciones, grupo_ids)

    payload = [{"header": LABEL_PENDIENTE, "items": buckets["pendiente"]}]
    for g in grupos:
        payload.append({"header": _header_grupo(g), "items": buckets[g["id"]]})
    payload.append({"header": LABEL_SIN, "items": buckets["sin_coincidencia"]})

    # Key incluye la firma de grupos para que sortables re-monte al agregar/eliminar
    firma = "_".join(g["id"] for g in grupos)
    result = sort_items(
        payload,
        multi_containers=True,
        direction="vertical",
        custom_style=CUSTOM_STYLE,
        key=f"{key_prefix}_sortable_{firma}",
    )

    label2id = _label_to_id(items)
    nuevas: dict = {}
    header_a_key = {LABEL_PENDIENTE: None, LABEL_SIN: "sin_coincidencia"}
    for g in grupos:
        header_a_key[_header_grupo(g)] = g["id"]

    for cont in result:
        dest = header_a_key.get(cont["header"])
        for lbl in cont["items"]:
            ot = label2id.get(lbl)
            if not ot:
                continue
            if dest is not None:
                nuevas[ot] = dest
            # dest == None → columna pendiente → no persiste

    return {"asignaciones": nuevas}


def _render_fallback(items: list[dict], asignaciones: dict, grupos: list[dict],
                     read_only: bool, key_prefix: str) -> dict:
    st.caption("Modo alternativo (sin drag & drop) — selecciona destino por OT.")
    opciones = ["Por clasificar"] + [_header_grupo(g) for g in grupos] + ["Sin coincidencia"]
    opciones_key = [None] + [g["id"] for g in grupos] + ["sin_coincidencia"]

    nuevas: dict = {}
    for it in items:
        ot = it["id_ot"]
        actual = asignaciones.get(ot)
        try:
            idx = opciones_key.index(actual)
        except ValueError:
            idx = 0
        st.markdown(f"**{it['label']}**")
        if read_only:
            st.caption(f"→ {opciones[idx]}")
            if opciones_key[idx] is not None:
                nuevas[ot] = opciones_key[idx]
        else:
            sel = st.radio(
                label="", options=opciones, index=idx,
                key=f"{key_prefix}_r_{ot}", horizontal=True,
                label_visibility="collapsed",
            )
            k = opciones_key[opciones.index(sel)]
            if k is not None:
                nuevas[ot] = k
        st.divider()

    return {"asignaciones": nuevas}
