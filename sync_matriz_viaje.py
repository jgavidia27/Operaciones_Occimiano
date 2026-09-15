"""
sync_matriz_viaje.py
====================
Calcula la matriz de viaje REAL entre EDS (por calle) y la guarda en eds_viaje.

Por qué existe: el planificador agrupaba por distancia en línea recta, y esa
distancia se equivoca por un factor de entre 1,3 y 5,2 según el par. Al no ser
constante, tampoco sirve para comparar: una jornada de 1,7 km en línea recta
resultó ser de 8,8 km de calle.

La matriz es ESTÁTICA —las estaciones no se mueven—, así que se calcula una vez
y el dashboard la lee de la base. Solo hay que volver a correr esto cuando
cambien las coordenadas en eds_geo.

Motor: OSRM público (router.project-osrm.org). Es gratis y no requiere clave,
pero no tiene SLA ni considera tráfico: sus tiempos son de velocidad libre.
Sirven para ORDENAR y COMPARAR rutas, no para prometer una hora de llegada.
Para tráfico real habría que pasar a Google Routes o a un OSRM propio.

Nota: consultar el OSRM público envía las coordenadas de las EDS a un servidor
de terceros. Con --osrm apuntando a una instancia propia, no salen de la red.

Uso:
    python sync_matriz_viaje.py --zona "Santiago (RM)" --dry-run
    python sync_matriz_viaje.py --zona "Santiago (RM)"
    python sync_matriz_viaje.py --todas
"""

import argparse
import time

import requests

from sync_numerales_subtarea import SUPABASE_URL, _sb_headers, log

OSRM_POR_DEFECTO = "https://router.project-osrm.org"
BLOQUE = 45            # coordenadas por petición: el demo público tope ~100
TOPE_MIN = 120         # no se guardan pares más lejanos que esto


def coords_eds(zona: str | None) -> list[tuple[str, float, float]]:
    """EDS con coordenadas, opcionalmente de una sola zona operativa."""
    import mp_planner as mp

    geo = mp._cargar_geo_raw()
    if geo.empty:
        return []
    geo = geo.dropna(subset=["latitud", "longitud"])
    if zona:
        rm = geo["comuna"].isin(mp.COMUNAS_RM)
        geo = geo[rm] if zona.startswith("Santiago") else geo[~rm]
    return [(r.eds_occim, float(r.latitud), float(r.longitud))
            for r in geo.itertuples()]


def tabla_osrm(base_url: str, pts: list, origenes: list, destinos: list) -> dict:
    """Un bloque de la matriz. `origenes`/`destinos` son índices dentro de `pts`."""
    idx = origenes + [k for k in destinos if k not in origenes]
    coords = ";".join(f"{pts[k][2]},{pts[k][1]}" for k in idx)
    params = {
        "sources": ";".join(str(idx.index(k)) for k in origenes),
        "destinations": ";".join(str(idx.index(k)) for k in destinos),
        "annotations": "duration,distance",
    }
    for intento in range(5):
        r = requests.get(f"{base_url}/table/v1/driving/{coords}",
                         params=params, timeout=120)
        if r.status_code == 200:
            j = r.json()
            if j.get("code") == "Ok":
                return j
            raise RuntimeError(f"OSRM respondió {j.get('code')}: {j.get('message')}")
        espera = 2 ** intento
        log(f"    HTTP {r.status_code}; reintento en {espera}s")
        time.sleep(espera)
    raise RuntimeError("OSRM no respondió tras 5 intentos")


def construir(base_url: str, pts: list) -> list[dict]:
    n = len(pts)
    bloques = [list(range(i, min(i + BLOQUE, n))) for i in range(0, n, BLOQUE)]
    log(f"{n} estaciones | {len(bloques)}x{len(bloques)} = {len(bloques) ** 2} peticiones")
    filas, descartados = [], 0
    for bi in bloques:
        for bj in bloques:
            j = tabla_osrm(base_url, pts, bi, bj)
            for a, ia in enumerate(bi):
                for b, jb in enumerate(bj):
                    if ia == jb:
                        continue
                    seg = j["durations"][a][b]
                    if seg is None:
                        continue
                    mins = seg / 60
                    if mins > TOPE_MIN:
                        descartados += 1
                        continue
                    filas.append({
                        "eds_origen": pts[ia][0], "eds_destino": pts[jb][0],
                        "minutos": round(mins, 1),
                        "metros": round(j["distances"][a][b] or 0, 0),
                        "fuente": "osrm",
                    })
            time.sleep(0.8)          # el demo público es de cortesía
    log(f"  pares dentro de {TOPE_MIN} min: {len(filas):,} | descartados por lejanía: {descartados:,}")
    return filas


def guardar(filas: list[dict]) -> int:
    ok = 0
    for i in range(0, len(filas), 500):
        ch = filas[i:i + 500]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/eds_viaje",
                          headers={**_sb_headers(),
                                   "Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates"},
                          json=ch, timeout=120)
        if r.status_code in (200, 201, 204):
            ok += len(ch)
        else:
            log(f"  ERROR {r.status_code}: {r.text[:160]}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zona", help='"Santiago (RM)" o "Regiones".')
    ap.add_argument("--todas", action="store_true", help="Todas las EDS juntas.")
    ap.add_argument("--osrm", default=OSRM_POR_DEFECTO,
                    help="URL del servidor OSRM (usa uno propio si no quieres "
                         "que las coordenadas salgan de tu red).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.todas and not args.zona:
        raise SystemExit("Indica --zona o --todas.")

    pts = coords_eds(None if args.todas else args.zona)
    if not pts:
        raise SystemExit("Sin EDS con coordenadas para esa selección.")
    log(f"EDS a procesar: {len(pts)}")

    if args.dry_run:
        log(f"--dry-run: serían {len(pts) ** 2:,} pares y "
            f"{(len(pts) // BLOQUE + 1) ** 2} peticiones. No se escribe nada.")
        return

    filas = construir(args.osrm, pts)
    n = guardar(filas)
    log(f"✔ eds_viaje: {n:,} pares guardados")


if __name__ == "__main__":
    main()
