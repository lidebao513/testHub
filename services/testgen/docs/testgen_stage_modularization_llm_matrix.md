# testgen 全阶段模块化与 LLM 接入矩阵

> 版本：v1.0 · 2026-09-17
> 范围：针对 testgen-service（`work/testgen-service/`）的 17 个子阶段，给出
> ① 大模型接入适配度矩阵 ② 两层模块化架构 ③ 跨项目复用方案
> ④ 对话模式测试可行性 ⑤ 复制项目安全演进策略 ⑥ 分阶段实施计划。
> 依据：已核对 `engine/pipeline.py` 的 `run_pipeline`（1282–1372 行）真实编排与 `service/app.py` 真实端点。

---

## 0. 可行性评估（先回答两个前置问题）

### 0.1 当前项目能否通过"对话模式"测试？
**结论：项目自身暂无原生对话入口，但已具备"被对话驱动"的全部基础，方案可行。**

| 项 | 现状（已核代码） |
|---|---|
| 原生对话/聊天端点 | **无**。`app.py` 仅 `POST /api/v1/generate`、`/execute`、`/verify/webhook`、`GET /api/v1/tasks/{id}` 等 REST + `cli.main` CLI |
| 已有 LLM 能力 | 有：`semantic_enrich`（语义增强）、`expert_review`/`PageExpert`（专家复审）、`llm_design`（用例设计），均走 `engine/expert/llm.py` 封装的 DashScope 客户端 |
| 已被对话驱动过 | 是：本会话即把"用 qwen3.8 重新生成 + 执行 + 验证"等自然语言意图转成 CLI/API 调用并跑通，证明"自然语言 → 阶段调用"链路成立 |
| 要做"原生对话测试" | 在现有 REST/CLI 之上加一层**薄对话代理（Dialogue Agent）**：接收 NL 意图 → 解析为阶段/端点调用 → 回传结构化结论。属"适配层新增"，不改动既有能力层 |

**判断**：对话模式不是"重写"，而是"加一个外壳"。它直接复用下文能力层的 11 个模块 + comparator，因此与模块化改造同构、可一并落地。

### 0.2 复制项目、保留旧项目应急、新项目做完善——是否可行？
**结论：完全可行，且应优先用 git 分支而非目录复制。**

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. git 分支（推荐）** | 保留 `main`（当前稳定 `85e20cf`）应急；新开 `dev/modularization` 做全面完善；验证通过后合并回 `main` | 历史连续、可回滚、八环门禁/pre-commit 天然覆盖，符合项目既有工程纪律 |
| B. 目录复制 | 复制 `work/testgen-service/` 为 `…-v2/` 独立工作区 | 完全隔离，但**丢失 git 历史/合并能力**，两棵树易漂移，不推荐 |
| C. 独立仓库 | 另建 repo 或 fork | 隔离最强，协作成本高，适合要长期分叉时 |

**当前 git 状态核查**：已在 `main`，最近提交 `85e20cf`（面板签名诚实 skip + LLM 超时放宽）；工作树仅含未跟踪的散脚本（`diagnose_*.py`/`run_*.py`）与 `docs/`，**已提交的核心 `engine/` `cli/` `service/` 干净稳定**——`main` 适合直接作为"别人应急使用"的基线。

> 分支落地命令（评审通过后执行）：
> ```bash
> git checkout -b dev/modularization      # 从稳定 main 拉新分支
> # 后续 comparator / 阶段契约 / 对话代理 都提交到此分支
> # 应急用户始终用 main：git checkout main
> # 完善并经八环门禁验证后：git checkout main && git merge dev/modularization
> ```

---

## 1. 17 子阶段 · 大模型接入适配度矩阵

> 适配度：✅强（语义理解核心价值）/ 🟡中（部分可增强）/ ❌否（机械/IO，不值得接 LLM）
> 当前状态：已接 / 未用 / 机械

