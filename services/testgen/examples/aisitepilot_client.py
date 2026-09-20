"""aiSitePilot → testgen 跨项目能力调用示例（P2-2）。

演示 aiSitePilot 如何**不引入 testgen 存储**，仅通过 HTTP 复用 testgen 的两项纯能力：

    1) POST /api/v1/analyze   —— 扫描 + 功能点抽取（对应任务清单里的 /api/v1/extract）
    2) POST /api/v1/compare   —— 语义比对（规则为主 · LLM 增强 · 诚实降级）

鉴权：所有端点要求头 ``X-Auth-Token``（testgen 配置 AUTH_TOKEN 时才校验；未配置则放行）。

依赖：仅标准库（urllib / json），便于在 aiSitePilot 直接落地，无需额外安装。

用法：
    python examples/aisitepilot_client.py \
        --base-url http://127.0.0.1:8000 \
        --auth-token **** \
        --analyze C:/path/to/project
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# 维度等取值应使用枚举而非裸字符串（通过 core.enums 复用 testgen 真值源）。
# 跨项目落地时，可改为你方自己的枚举或常量，保持与 testgen 约定一致即可。
from core.enums import TPType


@dataclass
class TestgenClient:
    """最小化的 testgen HTTP 客户端（无存储、无状态）。"""

    base_url: str
    auth_token: str = ""

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"X-Auth-Token": self.auth_token} if self.auth_token else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"{path} -> HTTP {e.code}: {body[:500]}") from e

    def analyze(
        self,
        local_path: str,
        project_name: str = "",
        include_business: bool = True,
        extract_pages: bool = True,
    ) -> dict[str, Any]:
        """功能点抽取（等价于任务清单里的 /api/v1/extract）。

        入参只含「代码路径」，**不含任何凭证**；返回功能点计数与样例。
        """
        return self._post(
            "/api/v1/analyze",
            {
                "local_path": local_path,
                "project_name": project_name,
                "include_business": include_business,
                "extract_pages": extract_pages,
            },
        )

    def compare(
        self,
        case: dict[str, Any],
        actual: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """语义比对：给定「预期(case) + 实际(actual)」返回比对结论。

        verdict ∈ {pass, fail, partial, inconclusive}；未配置 LLM 时仅规则判定，
        诚实降级 ``inconclusive``，绝不假装通过。
        """
        return self._post(
            "/api/v1/compare",
            {"case": case, "actual": actual, "context": context or {}},
        )


def _demo(base_url: str, auth_token: str, local_path: str) -> int:
    client = TestgenClient(base_url=base_url, auth_token=auth_token)

    print("== 1) 抽取功能点 (POST /api/v1/analyze) ==")
    ana = client.analyze(local_path, project_name="aisitepilot-demo")
    print(f"   文件数: {ana.get('files')}  功能点计数: {ana.get('counts')}")
    print(f"   语义合并去重: {ana.get('fp_semantic_merge', {}).get('deduped')}")
    fps = ana.get("functional_points") or []
    print(f"   样例功能点(前 3): {[fp.get('name') for fp in fps[:3]]}")

    print("\n== 2) 语义比对 (POST /api/v1/compare) ==")
    case = {
        "id": "LOGIN-01",
        "title": "登录成功跳转首页",
        "steps": [{"expect": "提交正确账号密码后跳转首页", "dimension": TPType.NORMAL.value}],
    }
    actual = {"status": "PASS", "notes": ["跳转成功"]}
    verdict = client.compare(case, actual)
    print("   单条比对结论:", json.dumps(verdict, ensure_ascii=False))

    # 批量比对示例
    batch = client.compare(
        case={},
        actual={},
        context={},
    )
    # 上面的单条已覆盖；批量用 items 字段：
    batch = client._post(
        "/api/v1/compare",
        {
            "items": [
                {"case": case, "actual": actual},
                {"case": case, "actual": {"status": "FAIL", "notes": ["停留在登录页"]}},
            ]
        },
    )
    print("   批量比对条数:", batch.get("count"))
    for v in batch.get("verdicts", []):
        print(f"     - {v.get('verdict')} (conf={v.get('confidence')}) : {v.get('reason')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="aiSitePilot 调用 testgen 能力示例")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--auth-token", default="")
    ap.add_argument("--analyze", default="", help="要抽取功能点的本地代码路径")
    args = ap.parse_args(argv)

    if not args.analyze:
        print("提示: 传 --analyze <本地代码路径> 可跑完整演示；否则仅打印客户端已就绪。")
        print(f"client = TestgenClient(base_url={args.base_url!r}, auth_token=***)")
        return 0
    try:
        return _demo(args.base_url, args.auth_token, args.analyze)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
