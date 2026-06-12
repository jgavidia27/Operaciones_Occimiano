@echo off
title Occimiano - Dashboard Operacional
color 1F
echo.
echo  Iniciando Occimiano en http://localhost:8501
echo.
cd /d "C:\Users\jgavi\Documents\occimiano_dashboard"
python -m streamlit run app.py --server.port 8501 --server.headless false
pause