| # | 子阶段 | 适配度 | 当前状态 | 理由与建议动作 |
|---|---|---|---|---|
| 6 | 功能点提取 `extract` | ✅强 | 未用（规则/AST） | 理解代码语义抽功能点，LLM 最擅长 → **接入 LLM 提取** |
| 9 | 测试点展开 `tp_expand` | ✅强 | 未用 | 把功能点设计成测试场景，核心价值点 → **接入 LLM 设计** |
| 13 | 用例生成 `case_gen` | ✅强 | 未用 | 功能点→具体用例，典型生成任务 → **接入 LLM 生成** |
| 12 | PRD 通道 `prd_ingest` | ✅强 | 默认关 | 需求文本解析成测试点，NLP 任务 → 开启并接 LLM |
| — | **comparator（新增）** | ✅强 | 设计中 | 实际 vs 预期 语义比对（用户明确要的模块）→ **LLM 比对 + 诚实降级** |
| 10 | 语义增强 `semantic_enrich` | ✅已用 | **已接 LLM** | 已落地，保留 |
| 11 | 专家复审 `expert_review` | ✅已用 | **已接 LLM** | PageExpert 反推菜单/补锚点，保留 |
| 14 | LLM 设计 `llm_design` | ✅已用 | **已接 LLM** | 已落地，保留 |
| 8 | 差异打标 `tag` | 🟡中 | 未用（规则 diff） | 语义化理解"改了什么/影响面"可增强 |
| 5 | 鉴权扫描 `auth_scan` | 🟡中 | 未用 | 语义识别鉴权接线，可增强 |
| 7 | 运行时 UI 发现 `runtime_ui` | 🟡部分 | 部分用 | 发现机械；S0 菜单反推已用 LLM |
| 15 | 用例执行 `execute` | 🟡事后 | 未用 | 执行本身不接 LLM；**之后接 comparator 做 LLM 比对** |
| 1 | 取码 `pull` | ❌ | — | git 操作，无语义价值 |
| 2 | 来源解析 `resolve_sources` | ❌ | — | 配置判定 |
| 3 | 项目注册 `register` | ❌ | — | DB upsert |
| 4 | 扫描 `scan` | ❌ | — | 文件索引（AST/正则） |
| 16 | 通道拆分 `partition` | ❌ | — | 路由分流逻辑 |
| 17 | 落库 `persist` | ❌ | — | DB 写入 |

**一句话**：强适配 7 项（extract / tp_expand / case_gen / prd_ingest / comparator + 已用 3 项）集中在"分析→生成"段；中适配 3 项（tag / auth_scan / runtime_ui 部分）；其余 7 项机械/IO 不接 LLM。

---

## 2. 两层模块化架构

> 关键约束：**其他项目不能直接调用绑定 testgen 存储的阶段**，否则会被 `projects/cases/runs` 表卡死。因此拆两层。

### 2.1 纯逻辑能力层（11 项，可独立复用）
`extract` · `tp_expand` · `case_gen` · `semantic_enrich` · `expert_review` · `llm_design` · `prd_ingest` · `comparator` · `runtime_ui` · `auth_scan` · `tag`

- 每项写成**无副作用纯函数 + `StageInput`/`StageOutput` 契约**，不碰 DB、不读项目状态。
- 可 `import` 或经 HTTP 直接调用，**跨项目零耦合**。

### 2.2 适配 / 编排层（6 项，绑定 testgen 存储与项目模型）
`pull` · `resolve_sources` · `register` · `scan` · `partition_channels` · `persist`

- 封装 DB / 项目状态 / 双通道拆分；**其他项目用需替换存储后端（依赖注入）**。
- 稳定契约，逻辑下沉到能力层，本层只做"接 testgen 的线"。

### 2.3 注册与编排
- 新增 `StageRegistry` 注册 17 个 stage；`pipeline` 改为"按 registry 组链"。
- 外部可跳过编排层单跑任意 stage（含跨项目调用能力层）。

