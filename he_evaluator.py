"""
he_evaluator.py — Motor de validación de horas extra.
======================================================

Por cada técnico × día evalúa:
  1) Última OT cerrada (Fracttal) — hora y ubicación EDS
  2) Hora de llegada a casa (GPS + geodistancia al domicilio)
  3) Marcación de salida (Buk Asistencia)
  4) Horario de turno (Buk raw_data.turno)

Emite veredicto por día:
  ✅ VALIDA            → HHEE reales
  ⚠️ DUDOSA            → hay HHEE pero con incongruencias (desvíos, marca tardía)
  ❌ NO_CORRESPONDE    → no hay HHEE reales (jornada normal)
  ⚪ SIN_DATOS         → falta info en alguna fuente

Ejecución:
    python he_evaluator.py                     # ultimos 30 días
    python he_evaluator.py --dias 90
    python he_evaluator.py --fecha 2026-07-10  # 1 día específico
    python he_evaluator.py --dry-run           # no escribe a Supabase
    python he_evaluator.py --tecnico "Breyans Toledo Quintana"
"""

import argparse
import json
import math
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone, date, time as dtime
from typing import Optional

import requests


# ── Cargar .env ─────────────────────────────────────────────────────────────
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ── Parámetros del motor (ajustables) ───────────────────────────────────────
RADIO_DOMICILIO_M       = 300      # metros para considerar "en casa" (GPS)
FACTOR_VEL_URBANA       = 2.5      # min por km (velocidad urbana Chile ~24 km/h)
BUFFER_TRASLADO_MIN     = 15       # buffer +15 min sobre tiempo teórico
TOPE_TRASLADO_MIN       = 120      # tope máximo (para trayectos largos tipo interprovincial)
TOLERANCIA_BUK_GPS_MIN  = 30       # tolerancia entre marcación Buk y llegada casa GPS
HORARIO_FIN_DEFAULT     = dtime(18, 0)  # fallback si Buk no trae turno
MIN_HHEE_PARA_PAGAR     = 15       # < 15 min no vale la pena registrar HHEE
TZ_CHILE_OFFSET_H       = -4       # UTC-4 (Chile continental)


# ── Utilidades ──────────────────────────────────────────────────────────────
def _norm_nombre(s: str) -> set[str]:
    """Normaliza un nombre → conjunto de palabras (sin acentos, mayusculas).
    Ej: 'Iván Ignacio Vergara Ferrari' → {'IVAN','IGNACIO','VERGARA','FERRARI'}"""
    if not s: return set()
    s = ''.join(c for c in unicodedata.normalize('NFD', str(s))
                if unicodedata.category(c) != 'Mn')
    return set(w for w in s.upper().split() if len(w) >= 2)


def match_nombre(nombre_fracttal: str, tecnicos_buk: dict) -> Optional[str]:
    """Encuentra el RUT del técnico Buk que matchea con el nombre Fracttal.
    Match: al menos 2 palabras coinciden (típicamente primer nombre + apellido).
    Retorna RUT o None si no matchea o hay ambigüedad."""
    if not nombre_fracttal: return None
    palabras_f = _norm_nombre(nombre_fracttal)
    if len(palabras_f) < 2: return None

    matches = []
    for rut, tec in tecnicos_buk.items():
        palabras_b = _norm_nombre(tec.get("nombre_completo", ""))
        interseccion = palabras_f & palabras_b
        if len(interseccion) >= 2:
            matches.append((rut, len(interseccion)))

    if not matches: return None
    # Si hay ambigüedad, tomar el de más palabras coincidentes
    matches.sort(key=lambda x: -x[1])
    return matches[0][0]


