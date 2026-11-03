@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo [pdf2onenote] Drag a PDF file onto this .bat
  echo [pdf2onenote] or run: python -m pdf2onenote all book.pdf --level 1
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo [pdf2onenote] .venv not found. Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pdf2onenote all "%~1" --level 1 --out "%~dpn1_chapters" %2 %3
pause
