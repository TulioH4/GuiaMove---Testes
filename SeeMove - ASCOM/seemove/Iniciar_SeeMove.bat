@echo off
setlocal
title SeeMove
cd /d "%~dp0"

echo ===============================================
echo   SeeMove - Monitoramento postural
echo ===============================================
echo.
echo   Iniciando... o painel abre sozinho no navegador
echo   em alguns segundos, em http://127.0.0.1:5000
echo.
echo   Para ENCERRAR: feche esta janela ou pressione Ctrl+C.
echo.

python main.py --kinect-sdk --kinect-sdk-depth

echo.
echo   SeeMove foi encerrado.
pause
