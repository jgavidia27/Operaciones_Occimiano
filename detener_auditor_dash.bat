@echo off
REM ============================================================
REM  Detener Auditor Dash 1.0 (alertizador.py)
REM  Desactiva las tareas de Windows que lo corren a las 9:30 y 14:00.
REM  Doble-clic y aprueba el permiso de administrador (UAC).
REM  Para reactivarlo, cambia /DISABLE por /ENABLE.
REM ============================================================
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Solicitando permisos de administrador...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
echo Desactivando Auditor Dash 1.0...
schtasks /Change /TN "Alertizador Occimiano 0930" /DISABLE
schtasks /Change /TN "Alertizador Occimiano 1400" /DISABLE
echo.
echo === Estado final ===
schtasks /Query /TN "Alertizador Occimiano 0930" /FO LIST | findstr /I "Estado Status"
schtasks /Query /TN "Alertizador Occimiano 1400" /FO LIST | findstr /I "Estado Status"
echo.
echo Auditor Dash 1.0 DETENIDO (ambas tareas desactivadas).
echo Si dice "Deshabilitado/Disabled", quedo detenido.
pause
