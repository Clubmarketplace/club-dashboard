@echo off
chcp 65001 >nul
title Club Marketplace - Iniciar Dashboard
cd /d %~dp0

echo Conferindo se o ambiente esta pronto, corrige sozinho se precisar...
call "%~dp0_preparar_ambiente.bat"
if errorlevel 1 (
    echo.
    echo [ERRO] Nao foi possivel preparar o ambiente. Veja a mensagem acima.
    pause
    exit /b 1
)

set PYVENV=%~dp0venv\Scripts\python.exe

echo.
echo Iniciando o backend numa janela separada...
start "Club Marketplace - Backend - nao feche sem querer" cmd /k "cd /d %~dp0 && "%PYVENV%" -m uvicorn app.main:app --reload"

echo Aguardando o servidor responder na porta 8000...
set TENTATIVAS=0

:AguardaServidor
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',8000); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto ServidorNoAr
set /a TENTATIVAS+=1
if %TENTATIVAS% GEQ 20 goto ServidorNaoSubiu
timeout /t 1 /nobreak >nul
goto AguardaServidor

:ServidorNoAr
echo Servidor no ar! Abrindo o painel no navegador...
start "" http://127.0.0.1:8000
goto Fim

:ServidorNaoSubiu
echo.
echo [AVISO] O servidor nao respondeu em 20 segundos.
echo Olha a janela do Backend que abriu -- ela deve ter uma mensagem de
echo erro explicando o que aconteceu. Copia o que aparecer la e manda
echo pra analise.
echo.

:Fim
echo O backend continua rodando na janela do Backend.
echo Pra PARAR o sistema, feche aquela janela, ou aperte Ctrl+C nela.
echo Essa janela aqui pode ser fechada sem problema.
echo.
pause
