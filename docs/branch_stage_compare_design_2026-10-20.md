# 分支阶段对比分析 · 总体方案（A）

> 目标：当用户提供一个分支、且同一分支在不同时间段多次提交代码时，能够"定格"每个时间点的代码为**阶段快照**，并对比/验证同一分支在不同**阶段**的代码差异与分析结果。
> 本方案基于 `services/testgen` 引擎的真实代码现状设计，**以复用为主、新增为辅**。

---

## 0. 一句话结论

项目里 `engine/diff_tag`（已能做 `base..target` 真·版本 diff）与 `engine/remote_fetch`（已能按 `ref` 取文件）已具备"阶段间 diff + 历史物化"的底层能力；本次只需补 **3 个缺口**：阶段锚定记录、历史 commit 物化封装、两阶段产物对比编排，再接 CLI/API/UI 入口即可。

---

## 1. 现状盘点（已具备，直接复用）

| 现有模块 | 能力 | 本次复用点 |
|---|---|---|
| `engine/diff_tag.py` | `build_context(repo, base, target)` 支持 `base..target` 真·版本 diff，输出**变更文件集 + hunk 行区间**；`resolve_ref`/`refs_available` 做 ref 解析与防缩写 SHA 误判；`tag_of_rel`/`tag_of_symbol` 做全量/更新打标 | **L3 阶段间 diff 核心引擎已就绪** |
| `engine/remote_fetch.py` | `materialize_remote(code_source, repo_url, dest, ref=…, paths=…)` 已支持按 `ref` 取文件（Codeup OpenAPI 只读，不克隆） | **L2 历史 commit 物化可复用**（传历史 commit sha 作 ref） |
| `engine/stage_registry.py` | 17 阶段注册表（能力层/适配层），含 `tag`(=diff_tag)、`comparator`(语义比对) | 阶段契约登记事实源 |
| `cli/main.py` | `pull`(取码)/`analyze`(扫描+提取)/`pipeline`(全链路)/`compare`(语义比对)/`runs`/`report` 命令齐全 | **L4 入口骨架** |
| `engine/codeup_client.py` | `list_branches`/`list_commits` 可拉分支与提交（mock 测试已有） | 分支/提交发现 |

---

## 2. 缺口（本次要补）

- **G1 阶段锚定记录**：无"记录某分支某时间点分析基于哪个 commit + 产物落盘"的机制——分支是流动的，不能拿当前 HEAD 当唯一真相。
- **G2 历史 commit 物化封装**：`remote_fetch` 能按 ref 取，但缺"把某历史 commit 完整代码树物化到独立目录"的统一封装（本地 `git archive` + 远程 `ref`）。
- **G3 两阶段产物对比编排**：无"读两阶段分析产物 → 产出测试点/问题增减报告"的高层函数。
- **G4 入口**：缺 `stage` / `stage-compare` 命令、API 端点与前端视图。

---

## 3. 五层架构（标注 复用 / 新增）

### L1 阶段锚定（**新增** StageSnapshot / StageStore）
- `StageSnapshot`：`repo, branch, commit(锚点 sha), base_commit(增量基线,可为 None), mode(full|incremental), created_at, stage_id, artifact_dir, source(local|codeup)`。
- `StageStore`：管理 `analysis/<repo-slug>/stages.json` 清单（add / list_by_branch / get / latest / resolve）。同一分支的多条记录 = 不同阶段。

### L2 历史代码获取（**复用** remote_fetch + 本地 git archive）
- `materialize_commit(repo, commit, dest)`：本地 `git archive <sha> | tar -x`；远程 `remote_fetch.materialize_remote(ref=sha)`。**绝不 checkout 主工作区**。

### L3 阶段间 diff（**复用** diff_tag.build_context）
- 对比两阶段 commit：`build_context(repo, base=commitA, target=commitB)` → 变更文件集 + hunks，驱动增量打标（全量/更新）。

### L4 分析产物标准化（**复用** pipeline + 带 commit 锚点）
- 每阶段产物（测试点/问题/覆盖率 JSON）落 `artifact_dir`，文件名带 commit 短哈希 + **md5 指纹**（沿用统一合约 v1.0），保证可重复对比。

### L5 自动验证（**新增** pytest + Playwright）
- 增量合理性：增量测试点数量 ≈ diff 中变更符号数（合理比例）。
- 失效判定：阶段 B 中，阶段 A 存在但对应符号已被删除的测试点标"失效/移除"。
- 可重复：同 commit 跑两次产物 md5 一致。
- UI 层：Playwright e2e 覆盖 正常/异常/边界（见 C5）。

---

## 4. 复用映射表（落到具体函数）

| 五层 | 复用现有 | 新增 |
|---|---|---|
| L1 锚定 | — | `engine/stage_snapshot.py`：`StageSnapshot` / `StageStore` / `resolve_commit` |
| L2 历史物化 | `engine/remote_fetch.materialize_remote(ref=sha)`、`git archive` | `materialize_commit()` 封装（本地+远程分支） |
| L3 阶段 diff | `engine/diff_tag.build_context` / `compute_changed_files` / `tag_of_symbol` | 编排调用（在 `stage_compare` 内） |
| L4 产物 | `engine/pipeline.run_pipeline` 产物、`core` 指纹 | `save_artifact` / `load_artifact` |
| L5 验证 | `tests/test_codeup_branches.py`(mock 范式) | `test_stage_snapshot.py` / `test_stage_compare.py` / e2e |

---

## 5. 边界与陷阱

1. **rebase / force push**：commit 会失效 → 用 tag 或 `git merge-base` 兜底；manifest 存短 sha + 时间戳，远程优先用完整 commit id。
2. **大仓 full clone 慢** → 复用 shallow + 加深；远程走 `materialize_remote`（不克隆）。
3. **阶段跨度大**（跨很多 commit）→ 支持"阶段 = 某时间窗口内的提交集合"，而非单一 commit；对比以阶段锚点 commit 为准。
4. **产物必须含 commit 锚点**，否则无法回溯"这份分析基于哪次代码"。
5. **安全**：历史物化绝不改主工作区；远程只读；脱敏沿用 `comparator.redact`。

---

## 6. 落地形态（CLI / API / UI）

- CLI：`testgen stage --repo <path> --branch <b> [--commit <sha>]`（记录快照）、`testgen stage-compare --repo <p> --branch <b> --from <stageIdA> --to <stageIdB>`。
- API：`POST /api/v1/stage-analyze`、`POST /api/v1/stage-compare`。
- 前端：控制台新增"分支阶段对比"视图（见 C4）。

> 详细的数据模型、manifest 结构、接口草图与单测策略见 **`docs/branch_stage_manifest_design_2026-10-20.md`（B）**。实施严格按 A+B 进行（见任务 #118–#122）。
