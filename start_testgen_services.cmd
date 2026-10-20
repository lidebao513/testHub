@echo off
REM ============================================================
REM  testgen 三服务一键启动（清端口 + 启动）
REM  用途：本机开发时一键拉起 testgen sidecar / Django / Vite
REM        先强制清理占用 3900/8900/8910 的孤儿进程，
REM        避免 "You don't have permission to access that port"
REM  端口规划：前端 Vite=3900 / 后端 Django=8900 / testgen 引擎 sidecar=8910
REM  用法：在项目根目录双击本文件，或在 cmd 中执行
REM        start_testgen_services.cmd
REM  注意：每个服务开一个独立窗口，关闭窗口即停止该服务
REM ============================================================
setlocal

set "ROOT=C:\Users\EDY\WorkBuddy\testhub_platform"
set "PY=%ROOT%\..\..\.workbuddy\binaries\python\envs\testhub_backend\Scripts\python.exe"
set "TGROOT=%ROOT%\services\testgen"
set "TG_PY=%TGROOT%\.venv\Scripts\python.exe"

echo ============================================================
echo [1/2] 清理可能残留的端口占用（3900/8900/8910）...
echo ============================================================
for %%P in (3900 8900 8910) do (
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%%P "') do (
        echo   端口 %%P 占用 PID=%%A，尝试结束...
        taskkill /F /PID %%A >nul 2>&1
    )
)

echo ============================================================
echo [2/2] 启动三服务（各开独立窗口）...
echo ============================================================
start "testgen-sidecar" cmd /k "cd /d %TGROOT% && %TG_PY% -m cli.main serve --host 127.0.0.1 --port 8910"
start "django" cmd /k "cd /d %ROOT% && %PY% manage.py runserver 0.0.0.0:8900 --noreload"
start "vite" cmd /k "cd /d %ROOT%\frontend && npm run dev"

echo.
echo 启动完成。访问地址：
echo   主平台 Vite : http://127.0.0.1:3900/
echo   控制台代理   : http://127.0.0.1:8900/testgen/
echo   sidecar     : http://127.0.0.1:8910/
echo.
echo （关闭对应窗口即可停止该服务）
endlocal
