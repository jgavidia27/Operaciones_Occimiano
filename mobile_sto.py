"""
mobile_sto.py — Vista móvil del Desempeño STO (Flask)
=====================================================
Lee los datos pre-calculados por el dashboard Streamlit (sto_data.json).
NO computa KPIs por su cuenta — es un visor puro del dashboard.

Ejecutar local:   python mobile_sto.py
Deploy Render:    gunicorn mobile_sto:app
"""

import json, os, traceback, secrets
from datetime import datetime, date, timedelta

from flask import Flask, request, render_template_string, send_file, redirect, url_for, session

from mobile_auth import (
    verify_pin, logout as auth_logout, current_user, requires_auth,
    get_user_info, SESSION_TTL_DAYS, requires_admin,
    USERS, ADMINS, get_all_pins, set_pin, generate_all_pins, get_pin,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=SESSION_TTL_DAYS)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_STO_DATA_PATH = os.path.join(_APP_DIR, "sto_data.json")

TRIMESTRES = {
    "T1": {"label": "T1 · Ene–Mar", "meses": [1, 2, 3]},
    "T2": {"label": "T2 · Abr–Jun", "meses": [4, 5, 6]},
    "T3": {"label": "T3 · Jul–Sep", "meses": [7, 8, 9]},
    "T4": {"label": "T4 · Oct–Dic", "meses": [10, 11, 12]},
}

BONO_TOTAL = 500_000
MAX_SLA = int(BONO_TOTAL * 0.40)
MAX_CAL = int(BONO_TOTAL * 0.30)
MAX_PREC = int(BONO_TOTAL * 0.30)


def _bono_sla(pct):
    m = MAX_SLA
    if pct >= 95: return 100, m
    if pct >= 93: return 90, int(m * .90)
    if pct >= 90: return 80, int(m * .80)
    if pct >= 85: return 50, int(m * .50)
    return 0, 0


def _bono_calidad(n_fallas, n_pms):
    exactitud = (1 - n_fallas / n_pms) * 100 if n_pms > 0 else (100.0 if n_fallas == 0 else 0.0)
    m = MAX_CAL
    if exactitud >= 98: return 100, m, exactitud
    if exactitud >= 96: return 90, int(m * .90), exactitud
    if exactitud >= 94: return 80, int(m * .80), exactitud
    if exactitud >= 92: return 70, int(m * .70), exactitud
    if exactitud >= 90: return 60, int(m * .60), exactitud
    return 0, 0, exactitud


def _bono_prec(pct):
    m = MAX_PREC
    if pct >= 95: return 100, m
    if pct >= 90: return 90, int(m * .90)
    if pct >= 85: return 80, int(m * .80)
    if pct >= 80: return 70, int(m * .70)
    if pct >= 75: return 60, int(m * .60)
    if pct >= 70: return 50, int(m * .50)
    return 0, 0


def _color_pct(pct):
    if pct >= 95: return "#22c55e"
    if pct >= 85: return "#f59e0b"
    return "#ef4444"


def _nivel_color(niv):
    if niv >= 90: return "#22c55e"
    if niv >= 70: return "#4ade80"
    if niv >= 50: return "#f59e0b"
    if niv > 0: return "#f97316"
    return "#ef4444"


def _clp_fmt(v):
    if v is None: return "$0"
    return f'${int(v):,.0f}'.replace(',', '.')


