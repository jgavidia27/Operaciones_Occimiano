# HHEE Daily Cron — Setup en GitHub Actions

Guía paso a paso para dejar el módulo HHEE corriendo automático a las 09:30 CLT cada día.

## Resumen

Un workflow de GitHub Actions ejecuta `hhee_daily.py` cada día. El script:

1. Detecta EDS nuevas de Fracttal → actualiza `estaciones_servicio`
2. Sincroniza nómina de Buk RRHH → actualiza `tecnicos_hhee`
3. Scrapea marcaciones ctrlit del día anterior → `buk_marcaciones`
4. Procesa CSV nuevo de Rastreosat (Google Drive) → `gps_eventos`
5. Recalcula veredictos HHEE de los últimos 14 días → `hhee_veredictos`

Al día siguiente ves los datos actualizados en el dashboard, sin tocar nada.

---

## Prerequisitos (1 vez)

### 1. Crear la tabla `hhee_sync_state` en Supabase

Ejecuta el SQL de `create_hhee_state_table.sql` en Supabase → SQL Editor → New query → Run.
Sirve para que el scraper de ctrlit persista sus cookies entre corridas.

### 2. Crear un Service Account en Google Cloud (para leer Drive)

1. Ve a https://console.cloud.google.com/ → Selecciona/crea un proyecto (ej. "occimiano-cron")
2. Menú lateral → **APIs & Services → Library** → busca **"Google Drive API"** → **Enable**
3. Menú lateral → **IAM & Admin → Service Accounts** → **Create Service Account**
   - Name: `hhee-cron-reader`
   - Rol: **NO le asignes rol** (le damos permiso directo a la carpeta después)
   - Create → Done
4. Clic en el service account creado → tab **"Keys"** → **Add Key → Create new key → JSON**
5. Se descarga un archivo `.json` (guárdalo temporalmente)
6. Abre el JSON, copia el campo `"client_email"` (algo tipo `hhee-cron-reader@occimiano-cron.iam.gserviceaccount.com`)

### 3. Compartir la carpeta Drive con el Service Account

1. En Google Drive, abre `OPERACIONES / OPERACIONES / HHEE semanal - GPS`
2. Botón derecho → **Compartir**
3. Pega el email del service account (paso 2.6) → Rol **Lector** (Viewer)
4. **Desmarca "Notificar"** → Enviar
5. Ahora copia el **ID de la carpeta** desde la URL:
   - URL típica: `https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrSt`
   - El ID es lo que va después de `/folders/`

### 4. Cargar 7 GitHub Secrets

Ve a https://github.com/jgavidia27/Operaciones_Occimiano → **Settings → Secrets and variables → Actions → New repository secret**.

Crea estos 7 secrets:

| Nombre | Valor |
|---|---|
| `SUPABASE_URL` | tu URL de Supabase (ya la tienes en Streamlit Secrets) |
| `SUPABASE_KEY` | tu key de Supabase |
| `BUK_API_TOKEN` | `bHWtKCdy7qR43esnnozqEcJQ` (token Buk RRHH) |
| `CTRLIT_USER` | tu usuario ctrlit |
| `CTRLIT_PASS` | tu password ctrlit |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Todo el JSON del paso 2.4** (copiar/pegar completo, incluyendo llaves) |
| `GDRIVE_HHEE_FOLDER_ID` | ID de la carpeta (paso 3.5) |

### 5. Subir la sesión de ctrlit a Supabase (1 vez)

Como ctrlit tiene reCAPTCHA, no se puede automatizar el login inicial. Corre en tu PC:

```powershell
cd C:\Users\jgavi\Documents\occimiano_dashboard
python sync_ctrlit.py --login-manual
```

Se abre un navegador, tú resuelves el captcha, y el script sube la sesión a Supabase (además del disco). Con eso GitHub Actions ya puede autenticarse por ~30 días. Cuando expire, repites este paso.

---

## Verificar que funciona

1. Ve a https://github.com/jgavidia27/Operaciones_Occimiano → **Actions**
2. Deberías ver el workflow **"HHEE Daily Sync"**
3. Clic → **"Run workflow"** (botón arriba a la derecha) → seleccionar rama `hhee-modulo` → Run
4. Espera ~5-10 min → deberías ver el step con ✅ en verde
5. Abre el dashboard → **Utilización del Tiempo → ⏰ Validación HHEE** → los veredictos aparecen actualizados

## Cuando algo falla

- **Ver logs**: Actions → última corrida → click en el step que falló
- **Sesión ctrlit expirada**: verás error tipo "sesión expiró". Corre `python sync_ctrlit.py --login-manual` en tu PC
- **Tabla `hhee_sync_state` no existe**: ejecuta el SQL del paso 1
- **Google Drive 403**: la carpeta no está compartida con el service account, o el JSON está mal

---

## Cambiar la hora del cron

Edita `.github/workflows/hhee-daily.yml` → línea `cron: "30 12 * * *"`:

- Formato: `minuto hora * * *` (en UTC)
- Chile en invierno es UTC-4 → 12:30 UTC = 08:30 CLT
- Chile en verano es UTC-3 → 12:30 UTC = 09:30 CLT
- Para 09:30 CLT en invierno pon `"30 13 * * *"`

---

## Ejecutar manualmente en tu PC

Los scripts individuales siguen funcionando en local:

```powershell
python sync_buk_rrhh.py
python sync_estaciones_from_ots.py --dias 30
python sync_ctrlit.py --fecha 15-07-2026
python sync_rastreosat_drive.py       # lee de G:\ si estás en tu PC
python he_evaluator.py --desde 2026-07-01 --hasta 2026-07-15
```

O todos juntos:
```powershell
python hhee_daily.py --dias 14
```
