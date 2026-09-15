"""
seed_tipo_mantencion.py
=======================
Puebla estaciones_servicio.tipo_mantencion y .duracion_mp_min desde el plan de
mantenimiento preventivo de operaciones.

Por qué hace falta un archivo externo: este dato NO está en Fracttal. Se
verificó por dos caminos y ninguno sirve —
  · duracion_estim_seg es un valor por defecto: mediana 2,67 h para todo,
    con p25 2,0 y p75 3,0. No distingue estaciones.
  · el tipo no se puede deducir de plan_tareas: las estaciones marcadas
    C/TERMO mayoritariamente ni siquiera tienen un plan TERMO asociado.
La correlación entre las horas del plan y las de Fracttal es 0,17.

Es conocimiento de terreno (qué equipos tiene instalados cada estación), así
que la planilla se usa como SEMILLA y a partir de ahí el dato vive en la base,
donde se puede corregir sin volver a tocar un Excel.

El sufijo B del maestro es un SEGUNDO equipo de lavado en la misma estación,
no otra estación: se suma su duración a la estación base y se guarda el tipo
más pesado de los dos.

Uso:
    python seed_tipo_mantencion.py --excel "ruta/al/calculo mtto prev.xlsx" --dry-run
    python seed_tipo_mantencion.py --excel "ruta/al/calculo mtto prev.xlsx"
"""

import argparse
import os
import re

import pandas as pd
import requests

from sync_numerales_subtarea import SUPABASE_URL, _sb_headers, log

# Orden de "peso": si una estación tiene dos equipos, manda el más exigente.
PESO_TIPO = {"SIMPLE": 0, "C/HIDROPACK": 1, "C/TERMO": 2}
# El maestro escribe el segundo equipo de varias formas: 60711B, "40046 B",
# "60001(B)", "60079 (B)". Todas apuntan a la misma estación.
_RX_B = re.compile(r"^(.*?)[\s_\-]*\(?B\)?$", re.I)


def _norm_tipo(valor) -> str | None:
    """'MTTO C/HIDROPACK' -> 'C/HIDROPACK'."""
    t = str(valor or "").strip().upper()
    t = re.sub(r"^MTTO\s*", "", t)
    return t if t in PESO_TIPO else None


def _min(valor) -> int | None:
    """'1:40' -> 100 minutos."""
    m = re.match(r"(\d+):(\d+)", str(valor or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def leer_plan(ruta: str) -> pd.DataFrame:
    df = pd.read_excel(ruta, sheet_name="RUTAS", header=2)
    df = df[df["RUTA"].notna() & df["RUTA"].astype(str).str.match(r"R\d+")]
    df["cod"] = df["N° EDS"].astype(str).str.strip()
    df["tipo"] = df["TIPO MANTENCIÓN"].map(_norm_tipo)
    df["min"] = df["HRS EST."].map(_min)
    return df[df["tipo"].notna() & df["min"].notna()]


def estaciones() -> set:
    rows, off = [], 0
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/estaciones_servicio",
                         headers=_sb_headers(),
                         params={"select": "eds_occim", "limit": 1000, "offset": off},
                         timeout=60)
        r.raise_for_status()
        b = r.json()
        if not b:
            break
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    return {x["eds_occim"] for x in rows}


def consolidar(plan: pd.DataFrame, codigos: set) -> dict:
    """{eds_occim: (tipo, minutos)} con los equipos B sumados a su estación."""
    def base(c: str) -> str:
        m = _RX_B.match(c)
        if m:
            b = m.group(1).strip()
            if b in codigos:
                return b
        return c

    acc: dict = {}
    for r in plan.itertuples():
        b = base(r.cod)
        tipo, mins = acc.get(b, (None, 0))
        mejor = r.tipo if (tipo is None or
                           PESO_TIPO[r.tipo] > PESO_TIPO[tipo]) else tipo
        acc[b] = (mejor, mins + int(r.min))
    return acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True, help="Plan de mantenimiento preventivo.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.excel):
        raise SystemExit(f"No existe el archivo: {args.excel}")

    plan = leer_plan(args.excel)
    log(f"Filas de estación en el plan: {len(plan)}")

    codigos = estaciones()
    datos = consolidar(plan, codigos)
    log(f"Estaciones consolidadas (equipos B sumados): {len(datos)}")

    conocidas = {k: v for k, v in datos.items() if k in codigos}
    ajenas = sorted(set(datos) - codigos)
    log(f"  cruzan con estaciones_servicio: {len(conocidas)}")
    if ajenas:
        log(f"  en el plan pero NO en la base ({len(ajenas)}): {', '.join(ajenas[:10])}")
    sin_dato = len(codigos) - len(conocidas)
    log(f"  EDS de la base que quedan SIN tipo: {sin_dato} "
        "(el plan solo cubre Santiago y las regiones centrales)")

    dobles = {k: v for k, v in conocidas.items() if v[1] > 150}
    if dobles:
        log(f"  estaciones con dos equipos (duración sumada): {len(dobles)} -> "
            + ", ".join(f"{k}:{v[1]}min" for k, v in sorted(dobles.items())[:8]))

    if args.dry_run:
        log("--dry-run: no se escribe nada.")
        return

    # PATCH fila por fila: toca SOLO estas dos columnas. Un upsert podría
    # insertar una EDS fantasma si el código no existiera.
    ok = err = 0
    for eds, (tipo, mins) in sorted(conocidas.items()):
        r = requests.patch(f"{SUPABASE_URL}/rest/v1/estaciones_servicio",
                           headers={**_sb_headers(), "Content-Type": "application/json"},
                           params={"eds_occim": f"eq.{eds}"},
                           json={"tipo_mantencion": tipo, "duracion_mp_min": mins},
                           timeout=30)
        if r.status_code in (200, 204):
            ok += 1
        else:
            err += 1
            if err <= 3:
                log(f"  ERROR {eds}: {r.status_code} {r.text[:120]}")
    log(f"✔ actualizadas: {ok} | errores: {err}")


if __name__ == "__main__":
    main()
