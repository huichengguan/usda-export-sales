@echo off
setlocal
title USDA Export Sales & Inspections - Auto Updater
cd /d "%~dp0"

echo ======================================================================
echo    USDA Export Sales & Inspections Automated Pipeline
echo ======================================================================
echo Directory: %~dp0
echo.
echo Checking USDA servers (FAS Export Sales & AMS Inspections)...
echo.

python fetch_and_update.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Pipeline encountered an error during execution.
    goto end
)

echo.
echo Checking git status for dashboard changes...
git status --porcelain index.html multi_commodity_payload.json > "%TEMP%\usda_git_status.txt"
set /p GIT_CHANGES=<"%TEMP%\usda_git_status.txt"
del "%TEMP%\usda_git_status.txt" 2>nul

if not "%GIT_CHANGES%"=="" (
    echo.
    echo [SYNC] New data or dashboard updates detected!
    echo Pushing updates to GitHub Pages...
    git add index.html multi_commodity_payload.json dashboard_template.html fetch_and_update.py
    git commit -m "Auto-update USDA Export Sales & Inspections dashboard [skip ci]"
    git push origin main
    if %ERRORLEVEL% EQU 0 (
        echo.
        echo [SUCCESS] Live GitHub Pages dashboard successfully updated!
        echo URL: https://huichengguan.github.io/usda-export-sales/
    ) else (
        echo.
        echo [WARN] Could not push to GitHub automatically.
    )
) else (
    echo [INFO] No new updates to push. Dashboard is already up to date.
    echo URL: https://huichengguan.github.io/usda-export-sales/
)

:end
echo.
echo ======================================================================
echo Execution complete.
echo ======================================================================
pause
