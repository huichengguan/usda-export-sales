@echo off
title Push USDA Export Sales to GitHub
cd /d "C:\Users\guang\.gemini\antigravity\scratch\usda_export_sales"
echo ========================================================
echo   Push USDA Export Sales Tracker to GitHub
echo ========================================================
echo.
set /p REPO_URL="Paste your GitHub repository URL (e.g. https://github.com/huichengguan/usda-export-sales.git): "

if "%REPO_URL%"=="" (
    echo [ERROR] No URL entered. Exiting.
    pause
    exit /b
)

echo.
echo -> Setting remote repository to: %REPO_URL%
git remote remove origin 2>nul
git remote add origin %REPO_URL%
git branch -M main

echo.
echo -> Pushing files to GitHub...
echo (If a browser window pops up, click 'Sign in with your browser' to authorize)
git push -u origin main

echo.
echo ========================================================
echo   Done! Your repository is now live on GitHub.
echo ========================================================
pause