---

## 3. 每阶段 StageInput / StageOutput 契约（简版）

> 统一字段：`project_id`、`source_kind`（code/url）、`trace_id`。下列为各阶段最小契约。

| 阶段 | StageInput | StageOutput |
|---|---|---|
| extract | `{files, language}` | `{functional_points[], merge_stats}` |
| tp_expand | `{functional_points, auth_profile, tag_by_fp}` | `{test_points[]}` |
| case_gen | `{test_points, auth_profile}` | `{cases[]}` |
| semantic_enrich | `{test_points, functional_points, llm_opts}` | `{test_points[]（富化）}` |
| expert_review | `{runtime_ui, test_points}` | `{test_points[]（增补）}` |
| llm_design | `{cases}` | `{cases[]（重设）}` |
| prd_ingest | `{prd_source}` | `{test_points[]（业务规则）}` |
| comparator | `{case（预期）, actual（执行结果）, model_opts}` | `{verdict, confidence, reason, diff[], rule_verdict}` |
| runtime_ui | `{base_url, login_opts}` | `{pages[], routes[], elements[], reachable}` |
| auth_scan | `{files}` | `{auth_profile}` |
| tag | `{functional_points, base, target}` | `{tag_by_fp}` |
| pull | `{repo_url, target_ref}` | `{local_path}` |
| resolve_sources | `{url, repo_url, local_path}` | `{has_code, runtime_enabled, runtime_options}` |
| register | `{name, source}` | `{project_id}` |
| scan | `{local_path}` | `{files{}}` |
| partition | `{result}` | `{code_channel, url_channel, summary}` |
| persist | `{result, channels}` | `{db_written, artifact_paths}` |

---

## 4. 跨项目调用方式

| 方式 | 形态 | 适用 |
|---|---|---|
| ① HTTP（推荐跨项目） | `POST /api/v1/<stage>`（如 `/api/v1/compare`、`/api/v1/extract`） | 零代码耦合，aiSitePilot / 督办系统 直接 REST 调用 |
| ② CLI | `testgen <stage> --project <pid> [--json-in --json-out]` | 本项目内联/脚本 |
| ③ import | `from engine.<stage> import run` | 同进程复用（需提供自身存储适配） |
| ④ 独立包（P2） | 抽 `engine/comparator` + 能力层为可 `pip install` 轻量包 | 跨项目复用且不依赖 testgen 部署 |

> 跨项目红线：能力层不读 testgen 的 `projects/cases/runs` 表；若需持久化，由调用方提供存储后端。

---

## 5. 对话模式测试（落地设计）

**定位**：适配层新增"对话代理"，不改动能力层。

```
用户自然语言 → Dialogue Agent（LLM 意图解析）
                    │  映射为阶段/端点
                    ├─ "测试这个URL"        → POST /api/v1/generate (+url)
                    ├─ "用 qwen3.8 重新生成" → 注入 EXPERT_MODEL 后 generate
                    ├─ "执行并对比结果"      → /api/v1/execute → POST /api/v1/compare
                    └─ "列出失败用例"        → GET /api/v1/tasks/{id}
                    ↓
              结构化结论回传用户
```

- 复用：意图解析可调 `engine/expert/llm`；执行/比对复用 comparator 与 executor。
- 诚实降级：LLM 解析失败→回退关键字路由；比对失败→回退规则判定（见 §8）。

---

## 6. 复制项目安全演进策略（落地设计）

**采用方案 A（git 分支）**：

1. `main` 冻结为应急基线（当前 `85e20cf`，核心代码干净）。
2. 新开 `dev/modularization` 分支承载全部完善：comparator、阶段契约、StageRegistry、对话代理。
3. 每步提交过八环门禁（secret/lint/format/enum/structure/type/security/test，cov≥60%）。
4. 验证通过后合并回 `main`；合并前应急用户始终用 `main`，不受扰动。
5. 散脚本（`diagnose_*.py`/`run_*.py`）与 `docs/` 视情况：设计文档提交入分支；临时诊断脚本按需清理或迁入 `scripts/`。