def _sb_headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distancia lineal en METROS entre 2 coordenadas."""
    R = 6371000  # radio Tierra en metros
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    try:
        s2 = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _to_chile(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None: return None
    return dt.astimezone(timezone(timedelta(hours=TZ_CHILE_OFFSET_H)))


def _parse_turno(turno_str: Optional[str]) -> tuple[dtime, dtime]:
    """'09:00-18:00' → (time(9,0), time(18,0)). Si no matchea, retorna default."""
    if not turno_str or not isinstance(turno_str, str):
        return dtime(9, 0), HORARIO_FIN_DEFAULT
    m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", turno_str.strip())
    if not m:
        return dtime(9, 0), HORARIO_FIN_DEFAULT
    return dtime(int(m.group(1)), int(m.group(2))), dtime(int(m.group(3)), int(m.group(4)))


def _fmt_min(m: Optional[int]) -> str:
    if m is None: return "—"
    if m == 0: return "0min"
    h, mn = divmod(abs(int(m)), 60)
    sig = "-" if m < 0 else ""
    return f"{sig}{h}h{mn:02d}min" if h else f"{sig}{mn}min"


# ── Fetch data desde Supabase ───────────────────────────────────────────────
def sb_get_all(tabla: str, params: str) -> list:
    """GET paginado (Supabase tope 1000)."""
    resultados = []
    offset = 0
    while True:
        h = {**_sb_headers(),
             "Range-Unit": "items",
             "Range": f"{offset}-{offset+999}"}
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabla}?{params}",
                         headers=h, timeout=30)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"Supabase {tabla} {r.status_code}: {r.text[:200]}")
        batch = r.json()
        if not batch: break
        resultados.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    return resultados


def fetch_tecnicos() -> dict[str, dict]:
    """{rut: {nombre, email, patente, equipo, domicilio_lat/lng}}."""
    rows = sb_get_all("tecnicos_hhee",
        "select=rut,nombre_completo,email,patente,equipo,domicilio_lat,domicilio_lng"
        "&excluir_hhee=eq.false&activo=eq.true")
    return {r["rut"]: r for r in rows if r.get("rut")}


def fetch_eds_coords() -> dict[str, tuple[float, float]]:
    """{eds_occim: (lat, lng)}. Solo estaciones con coords válidas (a completar)."""
    # Por ahora estaciones_servicio no tiene lat/lng directas. Si están,
    # las usamos; sino retornamos vacío y usamos fallback de tolerancia fija.
    try:
        rows = sb_get_all("estaciones_servicio",
            "select=eds_occim,lat,lng&lat=not.is.null&lng=not.is.null")
        return {r["eds_occim"]: (float(r["lat"]), float(r["lng"]))
                for r in rows if r.get("lat") and r.get("lng")}
    except Exception:
        return {}


def fetch_ots(fecha_ini: str, fecha_fin: str) -> list[dict]:
    """OTs finalizadas en el rango. fecha_ini/fecha_fin en formato YYYY-MM-DD."""
    return sb_get_all("ordenes_trabajo",
        f"select=id_ot,responsable,fecha_finalizacion,codigo_eds"
        f"&fecha_finalizacion=gte.{fecha_ini}T00:00:00"
        f"&fecha_finalizacion=lte.{fecha_fin}T23:59:59"
        f"&completada=eq.true"
        f"&order=fecha_finalizacion.desc")


def fetch_marcaciones(fecha_ini: str, fecha_fin: str) -> list[dict]:
    return sb_get_all("buk_marcaciones",
        f"select=rut,fecha,tipo,hora,raw_data"
        f"&fecha=gte.{fecha_ini}&fecha=lte.{fecha_fin}"
        f"&order=fecha.asc,rut.asc,tipo.asc,hora.asc")


def fetch_gps(fecha_ini: str, fecha_fin: str) -> list[dict]:
    return sb_get_all("gps_eventos",
        f"select=patente,fecha,timestamp,lat,lng,evento"
        f"&fecha=gte.{fecha_ini}&fecha=lte.{fecha_fin}"
        f"&evento=in.(motor_off,motor_on)"
        f"&order=patente.asc,timestamp.asc")


# ── Motor de reglas ─────────────────────────────────────────────────────────
def evaluar_dia(rut: str, tec: dict, fecha: date,
                ots_del_dia: list[dict],
                marcaciones: list[dict],
                gps_eventos: list[dict],
                eds_coords: dict) -> Optional[dict]:
    """Emite 1 veredicto para (técnico, fecha). Retorna None si no hay data mínima."""
    dom_lat = tec.get("domicilio_lat")
    dom_lng = tec.get("domicilio_lng")
    patente = tec.get("patente")
    nombre  = tec.get("nombre_completo", "?")

    # 1) Turno base (de la marcación Buk si la hay)
    marc_ent = next((m for m in marcaciones if m["tipo"] == "entrada"), None)
    marc_sal = next((m for m in marcaciones if m["tipo"] == "salida"), None)
    turno_str = None
    if marc_ent and marc_ent.get("raw_data"):
        turno_str = marc_ent["raw_data"].get("turno")
    elif marc_sal and marc_sal.get("raw_data"):
        turno_str = marc_sal["raw_data"].get("turno")
    turno_ini, turno_fin = _parse_turno(turno_str)

    # 2) Última OT del día (por técnico) — matching tolerante ya hecho arriba
    #    (ots_del_dia ya viene filtrado por RUT del técnico)
    ots_tec = ots_del_dia
    ots_tec.sort(key=lambda o: _parse_ts(o.get("fecha_finalizacion")) or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    ultima_ot = ots_tec[0] if ots_tec else None
    ultima_ot_fin_utc = _parse_ts(ultima_ot["fecha_finalizacion"]) if ultima_ot else None
    ultima_ot_fin_chi = _to_chile(ultima_ot_fin_utc)

    # 3) Lat/lng EDS de la última OT
    ot_lat = ot_lng = None
    if ultima_ot and ultima_ot.get("codigo_eds"):
        ot_lat, ot_lng = eds_coords.get(ultima_ot["codigo_eds"], (None, None))

    # 4) Llegada a casa (GPS motor_off cerca del domicilio, posterior a fin OT)
    llegada_casa = None
    if patente and dom_lat and dom_lng:
        gps_pat = [g for g in gps_eventos
                   if g["patente"] == patente and g.get("evento") == "motor_off"]
        for g in gps_pat:
            g_lat, g_lng = g.get("lat"), g.get("lng")
            if g_lat is None or g_lng is None: continue
            d = haversine_m(dom_lat, dom_lng, float(g_lat), float(g_lng))
            if d <= RADIO_DOMICILIO_M:
                ts = _parse_ts(g["timestamp"])
                if ultima_ot_fin_utc and ts and ts < ultima_ot_fin_utc:
                    continue  # motor_off ANTES de fin OT (mañana)
                llegada_casa = ts
                break

    marcacion_salida_utc = _parse_ts(marc_sal["hora"]) if marc_sal else None
    marcacion_salida_chi = _to_chile(marcacion_salida_utc)

    # 5) Tolerancia dinámica de traslado
    if ot_lat and ot_lng and dom_lat and dom_lng:
        dist_km = haversine_m(ot_lat, ot_lng, dom_lat, dom_lng) / 1000
        tolerancia_min = min(TOPE_TRASLADO_MIN,
                             int(dist_km * FACTOR_VEL_URBANA + BUFFER_TRASLADO_MIN))
    else:
        dist_km = None
        tolerancia_min = 90  # fallback si falta lat/lng

    # 6) Cálculos
    tramo_real_min = None
    if ultima_ot_fin_utc and llegada_casa:
        tramo_real_min = int((llegada_casa - ultima_ot_fin_utc).total_seconds() / 60)

    diff_buk_gps_min = None
    if llegada_casa and marcacion_salida_utc:
        diff_buk_gps_min = int((marcacion_salida_utc - llegada_casa).total_seconds() / 60)

    # HHEE candidatas: minutos entre turno_fin y llegada_casa
    # (llegar a casa antes del turno_fin no genera HHEE)
    hhee_min = None
    llegada_casa_chi = _to_chile(llegada_casa)
    if llegada_casa_chi:
        turno_fin_dt = datetime.combine(fecha, turno_fin,
                                          tzinfo=timezone(timedelta(hours=TZ_CHILE_OFFSET_H)))
        # HHEE = tiempo desde turno_fin hasta que llegó a casa MENOS el tramo esperado
        # (el traslado del trabajo a casa no cuenta como HHEE)
        if llegada_casa_chi > turno_fin_dt and ultima_ot_fin_chi:
            fin_ot_o_turno = max(ultima_ot_fin_chi, turno_fin_dt)
            hhee_min = int((llegada_casa_chi - fin_ot_o_turno).total_seconds() / 60)
            # Restar 30 min: el técnico debe marcar 30 min antes de llegar a casa
            hhee_min = max(0, hhee_min - 30)
        else:
            hhee_min = 0

    # 7) Veredicto
    razones = []
    veredicto = "sin_datos"

    if not ultima_ot_fin_utc:
        veredicto = "sin_datos"
        razones.append("Sin OTs cerradas registradas")
    elif not llegada_casa:
        veredicto = "sin_datos"
        razones.append(f"Sin GPS motor_off cerca de casa (radio {RADIO_DOMICILIO_M}m)"
                       f" posterior a la última OT")
    else:
        # Ya tenemos OT + llegada. ¿Hay HHEE?
        if not hhee_min or hhee_min < MIN_HHEE_PARA_PAGAR:
            veredicto = "no_corresponde"
            razones.append(f"Llegada a casa dentro/cerca del horario: "
                           f"turno {turno_fin.strftime('%H:%M')}, "
                           f"llegada {llegada_casa_chi.strftime('%H:%M') if llegada_casa_chi else '—'}")
        else:
            # Hay HHEE candidatas. Validar consistencia
            razones_dudosas = []
            if tramo_real_min and tolerancia_min and tramo_real_min > tolerancia_min:
                exceso = tramo_real_min - tolerancia_min
                razones_dudosas.append(
                    f"Traslado OT→casa: {_fmt_min(tramo_real_min)} vs esperado "
                    f"{_fmt_min(tolerancia_min)} (exceso {_fmt_min(exceso)})"
                )
            if diff_buk_gps_min is not None and diff_buk_gps_min > TOLERANCIA_BUK_GPS_MIN:
                razones_dudosas.append(
                    f"Marcación Buk {_fmt_min(diff_buk_gps_min)} DESPUÉS de llegar a casa"
                )
            if razones_dudosas:
                veredicto = "dudosa"
                razones.extend(razones_dudosas)
            else:
                veredicto = "valida"
                razones.append(
                    f"HHEE = {_fmt_min(hhee_min)} · traslado normal · "
                    f"marcación consistente ({_fmt_min(diff_buk_gps_min)} de dif con GPS)"
                )

    return {
        "rut":                       rut,
        "fecha":                     fecha.isoformat(),
        "ultima_ot_folio":           ultima_ot["id_ot"] if ultima_ot else None,
        "ultima_ot_fin":             ultima_ot["fecha_finalizacion"] if ultima_ot else None,
        "ultima_ot_lat":             ot_lat,
        "ultima_ot_lng":             ot_lng,
        "ultima_ot_eds":             ultima_ot.get("codigo_eds") if ultima_ot else None,
        "llegada_casa_estimada":     llegada_casa.isoformat() if llegada_casa else None,
        "marca_entrada":             marc_ent["hora"] if marc_ent else None,
        "marca_salida":              marc_sal["hora"] if marc_sal else None,
        "tramo_esperado_min":        tolerancia_min if dist_km else None,
        "tramo_real_min":            tramo_real_min,
        "hhee_declaradas_min":       None,  # a poblar cuando integremos buk_horas_extras
        "hhee_validadas_min":        hhee_min if veredicto == "valida" else 0,
        "veredicto":                 veredicto,
        "razon":                     " · ".join(razones)[:490],
        "evidencia_json":            {
            "nombre": nombre,
            "patente": patente,
            "turno": turno_str or f"default {turno_ini.strftime('%H:%M')}-{turno_fin.strftime('%H:%M')}",
            "dist_km_OT_a_casa": round(dist_km, 2) if dist_km else None,
            "tolerancia_min":     tolerancia_min,
            "diff_buk_gps_min":   diff_buk_gps_min,
            "params": {
                "radio_domicilio_m":       RADIO_DOMICILIO_M,
                "factor_vel_urbana":       FACTOR_VEL_URBANA,
                "tolerancia_buk_gps_min":  TOLERANCIA_BUK_GPS_MIN,
            },
        },
    }


# ── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias",  type=int, default=30, help="Rango hacia atras (default 30)")
    ap.add_argument("--fecha", help="Fecha unica YYYY-MM-DD (sobrescribe --dias)")
    ap.add_argument("--desde", help="Fecha desde YYYY-MM-DD")
    ap.add_argument("--hasta", help="Fecha hasta YYYY-MM-DD")
    ap.add_argument("--tecnico", help="Filtrar por nombre completo (subcadena)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap.parse_args()


def log_start(script):
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/hhee_sync_logs",
                          headers={**_sb_headers(), "Prefer": "return=representation"},
                          json={"script": script, "estado": "running"}, timeout=10)
        if r.status_code in (200, 201) and r.json():
            return r.json()[0].get("id")
    except Exception: pass
    return None


def log_end(log_id, estado, filas, mensaje=""):
    if not log_id: return
    try:
        requests.patch(f"{SUPABASE_URL}/rest/v1/hhee_sync_logs?id=eq.{log_id}",
                       headers={**_sb_headers(), "Prefer": "return=minimal"},
                       json={"estado": estado, "filas_upserted": filas,
                             "mensaje": (mensaje or "")[:500] or None,
                             "fin": datetime.now(timezone.utc).isoformat()},
                       timeout=10)
    except Exception: pass


def main():
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

    args = parse_args()

    # Rango de fechas
    if args.fecha:
        desde = hasta = args.fecha
    elif args.desde and args.hasta:
        desde, hasta = args.desde, args.hasta
    else:
        hoy = datetime.now(timezone(timedelta(hours=TZ_CHILE_OFFSET_H))).date()
        desde = (hoy - timedelta(days=args.dias)).isoformat()
        hasta = hoy.isoformat()

    print(f"[he_evaluator] Rango: {desde} → {hasta}  (dry-run={args.dry_run})")
    log_id = log_start("he_evaluator") if not args.dry_run else None

    try:
        print("[1/5] Cargando técnicos...")
        tecnicos = fetch_tecnicos()
        if args.tecnico:
            filtro = args.tecnico.lower()
            tecnicos = {r: t for r, t in tecnicos.items()
                        if filtro in (t.get("nombre_completo") or "").lower()}
        print(f"      {len(tecnicos)} técnicos activos.")

        print("[2/5] Cargando lat/lng de EDS (para tolerancia dinámica)...")
        eds_coords = fetch_eds_coords()
        print(f"      {len(eds_coords)} EDS con coords (resto usa fallback 90 min).")

        print(f"[3/5] Cargando OTs finalizadas {desde}..{hasta}...")
        ots = fetch_ots(desde, hasta)
        print(f"      {len(ots)} OTs cerradas.")

        print(f"[4/5] Cargando marcaciones Buk + GPS...")
        marcaciones_all = fetch_marcaciones(desde, hasta)
        gps_all = fetch_gps(desde, hasta)
        print(f"      {len(marcaciones_all)} marcaciones, {len(gps_all)} eventos GPS.")

        # Pre-mapear cada OT al RUT del tecnico (matching tolerante Fracttal→Buk)
        print(f"[5/5] Matcheando responsables OT → técnicos...")
        ot_a_rut: dict[int, Optional[str]] = {}
        no_matcheados: set[str] = set()
        for i, ot in enumerate(ots):
            resp = (ot.get("responsable") or "").strip()
            if not resp:
                ot_a_rut[i] = None
                continue
            rut_match = match_nombre(resp, tecnicos)
            ot_a_rut[i] = rut_match
            if not rut_match and resp:
                no_matcheados.add(resp)
        matched = sum(1 for v in ot_a_rut.values() if v)
        print(f"      {matched}/{len(ots)} OTs matcheadas con técnico.")
        if no_matcheados:
            print(f"      No matcheados ({len(no_matcheados)}): "
                  f"{list(no_matcheados)[:8]}{'...' if len(no_matcheados)>8 else ''}")

        print(f"      Evaluando técnico × día...")
        # Pre-computar OTs por (rut, fecha_chile)
        ots_por_rut_fecha: dict[tuple[str, date], list[dict]] = {}
        for i, ot in enumerate(ots):
            rut = ot_a_rut.get(i)
            if not rut: continue
            fecha_utc = _parse_ts(ot.get("fecha_finalizacion"))
            fecha_chi = _to_chile(fecha_utc)
            if not fecha_chi: continue
            key = (rut, fecha_chi.date())
            ots_por_rut_fecha.setdefault(key, []).append(ot)

        # Marcaciones por (rut, fecha)
        marc_por = {}
        for m in marcaciones_all:
            marc_por.setdefault((m["rut"], m["fecha"]), []).append(m)
        # GPS por (patente, fecha)
        gps_por = {}
        for g in gps_all:
            gps_por.setdefault((g["patente"], g["fecha"]), []).append(g)

        d0 = date.fromisoformat(desde); d1 = date.fromisoformat(hasta)
        veredictos = []
        cur = d0
        while cur <= d1:
            fs = cur.isoformat()
            for rut, tec in tecnicos.items():
                ots_dia = ots_por_rut_fecha.get((rut, cur), [])
                marc_dia = marc_por.get((rut, fs), [])
                gps_dia = gps_por.get((tec.get("patente") or "?", fs), [])

                v = evaluar_dia(rut, tec, cur, ots_dia, marc_dia, gps_dia, eds_coords)
                if v: veredictos.append(v)
            cur += timedelta(days=1)

        # Resumen por veredicto
        from collections import Counter
        conteo = Counter(v["veredicto"] for v in veredictos)
        print(f"\n=== RESULTADO ({len(veredictos)} evaluaciones) ===")
        for k, n in sorted(conteo.items(), key=lambda x: -x[1]):
            print(f"  {k:<20} : {n:>4}")

        # Mostrar top DUDOSAS y VALIDAS
        for tag in ("dudosa", "valida"):
            top = [v for v in veredictos if v["veredicto"] == tag][:10]
            if top:
                print(f"\n--- Ejemplos {tag.upper()} ---")
                for v in top:
                    t = tecnicos[v["rut"]]
                    print(f"  {v['fecha']} | {t['nombre_completo'][:30]:<30} | "
                          f"HHEE={v['hhee_validadas_min']}min | {v['razon'][:100]}")

        if args.dry_run:
            print(f"\n[DRY-RUN] No se escribió a hhee_veredictos.")
            return 0

        # Upsert en lotes.
        # OJO: hhee_veredictos tiene PK id (autoincrement) + UNIQUE(rut, fecha).
        # Para que PostgREST use esa UNIQUE constraint como dedup, hay que
        # pasar ?on_conflict=rut,fecha en la URL.
        print(f"\n[UPSERT] Escribiendo {len(veredictos)} veredictos...")
        for i in range(0, len(veredictos), 500):
            batch = veredictos[i:i+500]
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/hhee_veredictos?on_conflict=rut,fecha",
                headers={**_sb_headers(),
                         "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=batch, timeout=45,
            )
            if r.status_code not in (200, 201, 204):
                raise RuntimeError(f"Upsert {r.status_code}: {r.text[:300]}")
        print(f"  OK.")
        log_end(log_id, "success", len(veredictos), f"{desde}..{hasta}")
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\nERROR: {e}\n{tb}", file=sys.stderr)
        log_end(log_id, "error", 0, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
