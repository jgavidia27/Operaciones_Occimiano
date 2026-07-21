@echo off
REM ============================================================
REM Sync Programacion STO: Excel (Google Drive G:) -> Supabase
REM Corre en la PC del usuario porque necesita acceso a G:.
REM Programar en el Task Scheduler de Windows 2x/dia.
REM ============================================================
cd /d "C:\Users\jgavi\Documents\occimiano_dashboard"
pythonw sync_programacion_sto.py >> sync_programacion_sto.log 2>&1
