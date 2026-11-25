"""B-5 批次 3：前端 describe 传参与后端命名空间契约的守护测试。

前端是原生静态页（service/web/index.html），沿用仓库既有的「静态守护」先例
（test_multirepo_stage6.py），锁住三件事：扫描记住本地目录身份、describe 请求
带上该身份、清缓存按同一身份精确清理；并用真实的 describe_namespace() 验证
前端传参能被后端正确归一，避免前后端字段漂移。
"""

from pathlib import Path

from core.describe_namespace import describe_namespace


_INDEX_HTML = Path(__file__).resolve().parents[1] / "service" / "web" / "index.html"
_SCAN_PY = Path(__file__).resolve().parents[1] / "service" / "routers" / "scan.py"


def _html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def test_scan_records_local_path_identity() -> None:
    html = _html()
    assert 'let lastLocalPath = ""' in html
    assert 'let lastScanNamespace = ""' in html
    assert 'lastLocalPath = payload.local_path || ""' in html
    assert 'lastRepoUrl = payload.repo_url || ""' in html
    assert 'lastScanNamespace = "";' in html  # 身份变更先清空
    assert 'lastScanNamespace = r.namespace || ""' in html  # 成功后再写回


def test_describe_request_carries_scan_identity() -> None:
    html = _html()
    assert "{items, repo_url: lastRepoUrl, local_path: lastLocalPath}" in html
    assert 'api("/api/v1/describe"' in html
    assert 'namespace: it.namespace || ""' in html  # P1：逐条透传后端算好的 namespace
    assert "descBody.namespace = lastScanNamespace" in html  # 请求级双保险
    # 旧写法（只传 repo_url、本地目录丢身份）不得回来
    assert "{items, repo_url: descRepoUrl}" not in html


def test_multi_repo_mode_passes_item_namespace() -> None:
    """P1：多仓 items 逐条带后端回传 namespace，不再靠「不带身份」禁用缓存。"""
    html = _html()
    assert 'namespace: it.namespace || ""' in html
    assert "const multiMode =" not in html
    assert "? {items}" not in html


def test_scan_payload_attaches_repo_key_for_multi_spec() -> None:
    """P1：多仓命中 spec 才带 repo_key/project_id；前端只匹配，不拼 namespace。"""
    html = _html()
    assert "function _matchMultiRepoSpec" in html
    assert "repo_key: spec.repo_key" in html
    assert "project_id: /^\\d+$/.test(pidRaw)" in html
    assert (
        '"multi:"'
        not in html.split("function _matchMultiRepoSpec")[1].split("function _scanRepoPayload")[0]
    )


def test_clear_cache_uses_precise_scope() -> None:
    html = _html()
    assert "{namespace: lastScanNamespace}" in html  # P1：优先按 scan 回传 namespace 精确清
    assert "{repo_url: lastRepoUrl}" in html
    assert "{local_path: lastLocalPath}" in html
    assert 'api("/api/v1/describe/cache/clear"' in html
    # 旧写法（{repo_url: scope} 空串清全部）不得回来
    assert "{repo_url: scope}" not in html


def test_backend_accepts_exact_fields_frontend_sends() -> None:
    scan_src = _SCAN_PY.read_text(encoding="utf-8")
    assert 'local_path: str = ""' in scan_src
    assert 'repo_url: str = ""' in scan_src
    assert 'namespace: str = Field("", description="条目自带命名空间' in scan_src


def test_local_path_payload_maps_to_local_namespace(tmp_path) -> None:
    """前端传 local_path + 空 repo_url → 后端得到 local:<规范路径>，不再共用 ''。"""
    ns = describe_namespace(repo_url="", local_path=str(tmp_path))
    assert ns is not None
    assert ns.startswith("local:")
    assert str(tmp_path).lower().replace("\\", "/") in ns.lower().replace("\\", "/")
    # 另一个本地目录必须隔离
    other = describe_namespace(repo_url="", local_path=str(tmp_path / "other"))
    assert other != ns


def test_remote_payload_keeps_historical_namespace() -> None:
    """远程仓沿用 repo_url 原值 → 历史缓存零失效。"""
    url = "https://codeup.aliyun.com/org/repo.git"
    assert describe_namespace(repo_url=url, local_path="") == url
