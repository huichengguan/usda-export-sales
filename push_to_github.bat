@echo off
title Push USDA Export Sales to GitHub
cd /d "C:\Users\guang\.gemini\antigravity\scratch\usda_export_sales"
echo ========================================================
echo   Pushing USDA Export Sales Tracker to GitHub...
echo ========================================================
echo Target: https://github.com/huichengguan/usda-export-sales.git
echo.
git remote set-url origin https://github.com/huichengguan/usda-export-sales.git
git branch -M main
git push -u origin main
echo.
if %ERRORLEVEL% EQU 0 (
    echo ========================================================
    echo   [SUCCESS] Code successfully pushed to GitHub!
    echo ========================================================
) else (
    echo [NOTE] If a browser window popped up, please click 'Sign in' / 'Authorize' and try again.
)
echo.
pause
