@echo off
chcp 65001 >nul
title testhub one-click launcher (not a service)
set "ROOT=C:\Users\EDY\WorkBuddy\testhub_platform"
set "BACKEND_VENV=C:\Users\EDY\.workbuddy\binaries\python\envs\testhub_backend"
set "NODE=C:\Users\EDY\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
set "TESTGEN_VENV=C:\Users\EDY\WorkBuddy\testhub_platform\services\testgen\.venv"

echo Releasing ports 3000/8000/8100 if old processes exist...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":3000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8100 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

echo Starting testgen sidecar on 8100 ...
start "testgen-sidecar" cmd /k "cd /d %ROOT%\services\testgen && %TESTGEN_VENV%\Scripts\python.exe -m cli.main serve --host 127.0.0.1 --port 8100"

echo Starting backend API on 8000 ...
start "testhub-backend" cmd /k "cd /d %ROOT% && %BACKEND_VENV%\Scripts\python.exe manage.py runserver 0.0.0.0:8000"

echo Starting frontend on 3000 ...
start "testhub-frontend" cmd /k "cd /d %ROOT%\frontend && %NODE% node_modules\vite\bin\vite.js --host 0.0.0.0 --port 3000"

echo.
echo ===== testhub started (3 windows opened) =====
echo Frontend (entry) : http://localhost:3000   LAN: http://21.163.93.24:3000
echo Backend API      : http://127.0.0.1:8000
echo Login            : admin / Testhub  (prefilled, click login)
echo Close a window to stop that service.
echo ===============================================
pause >nul
