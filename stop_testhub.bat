@echo off
chcp 65001 >nul
title testhub stopper
echo Stopping testhub services on ports 3000/8000/8100 ...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":3000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8100 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
echo Done.
pause >nul
