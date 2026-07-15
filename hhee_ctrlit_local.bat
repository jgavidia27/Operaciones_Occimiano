@echo off
REM ============================================================================
REM  hhee_ctrlit_local.bat — Corre sync_ctrlit.py todos los días desde tu PC
REM ============================================================================
REM  Este script actualiza las marcaciones ctrlit del día anterior + recalcula
REM  los veredictos HHEE. El cron GitHub Actions no puede hacer ctrlit porque
REM  la sesión no valida en Linux — por eso corre aquí en tu Windows local.
REM
REM  Para automatizarlo con Windows Task Scheduler:
REM    1) Abre "Programador de tareas" (busca en menú Inicio)
REM    2) Acción → Crear tarea básica
REM    3) Nombre: "HHEE Ctrlit Daily"
REM    4) Desencadenador: Diariamente a las 09:15 CLT
REM    5) Acción: Iniciar un programa
REM       Programa: cmd.exe
REM       Argumentos: /c "C:\Users\jgavi\Documents\occimiano_dashboard\hhee_ctrlit_local.bat"
REM    6) Terminar → Aceptar
REM
REM  Requiere que tu PC esté prendida a las 09:15 AM.
REM  Si se te olvida, corre este .bat manualmente cuando quieras actualizar.
REM ============================================================================

cd /d C:\Users\jgavi\Documents\occimiano_dashboard

REM Log en un archivo temporal para poder revisar si algo falla
set LOG=%TEMP%\hhee_ctrlit_%DATE:~-4%-%DATE:~3,2%-%DATE:~0,2%.log

echo ============================================================ > "%LOG%"
echo [%DATE% %TIME%] Iniciando sync_ctrlit >> "%LOG%"
echo ============================================================ >> "%LOG%"

REM 1) Sync marcaciones del día anterior
python sync_ctrlit.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR en sync_ctrlit >> "%LOG%"
    echo    Puede ser sesión expirada. Corre manualmente:
    echo    python sync_ctrlit.py --login-manual
    exit /b 1
)

REM 2) Recalcular motor HHEE con la data fresca de ctrlit
python he_evaluator.py --dias 14 >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR en he_evaluator >> "%LOG%"
    exit /b 1
)

echo [%DATE% %TIME%] Sync ctrlit + evaluator OK >> "%LOG%"
echo Log guardado en: %LOG%
