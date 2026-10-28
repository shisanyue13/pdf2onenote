@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [pdf2onenote] creating venv...
python -m venv .venv
if errorlevel 1 goto fail
echo [pdf2onenote] installing core dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo [pdf2onenote] installing OPTIONAL OCR dependencies (scanned PDFs)...
".venv\Scripts\python.exe" -m pip install -r requirements-ocr.txt
if errorlevel 1 (
  echo [pdf2onenote] WARNING: OCR deps failed to install.
  echo [pdf2onenote] Split/import still work; scanned PDFs will fall back to fixed-page splitting.
)
echo.
echo [pdf2onenote] done. Now run:
echo     .venv\Scripts\python.exe -m pdf2onenote split book.pdf --dry-run
echo     .venv\Scripts\python.exe -m pdf2onenote all   book.pdf
pause
exit /b 0
:fail
echo [pdf2onenote] failed. Make sure Python 3.10+ is on PATH.
pause
exit /b 1
