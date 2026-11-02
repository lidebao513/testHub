# 分支阶段对比 · 阶段锚定与 manifest 细化设计（B）

> 本文件是 A 方案中 **L1（阶段锚定）** 与 **L2（历史物化）** 的细化设计，是 C1 实现的唯一事实源。C2–C5 严格按 A+B 推进。

---

## 1. 数据模型 `StageSnapshot`（engine/stage_snapshot.py）

```python
@dataclass
class StageSnapshot:
    repo: str            # 仓库标识：本地绝对路径 或 codeup repo_url
    branch: str          # 分支名（如 master / oa-20260909）
    commit: str          # 锚点 commit（短 sha 或完整 40 位；远程优先完整 id）
    base_commit: str | None  # 增量模式基线 commit；全量模式 = None
    mode: str            # "full" | "incremental"
    created_at: str      # ISO-8601 时间戳（UTC）
    stage_id: str        # 唯一 ID：f"{branch}@{short_commit}_{seq}"
    artifact_dir: str    # 产物目录（相对 analysis_root）
    source: str          # "local" | "codeup"
    meta: dict           # 扩展信息（如提交信息、作者、远程 org/repo）
```

**不变式**：`stage_id` 在同一 `repo` 下唯一；`commit` 必须能解析（否则 `create_snapshot` 抛 `ValueError`，不静默降级——沿用 diff_tag 诚实约束）。

---

## 2. `StageStore`（manifest 清单）

- **存储位置**：`analysis/<repo-slug>/stages.json`，其中 `repo-slug` 由 `repo` 归一化（本地路径取末段目录名；url 取 `org/repo` 末段；非法字符 → 安全 slug）。
- **schema**：`{"schema_version": 1, "repo": "...", "stages": [ <StageSnapshot as dict>, ... ]}`。
- **操作**：
  - `add(snap)`：原子写（写 tmp → rename），拒绝重复 `stage_id`。
  - `list_by_branch(branch)`：返回该分支所有快照，按 `created_at` 升序。
  - `get(stage_id)` / `latest(branch)`：精确 / 最近。
  - `resolve(ref)`：`stage_id` 或 `commit` 模糊匹配（前缀/包含）到唯一快照，否则 `KeyError`。
  - `save_artifact(snap, name, data)` / `load_artifact(snap, name)`：JSON 读写，落 `artifact_dir/<name>.json`。
- **并发/稳健**：单进程写；manifest 损坏（JSON 解析失败）→ 备份损坏文件 + 重建空清单，不崩。

---

## 3. `resolve_commit`（ref 归一化）

- 本地：`diff_tag.resolve_ref(repo, ref)` 做短名→`origin/<ref>` 回退，再 `git rev-parse <ref>^{commit}` 取完整 40 位 sha。
- 远程（codeup）：commit id 直接可用；分支名经 `CodeupClient` 解析到 id（复用 `list_commits`）。
- **缩写 SHA 拒绝**：沿用 `diff_tag._FULL_SHA` 校验（4–39 位 hex 一律拒绝，防前缀模糊匹配出错）。

---

## 4. `materialize_commit`（历史物化，L2）

```python
def materialize_commit(repo, commit, dest_dir, *, source="local",
                       code_source=None, repo_url=None, materializer=None) -> list[str]:
    """把历史 commit 的完整代码树物化到 dest_dir，返回相对路径列表。"""
```

- **本地**：`git -C repo archive <commit> | tar -x -C dest_dir`（不 checkout 主工作区，零干扰）。
- **远程**：`remote_fetch.materialize_remote(code_source, repo_url, dest_root, ref=<commit>, paths=None)`（OpenAPI 只读，取完整树）。
- **可注入**：`materializer` 参数允许单测用 stub 替换，避免依赖真实 git/网络。
- **安全**：`dest_dir` 必须在 `analysis_root` 内；`repo_escape_blocked` 校验（沿用 diff_tag）。

---

## 5. CLI / API 接口草图

### CLI（`cli/main.py` 新增）
```
testgen stage --repo <path|url> --branch <b> [--commit <sha>] [--base <sha>]
    → 解析 commit → materialize → 记录 StageSnapshot → 打印 stage_id
testgen stage-compare --repo <p> --branch <b> --from <stageIdA> --to <stageIdB>
    → 载入两阶段产物 → 产出对比报告（测试点 新增/失效/保留；问题 新增/修复；diff 统计）
```

### API（`service/app.py` 新增）
- `POST /api/v1/stage-analyze`：`{repo, branch, commit?, base?}` → `{stage_id, commit, artifact_dir}`。
- `POST /api/v1/stage-compare`：`{repo, branch, from_stage, to_stage}` → `{added_tps, removed_tps, kept_tps, added_issues, fixed_issues, diff_stat}`。

---

## 6. 单测策略（C1 落实）

- **StageStore 纯 IO**：`tmp_path` 下测 add / list_by_branch / get / latest / resolve / save_artifact / load_artifact；并发原子写（tmp+rename）；损坏 manifest 恢复。
- **resolve_commit / materialize_commit**：pytest git repo fixture（`git init` + 两次 commit 得真实 sha）→ 测 archive 物化产出与 resolve 归一化；远程分支用 `monkeypatch` 替换 `remote_fetch.materialize_remote`。
- **边界**：分支无提交 / commit 不可解析（抛 `ValueError`）/ manifest 损坏恢复 / repo 逃逸阻断。

---

## 7. 与 A 的对应

| A 五层 | B 落地点 |
|---|---|
| L1 阶段锚定 | §1 StageSnapshot + §2 StageStore |
| L2 历史物化 | §3 resolve_commit + §4 materialize_commit |
| L3/L4/L5 | 见 `stage_compare.py`（C2）/ CLI+API（C3）/ UI+e2e（C4/C5） |
