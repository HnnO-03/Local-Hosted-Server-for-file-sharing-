@echo off
setlocal
title Projects Laboratory Launcher

:: Always run from the folder containing this .bat file
cd /d "%~dp0"

:: ============================================================
:: Quick Config (change password or port here before interview)
:: ============================================================
set "LAB_PORT=8080"
set "LAB_ADMIN_PASSWORD=admin123"

:: Check that core project files exist
if not exist "server.py" (
    echo [ERROR] server.py not found in %cd%
    pause
    exit /b 1
)
if not exist "api.py" (
    echo [ERROR] api.py not found in %cd%
    pause
    exit /b 1
)

:: Detect Python command (python or py launcher)
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=py"
    ) else (
        echo [ERROR] Python 3 was not found in PATH.
        pause
        exit /b 1
    )
)

echo Starting Projects Laboratory Server on port %LAB_PORT%...
start "Projects Laboratory - Server" cmd /k "%PY_CMD% server.py"

:: Wait 2 seconds so the socket finishes binding before opening browser & client
timeout /t 2 /nobreak >nul

echo Opening Web Frontend...
start "" "http://127.0.0.1:%LAB_PORT%"

if exist "client.py" (
    echo Starting Pure-Socket CLI / Wireshark Client...
    start "Projects Laboratory - Client" cmd /k "%PY_CMD% client.py"
)

echo.
echo Everything is running!
echo Close the Server window (or press Ctrl+C inside it) to stop the server.
timeout /t 3 /nobreak >nul
endlocal