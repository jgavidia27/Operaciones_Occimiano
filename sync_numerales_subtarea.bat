@echo off
REM ============================================================
REM Sync Numerales Subtarea: Fracttal API -> Supabase
REM Sincroniza los ultimos 15 dias de OTs con formulario numeral.
REM Programar en Task Scheduler de Windows cada 2 horas.
REM ============================================================
cd /d "C:\Users\jgavi\Documents\occimiano_dashboard"
pythonw sync_numerales_subtarea.py --modo incremental >> sync_numerales_subtarea.log 2>&1
