"""
mobile_sto.py — Vista móvil del Desempeño STO (Flask)
=====================================================
Lee los datos pre-calculados por el dashboard Streamlit (sto_data.json).
NO computa KPIs por su cuenta — es un visor puro del dashboard.

Ejecutar local:   python mobile_sto.py
Deploy Render:    gunicorn mobile_sto:app
"""

import json, os, traceback
from datetime import datetime, date

from flask import Flask, request, render_template_string, send_file

app = Flask(__name__)
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


def _load_sto_data():
    if not os.path.exists(_STO_DATA_PATH):
        return None
    with open(_STO_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/wallpaper")
def wallpaper():
    return send_file(os.path.join(_APP_DIR, "wallpaper_light.jpg"), mimetype="image/jpeg")


@app.route("/")
def index():
  try:
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

    equipos_label = {k: v.get("label", k) for k, v in equipos_info.items()}

    # Lista de técnicos para selector
    all_tecnicos = []
    for eq_key, eq_info in equipos_info.items():
        for t_full in eq_info.get("miembros", []):
            t_short = full_to_short.get(t_full, t_full)
            all_tecnicos.append({"full": t_full, "short": t_short, "equipo": eq_info.get("label", eq_key)})
    all_tecnicos.sort(key=lambda x: x["short"])

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
    for t, d in tec_data.items():
        if not t or d["total"] == 0:
            continue
        pct = round(d["cumple"] / d["total"] * 100, 1)
        bp, bc = _bono_sla(pct)
        sla_tecnicos.append({
            "nombre": full_to_short.get(t, t),
            "equipo": equipos_label.get(d["equipo"], d["equipo"]),
            "pct": pct, "cumple": d["cumple"], "total": d["total"],
            "bono_pct": bp, "bono_clp": bc,
        })
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
    for t, d in ptec_data.items():
        if not t or d["total"] == 0:
            continue
        pct = round(d["buenas"] / d["total"] * 100, 1)
        bp, bc = _bono_prec(pct)
        prec_tecnicos.append({
            "nombre": full_to_short.get(t, t),
            "equipo": equipos_label.get(d["equipo"], d["equipo"]),
            "pct": pct, "buenas": d["buenas"], "total": d["total"], "bono_clp": bc,
        })
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

    for t, d in pm_by_tec.items():
        t_short = full_to_short.get(t, t)
        n_f = fallas_by_short.get(t_short, 0)
        n_pm = d["pms"]
        _, bc_t, ex_t = _bono_calidad(n_f, n_pm)
        cal_tecnicos.append({
            "nombre": t_short,
            "equipo": equipos_label.get(d["equipo"], d["equipo"]),
            "exactitud": round(ex_t, 1), "fallas": n_f, "pms": n_pm, "bono_clp": bc_t,
        })
    cal_tecnicos.sort(key=lambda x: x["exactitud"], reverse=True)

    cal_data = {"exactitud": round(exactitud, 1), "fallas": fallas_total,
                "pms_total": pms_total, "bono_pct": bp_cal, "bono_clp": bc_cal,
                "tecnicos": cal_tecnicos}

    bono_total = bc_sla + bc_cal + bc_prec

    return render_template_string(
        HTML_TEMPLATE,
        sla=sla_data, cal=cal_data, prec=prec_data,
        bono_total=bono_total,
        max_sla=MAX_SLA, max_cal=MAX_CAL, max_prec=MAX_PREC,
        bono_pool=BONO_TOTAL,
        trim_key=trim_key, trim_label=trim["label"], trimestres=TRIMESTRES,
        mes_sel=mes_sel, meses_filtro=meses_filtro,
        tecnico_sel=tecnico_sel, equipo_sel=equipo_sel,
        tecnicos=all_tecnicos, equipos=equipos_label,
        color_pct=_color_pct,
        updated_at=data.get("updated_at", "—"),
        now=datetime.now(),
    )
  except Exception:
    tb = traceback.format_exc()
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Error</title>
    <style>body{{font-family:monospace;background:#0f172a;color:#e2e8f0;padding:16px}}
    pre{{white-space:pre-wrap;word-break:break-all;font-size:12px;background:#1e293b;
    padding:12px;border-radius:8px;overflow-x:auto}}
    a{{color:#3b82f6}}</style></head><body>
    <h2 style="color:#ef4444">Error al cargar datos</h2>
    <p><a href="/">Volver al inicio</a></p>
    <pre>{tb}</pre></body></html>""", 500


ERROR_TEMPLATE = r"""
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Desempeño STO</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #0f172a; color: #e2e8f0;
    display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
  .msg { text-align: center; max-width: 360px; }
  .msg h2 { color: #f59e0b; margin-bottom: 12px; }
  .msg p { color: #94a3b8; font-size: .9rem; line-height: 1.5; }
  .msg a { color: #3b82f6; }
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
<title>Desempeño STO — Occimiano</title>
<style>
  :root {
    --bg: #0a1628; --card: rgba(15,22,42,.92); --border: rgba(51,65,85,.7);
    --text: #e2e8f0; --muted: #94a3b8;
    --green: #22c55e; --yellow: #f59e0b; --red: #ef4444;
    --blue: #3b82f6; --teal: #14b8a6;
    --accent: #0d5e6b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
    background-image: url('/wallpaper');
    background-size: cover; background-position: center; background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
  }
  body::before {
    content: ''; position: fixed; inset: 0; z-index: 0;
    background: linear-gradient(180deg, rgba(10,22,40,.88) 0%, rgba(10,22,40,.82) 50%, rgba(10,22,40,.90) 100%);
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
    background: linear-gradient(135deg, var(--teal), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
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
  .tab-btn.active { background: linear-gradient(135deg, var(--accent), var(--blue)); color: #fff; border-color: transparent; }
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
  <h1>Desempeño STO</h1>
  <p class="subtitle">Occimiano Operaciones · {{ now.strftime('%d/%m/%Y %H:%M') }}</p>
</div>
<div class="data-source">Datos del dashboard · actualizado {{ updated_at[:16] | replace('T',' ') }}</div>

<form class="filters" id="filterForm">
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
  <div class="tab-btn" onclick="showTab('bono')">Bonos</div>
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
  <h2>Ranking técnicos</h2>
  <div class="ranking">
    {% for t in sla.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span></span>
      <span class="rank-pct" style="color:{{ color_pct(t.pct) }}">{{ t.pct }}%</span>
      <span class="rank-clp">${{ '{:,.0f}'.format(t.bono_clp) }}</span>
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
  <h2>Ranking técnicos</h2>
  <div class="ranking">
    {% for t in cal.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span></span>
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
  <h2>Ranking técnicos</h2>
  <div class="ranking">
    {% for t in prec.tecnicos %}
    <div class="rank-row">
      <span class="rank-name">{{ t.nombre }} <span class="rank-eq">{{ t.equipo }}</span></span>
      <span class="rank-pct" style="color:{{ color_pct(t.pct) }}">{{ t.pct }}%</span>
      <span class="rank-clp">{{ t.buenas }}/{{ t.total }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- TAB BONOS -->
<div class="tab-content" id="tab-bono">
  <div class="kpi-card bono">
    <div class="kpi-header"><span class="kpi-title">Resumen Bono · {{ trim_label }}</span></div>
    <div class="bono-grid">
      <div class="bono-item">
        <div class="label">SLA (40%)</div>
        <div class="value" style="color:{{ color_pct(sla.pct) }}">${{ '{:,.0f}'.format(sla.bono_clp) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">{{ sla.pct }}% → {{ sla.bono_pct }}%</div>
      </div>
      <div class="bono-item">
        <div class="label">Efectividad (30%)</div>
        <div class="value" style="color:{{ color_pct(cal.exactitud) }}">${{ '{:,.0f}'.format(cal.bono_clp) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">{{ cal.exactitud }}% → {{ cal.bono_pct }}%</div>
      </div>
      <div class="bono-item">
        <div class="label">Precisión (30%)</div>
        <div class="value" style="color:{{ color_pct(prec.pct) }}">${{ '{:,.0f}'.format(prec.bono_clp) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">{{ prec.pct }}% → {{ prec.bono_pct }}%</div>
      </div>
      <div class="bono-item total">
        <div class="label">Bono estimado total</div>
        <div class="value">${{ '{:,.0f}'.format(bono_total) }}</div>
        <div style="font-size:.7rem;color:var(--muted)">de ${{ '{:,.0f}'.format(bono_pool) }} pool/trim</div>
      </div>
    </div>
  </div>

  <div style="margin-top:12px;padding:12px;background:var(--card);border-radius:8px;font-size:.78rem;backdrop-filter:blur(12px);">
    <div style="font-weight:700;color:var(--muted);margin-bottom:6px;">ESCALA BONOS</div>
    <div style="margin-bottom:6px;">
      <b>SLA (40% · $200K pool)</b><br>
      <span style="color:#22c55e">≥95%→100%</span> ·
      <span style="color:#16a34a">93→90%</span> ·
      <span style="color:#4ade80">90→80%</span> ·
      <span style="color:#f59e0b">85→50%</span> ·
      <span style="color:#ef4444">&lt;85%→0%</span>
    </div>
    <div style="margin-bottom:6px;">
      <b>Calidad MP (30% · $150K pool)</b><br>
      <span style="color:#22c55e">≥98%→100%</span> ·
      <span style="color:#16a34a">96→90%</span> ·
      <span style="color:#4ade80">94→80%</span> ·
      <span style="color:#65a30d">92→70%</span> ·
      <span style="color:#f59e0b">90→60%</span> ·
      <span style="color:#ef4444">&lt;90%→0%</span>
    </div>
    <div>
      <b>Precisión (30% · $150K pool)</b><br>
      <span style="color:#22c55e">≥95%→100%</span> ·
      <span style="color:#16a34a">90→90%</span> ·
      <span style="color:#4ade80">85→80%</span> ·
      <span style="color:#65a30d">80→70%</span> ·
      <span style="color:#f59e0b">75→60%</span> ·
      <span style="color:#f97316">70→50%</span> ·
      <span style="color:#ef4444">&lt;70%→0%</span>
    </div>
  </div>
</div>

<div class="footer">
  Occimiano Operaciones · Datos del dashboard principal<br>
  Última sincronización: {{ updated_at[:16] | replace('T',' ') }}
</div>

</div>

<script>
function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  event.target.classList.add('active');
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
