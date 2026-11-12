@echo off
chcp 65001 >nul 2>nul
setlocal enabledelayedexpansion
title JieYa - Recursive Extractor
cd /d "%~dp0"

rem ============================================================
rem  Auto-detect a usable Python 3.8+
rem
rem  NOTE 1) Windows "App execution alias" (WindowsApps\python.exe)
rem          is a 121-byte stub that just opens the Microsoft
rem          Store. So we never trust a file name: every
rem          candidate is actually EXECUTED once, and the stub
rem          fails that test and gets rejected automatically.
rem
rem  NOTE 2) This file must stay pure ASCII. cmd.exe gets out of
rem          sync when a .bat contains multi-byte characters and
rem          starts executing half-lines. The Chinese help text
rem          lives in python-help.txt and is printed by TYPE.
rem ============================================================

set "PY="
rem minimal capability check: only a real Python 3 can import these
set "CHECK=import sys,http.server,zipfile"

rem --- 1) py launcher: ask it for the real interpreter path ---
if not defined PY (
  for /f "delims=" %%E in ('py -3 -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "PY=%%E"
  call :verify
)

rem --- 2) python / python3 from PATH ---
if not defined PY (
  for /f "delims=" %%E in ('python -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "PY=%%E"
  call :verify
)
if not defined PY (
  for /f "delims=" %%E in ('python3 -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "PY=%%E"
  call :verify
)

rem --- 3) common install locations ---
if not defined PY (
  for %%R in (
    "%LOCALAPPDATA%\Programs\Python"
    "%ProgramFiles%"
    "%ProgramFiles(x86)%"
    "%USERPROFILE%\AppData\Local\Programs\Python"
  ) do (
    if exist "%%~R" (
      for /d %%V in ("%%~R\Python3*") do call :trypath "%%~V\python.exe"
    )
  )
)

rem --- 4) bundled Python ---
if not defined PY (
  for /d %%V in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do call :trypath "%%~V\python.exe"
)

if not defined PY goto :nopython

echo [JieYa] Python: %PY%
"%PY%" -u -X utf-8 "%~dp0JieYa.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo  Program exited with an error ^(code %RC%^).
  echo  Press any key to close.
  pause >nul
)
endlocal & exit /b %RC%

rem ============================================================
:verify
rem validate %PY%; clear it when unusable
if not defined PY exit /b
if not exist "%PY%" (
  set "PY="
  exit /b
)
"%PY%" -c "%CHECK%" >nul 2>nul
if errorlevel 1 set "PY="
exit /b

:trypath
rem try one concrete python.exe path
if defined PY exit /b
if not exist "%~1" exit /b
"%~1" -c "%CHECK%" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b

:nopython
chcp 65001 >nul 2>nul
echo.
if exist "%~dp0python-help.txt" (
  type "%~dp0python-help.txt"
) else (
  echo  ============================================================
  echo   [ERROR] No usable Python 3.8+ was found on this computer.
  echo  ============================================================
  echo.
  echo  If typing python opens the Microsoft Store, you only have
  echo  the Store placeholder, not a real Python.
  echo.
  echo  1. Install Python 3 from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH".
  echo  2. Or turn off the python.exe / python3.exe aliases under
  echo     Settings - Apps - Advanced app settings - App execution aliases.
  echo.
)
echo.
pause
exit /b 1
