"""合并冲突标注（F5 增强）：代码通道 vs 地址通道同名功能点合并时，结构化记录冲突。

守护三件事：
1. **去重同时标注**：合并不再静默——每条被收敛的功能点都留下结构化冲突（胜出方 / 落败方 /
   来源维度 / 保留原因），供报告标注「冲突的测试用例」；
2. **维度判定正确**：url(运行时) > code(真实源码) > code(低可信桩) 的保留方向 + 原因文案；
3. **向后兼容**：`examples` / `summary_line` / replaced / deduped 计数不变，旧备注与门禁不受影响。
"""

from core.contracts import FunctionalPoint
from core.enums import FType
from engine import fp_merge, pipeline


def _fp(
    ftype: str, name: str, file_path: str, *, semantic: str = "", module: str = ""
) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=f"FP-{ftype}.{name}.{file_path}",
        ftype=ftype,
        file_path=file_path,
        name=name,
        title=name,
        semantic=semantic,
        module=module,
    )


# ============================================================================
# 1. 代码通道 vs 地址通道：同名 API → 冲突，地址(运行时)优先
# ============================================================================
def test_code_vs_url_api_conflict_url_wins() -> None:
    code = _fp(
        FType.API.value, "GET /pc/tasks", "backend/api.py", semantic="任务·列表", module="任务"
    )
    url = _fp(
        FType.API.value,
        "GET /pc/tasks",
        "runtime:http://srv/pc/tasks",
        semantic="任务·列表(线上)",
        module="任务",
    )
    merged, stats = fp_merge.merge_functional_points([code], [url])

    assert len(merged) == 1
    assert stats["added"] == 0  # url 与 code 同名 → 不是新增，是冲突覆盖
    assert stats["replaced"] == 1  # 运行时(url) 覆盖静态(code)
    assert len(stats["conflicts"]) == 1

    c = stats["conflicts"][0]
    assert c["semantic_key"] == "api|GET /pc/tasks"
    assert c["survivor_source"] == "url"
    assert c["dropped_source"] == "code"
    assert c["survivor"]["file_path"] == "runtime:http://srv/pc/tasks"
    assert c["dropped"]["file_path"] == "backend/api.py"
    assert c["reason"] == "运行时(地址通道)优先于静态源码"
    # 凭证红线：冲突记录里不能出现明文账号密码（运行时 file_path 是 runtime:<url> 脱敏形态）
    assert "password" not in str(c)


# ============================================================================
# 2. 代码通道内：真实源码 vs 低可信桩(mock) → 冲突，真实源码优先
# ============================================================================
def test_real_vs_mock_api_conflict_real_wins() -> None:
    mock = _fp(FType.API.value, "POST /login", "frontend/mock-server/server.py")
    real = _fp(FType.API.value, "POST /login", "backend/auth.py")
    merged, stats = fp_merge.merge_functional_points([mock], [real])  # 故意 mock 在前

    assert len(merged) == 1
    assert stats["replaced"] == 1  # real(incoming) 优先级更高，覆盖 mock
    assert stats["deduped"] == 0
    c = stats["conflicts"][0]
    assert c["survivor_source"] == "code"
    assert c["dropped_source"] == "code"
    assert c["reason"] == "真实源码优先于低可信桩(mock/stub/faker/demo/fixture)"


# ============================================================================
# 3. 同源同名平级 → 保留首次出现，原因文案正确
# ============================================================================
def test_same_source_equal_rank_keeps_first() -> None:
    first = _fp(FType.API.value, "GET /x", "a.py", semantic="一", module="M")
    second = _fp(FType.API.value, "GET /x", "b.py", semantic="二", module="M")
    merged, stats = fp_merge.merge_functional_points([first], [second])

    assert merged[0].file_path == "a.py"
    assert stats["conflicts"][0]["reason"] == "同源同名保留首次出现(平级)"


# ============================================================================
# 4. 跨族 / 非路径项不参与合并（不误删，也不产生冲突）
# ============================================================================
def test_component_not_merged_no_conflict() -> None:
    a = _fp(FType.COMPONENT.value, "SearchBar", "x/SearchBar.tsx")
    b = _fp(FType.COMPONENT.value, "SearchBar", "y/SearchBar.tsx")  # 跨目录同名
    merged, stats = fp_merge.merge_functional_points([a], [b])
    assert len(merged) == 2
    assert stats["conflicts"] == []


# ============================================================================
# 5. 向后兼容：examples / summary_line / 计数不变
# ============================================================================
def test_backward_compat_summary_line_and_examples() -> None:
    code = _fp(FType.API.value, "GET /pc/tasks", "backend/api.py")
    url = _fp(FType.API.value, "GET /pc/tasks", "runtime:http://srv/pc/tasks")
    _, stats = fp_merge.merge_functional_points([code], [url])

    assert len(stats["examples"]) == 1  # 仍有一条文本示例
    line = fp_merge.summary_line(stats, "功能点合并")
    assert line is not None
    assert "覆盖" in line


# ============================================================================
# 6. 流水线入口（run_pipeline 实际调用的合并函数）透出 conflicts
# ============================================================================
def test_pipeline_merge_helper_returns_conflicts() -> None:
    static = [
        _fp(FType.PAGE.value, "/pc/tasks", "pages/TasksPage.tsx"),
        _fp(FType.API.value, "GET /pc/tasks", "backend/api.py"),
    ]
    runtime = [
        _fp(FType.PAGE.value, "/pc/tasks", "runtime:http://srv/pc/tasks"),  # 同名页面
        _fp(FType.API.value, "GET /pc/tasks", "runtime:http://srv/pc/tasks"),  # 同名接口
    ]
    merged, stats = pipeline._merge_runtime_fps(static, runtime)
    assert len(merged) == 2  # 4 条收敛为 2 条（页面 + 接口各一）
    assert len(stats["conflicts"]) == 2
    assert all(c["survivor_source"] == "url" for c in stats["conflicts"])
