"""pytest 公共配置：自举 sys.path + 隔离运行环境。

隔离要点：在**任何业务模块被导入之前**把数据/输出/工作区目录指向临时目录，
避免测试污染真实 `data/` 与 `outputs/`。因此这一段必须在模块顶层执行。
"""

import os
import sys
import tempfile


_SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)

_TMP = tempfile.mkdtemp(prefix="testgen-test-")
os.environ.setdefault("APP_ENV", "test")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["OUTPUT_DIR"] = os.path.join(_TMP, "outputs")
os.environ["WORKSPACE_ROOT"] = os.path.join(_TMP, "workspaces")
os.environ["DB_PATH"] = os.path.join(_TMP, "data", "testgen.db")
os.environ["LOG_FORMAT"] = "text"
os.environ.pop("AUTH_TOKEN", None)
os.environ["LLM_ENHANCE"] = "off"

import pytest

from core.config import get_settings, reset_settings
from core.db import connect, init_db


@pytest.fixture()
def fresh_db():
    """每个用例一套干净的库。"""
    reset_settings()
    get_settings().ensure_dirs()
    conn = connect()
    init_db(conn)
    # F12 新增表同样要清：否则不同用例的批次（如固定 batch_id）会互相串味
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM run_batches")
    conn.execute("DELETE FROM cases")
    conn.execute("DELETE FROM test_points")
    conn.execute("DELETE FROM functional_points")
    conn.execute("DELETE FROM projects")
    conn.commit()
    conn.close()
    yield
    reset_settings()


@pytest.fixture()
def sample_repo(tmp_path):
    """构造一个最小可分析仓库：1 个 FastAPI 路由 + 1 个业务函数 + 1 个前端路由。"""
    src = tmp_path / "sample_app"
    (src / "billing").mkdir(parents=True)
    (src / "billing" / "api.py").write_text(
        '"""账单接口。"""\n'
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/api/v1")\n'
        "\n"
        '\n@router.get("/invoices")\n'
        "def list_invoices():\n"
        '    """查询账单列表。"""\n'
        "    return []\n"
        "\n"
        '\n@router.get("/invoices/{invoice_id}")\n'
        "def get_invoice(invoice_id: str):\n"
        '    """查询单张账单。"""\n'
        "    return {}\n"
        "\n"
        '\n@router.post("/invoices")\n'
        "def create_invoice(payload: dict):\n"
        '    """创建账单。"""\n'
        "    return payload\n"
        "\n"
        '\n@router.delete("/invoices/{invoice_id}")\n'
        "def delete_invoice(invoice_id: str):\n"
        '    """删除账单。"""\n'
        "    return {}\n"
        "\n"
        "\ndef calculate_total(items):\n"
        '    """计算账单总额。"""\n'
        "    return sum(items)\n",
        encoding="utf-8",
    )
    (src / "routes.js").write_text(
        "export default [{ path: '/dashboard', name: 'Dashboard' }]\n", encoding="utf-8"
    )
    # React 页面层（JSX `<Route path="...">`）：用于守护「前端文件必须被扫描到」这条底线。
    # 曾因扫描器扩展名白名单缺 .tsx，真实仓库上 147 个 .tsx 整层不可见 → UI 层功能点恒为 0。
    (src / "web").mkdir()
    (src / "web" / "App.tsx").write_text(
        "import { Route, Routes } from 'react-router'\n"
        "\n"
        "export default function App() {\n"
        "  return (\n"
        "    <Routes>\n"
        '      <Route path="/pc/chat" element={<PcChatPage />} />\n'
        '      <Route path="/pc/tasks" element={<PcTasksPage />} />\n'
        "    </Routes>\n"
        "  )\n"
        "}\n",
        encoding="utf-8",
    )
    # 交互组件（含交互钩子）→ 应有 UI 层「交互元素可用」测试点
    (src / "web" / "SearchBar.tsx").write_text(
        "export default function SearchBar({ onSearch }: { onSearch: (q: string) => void }) {\n"
        "  return (\n"
        "    <form onSubmit={(e) => { e.preventDefault(); onSearch('') }}>\n"
        '      <input type="text" onChange={(e) => onSearch(e.target.value)} />\n'
        '      <button type="submit">搜索</button>\n'
        "    </form>\n"
        "  )\n"
        "}\n",
        encoding="utf-8",
    )
    return src


@pytest.fixture()
def git_repo(tmp_path):
    """构造一个**真实 git 仓库**（2 次提交），第二次提交改了后端代码与前端路由。

    增量通道必须在真仓库上验证：只喂 `DiffContext` 的手工构造测试无法暴露
    「ref 解析失败 → 静默降级为全量」这类缺陷。
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("环境未安装 git，跳过增量通道测试")

    repo = tmp_path / "git_app"
    (repo / "billing").mkdir(parents=True)
    api = repo / "billing" / "api.py"

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    if git("init", "-q").returncode != 0:
        pytest.skip("无法在当前环境 git init")

    # 提交 1：只有查询接口 + 一个此后不再改动的文件（用于验证『全量』标签）
    api.write_text(
        '"""账单接口。"""\n'
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/api/v1")\n'
        "\n"
        '\n@router.get("/invoices")\n'
        "def list_invoices():\n"
        '    """查询账单列表。"""\n'
        "    return []\n",
        encoding="utf-8",
    )
    (repo / "helpers.py").write_text(
        '"""通用工具。"""\n'
        "\n"
        "\ndef normalize_name(value):\n"
        '    """规范化名称。"""\n'
        "    return value.strip()\n",
        encoding="utf-8",
    )
    git("add", "-A")
    if git("commit", "-q", "-m", "c1").returncode != 0:
        pytest.skip("无法在当前环境 git commit")

    # 提交 2：新增写接口 + 前端路由（构成 base..target 的真实差异）
    api.write_text(
        '"""账单接口。"""\n'
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/api/v1")\n'
        "\n"
        '\n@router.get("/invoices")\n'
        "def list_invoices():\n"
        '    """查询账单列表。"""\n'
        "    return []\n"
        "\n"
        '\n@router.post("/invoices")\n'
        "def create_invoice(payload: dict):\n"
        '    """创建账单。"""\n'
        "    return payload\n",
        encoding="utf-8",
    )
    (repo / "routes.js").write_text(
        "export default [{ path: '/dashboard', name: 'Dashboard' }]\n", encoding="utf-8"
    )
    git("add", "-A")
    assert git("commit", "-q", "-m", "c2").returncode == 0

    return repo
