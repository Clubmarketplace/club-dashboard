@echo off
chcp 65001 >nul
title Club Marketplace - Instalacao
cd /d %~dp0

echo ============================================
echo   Instalando o Dashboard Club Marketplace
echo ============================================
echo.

call "%~dp0_preparar_ambiente.bat"
if errorlevel 1 (
    echo.
    echo [ERRO] A instalacao nao foi concluida. Veja a mensagem acima.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Instalacao concluida!
echo ============================================
echo.
echo Confere se o arquivo .env tem preenchido ML_CLIENT_ID, ML_CLIENT_SECRET,
echo ML_REDIRECT_URI e ANTHROPIC_API_KEY antes de usar o sistema de verdade.
echo.
echo Agora use o arquivo iniciar_dashboard.bat pra abrir o painel.
echo.
pause