**Checkpoint**：合并前必须在 `dev` 分支跑通"生成→执行→comparator 比对→对话代理"端到端，并确认 `main` 仍可独立 `cli.main pipeline --url …` 跑通，才算安全。

---

## 7. 分阶段实施计划

| 阶段 | 内容 | 产出 | 门禁 |
|---|---|---|---|
| **P0** | 建 `engine/comparator/`（compare_one/compare_batch，复用 `expert/llm`，含降级）；加 `POST /api/v1/compare` + CLI `testgen compare` | comparator 模块 + 端点 + 单测（含降级分支） | 八环全绿 |
| **P0** | 能力层 11 项补 `StageInput/Output` 契约 + `StageRegistry` | 契约文件 + 注册表 | 八环全绿 |
| **P1** | 适配层 6 项改为经 registry 组链；`pipeline` 去硬编码编排 | 编排解耦 | 八环全绿 |
| **P1** | 对话代理（Dialogue Agent）外壳 | NL→阶段映射 + 端点 | 八环全绿 |
| **P2** | 能力层抽独立可 `pip install` 包；跨项目调用示例（aiSitePilot） | 包 + 示例 | 八环全绿 |
| **贯穿** | 分支策略执行：`dev/modularization` 开发，`main` 应急 | 双线并行 | 每步门禁 |

---

## 8. 安全红线（延续项目一贯风格）

- **凭证零落地**：password / otp / api_key 不落库、不入日志、不入产物；comparator 与 executor 同上（已验证 `runs.detail` 明文"密码与动态口令均不记录"）。
- **诚实降级**：LLM 超时/被墙/解析失败 → 回退规则判定并标 `verdict=inconclusive` / `skipped≠pass`，**绝不假装通过**。
- **面板签名不导航**：`area::` 前缀用例诚实 skip（已实现 `85e20cf`）。
- **main 保护**：应急基线不动；完善只在 `dev` 分支，经门禁+端到端验证才合并。
- **密钥不入仓**：`.env` 已被 `*.env` 规则忽略（`git check-ignore` 验证），`EXPERT_API_KEY` 不提交。

---

## 9. 风险与开放问题

| 风险 / 问题 | 说明 | 缓解 |
|---|---|---|
| LLM 一致性 | 同一用例多次比对结论可能漂移 | 固定 model+temperature；结论带 `confidence`；规则判定并存 |
| 成本 | 1856 用例全量 LLM 比对开销大 | 默认仅对 `fail`/`error` 用例启用 comparator；规则为主 |
| 跨项目模型配置 | 他项目需自带 `api_key/base_url/model` | comparator 接收 `model_opts` 覆盖，不强绑 testgen 配置 |
| 分支漂移 | dev 长期不合并致冲突 | 小步提交、频繁 rebase main |
| comparator 判定口径 | "通过"定义需与被测系统对齐 | 在契约中明确 `verdict` 枚举与阈值，由调用方配置 |

---

## 10. 结论

1. **对话模式可行**：项目无原生对话端点，但具备被对话驱动的全部基础；加一层薄对话代理即可，复用模块化阶段。
2. **复制项目可行且推荐 git 分支**：`main` 应急、`dev/modularization` 完善，符合既有八环门禁纪律。
3. **模块化可行**：17 阶段拆"能力层 11 + 适配层 6"，能力层可 import / HTTP / 独立包三种方式跨项目调用；适配层需替换存储后端。
4. **LLM 接入重点**：集中在"分析→生成"段（extract/tp_expand/case_gen/prd_ingest/comparator），其余机械阶段不接。
5. **下一步**：评审通过即从 P0 落地 comparator + 阶段契约 + 对话代理，并在 `dev` 分支端到端验证。
