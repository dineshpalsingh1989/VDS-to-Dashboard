@echo off
rem Start the IRIV Edge AI demo on Windows: double-click this file, then open http://127.0.0.1:8060/ai
cd /d "%~dp0..\.."
where uv >nul 2>nul
if %errorlevel%==0 (
  uv run --no-project --python 3.14 --with-requirements requirements.txt python iriv-edge\demo\run_demo.py %*
) else (
  python iriv-edge\demo\run_demo.py %*
)
if errorlevel 1 (
  echo.
  echo The demo needs Python 3.14 with the packages in requirements.txt, or uv: https://docs.astral.sh/uv/
)
pause
