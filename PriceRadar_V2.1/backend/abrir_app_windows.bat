@echo off
chcp 65001 >nul
if not exist .venv\Scripts\python.exe (
  echo Ambiente não encontrado. Execute instalar_windows.bat primeiro.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
start "" http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000
