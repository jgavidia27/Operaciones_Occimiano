# Correctivas · Espejo

Aplicación **espejo** (solo lectura) de la tabla `llamados_correctivos`
de Supabase. Es un panel liviano, complementario al dashboard principal
de Occimiano, pensado para monitorear en vivo lo que los 3 robots
(Copec / Aramco / Shell) y las OTs directas están alimentando.

**No reemplaza a Supabase ni al dashboard principal** — Supabase sigue
siendo el almacén de verdad; este sitio solo lo lee.

## Vistas

- **📰 Feed cronológico**: tarjetas ordenadas por fecha (más nuevo
  arriba). Cada card muestra OS, EDS, cliente, técnico, falla y badges
  de fuente + prioridad + cumplimiento. Ideal para monitoreo pasivo.
- **📋 Tabla enriquecida**: tabla ordenable con export a CSV. Ideal
  para búsquedas rigurosas y análisis.

Filtros globales (fuente, cliente, prioridad, cumplimiento, rango de
fechas, buscador libre) se aplican a ambas vistas simultáneamente.

## Deploy en Streamlit Cloud

1. Ir a <https://share.streamlit.io/> con la misma cuenta que hostea el
   dashboard principal.
2. **New app** → seleccionar el repositorio
   `jgavidia27/Operaciones_Occimiano`, rama `main`.
3. **Main file path**: `correctivas_mirror/app.py`
4. **Advanced settings** → **Secrets** (pegar tal cual):
   ```toml
   SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
   SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
   ```
   (usar la service_role key del proyecto de Supabase — la misma que ya
   usa el dashboard principal).
5. **App URL** sugerida: `correctivas-occimiano`
   → queda en <https://correctivas-occimiano.streamlit.app>
6. **Deploy**.

El primer despliegue toma ~2 min. Cada `git push origin main` recicla la
app automáticamente en unos ~30 s.

## Correr localmente

```bash
cd correctivas_mirror
pip install -r requirements.txt
export SUPABASE_URL="https://puefgkyjghwwgdfxbrex.supabase.co"
export SUPABASE_KEY="..."
streamlit run app.py
```

## Datos

- Fuente única: tabla `llamados_correctivos` de Supabase.
- Corte inicial: **2026-05-01** (configurable en `FECHA_CORTE` dentro de
  `app.py`).
- Cache Streamlit: 5 minutos (botón **🔄 Recargar** fuerza refresco).

## Roadmap (paso 2, futuro)

- Que los robots (Copec / Aramco / Shell) también envíen a este mirror,
  en paralelo a Supabase — hoy solo lee de Supabase.
- Alertas push cuando entra una P1.
- Vista móvil optimizada (por ahora es responsiva pero pensada para
  escritorio).
