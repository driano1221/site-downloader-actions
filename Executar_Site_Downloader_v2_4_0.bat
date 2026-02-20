@echo off
REM Site Downloader v2.4.0 - Launcher para Windows
REM Este script inicia o Site Downloader

setlocal
cd /d %~dp0

echo ========================================
echo  Site Downloader v2.4.0
echo  Versao Melhorada e Robusta
echo ========================================
echo.

REM Verifica se Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao encontrado!
    echo.
    echo Por favor, instale Python 3.8+ de:
    echo https://www.python.org/downloads/
    echo.
    echo Certifique-se de marcar "Add Python to PATH" durante a instalacao.
    echo.
    pause
    exit /b 1
)

echo [OK] Python encontrado
echo.

REM Verifica se wget esta instalado
where wget >nul 2>&1
if %errorlevel% neq 0 (
    echo [AVISO] wget nao encontrado no PATH!
    echo.
    echo Para baixar sites, voce precisa do wget instalado.
    echo.
    echo Opcoes de instalacao:
    echo 1. Chocolatey: choco install wget
    echo 2. Baixar de: https://eternallybored.org/misc/wget/
    echo.
    echo Adicione o wget ao PATH do Windows apos instalar.
    echo.
    pause
) else (
    echo [OK] wget encontrado
    echo.
)

REM Inicia o aplicativo
echo Iniciando Site Downloader...
echo.

python site_downloader_v2_4_0.py

if %errorlevel% neq 0 (
    echo.
    echo [ERRO] Aplicacao encerrou com erro (codigo: %errorlevel%)
    echo.
    pause
)

endlocal
