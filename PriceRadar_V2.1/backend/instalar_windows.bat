@echo off
chcp 65001 >nul
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Instalacao concluida. O banco comeca vazio nesta versao.
echo Execute abrir_app_windows.bat para abrir o PriceRadar.
pause
