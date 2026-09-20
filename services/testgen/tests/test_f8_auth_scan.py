"""F8 · 安全期望值来自代码事实（auth_scan）契约测试。

要守的核心：**安全用例的期望不能是写死的 401/403**。
- 代码里没有鉴权接线（公开接口）→ 写死 401/403 会产出**假失败**（把设计意图当缺陷）；
- 代码里有接线 → 期望与代码无关就等于**没验证接线**，只是把模板抄了一遍。

因此本模块从源码推 `AuthMode`（required / optional / absent），三处必须同口径：
测试点期望（`tp_expand`）→ 用例断言（`case_gen`）→ 执行器判定（`executor`）。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import AuthMode
from engine import auth_scan, case_gen, executor, tp_expand
from engine.scan import SourceFile


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def _fp(file_path: str, name: str = "GET /api/v1/orders") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-deadbeef",
        ftype="api",
        file_path=file_path,
        name=name,
        title="订单 · 查询订单",
        module="orders",
    )


# ============================================================================
# 扫描与模式判定
# ============================================================================
def test_no_wiring_is_absent():
    """一个只有路由、没有任何鉴权接线的文件 → 公开接口（absent）。"""
    files = {
        "app/api.py": _sf(
            "app/api.py",
            '"""订单接口。"""\n'
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            '\n@router.get("/orders")\n'
            "def list_orders():\n"
            '    """订单列表。"""\n'
            "    return []\n",
        )
    }
    profile = auth_scan.scan_auth(files)
    assert profile.mode == AuthMode.ABSENT.value
    assert profile.wired is False
    assert profile.mode_for("app/api.py") == AuthMode.ABSENT.value


def test_route_level_dependency_is_required():
    """只有**含接线**的文件才算 required——按文件判定，不是一刀切全仓。"""
    files = {
        "app/secure.py": _sf(
            "app/secure.py",
            "from fastapi import APIRouter, Depends\n"
            "router = APIRouter()\n"
            "\n"
            "\n@router.get('/orders', dependencies=[Depends(require_auth)])\n"
            "def list_orders():\n"
            "    return []\n",
        ),
        "app/open.py": _sf(
            "app/open.py",
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "\n@router.get('/health')\n"
            "def health():\n"
            "    return {}\n",
        ),
    }
    profile = auth_scan.scan_auth(files)
    assert profile.mode == AuthMode.REQUIRED.value
    assert profile.mode_for("app/secure.py") == AuthMode.REQUIRED.value
    assert profile.mode_for("app/open.py") == AuthMode.ABSENT.value


def test_current_user_dependency_and_bearer_are_wiring():
    files = {
        "app/api.py": _sf(
            "app/api.py",
            "from fastapi import Depends\n"
            "security = HTTPBearer()\n"
            "\n"
            "\ndef current_user(token=Depends(get_current_user)):\n"
            "    return token\n",
        )
    }
    profile = auth_scan.scan_auth(files)
    assert profile.wired is True
    assert profile.mode_for("app/api.py") == AuthMode.REQUIRED.value
    assert profile.signals  # 必须留下可复核的信号统计


def test_passthrough_placeholder_degrades_to_optional():
    """`if not <auth 配置>: return` = 未配置即放行 → optional（点名配置问题，不算代码缺陷）。"""
    files = {
        "app/api.py": _sf(
            "app/api.py",
            "from fastapi import Depends\n"
            "AUTH_TOKEN = ''\n"
            "bearer = HTTPBearer()\n"
            "\n"
            "\ndef require_auth():\n"
            "    if not AUTH_TOKEN:\n"
            "        return\n"
            "    raise PermissionError('denied')\n",
        )
    }
    profile = auth_scan.scan_auth(files)
    assert profile.passthrough is True
    assert profile.wired is True, "占位分支本身不算接线，必须同时有接线信号"
    assert profile.mode == AuthMode.OPTIONAL.value
    assert profile.mode_for("app/api.py") == AuthMode.OPTIONAL.value


def test_global_dependency_wires_every_route():
    files = {
        "app/main.py": _sf(
            "app/main.py",
            "from fastapi import FastAPI, Depends\n"
            "app = FastAPI(dependencies=[Depends(verify_token)])\n"
            "\n"
            "\napp.include_router(router, dependencies=[Depends(audit)])\n",
        )
    }
    profile = auth_scan.scan_auth(files)
    assert profile.global_auth is True
    # 全局接线 → 任何文件（含未单独接线的）都算「有接线」
    assert profile.mode_for("app/anything.py") == AuthMode.REQUIRED.value


def test_non_source_files_are_ignored():
    """HTML 模板里的 "Authorization" 属噪声，不得成为鉴权证据。"""
    files = {"templates/idx.html": _sf("templates/idx.html", "<p>Authorization: Bearer abc</p>")}
    profile = auth_scan.scan_auth(files)
    assert profile.mode == AuthMode.ABSENT.value
    assert profile.to_dict()["files_with_auth"] == []


def test_evidence_is_human_checkable():
    files = {
        "app/api.py": _sf(
            "app/api.py",
            "from fastapi import Depends\n"
            "\n"
            "\n@Depends(require_auth)\n"
            "def handler():\n"
            "    return None\n",
        )
    }
    profile = auth_scan.scan_auth(files)
    assert profile.evidence, "结论必须带证据，否则人工无法复核"
    assert ":" in profile.evidence[0], "证据形如 文件:行号"


def test_mode_of_fp_without_profile_keeps_v1_conservative_default():
    """没有画像（未提供代码）时退回 required —— 不得退化成「全部公开」。"""
    assert auth_scan.mode_of_fp(_fp("app/api.py"), None) == AuthMode.REQUIRED.value


def test_summary_line_is_self_explanatory():
    line = auth_scan.scan_auth({}).summary_line()
    assert "鉴权接线扫描（F8）" in line
    assert "模式=absent" in line


# ============================================================================
# 三处口径一致（测试点期望 / 用例断言 / 执行器判定）
# ============================================================================
def _profile(text: str) -> auth_scan.AuthProfile:
    return auth_scan.scan_auth({"app/api.py": _sf("app/api.py", text)})


def test_test_point_expectation_follows_auth_mode():
    fp = _fp("app/api.py")
    public = _profile("from fastapi import APIRouter\nrouter = APIRouter()\n")
    wired = _profile(
        "from fastapi import Depends\n@router.get('/x', dependencies=[Depends(require_auth)])\n"
    )
    absent_expect = tp_expand._expect_of(fp, "安全", "安全-鉴权缺失", public.mode_for("app/api.py"))
    wired_expect = tp_expand._expect_of(fp, "安全", "安全-鉴权缺失", wired.mode_for("app/api.py"))
    assert public.mode_for("app/api.py") == AuthMode.ABSENT.value
    assert wired.mode_for("app/api.py") == AuthMode.REQUIRED.value
    assert "公开接口" in absent_expect and "401/403" not in absent_expect
    assert "401/403" in wired_expect


def test_case_assertion_follows_auth_mode():
    absent = case_gen._security_assert(AuthMode.ABSENT.value)
    optional = case_gen._security_assert(AuthMode.OPTIONAL.value)
    required = case_gen._security_assert(AuthMode.REQUIRED.value)
    assert "公开接口" in absent
    assert "占位实现" in optional
    assert "401/403" in required


def test_executor_judges_public_api_as_reachable():
    """absent → 200 判通过（公开接口可访问），而不是「鉴权缺失失败」。"""
    passed, detail = executor._judge("安全", "安全-鉴权缺失", 200, AuthMode.ABSENT.value)
    assert passed is True
    assert "公开接口" in detail


def test_executor_judges_wired_api_as_must_reject():
    passed, detail = executor._judge("安全", "安全-鉴权缺失", 200, AuthMode.REQUIRED.value)
    assert passed is False
    assert "未被拒绝" in detail


def test_optional_mode_still_requires_rejection_but_flags_config():
    """optional 仍按「应被拒」判定——把「未配置即放行」判成通过会洗掉真实安全问题。"""
    passed, _ = executor._judge("安全", "安全-鉴权缺失", 200, AuthMode.OPTIONAL.value)
    assert passed is False
    notes = executor._auth_notes("安全", "安全-鉴权缺失", AuthMode.OPTIONAL.value, 200)
    assert any("占位实现" in n and "配置" in n for n in notes)


def test_absent_mode_note_names_the_evidence_source():
    notes = executor._auth_notes("安全", "安全-鉴权缺失", AuthMode.ABSENT.value, 200)
    assert any("auth_scan" in n for n in notes)


def test_build_case_without_profile_keeps_conservative_auth_mode():
    """未传画像（看不到代码）时，用例仍按 required 生成——**不得**退化成「全部公开」。"""
    case = case_gen.build_case(
        {
            "tp_id": "TP-00000001",
            "fp_contract_id": "FP-deadbeef",
            "category": "安全",
            "title": "[安全] 删除订单越权",
            "module": "orders",
            "source": "app/api.py",
            "method": "DELETE",
            "area": "/api/v1/orders/{id}",
            "dimension": "安全-越权",
            "expect": "e",
            "verify_layer": "接口",
        }
    )
    assert case is not None
    assert case.steps[0]["auth_mode"] == AuthMode.REQUIRED.value


def test_privilege_escalation_is_skipped_when_no_wiring_exists():
    """无鉴权接线 → 越权防护无从验证：如实 skipped 并说明缺什么，不产出假失败。"""
    case = case_gen.build_case(
        {
            "tp_id": "TP-00000001",
            "fp_contract_id": "FP-deadbeef",
            "category": "安全",
            "title": "[安全] 删除订单越权",
            "module": "orders",
            "source": "app/api.py",
            "method": "DELETE",
            "area": "/api/v1/orders/{id}",
            "dimension": "安全-越权",
            "expect": "e",
            "verify_layer": "接口",
        }
    )
    assert case is not None
    case.steps[0]["auth_mode"] = AuthMode.ABSENT.value
    # 用读操作（GET）避开「写操作默认不执行」守卫，才能命中「无鉴权接线 → 越权不可验证」分支
    case.steps[0]["method"] = "GET"
    result = executor.execute_case(case, executor.ExecutorOptions(base_url="http://api.local"))
    assert result.status == executor.ExecStatus.SKIPPED.value
    assert "auth_mode=absent" in result.notes[0]
    assert "auth_scan" in result.notes[0]
