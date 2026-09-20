@echo off
REM Compila kinect_color_bridge.cpp contra o Kinect for Windows SDK v1.8.
REM Precisa do Visual Studio Build Tools (workload C++) instalado.

cd /d "%~dp0"
set KINECT_SDK=C:\Program Files\Microsoft SDKs\Kinect\v1.8

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64
if errorlevel 1 exit /b 1

cl /nologo /EHsc /O2 ^
   /I "%KINECT_SDK%\inc" ^
   kinect_color_bridge.cpp ^
   /link /LIBPATH:"%KINECT_SDK%\lib\amd64" Kinect10.lib ^
   /OUT:kinect_color_bridge.exe

if errorlevel 1 exit /b 1
echo.
echo Build OK: kinect_color_bridge.exe
