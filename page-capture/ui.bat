@echo off
rem Abre a interface web do page-capture (duplo clique).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente .venv nao encontrado. Veja o README: py -m venv .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" capture_ui.py %*
if errorlevel 1 pause