def _load_sto_data():
    # Primero intentar Supabase (deploy remoto)
    try:
        import requests as _req
        _sb_url = os.getenv("SUPABASE_URL", "")
        _sb_key = os.getenv("SUPABASE_KEY", "")
        if _sb_url and _sb_key:
            r = _req.get(
                f"{_sb_url}/rest/v1/sto_data_export?id=eq.latest&select=data",
                headers={"apikey": _sb_key, "Authorization": f"Bearer {_sb_key}"},
                timeout=10,
            )
            if r.status_code == 200:
                rows = r.json()
                if rows and isinstance(rows, list) and rows[0].get("data"):
                    return rows[0]["data"]
    except Exception:
        pass
    # Fallback: archivo local
    if not os.path.exists(_STO_DATA_PATH):
        return None
    with open(_STO_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/wallpaper")
def wallpaper():
    return send_file(os.path.join(_APP_DIR, "wallpaper_light.jpg"), mimetype="image/jpeg")


@app.route("/bg-mobile.png")
def bg_mobile():
    return send_file(os.path.join(_APP_DIR, "bg_mobile.png"), mimetype="image/png")


@app.route("/manifest.json")
def manifest():
    return {
        "name": "Indicadores Operacionales - Occim",
        "short_name": "Occim STO",
        "description": "Desempeño STO - versión mobile",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#151B23",
        "theme_color": "#00897B",
        "orientation": "portrait",
        "icons": [
            {"src": "/app-icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
    }


@app.route("/app-icon.svg")
def app_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0%" stop-color="#004D40"/><stop offset="100%" stop-color="#26C6DA"/>
</linearGradient></defs>
<rect width="512" height="512" rx="80" fill="url(#g)"/>
<text x="256" y="300" font-size="280" text-anchor="middle" dominant-baseline="central"
  font-family="Arial,sans-serif" fill="white" font-weight="bold">STO</text>
<text x="256" y="430" font-size="80" text-anchor="middle" fill="#4DD0E1"
  font-family="Arial,sans-serif">OCCIM</text>
</svg>"""
    return svg, 200, {"Content-Type": "image/svg+xml"}


@app.route("/login", methods=["GET", "POST"])
def login_page():
    msg = ""
    msg_kind = "info"
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pin = request.form.get("pin", "").strip()
        ok, m = verify_pin(email, pin)
        if ok:
            return redirect(request.args.get("next") or url_for("index"))
        msg = m
        msg_kind = "error"
    return render_template_string(LOGIN_TEMPLATE, msg=msg, msg_kind=msg_kind)


@app.route("/logout")
def logout_page():
    auth_logout()
    return redirect(url_for("login_page"))


@app.route("/cambiar-pin", methods=["GET", "POST"])
@requires_auth
def cambiar_pin():
    user = current_user()
    msg = ""
    msg_kind = ""
    if request.method == "POST":
        pin_actual = request.form.get("pin_actual", "").strip()
        pin_nuevo = request.form.get("pin_nuevo", "").strip()
        pin_confirm = request.form.get("pin_confirm", "").strip()
        stored = get_pin(user["email"])
        if pin_actual != stored:
            msg, msg_kind = "El PIN actual es incorrecto.", "error"
        elif len(pin_nuevo) != 4 or not pin_nuevo.isdigit():
            msg, msg_kind = "El nuevo PIN debe ser de 4 digitos.", "error"
        elif pin_nuevo != pin_confirm:
            msg, msg_kind = "Los PINs nuevos no coinciden.", "error"
        elif pin_nuevo == pin_actual:
            msg, msg_kind = "El nuevo PIN debe ser diferente al actual.", "error"
        else:
            if set_pin(user["email"], pin_nuevo):
                msg, msg_kind = "PIN actualizado correctamente.", "success"
            else:
                msg, msg_kind = "Error al guardar. Intenta nuevamente.", "error"
    return render_template_string(CAMBIAR_PIN_TEMPLATE, user=user, msg=msg, msg_kind=msg_kind)


@app.route("/admin/pins", methods=["GET", "POST"])
@requires_auth
def admin_pins():
    u = current_user()
    if not u or u.get("email") != "jgavidia@occimiano.cl":
        return redirect(url_for("index"))
    msg = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "generate_all":
            n = generate_all_pins()
            msg = f"{n} PINs generados." if n else "Todos ya tienen PIN."
        elif action == "set_pin":
            email = request.form.get("email", "").strip().lower()
            new_pin = request.form.get("new_pin", "").strip()
            if len(new_pin) == 4 and new_pin.isdigit():
                if set_pin(email, new_pin):
                    msg = f"PIN actualizado para {email}."
                else:
                    msg = "Error guardando PIN."
            else:
                msg = "El PIN debe ser de 4 dígitos."
    pins = get_all_pins()
    all_users = []
    for email, info in sorted(USERS.items(), key=lambda x: x[1]["team"]):
        all_users.append({"email": email, "short": info["short"], "team": info["team"], "pin": pins.get(email, "—")})
    for email in sorted(ADMINS):
        all_users.append({"email": email, "short": "Admin", "team": "Admin", "pin": pins.get(email, "—")})
    return render_template_string(ADMIN_PINS_TEMPLATE, users=all_users, msg=msg)


@app.route("/")
@requires_auth
def index():
  try:
    user = current_user()
    data = _load_sto_data()
    if data is None:
        return render_template_string(ERROR_TEMPLATE,
            msg="No se encontró sto_data.json. Abre el dashboard Streamlit (pantalla Desempeño STO) para generar los datos.",
            updated_at="—"), 503

    _hoy_mes = date.today().month
    _trim_default = next((k for k, v in TRIMESTRES.items() if _hoy_mes in v["meses"]), "T2")
    trim_key = request.args.get("trim", _trim_default)
    mes_sel = request.args.get("mes", "")
    tecnico_sel = request.args.get("tecnico", "")
    equipo_sel = request.args.get("equipo", "")
    tab_sel = request.args.get("tab", "sla")

    # ── Forzar filtros según permisos del usuario ──────────────────────
    if not user["is_admin"]:
        equipo_sel = user["team"]  # solo su equipo, no permitir cambiar

    trim = TRIMESTRES.get(trim_key, TRIMESTRES[_trim_default])
    meses_filtro = trim["meses"]
    if mes_sel:
        try:
            mes_num = int(mes_sel)
            if mes_num in meses_filtro:
                meses_filtro = [mes_num]
        except ValueError:
            pass

    equipos_info = data.get("equipos", {})
    full_to_short = data.get("full_to_short", {})
    tech_name_map = data.get("tech_name_map", {})
    seniors = set(data.get("seniors", []))
    for _eq_v in equipos_info.values():
        _snr = _eq_v.get("senior", "")
        if _snr:
            seniors.add(_snr)

    equipos_label = {k: v.get("label", k) for k, v in equipos_info.items()}

    # Lista de técnicos para selector
    all_tecnicos = []
    for eq_key, eq_info in equipos_info.items():
        for t_full in eq_info.get("miembros", []):
            t_short = full_to_short.get(t_full, t_full)
            all_tecnicos.append({"full": t_full, "short": t_short, "equipo": eq_info.get("label", eq_key)})
    all_tecnicos.sort(key=lambda x: x["short"])

    # Helper: asegura que TODOS los seniors aparezcan por cada equipo que lideran,
    # aunque no ejecuten trabajos directamente a su nombre. Los seniors muestran
    # SIEMPRE el promedio de su equipo (etiqueta PROMEDIO EQUIPO).
    # `agg_key` = clave del dict agregado por equipo ("prec", "sla", "cal").
    def _ensure_seniors_in_ranking(rows_list, agg_by_eq, build_row_fn):
        """rows_list: lista actual de dicts con al menos nombre/equipo/es_senior.
           agg_by_eq: dict {eq_key: aggregated_data} para calcular métricas de equipo.
           build_row_fn(senior_short, eq_key, eq_data) → dict con la fila del senior.
           Deduplica por (nombre_short, equipo_label) para no repetir."""
        # Filtrar respetando filtros activos (equipo_sel, tecnico_sel)
        if tecnico_sel and not _is_senior_row_of_selected(tecnico_sel):
            # Si el usuario filtró por un técnico no-senior, no agregamos seniors.
            return rows_list
        _present = {(r["nombre"], r["equipo"]) for r in rows_list}
        for eq_key, eq_info in equipos_info.items():
            if equipo_sel and eq_key != equipo_sel:
                continue
            senior_short = eq_info.get("senior", "")
            if not senior_short or senior_short not in seniors:
                continue
            eq_label = equipos_label.get(eq_key, eq_key)
            if (senior_short, eq_label) in _present:
                continue
            eq_data = agg_by_eq.get(eq_key)
            if not eq_data:
                continue
            new_row = build_row_fn(senior_short, eq_key, eq_data)
            if new_row:
                rows_list.append(new_row)
        return rows_list

    def _is_senior_row_of_selected(tec_full):
        """True si el técnico seleccionado en el filtro es senior."""
        return full_to_short.get(tec_full, tec_full) in seniors

    # ═══ AGREGAR DATOS DEL DASHBOARD ═══

    # SLA — filtrar por mes, equipo, técnico
    sla_raw = [r for r in data.get("sla", []) if r.get("mes_num") in meses_filtro]
    if equipo_sel:
        sla_raw = [r for r in sla_raw if r.get("equipo") == equipo_sel]
    if tecnico_sel:
        sla_raw = [r for r in sla_raw if r.get("tecnico") == tecnico_sel]

    sla_cumple = sum(int(r.get("cumple", 0)) for r in sla_raw)
    sla_total = sum(int(r.get("total", 0)) for r in sla_raw)
    sla_pct = round(sla_cumple / sla_total * 100, 1) if sla_total > 0 else 0
    bp_sla, bc_sla = _bono_sla(sla_pct)

    # SLA por equipo
    sla_equipos = []
    if not equipo_sel and not tecnico_sel:
        eq_data = {}
        for r in sla_raw:
            eq = r.get("equipo", "")
            if eq not in eq_data:
                eq_data[eq] = {"cumple": 0, "total": 0}
            eq_data[eq]["cumple"] += int(r.get("cumple", 0))
            eq_data[eq]["total"] += int(r.get("total", 0))
        for eq, d in eq_data.items():
            if eq and eq in equipos_info:
                pct = round(d["cumple"] / d["total"] * 100, 1) if d["total"] > 0 else 0
                bp, bc = _bono_sla(pct)
                sla_equipos.append({
                    "nombre": equipos_label.get(eq, eq),
                    "senior": equipos_info[eq].get("senior", ""),
                    "pct": pct, "cumple": d["cumple"], "total": d["total"],
                    "bono_pct": bp, "bono_clp": bc,
                })

    # SLA por técnico (ranking)
    sla_tecnicos = []
    tec_data = {}
    for r in [r for r in data.get("sla", []) if r.get("mes_num") in meses_filtro]:
        if equipo_sel and r.get("equipo") != equipo_sel:
            continue
        t = r.get("tecnico", "")
        eq = r.get("equipo", "")
        if t not in tec_data:
            tec_data[t] = {"equipo": eq, "cumple": 0, "total": 0}
        tec_data[t]["cumple"] += int(r.get("cumple", 0))
        tec_data[t]["total"] += int(r.get("total", 0))
    _sla_eq_agg = {}
    for t, d in tec_data.items():
        eq = d["equipo"]
        if eq not in _sla_eq_agg:
            _sla_eq_agg[eq] = {"cumple": 0, "total": 0}
        _sla_eq_agg[eq]["cumple"] += int(d["cumple"])
        _sla_eq_agg[eq]["total"] += int(d["total"])
    for t, d in tec_data.items():
        if not t or d["total"] == 0:
            continue
        t_short = full_to_short.get(t, t)
        _is_snr = t_short in seniors
        if _is_snr:
            eq_d = _sla_eq_agg.get(d["equipo"], d)
            c, tot = eq_d["cumple"], eq_d["total"]
            pct = round(c / tot * 100, 1) if tot > 0 else 0
        else:
            c, tot = d["cumple"], d["total"]
            pct = round(c / tot * 100, 1)
        bp, bc = _bono_sla(pct)
        sla_tecnicos.append({
            "nombre": t_short,
            "equipo": equipos_label.get(d["equipo"], d["equipo"]),
            "pct": pct, "cumple": c, "total": tot,
            "bono_pct": bp, "bono_clp": bc, "es_senior": _is_snr,
        })
    # Rellenar seniors faltantes por cada equipo que lideran
    def _build_sla_senior_row(snr_short, eq_key, eq_d):
        tot = eq_d.get("total", 0)
        if tot <= 0: return None
        c = eq_d.get("cumple", 0)
        pct = round(c / tot * 100, 1)
        bp, bc = _bono_sla(pct)
        return {"nombre": snr_short, "equipo": equipos_label.get(eq_key, eq_key),
                "pct": pct, "cumple": c, "total": tot,
                "bono_pct": bp, "bono_clp": bc, "es_senior": True}
    sla_tecnicos = _ensure_seniors_in_ranking(sla_tecnicos, _sla_eq_agg, _build_sla_senior_row)
    sla_tecnicos.sort(key=lambda x: x["pct"], reverse=True)

    sla_data = {"pct": sla_pct, "cumple": sla_cumple, "total": sla_total,
                "bono_pct": bp_sla, "bono_clp": bc_sla,
                "equipos": sla_equipos, "tecnicos": sla_tecnicos}

    # PRECISIÓN — filtrar por mes, equipo, técnico
    prec_raw = [r for r in data.get("precision", []) if r.get("mes_num") in meses_filtro]
    if equipo_sel:
        prec_raw = [r for r in prec_raw if r.get("equipo") == equipo_sel]

    # Seniors → mostrar promedio del equipo completo (base para su bono)
    _t_short_mob = full_to_short.get(tecnico_sel, tecnico_sel) if tecnico_sel else ""
    _is_senior_mob = tecnico_sel and _t_short_mob in seniors
    if tecnico_sel:
        if _is_senior_mob:
            _eq_key_mob = next((eq for eq, info in equipos_info.items()
                if tecnico_sel in info.get("miembros", [])), "")
            if _eq_key_mob:
                prec_raw = [r for r in prec_raw if r.get("equipo") == _eq_key_mob]
        else:
            prec_raw = [r for r in prec_raw if r.get("tecnico") == tecnico_sel]

    prec_buenas = sum(int(r.get("buenas", 0)) for r in prec_raw)
    prec_total = sum(int(r.get("total", 0)) for r in prec_raw)
    prec_pct = round(prec_buenas / prec_total * 100, 1) if prec_total > 0 else 0
    bp_prec, bc_prec = _bono_prec(prec_pct)

    # Precisión por técnico
    prec_tecnicos = []
    ptec_data = {}
    for r in [r for r in data.get("precision", []) if r.get("mes_num") in meses_filtro]:
        if equipo_sel and r.get("equipo") != equipo_sel:
            continue
        t = r.get("tecnico", "")
        eq = r.get("equipo", "")
        if t not in ptec_data:
            ptec_data[t] = {"equipo": eq, "buenas": 0, "total": 0}
        ptec_data[t]["buenas"] += int(r.get("buenas", 0))
        ptec_data[t]["total"] += int(r.get("total", 0))
    # Agregar promedio equipo por senior (sumar todos los miembros)
    _eq_agg = {}
    for t, d in ptec_data.items():
        eq = d["equipo"]
        if eq not in _eq_agg:
            _eq_agg[eq] = {"buenas": 0, "total": 0}
        _eq_agg[eq]["buenas"] += int(d["buenas"])
        _eq_agg[eq]["total"] += int(d["total"])

    for t, d in ptec_data.items():
        if not t or d["total"] == 0:
            continue
        t_short = full_to_short.get(t, t)
        _is_snr = t_short in seniors
        if _is_snr:
            # Senior → usar datos agregados del equipo
            eq_key = d["equipo"]
            eq_d = _eq_agg.get(eq_key, d)
            b, tot = eq_d["buenas"], eq_d["total"]
            pct = round(b / tot * 100, 1) if tot > 0 else 0
            bp, bc = _bono_prec(pct)
            prec_tecnicos.append({
                "nombre": t_short,
                "equipo": equipos_label.get(d["equipo"], d["equipo"]),
                "pct": pct, "buenas": b, "total": tot, "bono_clp": bc,
                "es_senior": True,
            })
        else:
            pct = round(d["buenas"] / d["total"] * 100, 1)
            bp, bc = _bono_prec(pct)
            prec_tecnicos.append({
                "nombre": t_short,
                "equipo": equipos_label.get(d["equipo"], d["equipo"]),
                "pct": pct, "buenas": d["buenas"], "total": d["total"], "bono_clp": bc,
                "es_senior": False,
            })
    # Rellenar seniors faltantes por cada equipo que lideran
    def _build_prec_senior_row(snr_short, eq_key, eq_d):
        tot = eq_d.get("total", 0)
        if tot <= 0: return None
        b = eq_d.get("buenas", 0)
        pct = round(b / tot * 100, 1)
        bp, bc = _bono_prec(pct)
        return {"nombre": snr_short, "equipo": equipos_label.get(eq_key, eq_key),
                "pct": pct, "buenas": b, "total": tot, "bono_clp": bc,
                "es_senior": True}
    prec_tecnicos = _ensure_seniors_in_ranking(prec_tecnicos, _eq_agg, _build_prec_senior_row)
    prec_tecnicos.sort(key=lambda x: x["pct"], reverse=True)

    prec_data = {"pct": prec_pct, "buenas": prec_buenas, "total": prec_total,
                 "bono_pct": bp_prec, "bono_clp": bc_prec, "tecnicos": prec_tecnicos}

    # EFECTIVIDAD — reincidencias + PMs
    reinc_raw = [r for r in data.get("reincidencias", []) if r.get("mes_num") in meses_filtro]
    pm_raw = [r for r in data.get("pms", []) if r.get("mes_num") in meses_filtro]
    if equipo_sel:
        reinc_raw = [r for r in reinc_raw if r.get("equipo") == equipo_sel]
        pm_raw = [r for r in pm_raw if r.get("equipo") == equipo_sel]
    if tecnico_sel:
        t_short = full_to_short.get(tecnico_sel, tecnico_sel)
        reinc_raw = [r for r in reinc_raw if r.get("tecnico_short") == t_short]
        pm_raw = [r for r in pm_raw if r.get("tecnico") == tecnico_sel]

    fallas_total = sum(int(r.get("fallas", 0)) for r in reinc_raw)
    pms_total = sum(int(r.get("pms", 0)) for r in pm_raw)
    bp_cal, bc_cal, exactitud = _bono_calidad(fallas_total, pms_total)

    # Efectividad por técnico
    cal_tecnicos = []
    # PMs por técnico
    pm_by_tec = {}
    for r in [r for r in data.get("pms", []) if r.get("mes_num") in meses_filtro]:
        if equipo_sel and r.get("equipo") != equipo_sel:
            continue
        t = r.get("tecnico", "")
        eq = r.get("equipo", "")
        if t not in pm_by_tec:
            pm_by_tec[t] = {"equipo": eq, "pms": 0}
        pm_by_tec[t]["pms"] += int(r.get("pms", 0))
    # Fallas por técnico (short name)
    fallas_by_short = {}
    for r in [r for r in data.get("reincidencias", []) if r.get("mes_num") in meses_filtro]:
        if equipo_sel and r.get("equipo") != equipo_sel:
            continue
        ts = r.get("tecnico_short", "")
        if ts not in fallas_by_short:
            fallas_by_short[ts] = 0
        fallas_by_short[ts] += int(r.get("fallas", 0))

    _cal_eq_pm = {}
    _cal_eq_f = {}
    for t, d in pm_by_tec.items():
        eq = d["equipo"]
        _cal_eq_pm[eq] = _cal_eq_pm.get(eq, 0) + d["pms"]
    for ts, n_f in fallas_by_short.items():
        t_full = tech_name_map.get(ts, ts)
        eq = pm_by_tec.get(t_full, {}).get("equipo", "")
        if eq:
            _cal_eq_f[eq] = _cal_eq_f.get(eq, 0) + n_f
    for t, d in pm_by_tec.items():
        t_short = full_to_short.get(t, t)
        _is_snr = t_short in seniors
        if _is_snr:
            eq = d["equipo"]
            n_f = _cal_eq_f.get(eq, 0)
            n_pm = _cal_eq_pm.get(eq, 0)
        else:
            n_f = fallas_by_short.get(t_short, 0)
            n_pm = d["pms"]
        _, bc_t, ex_t = _bono_calidad(n_f, n_pm)
        cal_tecnicos.append({
            "nombre": t_short,
            "equipo": equipos_label.get(d["equipo"], d["equipo"]),
            "exactitud": round(ex_t, 1), "fallas": n_f, "pms": n_pm, "bono_clp": bc_t,
            "es_senior": _is_snr,
        })
    # Rellenar seniors faltantes: los seniors que no ejecutan PMs a su nombre
    # (Luis Pinto, Victor Bahamonde) igualmente deben aparecer mostrando el
    # promedio del equipo — es la base de su bono de Efectividad MP.
    _cal_eq_agg = {eq: {"fallas": _cal_eq_f.get(eq, 0), "pms": _cal_eq_pm.get(eq, 0)}
                   for eq in equipos_info}
    def _build_cal_senior_row(snr_short, eq_key, eq_d):
        n_pm = eq_d.get("pms", 0)
        if n_pm <= 0: return None
        n_f = eq_d.get("fallas", 0)
        _, bc_t, ex_t = _bono_calidad(n_f, n_pm)
        return {"nombre": snr_short, "equipo": equipos_label.get(eq_key, eq_key),
                "exactitud": round(ex_t, 1), "fallas": n_f, "pms": n_pm,
                "bono_clp": bc_t, "es_senior": True}
    cal_tecnicos = _ensure_seniors_in_ranking(cal_tecnicos, _cal_eq_agg, _build_cal_senior_row)
    cal_tecnicos.sort(key=lambda x: x["exactitud"], reverse=True)

    cal_data = {"exactitud": round(exactitud, 1), "fallas": fallas_total,
                "pms_total": pms_total, "bono_pct": bp_cal, "bono_clp": bc_cal,
                "tecnicos": cal_tecnicos}

    bono_total = bc_sla + bc_cal + bc_prec

    # ═══ RESUMEN BONOS POR EQUIPO (pre-computed by dashboard) ═══
    bono_table = data.get("bono_table", {})
    if bono_table and trim_key in bono_table:
        bono_equipos = bono_table[trim_key]
    else:
        _nn = lambda s: ' '.join(s.split())
        seniors_set = set(data.get("seniors", []))
        _pm_bono = {}
        for r in [r for r in data.get("pms", []) if r.get("mes_num") in meses_filtro]:
            t = _nn(r.get("tecnico", ""))
            _pm_bono[t] = _pm_bono.get(t, 0) + int(r.get("pms", 0))
        _fallas_bono = {}
        for r in [r for r in data.get("reincidencias", []) if r.get("mes_num") in meses_filtro]:
            ts = r.get("tecnico_short", "")
            _fallas_bono[ts] = _fallas_bono.get(ts, 0) + int(r.get("fallas", 0))
        bono_equipos = []
        for eq_key, eq_info in equipos_info.items():
            miembros_full = eq_info.get("miembros", [])
            if not miembros_full: continue
            n_eq = len(miembros_full)
            eq_sla_ok = eq_sla_tot = eq_prec_b = eq_prec_t = eq_fallas = eq_pms = 0
            tec_rows = []
            for tf in miembros_full:
                ts = full_to_short.get(tf, tf)
                tf_n = _nn(tf)
                s_ok = s_tot = p_b = p_t = 0
                for r in [r for r in data.get("sla", []) if r.get("mes_num") in meses_filtro]:
                    if _nn(r.get("tecnico", "")) == tf_n:
                        s_ok += int(r.get("cumple", 0)); s_tot += int(r.get("total", 0))
                eq_sla_ok += s_ok; eq_sla_tot += s_tot
                for r in [r for r in data.get("precision", []) if r.get("mes_num") in meses_filtro]:
                    if _nn(r.get("tecnico", "")) == tf_n:
                        p_b += int(r.get("buenas", 0)); p_t += int(r.get("total", 0))
                eq_prec_b += p_b; eq_prec_t += p_t
                n_pm = _pm_bono.get(tf_n, 0); n_f = _fallas_bono.get(ts, 0)
                eq_fallas += n_f; eq_pms += n_pm
                s_pct = round(s_ok/s_tot*100, 1) if s_tot > 0 else None
                p_pct = round(p_b/p_t*100, 1) if p_t > 0 else None
                mp_pct = round((1-n_f/n_pm)*100, 1) if n_pm > 0 else None
                ns = _bono_sla(s_pct)[0] if s_pct is not None else 0
                nm = _bono_calidad(n_f, n_pm)[0] if n_pm > 0 else 0
                np_ = _bono_prec(p_pct)[0] if p_pct is not None else 0
                tec_rows.append({
                    "short": ts, "is_senior": ts in seniors_set,
                    "sla_pct": s_pct, "sla_ok": s_ok, "sla_tot": s_tot, "sla_niv": ns,
                    "mp_pct": mp_pct, "mp_f": n_f, "mp_pm": n_pm, "mp_niv": nm,
                    "prec_pct": p_pct, "prec_b": p_b, "prec_t": p_t, "prec_niv": np_,
                    "cumpl": round(.40*ns+.30*nm+.30*np_, 1),
                })
            eq_sp = round(eq_sla_ok/eq_sla_tot*100, 1) if eq_sla_tot > 0 else None
            eq_mp = round((1-eq_fallas/eq_pms)*100, 1) if eq_pms > 0 else None
            eq_pp = round(eq_prec_b/eq_prec_t*100, 1) if eq_prec_t > 0 else None
            ens = _bono_sla(eq_sp)[0] if eq_sp is not None else 0
            enm = _bono_calidad(eq_fallas, eq_pms)[0] if eq_pms > 0 else 0
            enp = _bono_prec(eq_pp)[0] if eq_pp is not None else 0
            eq_c = round(.40*ens+.30*enm+.30*enp, 1)
            for row in tec_rows:
                if row.get("is_senior"):
                    row.update({"sla_pct":eq_sp,"sla_ok":eq_sla_ok,"sla_tot":eq_sla_tot,"sla_niv":ens,
                        "mp_pct":eq_mp,"mp_f":eq_fallas,"mp_pm":eq_pms,"mp_niv":enm,
                        "prec_pct":eq_pp,"prec_b":eq_prec_b,"prec_t":eq_prec_t,"prec_niv":enp,
                        "cumpl":eq_c})
            ppi = int(int(BONO_TOTAL/n_eq)*.50); ppe = ppi
            be = int(ppe*.40*ens/100+ppe*.30*enm/100+ppe*.30*enp/100)
            for row in tec_rows:
                ns,nm,np_ = row["sla_niv"],row["mp_niv"],row["prec_niv"]
                bi = int(ppi*.40*ns/100+ppi*.30*nm/100+ppi*.30*np_/100)
                row.update({"bono_ind":bi,"bono_eq":be,"bono_cc":0,"total_trim":bi+be,"prom_mensual":(bi+be)//3})
            bono_equipos.append({
                "key":eq_key,"label":equipos_label.get(eq_key,eq_key),
                "senior":eq_info.get("senior",""),"n_eq":n_eq,
                "pp_ind":ppi,"pp_eq":ppe,"n_semanas_cc":0,"bono_cc_eq":0,
                "tecs":tec_rows,
                "eq":{"sla_pct":eq_sp,"sla_ok":eq_sla_ok,"sla_tot":eq_sla_tot,"sla_niv":ens,
                    "mp_pct":eq_mp,"mp_f":eq_fallas,"mp_pm":eq_pms,"mp_niv":enm,
                    "prec_pct":eq_pp,"prec_b":eq_prec_b,"prec_t":eq_prec_t,"prec_niv":enp,
                    "cumpl":eq_c},
            })

    # Determinar si el usuario es senior de su equipo
    _my_short = user.get("short", "")
    _user_is_senior = _my_short in seniors

    if not user["is_admin"]:
        _my_team_label = equipos_label.get(user["team"], user["team"])
        all_tecnicos = [t for t in all_tecnicos if t["equipo"] == _my_team_label]
        bono_equipos = [b for b in bono_equipos if b.get("key") == user["team"]]
        equipos_label = {user["team"]: _my_team_label}

        if not _user_is_senior:
            sla_data["tecnicos"] = [t for t in sla_data["tecnicos"] if t["nombre"] == _my_short]
            cal_data["tecnicos"] = [t for t in cal_data["tecnicos"] if t["nombre"] == _my_short]
            prec_data["tecnicos"] = [t for t in prec_data["tecnicos"] if t["nombre"] == _my_short]
            for _beq in bono_equipos:
                _beq["tecs"] = [t for t in _beq.get("tecs", []) if t["short"] == _my_short]

    # Filtro de equipo para admins (bono_equipos)
    if user["is_admin"] and equipo_sel:
        bono_equipos = [b for b in bono_equipos if b.get("key") == equipo_sel]

    return render_template_string(
        HTML_TEMPLATE,
        user=user, user_is_senior=_user_is_senior,
        sla=sla_data, cal=cal_data, prec=prec_data,
        bono_total=bono_total, bono_equipos=bono_equipos,
        max_sla=MAX_SLA, max_cal=MAX_CAL, max_prec=MAX_PREC,
        bono_pool=BONO_TOTAL,
        trim_key=trim_key, trim_label=trim["label"], trimestres=TRIMESTRES,
        mes_sel=mes_sel, meses_filtro=meses_filtro,
        tecnico_sel=tecnico_sel, equipo_sel=equipo_sel, tab_sel=tab_sel,
        tecnicos=all_tecnicos, equipos=equipos_label,
        color_pct=_color_pct, nivel_color=_nivel_color, clp_fmt=_clp_fmt,
        updated_at=data.get("updated_at", "—"),
        now=datetime.now(),
    )
  except Exception:
    tb = traceback.format_exc()
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Error</title>
    <style>body{{font-family:monospace;background:#151B23;color:#e2e8f0;padding:16px}}
    pre{{white-space:pre-wrap;word-break:break-all;font-size:12px;background:#1e293b;
    padding:12px;border-radius:8px;overflow-x:auto}}
    a{{color:#3b82f6}}</style></head><body>
    <h2 style="color:#ef4444">Error al cargar datos</h2>
    <p><a href="/">Volver al inicio</a></p>
    <pre>{tb}</pre></body></html>""", 500


ERROR_TEMPLATE = r"""
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Indicadores Operacionales - Occim 📲</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📲</text></svg>">
<style>
  body { font-family: -apple-system, sans-serif; background: #151B23; color: #e2e8f0;
    display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
  .msg { text-align: center; max-width: 360px; }
  .msg h2 { color: #f59e0b; margin-bottom: 12px; }
  .msg p { color: #9CA3B0; font-size: .9rem; line-height: 1.5; }
  .msg a { color: #4DD0E1; }
</style></head><body>
<div class="msg">
  <h2>Datos no disponibles</h2>
  <p>{{ msg }}</p>
  <p style="margin-top:16px;font-size:.8rem;">
    <a href="/" onclick="setTimeout(()=>location.reload(),3000)">Reintentar</a>
  </p>
</div>
</body></html>
"""


HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Indicadores Operacionales - Occim 📲</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📲</text></svg>">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#00897B">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Occim STO">
<link rel="apple-touch-icon" href="/app-icon.svg">
<style>
  :root {
    --bg: #151B23; --card: rgba(22,28,36,.90); --border: rgba(65,75,90,.50);
    --text: #e2e8f0; --muted: #9CA3B0;
    --green: #22c55e; --yellow: #f59e0b; --red: #ef4444;
    --blue: #26C6DA; --teal: #4DD0E1;
    --accent: #00897B;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
    background-image: url('/bg-mobile.png');
    background-size: cover; background-position: center; background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
  }
  body::before {
    content: ''; position: fixed; inset: 0; z-index: 0;
    background: linear-gradient(180deg, rgba(21,27,35,.88) 0%, rgba(21,27,35,.80) 50%, rgba(21,27,35,.90) 100%);
    pointer-events: none;
  }
  .app-container {
    position: relative; z-index: 1;
    padding: 12px; max-width: 480px; margin: 0 auto;
  }
  .app-header {
    text-align: center; padding: 10px 0 6px;
    border-bottom: 1px solid var(--border); margin-bottom: 14px;
  }
  .app-header h1 {
    font-size: 1.35rem; font-weight: 800; letter-spacing: .02em;
    color: #ffffff;
  }
  .subtitle { text-align: center; font-size: .75rem; color: var(--muted); margin-top: 2px; }
  h2 { font-size: 1.05rem; margin: 16px 0 8px; color: var(--muted); letter-spacing: .03em; }
  .filters { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
  .filters select {
    flex: 1; min-width: 0; padding: 8px 6px; border-radius: 8px;
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    font-size: .82rem; appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%2394a3b8'%3E%3Cpath d='M2 4l4 4 4-4'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 8px center;
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  }
  .kpi-card {
    background: var(--card); border-radius: 14px; padding: 16px;
    margin-bottom: 10px; border-left: 4px solid var(--blue);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 2px 12px rgba(0,0,0,.3);
  }
  .kpi-card.sla   { border-left-color: var(--blue); }
  .kpi-card.cal   { border-left-color: var(--teal); }
  .kpi-card.prec  { border-left-color: var(--yellow); }
  .kpi-card.bono  { border-left-color: var(--green); }
  .kpi-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
  .kpi-title { font-size: .82rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .kpi-big { font-size: 2rem; font-weight: 800; line-height: 1.1; }
  .kpi-detail { font-size: .78rem; color: var(--muted); margin-top: 4px; }
  .kpi-bono { display: inline-block; padding: 2px 10px; border-radius: 6px; font-size: .78rem; font-weight: 700; color: #fff; margin-top: 6px; }
  .ranking {
    margin-top: 10px; background: var(--card); border-radius: 10px; padding: 10px 12px;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 2px 8px rgba(0,0,0,.2);
  }
  .rank-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid var(--border); font-size: .82rem;
  }
  .rank-row:last-child { border-bottom: none; }
  .rank-name { flex: 1; }
  .rank-eq { color: var(--muted); font-size: .72rem; margin-left: 4px; }
  .rank-pct { font-weight: 700; min-width: 55px; text-align: right; }
  .rank-clp { color: var(--muted); font-size: .72rem; min-width: 70px; text-align: right; }
  .bono-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 8px; }
  .bono-item {
    background: rgba(148,163,184,.08); border-radius: 8px; padding: 10px; text-align: center;
  }
  .bono-item .label { font-size: .7rem; color: var(--muted); text-transform: uppercase; }
  .bono-item .value { font-size: 1.1rem; font-weight: 800; margin-top: 2px; }
  .bono-item.total { grid-column: span 2; }
  .bono-item.total .value { font-size: 1.5rem; color: var(--green); }
  .tabs { display: flex; gap: 4px; margin-bottom: 10px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .tab-btn {
    padding: 6px 12px; border-radius: 8px; font-size: .78rem; font-weight: 600;
    background: var(--card); color: var(--muted); border: 1px solid var(--border);
    white-space: nowrap; cursor: pointer; flex-shrink: 0;
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  }
  .tab-btn.active { background: linear-gradient(135deg, #005F56, #26C6DA); color: #fff; border-color: transparent; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .eq-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .eq-card {
    background: var(--card); border-radius: 10px; padding: 10px; text-align: center;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 1px 6px rgba(0,0,0,.2);
  }
  .eq-card .name { font-weight: 700; font-size: .82rem; }
  .eq-card .senior { font-size: .7rem; color: var(--muted); }
  .eq-card .pct { font-size: 1.3rem; font-weight: 800; margin: 4px 0; }
  .eq-card .detail { font-size: .7rem; color: var(--muted); }
  .eq-card .bono-badge { font-size: .7rem; padding: 2px 6px; border-radius: 4px; color: #fff; font-weight: 700; }
  .footer { text-align: center; font-size: .7rem; color: var(--muted); margin-top: 20px; padding: 10px 0; border-top: 1px solid var(--border); }
  .clear-filter { font-size: .72rem; color: var(--teal); text-decoration: none; margin-left: 6px; }
  .data-source { text-align: center; font-size: .68rem; color: var(--muted); margin-bottom: 10px; opacity: .7; }
</style>
</head>
<body>
<div class="app-container">

<div class="app-header">
  <div style="display:flex;align-items:center;justify-content:space-between;">
    <div>
      <div style="font-size:.78rem;color:var(--muted);letter-spacing:.5px;margin-bottom:2px;">📲 Indicadores Operacionales - Occim</div>
      <h1 style="margin:0;">Indicadores STO</h1>
    </div>
    <div style="display:flex;gap:6px;">
      {% if user.is_admin %}<a href="/admin/pins" style="background:rgba(59,130,246,.2);color:#93c5fd;border:1px solid rgba(59,130,246,.4);border-radius:8px;padding:6px 10px;font-size:.72rem;text-decoration:none;white-space:nowrap;" title="Administrar PINs">⚙️ PINs</a>{% endif %}
      <a href="javascript:location.reload()" style="background:var(--accent);color:#fff;border:none;border-radius:8px;padding:6px 10px;font-size:.72rem;text-decoration:none;white-space:nowrap;" title="Refrescar datos">🔄</a>
      <a href="/logout" style="background:rgba(239,68,68,.2);color:#fca5a5;border:1px solid rgba(239,68,68,.4);border-radius:8px;padding:6px 10px;font-size:.72rem;text-decoration:none;white-space:nowrap;" title="Cerrar sesión">⏻ Salir</a>
    </div>
  </div>
  <p class="subtitle">
    {% if user.is_admin %}👤 Admin · {{ user.email }}{% else %}👤 {{ user.short }} · Equipo {{ user.team }}{% endif %}
    · {{ now.strftime('%d/%m/%Y %H:%M') }}
  </p>
</div>
<div class="data-source">Datos del dashboard · actualizado {{ updated_at[:16] | replace('T',' ') }}</div>

<form class="filters" id="filterForm">
  <input type="hidden" name="tab" id="tabInput" value="{{ tab_sel }}">
  <select name="trim" onchange="this.form.submit()">
    {% for k, v in trimestres.items() %}
    <option value="{{ k }}" {{ 'selected' if k == trim_key }}>{{ v.label }}</option>
    {% endfor %}
  </select>
  <select name="mes" onchange="this.form.submit()">
    <option value="">Todo el trim.</option>
    {% for m in trimestres[trim_key].meses %}
    <option value="{{ m }}" {{ 'selected' if mes_sel == m|string }}>
      {{ ['','Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'][m] }}
    </option>
    {% endfor %}
  </select>
  <select name="equipo" onchange="document.querySelector('[name=tecnico]').value=''; this.form.submit()">
    <option value="">Todos los eq.</option>
    {% for k, lbl in equipos.items() %}
    <option value="{{ k }}" {{ 'selected' if equipo_sel == k }}>{{ lbl }}</option>
    {% endfor %}
  </select>
  <select name="tecnico" onchange="this.form.submit()">
    <option value="">Todos los téc.</option>
    {% for t in tecnicos %}
    {% if not equipo_sel or t.equipo == equipos.get(equipo_sel, equipo_sel) %}
    <option value="{{ t.full }}" {{ 'selected' if tecnico_sel == t.full }}>{{ t.short }}</option>
    {% endif %}
    {% endfor %}
  </select>
</form>

{% if tecnico_sel or equipo_sel %}
<div style="text-align:center;margin-bottom:10px;">
  <a href="/?trim={{ trim_key }}&mes={{ mes_sel }}" class="clear-filter">Limpiar filtros de equipo/técnico</a>
</div>
{% endif %}

<div class="tabs">
  <div class="tab-btn active" onclick="showTab('sla')">SLA</div>
  <div class="tab-btn" onclick="showTab('cal')">Efectividad MP</div>
  <div class="tab-btn" onclick="showTab('prec')">Precisión Fracttal</div>
  <div class="tab-btn" onclick="showTab('bono')">Resumen Bonos</div>
</div>

<!-- TAB SLA -->
<div class="tab-content active" id="tab-sla">
  <div class="kpi-card sla">
    <div class="kpi-header"><span class="kpi-title">Productividad SLA (40%)</span></div>
    <div class="kpi-big" style="color:{{ color_pct(sla.pct) }}">{{ sla.pct }}%</div>
    <div class="kpi-detail">{{ sla.cumple }} / {{ sla.total }} llamados cumplen SLA</div>
    <div class="kpi-bono" style="background:{{ color_pct(sla.pct) }}">
      {{ sla.bono_pct }}% bono → ${{ '{:,.0f}'.format(sla.bono_clp) }}
    </div>
  </div>
  {% if sla.equipos %}
  <h2>Por equipo</h2>
  <div class="eq-cards">
    {% for eq in sla.equipos %}
    <div class="eq-card">
      <div class="name">{{ eq.nombre }}</div>
      <div class="senior">{{ eq.senior }}</div>
      <div class="pct" style="color:{{ color_pct(eq.pct) }}">{{ eq.pct }}%</div>
      <div class="detail">{{ eq.cumple }}/{{ eq.total }}</div>
      <div class="bono-badge" style="background:{{ color_pct(eq.pct) }}">${{ '{:,.0f}'.format(eq.bono_clp) }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  {% if sla.tecnicos %}
  <h2>{% if user.is_admin or user_is_senior %}Ranking técnicos{% else %}Tu desempeño{% endif %}</h2>
  <div class="ranking">
    {% for t in sla.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span>{% if t.es_senior %} <span style="background:#f59e0b;color:#000;border-radius:3px;padding:1px 5px;font-size:.6rem;font-weight:700;vertical-align:middle;">PROMEDIO EQUIPO</span>{% endif %}</span>
      <span class="rank-pct" style="color:{{ color_pct(t.pct) }}">{{ t.pct }}%</span>
      <span class="rank-clp">{{ t.cumple }}/{{ t.total }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- TAB EFECTIVIDAD -->
<div class="tab-content" id="tab-cal">
  <div class="kpi-card cal">
    <div class="kpi-header"><span class="kpi-title">Efectividad MP (30%)</span></div>
    <div class="kpi-big" style="color:{{ color_pct(cal.exactitud) }}">{{ cal.exactitud }}%</div>
    <div class="kpi-detail">
      {{ cal.fallas }} falla(s) post-preventiva · {{ cal.pms_total }} PMs evaluados
    </div>
    <div class="kpi-bono" style="background:{{ color_pct(cal.exactitud) }}">
      {{ cal.bono_pct }}% bono → ${{ '{:,.0f}'.format(cal.bono_clp) }}
    </div>
  </div>
  {% if cal.tecnicos %}
  <h2>{% if user.is_admin or user_is_senior %}Ranking técnicos{% else %}Tu desempeño{% endif %}</h2>
  <div class="ranking">
    {% for t in cal.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span>{% if t.es_senior %} <span style="background:#f59e0b;color:#000;border-radius:3px;padding:1px 5px;font-size:.6rem;font-weight:700;vertical-align:middle;">PROMEDIO EQUIPO</span>{% endif %}</span>
      <span class="rank-pct" style="color:{{ color_pct(t.exactitud) }}">{{ t.exactitud }}%</span>
      <span class="rank-clp">{{ t.fallas }}F / {{ t.pms }}PM</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- TAB PRECISIÓN -->
<div class="tab-content" id="tab-prec">
  <div class="kpi-card prec">
    <div class="kpi-header"><span class="kpi-title">Precisión Fracttal (30%)</span></div>
    <div class="kpi-big" style="color:{{ color_pct(prec.pct) }}">{{ prec.pct }}%</div>
    <div class="kpi-detail">
      {{ prec.buenas }} / {{ prec.total }} OTs correctas (score 75/75)
    </div>
    <div class="kpi-bono" style="background:{{ color_pct(prec.pct) }}">
      {{ prec.bono_pct }}% bono → ${{ '{:,.0f}'.format(prec.bono_clp) }}
    </div>
  </div>
  {% if prec.tecnicos %}
  <h2>{% if user.is_admin or user_is_senior %}Ranking técnicos{% else %}Tu desempeño{% endif %}</h2>
  <div class="ranking">
    {% for t in prec.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span>{% if t.es_senior %} <span style="background:#f59e0b;color:#000;border-radius:3px;padding:1px 5px;font-size:.6rem;font-weight:700;vertical-align:middle;">PROMEDIO EQUIPO</span>{% endif %}</span>
      <span class="rank-pct" style="color:{{ color_pct(t.pct) }}">{{ t.pct }}%</span>
      <span class="rank-clp">{{ t.buenas }}/{{ t.total }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- TAB RESUMEN BONOS -->
<div class="tab-content" id="tab-bono">
  <div class="kpi-card bono">
    <div class="kpi-header"><span class="kpi-title">Cumplimiento Grupal · {{ trim_label }}</span></div>
    <div class="bono-grid">
      <div class="bono-item">
        <div class="label">SLA (40%)</div>
        <div class="value" style="color:{{ color_pct(sla.pct) }}">{{ sla.pct }}%</div>
        <div style="font-size:.7rem;color:var(--muted)">Nivel bono → {{ sla.bono_pct }}%</div>
      </div>
      <div class="bono-item">
        <div class="label">Efectividad (30%)</div>
        <div class="value" style="color:{{ color_pct(cal.exactitud) }}">{{ cal.exactitud }}%</div>
        <div style="font-size:.7rem;color:var(--muted)">Nivel bono → {{ cal.bono_pct }}%</div>
      </div>
      <div class="bono-item">
        <div class="label">Precisión (30%)</div>
        <div class="value" style="color:{{ color_pct(prec.pct) }}">{{ prec.pct }}%</div>
        <div style="font-size:.7rem;color:var(--muted)">Nivel bono → {{ prec.bono_pct }}%</div>
      </div>
    </div>
  </div>

  {% for eq in bono_equipos %}
  {% if not equipo_sel or eq.key == equipo_sel %}
  <div style="margin-top:14px;">
    <div style="font-size:.95rem;font-weight:700;color:var(--text);border-bottom:2px solid #005F56;padding-bottom:4px;margin-bottom:8px;">
      🌐 Equipo {{ eq.label }} <span style="font-size:.75rem;color:var(--muted);font-weight:400;">— Senior: {{ eq.senior }}</span>
    </div>
    <div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">
      <table style="width:100%;border-collapse:collapse;font-size:.75rem;color:var(--text);min-width:420px;">
        <thead>
          <tr style="background:#005F56;color:#fff;">
            <th style="padding:6px 8px;text-align:left;border-radius:6px 0 0 0;">KPI</th>
            {% for t in eq.tecs %}
            <th style="padding:6px 8px;text-align:center;white-space:nowrap;">{{ t.short }}</th>
            {% endfor %}
            <th style="padding:6px 8px;text-align:center;font-style:italic;border-radius:0 6px 0 0;">EQUIPO</th>
          </tr>
        </thead>
        <tbody>
          <tr style="background:rgba(255,255,255,0.04);">
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">SLA <span style="color:var(--muted);font-size:.65rem;">(40%)</span></td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">
              {% if t.sla_pct is not none %}<span style="color:{{ color_pct(t.sla_pct) }}">{{ t.sla_ok }}/{{ t.sla_tot }} = {{ t.sla_pct }}%</span><br><span style="font-size:.65rem;font-weight:700;color:{{ nivel_color(t.sla_niv) }};">→ {{ t.sla_niv }}%</span>{% else %}<span style="color:var(--muted);">—</span>{% endif %}
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;background:rgba(1,121,138,0.1);">
              {% if eq.eq.sla_pct is not none %}<span style="color:{{ color_pct(eq.eq.sla_pct) }}">{{ eq.eq.sla_ok }}/{{ eq.eq.sla_tot }} = {{ eq.eq.sla_pct }}%</span><br><span style="font-size:.65rem;font-weight:700;color:{{ nivel_color(eq.eq.sla_niv) }};">→ {{ eq.eq.sla_niv }}%</span>{% else %}—{% endif %}
            </td>
          </tr>
          <tr>
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">Efectividad MP <span style="color:var(--muted);font-size:.65rem;">(30%)</span></td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">
              {% if t.mp_pct is not none %}<span style="color:{{ color_pct(t.mp_pct) }}">{{ t.mp_f }}F/{{ t.mp_pm }}PM = {{ t.mp_pct }}%</span><br><span style="font-size:.65rem;font-weight:700;color:{{ nivel_color(t.mp_niv) }};">→ {{ t.mp_niv }}%</span>{% else %}<span style="color:var(--muted);">—</span>{% endif %}
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;background:rgba(1,121,138,0.1);">
              {% if eq.eq.mp_pct is not none %}<span style="color:{{ color_pct(eq.eq.mp_pct) }}">{{ eq.eq.mp_f }}F/{{ eq.eq.mp_pm }}PM = {{ eq.eq.mp_pct }}%</span><br><span style="font-size:.65rem;font-weight:700;color:{{ nivel_color(eq.eq.mp_niv) }};">→ {{ eq.eq.mp_niv }}%</span>{% else %}—{% endif %}
            </td>
          </tr>
          <tr style="background:rgba(255,255,255,0.04);">
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">Precisión <span style="color:var(--muted);font-size:.65rem;">(30%)</span></td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">
              {% if t.prec_pct is not none %}<span style="color:{{ color_pct(t.prec_pct) }}">{{ t.prec_b }}/{{ t.prec_t }} = {{ t.prec_pct }}%</span><br><span style="font-size:.65rem;font-weight:700;color:{{ nivel_color(t.prec_niv) }};">→ {{ t.prec_niv }}%</span>{% else %}<span style="color:var(--muted);">—</span>{% endif %}
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;background:rgba(1,121,138,0.1);">
              {% if eq.eq.prec_pct is not none %}<span style="color:{{ color_pct(eq.eq.prec_pct) }}">{{ eq.eq.prec_b }}/{{ eq.eq.prec_t }} = {{ eq.eq.prec_pct }}%</span><br><span style="font-size:.65rem;font-weight:700;color:{{ nivel_color(eq.eq.prec_niv) }};">→ {{ eq.eq.prec_niv }}%</span>{% else %}—{% endif %}
            </td>
          </tr>
          <tr style="background:rgba(1,121,138,0.08);">
            <td style="padding:6px 8px;font-weight:700;border-top:2px solid rgba(255,255,255,0.15);border-bottom:1px solid rgba(255,255,255,0.08);">Cumpl. ponderado</td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;font-weight:700;border-top:2px solid rgba(255,255,255,0.15);border-bottom:1px solid rgba(255,255,255,0.08);">
              <span style="color:{{ color_pct(t.cumpl) }}">{{ t.cumpl }}%</span>
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;font-weight:700;font-style:italic;border-top:2px solid rgba(255,255,255,0.15);border-bottom:1px solid rgba(255,255,255,0.08);background:rgba(1,121,138,0.1);">
              <span style="color:{{ color_pct(eq.eq.cumpl) }}">{{ eq.eq.cumpl }}%</span>
            </td>
          </tr>
          <tr>
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">Terreno individual</td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">
              <span style="font-weight:700;color:{{ nivel_color(t.cumpl) }};">{{ clp_fmt(t.bono_ind) }}</span>
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;color:var(--muted);">—</td>
          </tr>
          <tr style="background:rgba(255,255,255,0.04);">
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">Terreno colectivo</td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">
              <span style="font-weight:700;color:{{ nivel_color(eq.eq.cumpl) }};">{{ clp_fmt(t.bono_eq) }}</span>
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;background:rgba(1,121,138,0.1);">
              <span style="font-weight:700;color:{{ nivel_color(eq.eq.cumpl) }};">{{ clp_fmt(eq.tecs[0].bono_eq) }}</span>
            </td>
          </tr>
          {% if eq.n_semanas_cc is defined and eq.n_semanas_cc > 0 %}
          <tr>
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">Callcenter <span style="color:var(--muted);font-size:.6rem;">({{ eq.n_semanas_cc }} sem)</span></td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">
              <span style="font-weight:700;">{{ clp_fmt(t.bono_cc) }}</span>
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;background:rgba(1,121,138,0.1);">
              <span style="font-weight:700;">{{ clp_fmt(eq.bono_cc_eq) }}</span>
            </td>
          </tr>
          {% else %}
          <tr>
            <td style="padding:6px 8px;font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);">Callcenter</td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);color:var(--muted);">—</td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);font-style:italic;color:var(--muted);">—</td>
          </tr>
          {% endif %}
          <tr style="background:rgba(1,121,138,0.12);">
            <td style="padding:8px;font-weight:800;font-size:.8rem;border-top:2px solid rgba(255,255,255,0.15);">TOTAL trimestral</td>
            {% for t in eq.tecs %}
            <td style="padding:8px;text-align:center;font-weight:800;font-size:.85rem;border-top:2px solid rgba(255,255,255,0.15);">
              <span style="color:{{ nivel_color(t.cumpl) }};">{{ clp_fmt(t.total_trim) }}</span>
            </td>
            {% endfor %}
            <td style="padding:8px;text-align:center;border-top:2px solid rgba(255,255,255,0.15);font-style:italic;color:var(--muted);">—</td>
          </tr>
          <tr style="background:rgba(1,121,138,0.12);">
            <td style="padding:6px 8px;font-weight:700;font-size:.78rem;">Promedio mensual <span style="color:var(--muted);font-size:.6rem;">(÷3)</span></td>
            {% for t in eq.tecs %}
            <td style="padding:6px 8px;text-align:center;font-weight:700;font-size:.8rem;">
              {{ clp_fmt(t.prom_mensual) }}
            </td>
            {% endfor %}
            <td style="padding:6px 8px;text-align:center;font-style:italic;color:var(--muted);">—</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}
  {% endfor %}
</div>

<div class="footer">
  📲 Indicadores Operacionales - versión mobile 1.2<br>
  Última sincronización: {{ updated_at[:16] | replace('T',' ') }}<br>
  <a href="/cambiar-pin" style="color:#4DD0E1;font-size:.75rem;text-decoration:none;">🔑 Cambiar PIN</a>
  &nbsp;·&nbsp;
  <a href="/logout" style="color:#9CA3B0;font-size:.75rem;text-decoration:none;">Cerrar sesión</a>
</div>

</div>

<script>
function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  event.target.classList.add('active');
  document.getElementById('tabInput').value = id;
}
(function(){
  var t = '{{ tab_sel }}';
  if (t && document.getElementById('tab-' + t)) { showTab(t); }
})();
</script>
</body>
</html>
"""


LOGIN_TEMPLATE = r"""
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>Acceso · Indicadores Operacionales</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔐</text></svg>">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#151B23 url('/bg-mobile.png') center/cover fixed;color:#e2e8f0;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
  body::before{content:'';position:fixed;inset:0;background:linear-gradient(180deg,rgba(21,27,35,.92),rgba(21,27,35,.85));}
  .card{position:relative;z-index:1;background:rgba(22,28,36,.95);border:1px solid rgba(65,75,90,.50);
    border-radius:16px;padding:32px 28px;max-width:380px;width:100%;
    box-shadow:0 10px 40px rgba(0,0,0,.5);}
  h1{font-size:1.4rem;color:#fff;margin-bottom:6px;text-align:center;}
  .sub{color:#9CA3B0;font-size:.85rem;text-align:center;margin-bottom:24px;line-height:1.4;}
  .sub2{color:#cbd5e1;font-size:1rem;text-align:center;margin-bottom:4px;}
  label{display:block;color:#cbd5e1;font-size:.8rem;margin-bottom:8px;letter-spacing:.02em;text-transform:uppercase;}
  input[type=email],input[type=password]{width:100%;padding:14px 12px;border-radius:10px;
    border:1px solid rgba(65,75,90,.6);background:rgba(22,28,36,.6);color:#e2e8f0;
    font-size:16px;outline:none;transition:border .15s;}
  input:focus{border-color:#4DD0E1;}
  .pin-input{text-align:center;letter-spacing:.3em;font-size:22px;font-family:monospace;}
  .gap{margin-top:16px;}
  button{width:100%;margin-top:18px;padding:14px;border:0;border-radius:10px;
    background:#00897B;color:#fff;font-size:1rem;font-weight:600;cursor:pointer;transition:.15s;}
  button:hover{background:#4DD0E1;}
  .msg{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:.85rem;}
  .msg.error{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.4);color:#fca5a5;}
  .msg.info{background:rgba(59,130,246,.15);border:1px solid rgba(59,130,246,.4);color:#93c5fd;}
  .hint{margin-top:18px;text-align:center;font-size:.75rem;color:#64748b;line-height:1.5;}
</style></head><body>
<div class="card">
  <h1>🔐 Indicadores Operacionales</h1>
  <p class="sub2">Iniciar sesion</p>
  <p class="sub">Ingresa tu correo corporativo y tu PIN de acceso.</p>
  <form method="post">
    <label for="email">Correo</label>
    <input type="email" id="email" name="email" required autofocus
      placeholder="tu_correo@occimiano.cl" autocomplete="email">
    <div class="gap"></div>
    <label for="pin">PIN</label>
    <input type="password" id="pin" name="pin" required maxlength="4"
      pattern="[0-9]{4}" inputmode="numeric" placeholder="****" autocomplete="current-password"
      class="pin-input">
    <button type="submit">Ingresar</button>
  </form>
  {% if msg %}<div class="msg {{ msg_kind }}">{{ msg }}</div>{% endif %}
  <p class="hint">Solo personal autorizado de Occimiano.<br>Si no tienes acceso, contacta a operaciones.</p>
</div>
</body></html>
"""


CAMBIAR_PIN_TEMPLATE = r"""
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>Cambiar PIN</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔑</text></svg>">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#151B23 url('/bg-mobile.png') center/cover fixed;color:#e2e8f0;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
  body::before{content:'';position:fixed;inset:0;background:linear-gradient(180deg,rgba(21,27,35,.92),rgba(21,27,35,.85));}
  .card{position:relative;z-index:1;background:rgba(22,28,36,.95);border:1px solid rgba(65,75,90,.50);
    border-radius:16px;padding:32px 28px;max-width:380px;width:100%;
    box-shadow:0 10px 40px rgba(0,0,0,.5);}
  h1{font-size:1.3rem;color:#fff;margin-bottom:6px;text-align:center;}
  .sub{color:#9CA3B0;font-size:.85rem;text-align:center;margin-bottom:20px;line-height:1.4;}
  .user-info{text-align:center;color:#4DD0E1;font-size:.9rem;font-weight:600;margin-bottom:20px;}
  label{display:block;color:#cbd5e1;font-size:.8rem;margin-bottom:8px;letter-spacing:.02em;text-transform:uppercase;}
  input[type=password]{width:100%;padding:14px 12px;border-radius:10px;
    border:1px solid rgba(65,75,90,.6);background:rgba(22,28,36,.6);color:#e2e8f0;
    font-size:16px;outline:none;transition:border .15s;
    text-align:center;letter-spacing:.3em;font-size:22px;font-family:monospace;}
  input:focus{border-color:#4DD0E1;}
  .gap{margin-top:16px;}
  button{width:100%;margin-top:18px;padding:14px;border:0;border-radius:10px;
    background:#00897B;color:#fff;font-size:1rem;font-weight:600;cursor:pointer;transition:.15s;}
  button:hover{background:#4DD0E1;}
  .msg{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:.85rem;}
  .msg.error{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.4);color:#fca5a5;}
  .msg.success{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.4);color:#86efac;}
  .back{display:block;text-align:center;margin-top:18px;color:#64748b;font-size:.85rem;text-decoration:none;}
  .back:hover{color:#9CA3B0;}
</style></head><body>
<div class="card">
  <h1>🔑 Cambiar PIN</h1>
  <div class="user-info">{{ user.short }}</div>
  <p class="sub">Ingresa tu PIN actual y elige uno nuevo de 4 digitos.</p>
  <form method="post">
    <label for="pin_actual">PIN actual</label>
    <input type="password" id="pin_actual" name="pin_actual" required maxlength="4"
      pattern="[0-9]{4}" inputmode="numeric" placeholder="****" autocomplete="current-password">
    <div class="gap"></div>
    <label for="pin_nuevo">Nuevo PIN</label>
    <input type="password" id="pin_nuevo" name="pin_nuevo" required maxlength="4"
      pattern="[0-9]{4}" inputmode="numeric" placeholder="****" autocomplete="new-password">
    <div class="gap"></div>
    <label for="pin_confirm">Confirmar nuevo PIN</label>
    <input type="password" id="pin_confirm" name="pin_confirm" required maxlength="4"
      pattern="[0-9]{4}" inputmode="numeric" placeholder="****" autocomplete="new-password">
    <button type="submit">Cambiar PIN</button>
  </form>
  {% if msg %}<div class="msg {{ msg_kind }}">{{ msg }}</div>{% endif %}
  <a href="/" class="back">← Volver al dashboard</a>
</div>
</body></html>
"""


ADMIN_PINS_TEMPLATE = r"""
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>Admin PINs · Indicadores Operacionales</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚙️</text></svg>">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#151B23;color:#e2e8f0;min-height:100vh;padding:20px;}
  .wrap{max-width:700px;margin:0 auto;}
  h1{font-size:1.3rem;color:#fff;margin-bottom:4px;}
  .sub{color:#9CA3B0;font-size:.85rem;margin-bottom:16px;}
  .actions{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;}
  .btn{padding:10px 18px;border:0;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;}
  .btn-gen{background:#00897B;color:#fff;} .btn-gen:hover{background:#4DD0E1;}
  .btn-back{background:rgba(65,75,90,.5);color:#cbd5e1;text-decoration:none;display:inline-block;} .btn-back:hover{background:rgba(65,75,90,.8);}
  .msg{padding:10px 12px;border-radius:8px;font-size:.85rem;margin-bottom:14px;
    background:rgba(0,137,123,.15);border:1px solid rgba(0,137,123,.4);color:#80CBC4;}
  table{width:100%;border-collapse:collapse;font-size:.85rem;}
  th{text-align:left;color:#9CA3B0;font-size:.75rem;text-transform:uppercase;letter-spacing:.03em;
    padding:8px 10px;border-bottom:1px solid rgba(65,75,90,.5);}
  td{padding:8px 10px;border-bottom:1px solid rgba(65,75,90,.3);}
  .team{color:#4DD0E1;font-size:.8rem;}
  .pin-cell{font-family:monospace;font-size:1rem;color:#fbbf24;letter-spacing:.15em;}
  .edit-form{display:flex;gap:6px;align-items:center;}
  .edit-form input{width:70px;padding:6px 8px;border-radius:6px;border:1px solid rgba(65,75,90,.6);
    background:rgba(22,28,36,.6);color:#e2e8f0;font-size:14px;text-align:center;
    font-family:monospace;letter-spacing:.15em;}
  .edit-form button{padding:6px 12px;border:0;border-radius:6px;background:#00897B;color:#fff;
    font-size:.8rem;cursor:pointer;} .edit-form button:hover{background:#4DD0E1;}
</style></head><body>
<div class="wrap">
  <h1>⚙️ Administrar PINs</h1>
  <p class="sub">Asigna o cambia el PIN de cada tecnico. Comparte el PIN por WhatsApp.</p>
  <div class="actions">
    <form method="post" style="display:inline"><input type="hidden" name="action" value="generate_all">
      <button type="submit" class="btn btn-gen">Generar PINs faltantes</button></form>
    <a href="/" class="btn btn-back">Volver al dashboard</a>
  </div>
  {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
  <table>
    <tr><th>Nombre</th><th>Equipo</th><th>PIN</th><th>Cambiar</th></tr>
    {% for u in users %}
    <tr>
      <td>{{ u.short }}<br><span style="color:#64748b;font-size:.75rem;">{{ u.email }}</span></td>
      <td><span class="team">{{ u.team }}</span></td>
      <td class="pin-cell">{{ u.pin }}</td>
      <td><form method="post" class="edit-form">
        <input type="hidden" name="action" value="set_pin">
        <input type="hidden" name="email" value="{{ u.email }}">
        <input type="text" name="new_pin" maxlength="4" pattern="[0-9]{4}" inputmode="numeric" placeholder="0000">
        <button type="submit">OK</button>
      </form></td>
    </tr>
    {% endfor %}
  </table>
</div>
</body></html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
