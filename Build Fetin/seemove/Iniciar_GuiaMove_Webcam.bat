@echo off
setlocal
title GuiaMove (Webcam)
cd /d "%~dp0"

echo ===============================================
echo   GuiaMove - Monitoramento postural (WEBCAM)
echo ===============================================
echo.
echo   Iniciando com a webcam padrao (indice 0)... o painel abre
echo   sozinho no navegador em http://127.0.0.1:5000
echo.
echo   Se abrir a camera errada, edite este arquivo e troque
echo   "--camera 0" por 1, 2...
echo.
echo   Para ENCERRAR: feche esta janela ou pressione Ctrl+C.
echo.

python main.py --camera 0

echo.
echo   GuiaMove foi encerrado.
pause
