"""启动/停止本地开发栈（sidecar:8910 + Django:8900 + Celery + Vite:3900）。

为什么不用 cmd/bat：本机 PowerShell 的环境里同时存在 `HTTP_PROXY` 与 `http_proxy`
两套大写/小写重复键，`Start-Process` 重建环境字典时会抛「已添加项」；而会话结束时
宿主会按进程树回收 `Start-Process` / `start` 拉起的子进程，服务起来几秒就被清掉。

本脚本用 `DETACHED` + 独立隐藏控制台起子进程，并由 WMI（`Invoke-CimMethod
Win32_Process Create`）或计划任务调用，使服务父进程不在当前会话的作业对象里，
从而在会话结束、终端关闭后继续存活。

用法：
    python scripts/dev_stack.py start    # 后台拉起四件套，写 logs/*.log 与 logs/stack.pids
    python scripts/dev_stack.py stop     # 停 3900/8900/8910 监听进程与 celery worker
    python scripts/dev_stack.py status   # 打印端口监听状态

注意：MySQL80 / Memurai 属于 Windows 服务，请用
`Start-Service MySQL80; Start-Service Memurai` 管理，本脚本不处理。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
PY = ROOT / "venv" / "Scripts" / "python.exe"
SIDECAR_PY = ROOT / "services" / "testgen" / ".venv" / "Scripts" / "python.exe"
NODE = Path(r"C:\Users\EDY\.workbuddy\binaries\node\versions\22.22.2-6\node.exe")
MEMURAI = Path(r"C:\Program Files\Memurai\memurai.exe")

# tag, 可执行文件, 参数, 工作目录, 监听端口（已监听则跳过）
SERVICES = [
    # Memurai 以**不带 conf** 的方式起：`Program Files\Memurai\memurai.conf` 里有
    # `requirepass "1234"`，而本项目 .env 的 REDIS_URL 是
    # `redis://127.0.0.1:6379/0`（无密码）。走 Windows 服务（Memurai 服务）会带上
    # 密码，导致 celery 报 "Cannot connect ... Authentication required"。
    # 若 6379 已由服务占用则跳过本项。
    ("memurai", MEMURAI, [], ROOT, "6379"),
    (
        "sidecar",
        SIDECAR_PY,
        ["-m", "cli.main", "serve", "--host", "127.0.0.1", "--port", "8910"],
        ROOT / "services" / "testgen",
        "8910",
    ),
    ("django", PY, ["manage.py", "runserver", "0.0.0.0:8900", "--noreload"], ROOT, "8900"),
    (
        "celery",
        PY,
        ["-m", "celery", "-A", "backend", "worker", "-l", "info", "-P", "solo", "--concurrency=2"],
        ROOT,
        "",
    ),
    (
        "vite",
        NODE,
        ["node_modules\\vite\\bin\\vite.js", "--host", "0.0.0.0", "--port", "3900"],
        ROOT / "frontend",
        "3900",
    ),
]

APP_PORTS = ("3900", "8900", "8910")

# CREATE_NO_WINDOW：为控制台程序创建无窗口的独立控制台（不与调用方共享）
# CREATE_NEW_PROCESS_GROUP：脱离调用方控制台的 Ctrl 事件影响
# CREATE_BREAKAWAY_FROM_JOB：允许子进程脱离调用方的作业对象——否则宿主（编辑器/终端）
#   关闭会话时会把作业里的进程一起回收，服务起来几秒就消失
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _port_open(port: str) -> bool:
    """端口是否已在监听（用于「已由服务/旧进程占用则跳过」判断）。"""
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def start() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    pids: list[str] = []
    for tag, exe, args, cwd, port in SERVICES:
        if _port_open(port):
            print(f"[{tag}] port {port} already listening, skip")
            continue
        if not Path(exe).exists():
            print(f"[{tag}] 可执行文件不存在：{exe}", file=sys.stderr)
            return 1
        out = open(LOGS / f"{tag}.out.log", "ab", buffering=0)
        err = open(LOGS / f"{tag}.err.log", "ab", buffering=0)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        out.write(f"\n===== {stamp} start =====\n".encode())
        popen_kwargs = {
            "args": [str(exe), *args],
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": out,
            "stderr": err,
        }
        try:
            proc = subprocess.Popen(
                **popen_kwargs,
                creationflags=CREATE_NO_WINDOW
                | CREATE_NEW_PROCESS_GROUP
                | CREATE_BREAKAWAY_FROM_JOB,
            )
        except OSError:
            # 调用方作业对象不允许 breakaway（未设 BREAKAWAY_OK）时退化为普通创建
            proc = subprocess.Popen(
                **popen_kwargs,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            )
        print(f"[{tag}] pid={proc.pid}")
        pids.append(f"{tag}={proc.pid}")
        # 子进程已持有自己的句柄副本，父进程可安全关闭
        out.close()
        err.close()
        time.sleep(1.5)
    (LOGS / "stack.pids").write_text("\n".join(pids) + "\n", encoding="utf-8")
    return 0


def _run(args: list[str], timeout: int = 30) -> str:
    """执行外部命令并返回文本输出（netstat 等用 GBK 输出，按 GBK 解码）。"""
    try:
        raw = subprocess.run(args, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return raw.decode("gbk", "replace")


def _listening_pids(ports: tuple[str, ...]) -> set[str]:
    text = _run(["netstat", "-ano"])
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] in ports:
                found.add(parts[4])
    return found


def stop() -> int:
    for pid in _listening_pids(APP_PORTS):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", pid], capture_output=True, timeout=30
        )
        print(f"killed listener pid={pid}")
    # celery worker 不监听端口，按命令行匹配
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' AND "
            "CommandLine LIKE '%celery%'\" | ForEach-Object { Stop-Process -Id "
            "$_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
        timeout=60,
    )
    # Memurai：若以 Windows 服务运行则停服务，否则杀掉独立进程（本项目 6379 仅自用）
    subprocess.run(["powershell", "-NoProfile", "-Command", "Stop-Service Memurai -Force -ErrorAction SilentlyContinue"], capture_output=True, timeout=60)
    if _port_open("6379"):
        subprocess.run(["taskkill", "/F", "/IM", "memurai.exe"], capture_output=True, timeout=30)
        print("killed memurai.exe")
    print("done.")
    return 0


def status() -> int:
    text = _run(["netstat", "-ano"])
    for port in ("3306", "6379", "3900", "8900", "8910"):
        hit = [
            line.split()[4]
            for line in text.splitlines()
            if len(line.split()) >= 5
            and line.split()[0] == "TCP"
            and line.split()[3] == "LISTENING"
            and line.split()[1].rsplit(":", 1)[-1] == port
        ]
        print(f"{port} {'LISTEN pid=' + hit[0] if hit else '-'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="testhub dev stack launcher")
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    return {"start": start, "stop": stop, "status": status}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
