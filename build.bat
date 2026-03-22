@echo off
setlocal

echo === Build platform: Windows ===

python -m pip install -r requirements.txt --quiet
if errorlevel 1 exit /b 1

python -m pip install pyinstaller --quiet
if errorlevel 1 exit /b 1

python -m PyInstaller codex_register.spec --clean --noconfirm
if errorlevel 1 exit /b 1

set "OUTPUT=dist\codex-register-windows-X64.exe"

if exist dist\codex-register.exe (
    move /Y dist\codex-register.exe "%OUTPUT%" >nul
    echo === Build complete: %OUTPUT% ===
) else (
    echo === Build failed: dist\codex-register.exe not found ===
    exit /b 1
)
