@echo off
title PredictX Backend
cd /d "C:\Users\Victor\Documents\Personal Workstation\football\predictx"

echo [PredictX] Starting backend on http://127.0.0.1:8002 ...

:: Start uvicorn in this window
start "PredictX Backend" cmd /k "C:\Program Files\Python39\Scripts\uvicorn.exe" app.main:app --reload --host 127.0.0.1 --port 8002

:: Wait 4 seconds for the server to boot, then open the browser
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:8002/docs"
