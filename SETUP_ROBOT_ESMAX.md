# Setup Robot ESMAX

## 1. Instalar dependencias Python

```
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```

## 2. Crear credenciales Google (una sola vez)

1. Ve a https://console.cloud.google.com/
2. Crea un proyecto nuevo (ej. "robot-occimiano")
3. Ve a **APIs y Servicios → Biblioteca**
4. Busca "Gmail API" → Habilitar
5. Ve a **APIs y Servicios → Credenciales**
6. Clic en **+ Crear credenciales → ID de cliente OAuth 2.0**
7. Tipo de aplicación: **Aplicación de escritorio**
8. Nombre: "robot-esmax"
9. Clic en **Crear** → descargar JSON
10. Renombrar el archivo descargado a: `credentials_esmax.json`
11. Copiarlo a: `C:\Users\jgavi\Documents\occimiano_dashboard\`

En **Pantalla de consentimiento OAuth**, agrega tu email como usuario de prueba.

## 3. Primera ejecución (autenticación)

```
cd C:\Users\jgavi\Documents\occimiano_dashboard
python robot_esmax.py
```

Abrirá el navegador → autoriza con tu cuenta Gmail → listo. Guarda `token_esmax.json`.

## 4. Ejecuciones posteriores

```
python robot_esmax.py
```

No requiere navegador. El token se renueva automáticamente.

## 5. Programar ejecución automática (opcional)

Abrir **Programador de tareas de Windows**:
- Acción: `python C:\Users\jgavi\Documents\occimiano_dashboard\robot_esmax.py`
- Frecuencia: cada 2-4 horas
