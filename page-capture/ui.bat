@echo off
rem Atualiza (git pull) e abre a interface web do page-capture (duplo clique).
rem Tudo dentro de um bloco ( ... ): o cmd le o bloco inteiro antes de o executar,
rem por isso um git pull que altere este ficheiro nao afeta a execucao em curso.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
(
  if not exist ".venv\Scripts\python.exe" (
    echo Ambiente .venv nao encontrado. Veja o README: py -m venv .venv
    pause
    exit /b 1
  )
  where git >nul 2>nul
  if errorlevel 1 (
    echo [aviso] git nao encontrado; a usar a versao atual.
  ) else (
    set "OLD="
    for /f %%i in ('git rev-parse HEAD 2^>nul') do set "OLD=%%i"
    echo A atualizar ^(git pull^)...
    git pull --ff-only
    if errorlevel 1 (
      echo [aviso] git pull falhou; a usar a versao atual.
    ) else if defined OLD (
      git diff --quiet !OLD! HEAD -- requirements.txt requirements-dev.txt
      if errorlevel 1 (
        echo Dependencias alteradas: a instalar...
        ".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
        if errorlevel 1 echo [aviso] pip install falhou; veja a mensagem acima.
      )
    )
  )
  ".venv\Scripts\python.exe" capture_ui.py %*
  if errorlevel 1 pause
  exit /b
)
