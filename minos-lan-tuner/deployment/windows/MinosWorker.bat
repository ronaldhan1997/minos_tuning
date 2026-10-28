@echo off
setlocal EnableExtensions
title Minos worker
cd /d "%~dp0..\.."
if not exist "apps\worker\cli.py" (
  echo Keep this file in minos-lan-tuner\deployment\windows inside the repo checkout.
  pause
  exit /b 1
)

set "STATE=%LOCALAPPDATA%\MinosWorker"
if not exist "%STATE%" mkdir "%STATE%"
set "ENVFILE=%STATE%\worker.env"
set "CACHE_DIR=%STATE%\cache"
set "WORK_DIR=%STATE%\work"
set "PY="
set "PYARGS="

if /I "%~1"=="stop" goto stop
if /I "%~1"=="setup" goto ask
if not "%~1"=="" goto fromargs
if exist "%ENVFILE%" goto load
goto ask

:fromargs
if "%~2"=="" (
  echo Usage: MinosWorker.bat http://ubuntu-ip:8091 TOKEN [worker-id]
  echo        MinosWorker.bat setup
  echo        MinosWorker.bat stop
  pause
  exit /b 1
)
set "COORDINATOR_URL=%~1"
set "WORKER_TOKEN=%~2"
if "%~3"=="" (set "WORKER_ID=%COMPUTERNAME%") else (set "WORKER_ID=%~3")
goto save

:ask
echo.
echo This window registers this PC and then stays open as the worker.
echo Close the window to stop it. Run MinosWorker.bat setup to change the server.
echo.
echo Use the Ubuntu LAN address, for example http://192.168.1.20:8091
echo 127.0.0.1 is this PC, not the coordinator.
echo The token is printed once on the Ubuntu machine by:
echo   python -m apps.coordinator.cli worker token NAME
echo.
set /p "COORDINATOR_URL=Coordinator URL: "
set /p "WORKER_TOKEN=Worker token: "
set "WORKER_ID=%COMPUTERNAME%"
set /p "WORKER_ID=Worker id [%COMPUTERNAME%]: "
goto save

:save
if "%COORDINATOR_URL%"=="" goto missing
if "%WORKER_TOKEN%"=="" goto missing
if "%WORKER_ID%"=="" set "WORKER_ID=%COMPUTERNAME%"
> "%ENVFILE%" echo COORDINATOR_URL=%COORDINATOR_URL%
>>"%ENVFILE%" echo WORKER_TOKEN=%WORKER_TOKEN%
>>"%ENVFILE%" echo WORKER_ID=%WORKER_ID%
goto run

:load
for /f "usebackq tokens=1,* delims==" %%A in ("%ENVFILE%") do (
  if not "%%A"=="" set "%%A=%%B"
)
goto run

:missing
echo A coordinator URL and a worker token are both required.
pause
exit /b 1

:run
if "%SCORING_VERSION%"=="" set "SCORING_VERSION=v1"
if "%EXECUTION_MODE%"=="" set "EXECUTION_MODE=mock"
if "%MAX_CONCURRENT_JOBS%"=="" set "MAX_CONCURRENT_JOBS=1"
call :findpython
if errorlevel 1 goto nopython
"%PY%" %PYARGS% -c "import apps.worker.cli" >nul 2>&1
if errorlevel 1 (
  echo Installing worker dependencies. This step needs a network connection once.
  "%PY%" %PYARGS% -m pip install -e .
  if errorlevel 1 (
    echo pip install failed.
    pause
    exit /b 1
  )
)
echo Checking this PC, then registering %WORKER_ID% at %COORDINATOR_URL%
"%PY%" %PYARGS% -m apps.worker.cli doctor
if errorlevel 1 (
  echo Doctor failed. This checkout must be the same Minos commit as the coordinator.
  pause
  exit /b 1
)
"%PY%" %PYARGS% -m apps.worker.cli register
if errorlevel 1 (
  echo Register failed. Check the URL, the token, and that the coordinator listens on a LAN address.
  pause
  exit /b 1
)
echo Registered as %WORKER_ID%. Refresh the Workers page.
echo Leave this window open. Closing it stops the worker.
"%PY%" %PYARGS% -m apps.worker.cli start
exit /b %ERRORLEVEL%

:stop
if not exist "%ENVFILE%" (
  echo No saved worker settings. Run MinosWorker.bat once before stop.
  pause
  exit /b 1
)
for /f "usebackq tokens=1,* delims==" %%A in ("%ENVFILE%") do (
  if not "%%A"=="" set "%%A=%%B"
)
if "%SCORING_VERSION%"=="" set "SCORING_VERSION=v1"
if "%EXECUTION_MODE%"=="" set "EXECUTION_MODE=mock"
if "%MAX_CONCURRENT_JOBS%"=="" set "MAX_CONCURRENT_JOBS=1"
call :findpython
if errorlevel 1 goto nopython
"%PY%" %PYARGS% -m apps.worker.cli stop
exit /b %ERRORLEVEL%

:nopython
echo Python 3.11 was not found. Install it from https://www.python.org/downloads/windows/
echo and enable "Add python.exe to PATH".
pause
exit /b 1

:findpython
py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 goto findpython2
set "PY=py"
set "PYARGS=-3.11"
goto :eof
:findpython2
python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=python"
set "PYARGS="
goto :eof
