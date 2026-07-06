@echo off
REM ============================================================
REM Sync duraciones netas (solo lavadora): Fracttal -> Supabase
REM Programar en Task Scheduler cada 2 horas.
REM ============================================================
cd /d "C:\Users\jgavi\Documents\occimiano_dashboard"
python -u sync_estim_neta.py --modo incremental >> sync_estim_neta.log 2>&1
