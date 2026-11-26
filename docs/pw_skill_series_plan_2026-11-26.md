# PW Skill 系列方案（PW-1 / PW-2 / PW-3）

> 文档日期：2026-11-26
> 来源：`docs/optimization_batches_2026-10-20.md`（Batch 2 = PW-1 + PW-2；PW-3 属 ④ 能力引入）、`docs/playwright_cli_skills_adoption_and_code_tradeoff_2026-10-20.md`（9 项能力权衡）、`docs/code_review_full_2026-10-26.md`（模块 C / 批次 B2）。
> 性质：**方案文档，本轮不落地任何代码或测试改动**。按项目硬规则，待逐项拍板后再分批实施。
> 范围：仅 `testhub_platform` 仓库内的 `apps/ui_automation`。`C:\Users\EDY\WorkBuddy\aiSitePilot` 是**另一个独立项目**，不纳入本次实施（详见 §1）。

---

## 0. 结论先行

| 项 | 原始定义 | 当前真实状态 | 本方案建议 |
|---|---|---|---|
| PW-1 Skill 规范层 | `SKILL.md` 式模板 + 约束清单（探页面→生成→自愈），Agent 每次加载 | **完全未实现**：提示词散落在 `ai_base.py` 代码内字符串，无外置规范层；预探索已做但非独立阶段；自动修复明确"不实现" | 做，但**只做规范外置 + 加载 + 三段纪律**；"自愈"按"失败诊断建议"表述，真正自动修复归 PW-6 另批 |
| PW-2 断言来源纪律 | 关键业务禁止 Agent 改断言；非关键改断言须记录来源与 diff | 三层断言引擎**已实现**（#181）；**来源字段 / 禁改校验 / diff 记录完全缺失**；闭环当前不改断言，但无护栏 | 做，以"只增字段不改语义"方式落地；先覆盖 ui_automation 闭环，testgen 侧另批评估 |
| PW-3 常驻 + 命名会话隔离 | 持久化会话 + 命名隔离，多角色并行、长流程保登录态 | **部分实现**：#180（本仓库，commit `b6fa244`）已做每任务临时 `user_data_dir` + 动态调试端口；`UI_ISOLATE_SESSIONS` 开关 / 命名会话 / `storage_state` 持久化 / 常驻复用**均未实现** | 拆两段：PW-3a 命名会话 + 登录态导入导出（低风险，建议先做）；PW-3b 常驻会话池（重做驱动层，高风险，**单独拍板**） |

**核心判断**：PW-1 与 PW-2 是零新架构、低成本高杠杆，可连续做；PW-3 的"并发不串味"目标已由 #180 达成，剩余价值在"命名复用登录态"，必须拆批并先定密钥卫生，否则 `storage_state`（含 Cookie）会成为新的敏感面。

---

## 1. 范围与边界

1. **仓库边界**：本方案只改 `C:\Users\EDY\WorkBuddy\testhub_platform`。aiSitePilot 独立项目里的 `storage_state` 登录态复用（`src/executor.py:49-66`）只作为**参考实现**，不直接复用其代码、不改其文件。
2. **既有能力不重做**：#177 预探索、#180 会话隔离、#181 三层断言、M10 提示词注入边界、#184 智能输入、闭环编排，均已落地。PW-1/2/3 只在外围加"规范层 / 来源护栏 / 命名会话"，不重写这些内核。
3. **能力引入与修复分离**：按总纲边界原则，PW 系列不因"顺手"改现有 API 契约；确需新增字段时只增不删、默认可空、旧消费者无感。
4. **PW-6（Plan→Generate→Heal 三段固化）不在本轮**：PW-1 的 Skill 文本可**描述**三段纪律，但"自动修复用例"本身仍需单独立项；本轮不得借 Skill 之名偷偷实现自愈。

---

## 2. 现状核对（决定改法的依据）

| 能力 | 已实现 | 缺口 | 证据 |
|---|---|---|---|
| AI prompt 拼装 | `run_task` 内 `final_task += ...` 逐段拼接；预探索、安全边界、规则区顺序已在 | 无外置规范文件；新增纪律只能继续堆代码字符串 | `apps/ui_automation/ai_base.py:2898-2964`，`build_pre_explore_prompt()` `:670`，`_security_boundary_prompt()` `:928` |
| 三段流程 | 探页面：`_run_pre_explore()` 只读快照；生成：`analyze_task` 拆解 + 桥接 | 自愈无；`ai_base.py:3117/3187` 明确"提示但不自动修复" | `ai_base.py:2670-2705`，`closed_loop.py:191,272-276`（仅 `error` 重试） |
| 三层断言 | L1 AI / L2 规则 / L3 会话 + 报告 + 渲染 + 落库 | 无来源字段、无关键业务标记、无改断言护栏 | `apps/ui_automation/assertions.py:340,357,120,265,428,458`；`models.py:1089 AIExecutionRecord.assertion_report`；`views.py:3423 apply_assertion_report` |
| 断言在闭环里的地位 | `ClosedLoopOrchestrator` 记录 `assertion_report` / `assertion_signal` 并参与 verdict | 闭环不修改断言（好），但**没有机制阻止未来的修复代码改断言** | `closed_loop.py:253-259`，`RETRYABLE_VERDICTS={"error"}` `:191` |
| 会话隔离 | 每任务临时 profile：`tempfile.mkdtemp(prefix='browser-use-user-data-dir-ui-')`；调试端口动态分配；僵尸清理按目录判断 | 无命名会话、无登录态复用、无隔离开关、无常驻上下文 | `ai_base.py:2543,2638-2659,2491-2541`；测试 `apps/ui_automation/tests/test_session_isolation.py` |
| 测试与门禁 | 根 `pytest.ini`：DJANGO_SETTINGS_MODULE=backend.test_settings，testpaths=apps,tests；ui_automation 测试 18 个文件 | 无 Skill 加载 / 断言来源 / 命名会话测试 | `venv\Scripts\python.exe -m pytest apps/ui_automation/tests/ -q`；testgen 八环门禁属 `services/testgen/.venv`，与根 venv 收集范围分离 |

> 注：`apps/ui_automation/tests/test_session_isolation.py` 文件头注释称 "user_data_dir 保持 None，由 browser-use 自动 mkdtemp"，与代码 `ai_base.py:2656` 显式 `mkdtemp` 不一致。**以代码为准**，实施时顺手修正注释（属文档性修正，不改行为）。

---

## 3. 分批详细方案

统一结构：**(a) 目标 / (b) 实施 / (c) 自测 / (d) e2e 验证要点 / (e) 风险与权衡**。

### 批次 1 — PW-1 Skill 规范层（建议先做）

**(a) 目标**：把"AI 怎么干 UI 测试"从散落的字符串变成**可版本化、可加载、可测试**的规范文件；Agent 每次执行前加载一次，三段纪律有单一来源。

**(b) 实施**

1. 新增规范文件目录与文件：
   - `apps/ui_automation/skills/UI_TEST_SKILL.md`（主规范，中文，带 `version` 头）。
   - 内容四段：**探页面纪律**（只读快照先行，禁止盲点）、**生成纪律**（动作与断言分离；浏览器原生参数；不猜凭据）、**失败诊断纪律**（记录失败证据，禁止擅自删/放宽断言——与 PW-2 呼应）、**会话纪律**（默认独立 profile，命名会话须显式声明——与 PW-3 呼应）。
   - 明确写出"三段"是 `观察 → 执行 → 核验`，并注明"自动修复用例（Heal）尚未实现，诊断仅产出建议"，避免规范承诺超出实际能力。
2. 新增加载器 `apps/ui_automation/skill_loader.py`（纯函数，无 Django 依赖）：
   - `load_skill(name, *, base_dir=None) -> SkillDocument`：`version` + `body` + `digest`，缺文件/空内容→返回 `None` 并记日志（**fail-open**，不阻断执行）。
   - `render_skill_block(doc) -> str`：固定包裹标记（如 `=== UI_TEST_SKILL v1 (sha256:xxxx) ===` ... `=== END ===`），便于日志核验与去重。
   - 长度上限（建议 body ≤ 4000 字符），超限截断并告警，防止 token 成本失控。
3. 接线：`ai_base.py:2898` 的 `final_task` 拼装链，在**预探索指令之后、规则区之前**插入 `render_skill_block(load_skill("UI_TEST_SKILL"))`（与现有顺序语义一致：先看页面 → 再读规范 → 再读硬规则）。
   - 幂等：若 `final_task` 已含 `=== UI_TEST_SKILL` 标记则跳过，防止重复注入。
   - **本批不删除现有硬编码规则**，Skill 只承载"纪律"不重复"具体规则"，避免双源冲突；收敛硬规则留待 PW-4（快照降 Token）另批。
4. 不改任何 API 契约、不改模型、不改 migration。

**(c) 自测**
- 新增 `apps/ui_automation/tests/test_skill_loader.py`：
  - 正常加载 → `version/body/digest` 正确；重复渲染不重复注入；
  - 文件缺失 / 空文件 / 超长 → `None` 或截断，且**不抛异常**；
  - 注入顺序断言：skill 块位置在预探索块之后、安全边界块之前。
- 更新 `apps/ui_automation/tests/test_run_task_prompt_wiring.py`：断言最终 prompt 含 skill 标记且仅一次。
- 全量：`venv\Scripts\python.exe -m pytest apps/ui_automation/tests/ -q`；ruff/format（门禁同款 ruff）。

**(d) e2e 验证要点**
- 起服务，真实跑一条 AICase 执行，抓执行日志：确认 skill 块存在、版本头正确、只出现一次、位于预探索之后；执行结果与未加载前一致（无行为回归）。
- 对照两次执行：一次正常加载、一次临时改名规范文件触发 fail-open，两次都应成功执行，后者日志有降级告警。

**(e) 风险与权衡**
- 风险：Skill 文本过长 → token 成本上升（用 4000 字符上限 + 只放纪律不放规则缓解）。
- 风险：规范与硬编码规则口径冲突（本批刻意不重叠，收敛另批）。
- 风险：fail-open 导致"规范没加载也不知道"（日志告警 + e2e 核验版本头）。

---

### 批次 2 — PW-2 断言来源纪律（PW-1 通过后做）

**(a) 目标**：断言**可溯源、不可被静默放宽**。关键业务（支付/订单/结算）禁止修复流程改断言；非关键业务改动必须记录来源与 diff。

**(b) 实施**

1. 数据层（**只增字段、不改语义**）：
   - `AssertionItem` 增可选 `source: str`（取值 `ai` / `rule` / `session`，与现有 `level` 对齐）与 `critical: bool = False`。
   - `AssertionReport.to_dict()` 增 `assertion_changes: list[dict]`，默认空数组；旧消费者忽略新键即可。
   - `AIExecutionRecord.assertion_report` 已是 JSONField，**无需 migration**。
2. 来源标注：
   - `generate_ai_assertions` / `parse_ai_assertions` 出来的条目 `source="ai"`；`extract_rule_assertions` → `source="rule"`；`evaluate_session_assertions` → `source="session"`。
   - testgen 侧 `engine/case_gen.py:289 design_assertions` 本轮**只做只读识别**（其 `TestPoint.origin` 已有 rule/semantic/impact 语义），不改其产出契约；是否给 step assertions 加来源标记另批拍板。
3. 关键业务识别（纯函数，可测）：
   - `assertion_guard.py` 中 `is_critical_business(text) -> bool`：默认关键词 `支付/付款/订单/结算/金额/交易/pay/order/checkout/payment`，大小写不敏感，命中任一即 critical；关键词通过常量暴露，便于后续配置化。
   - 调用点：`views.py` 生成断言前后、`closed_loop.py` 修复/重试前，把 `task_description` 与用例名拼成判定文本。
4. 改断言护栏：
   - `snapshot_assertions(items) -> dict`：按 `(level, type, value, task_id)` 生成有序指纹列表 + sha256。
   - `guard_assertion_change(before, after, *, critical) -> AssertionGuardResult`：
     - `critical=True` 且指纹变化 → 拒绝：`AssertionMutationError`，保留原断言，执行记录写 `blocked_assertion_change`；
     - `critical=False` 且指纹变化 → 放行，但把 `{field, before, after, source, reason}` 追加到 `assertion_changes`；
     - 无变化 → 直通。
   - 接线：`views.py:3423 apply_assertion_report` 之前做一次快照/比对；`closed_loop.py` 的 `error` 重试分支写回断言前也过护栏（当前闭环不改断言，属于**预防性护栏**，测试用模拟修复函数验证）。
   - 关键业务被拒绝时**不伪装通过**：verdict 仍按实际执行结果，仅断言保持原样并记录阻断事件。
5. 不改 `RETRYABLE_VERDICTS`、不改 verdict 合成逻辑、不改前端。

**(c) 自测**
- 新增 `apps/ui_automation/tests/test_assertion_guard.py`：
  - critical 关键词命中（中英文、大小写）；
  - critical 删除/放宽断言 → `AssertionMutationError`，原断言不变；
  - 非 critical 改断言 → 放行且 `assertion_changes` 有完整 diff；
  - 指纹顺序稳定、同内容不同顺序不误报；
  - `to_dict()` 向后兼容（旧键不变，新键默认空）。
- 更新 `apps/ui_automation/tests/test_assertions.py`：断言新 `source` 字段正确标注。
- 更新闭环测试：注入"模拟修复尝试删断言" → critical 被拦、非 critical 留痕。
- 全量回归 + ruff/format。

**(d) e2e 验证要点**
- 构造两条真实 AICase：一条任务描述含"支付订单"，一条普通查询；分别触发一次带断言改动的修复模拟。
- 核验：支付那条断言原文保留、执行记录出现 `blocked_assertion_change`；普通那条 `assertion_changes` 有 before/after 与来源；两条 verdict 均按真实执行结果计算（无假通过）。

**(e) 风险与权衡**
- 风险：关键词误判导致过度保护（只对关键业务 fail-closed，误判代价是拒绝改断言而非阻塞测试；关键词表可迭代）。
- 风险：`source`/`critical` 新字段影响旧消费者（只增可空字段 + 默认空 changes，前端无需改）。
- 风险：护栏接在多个写回点可能漏接（本批只接 `apply_assertion_report` 与闭环写回两处，并在测试里列出未接点清单）。
- 权衡：本批**不实现自动修复**，护栏是为未来 Heal 能力提前上锁，符合"先锁断言来源纪律"的原始判断。

---

### 批次 3 — PW-3a 命名会话 + 登录态导入导出（建议做；PW-3b 常驻池另议）

**(a) 目标**：在**不破坏 #180 每任务隔离**的前提下，支持显式命名会话（如 `admin` / `user`）复用登录态，满足"多角色并行、长流程不重复登录"。

**(b) 实施**

1. 配置：`UI_SESSION_ALIAS_ENABLED`（默认 `False`，显式开启才生效）、`UI_SESSION_STATE_DIR`（默认空，必须显式指定目录）；**保留 #180 的临时 profile 行为为默认**。
   - 不引入 `UI_ISOLATE_SESSIONS` 开关来关闭隔离：关闭隔离会让"串味"重新出现，与 #180 目标冲突；如需关闭须单独拍板。
2. 新增 `apps/ui_automation/session_registry.py`：
   - `SessionRegistry(state_dir)`：`save(alias, storage_state) -> path`、`load(alias) -> dict | None`、`clear(alias)`；
   - alias 白名单校验：`^[A-Za-z0-9_-]{1,64}$`，拒绝路径穿越；文件权限 `0600`（Windows 用 ACL 收紧或至少非共享目录）；
   - 存储内容**不入库、不进日志**；日志只记 alias 与文件是否存在。
3. 驱动层接线：`ai_base.py:2543 _create_browser_profile` 与 `run_task` 会话创建处：
   - 显式传 `session_alias` 且开关开启 → 从 registry 载入 `storage_state` 注入 `BrowserProfile`（沿用 browser-use 支持字段）；执行成功后按 `persist_session_state` 策略回写；
   - 未传 alias / 开关关闭 → 完全走现有 #180 临时 profile 分支，**行为零变化**。
4. 参数来源：`run_adhoc` / AICase 执行请求新增可选 `session_alias`、`persist_session_state`（默认 `False`）；不新增必填字段，不改现有响应体。
5. 密钥卫生（与 PW-7 关联）：
   - 扩展 secret 扫描规则，覆盖 `storage_state*.json` / `*session-state*` 与 Cookie 字段；
   - `.gitignore` 排除会话状态目录的默认命名；不把真实 Cookie 写入测试夹具（测试用假数据）。

**(c) 自测**
- 新增 `apps/ui_automation/tests/test_session_registry.py`：alias 校验/穿越拒绝、save→load 往返、缺失返回 None、clear 幂等、文件权限、日志不含 Cookie。
- 扩展 `apps/ui_automation/tests/test_session_isolation.py`：
  - 未开启别名 → 仍每任务独立 profile（回归 #180）；
  - 两个不同 alias 并发 → 各自 storage_state 不串；
  - 同一 alias 两次执行 → 第二次复用登录态；
  - 损坏/过期 state → 降级为全新会话且不抛异常。
- 全量回归 + ruff/format + secret 扫描。

**(d) e2e 验证要点**
- 真机：先用 `admin` 别名登录一次并持久化，再跑第二条任务确认免登录；用 `user` 别名并发跑，确认两者登录态与页面状态不交叉；关闭开关再跑一次，确认回退到临时 profile 且需重新登录。
- 抓包/日志核验：Cookie 不出现在执行日志、不落 Django 库。

**(e) 风险与权衡**
- 风险：`storage_state` 含会话 Cookie，等同凭据 → 默认关闭 + 权限收紧 + secret 扫描 + 不入库（**本批最大风险点，必须先过密钥卫生再接线**）。
- 风险：与 #180 隔离语义冲突 → 只在显式 alias 时复用，默认保持不变。
- 风险：browser-use 版本对 `storage_state` 支持面需实测确认 → 实施时先做一次探针，不支持则降级为"仅 profile 目录复用"并如实记录，不硬上。
- **PW-3b（常驻会话池 / attach 调试）不在本批**：涉及浏览器进程常驻、CDP attach、资源回收与状态污染，属"重做驱动层"，收益中高风险高；建议 PW-3a 真机验收通过后再单独拍板。

---

## 4. 批次顺序与依赖

| 顺序 | 批次 | 依赖 | 是否改契约 | 是否需 migration | 建议 |
|---|---|---|---|---|---|
| 1 | PW-1 Skill 规范层 | 无 | 否 | 否 | 先做 |
| 2 | PW-2 断言来源纪律 | PW-1（纪律文本先行） | 只增可选字段 | 否 | 接着做 |
| 3 | PW-3a 命名会话 + 登录态 | PW-2（密钥卫生规则） | 只增可选参数 | 否 | 拍板后做 |
| 4 | PW-3b 常驻会话池 / attach | PW-3a 真机通过 | 待定 | 待定 | **单独拍板**，不在本系列默认范围 |

每批按固定收尾：**自测（针对性 + 全量回归）→ 列后续任务表 → feature 分支 → commit（`feat:`）→ PR（标题 `<type>: <描述>`，无 scope）→ CI 5/5 → 合并 → main ff-only 同步**。八环门禁属 testgen venv，本系列主改 Django ui_automation；若某批触及 `services/testgen`（如 PW-2 的 testgen 断言来源），需额外补跑八环门禁。

---

## 5. 待你拍板的决策点

| # | 决策 | 建议 | 影响 |
|---|---|---|---|
| D1 | aiSitePilot 独立项目是否纳入 PW-1 规范层？ | **不纳入**，本轮只做本仓库 ui_automation；aiSitePilot 如需同款规范另开任务 | 决定改动范围与 PR 体积 |
| D2 | PW-1 是否只做"规范外置 + 加载 + 三段纪律"，不实现自愈？ | **是**，自愈归 PW-6 另批 | 避免规范承诺超出实际能力 |
| D3 | PW-2 关键业务口径：默认关键词表 + 只增字段？ | **是**，关键词表可迭代；testgen 侧断言来源另批 | 决定护栏覆盖面与是否碰 testgen 契约 |
| D4 | PW-3 是否拆 3a/3b，先做命名会话且默认关闭？ | **是**，3a 先做、默认关闭，3b 单独拍板 | 控制会话凭据风险与驱动层改动面 |
| D5 | PW-3a 的 `storage_state` 是否允许持久化到磁盘？ | **允许但默认关闭**，必须先完成密钥卫生与权限收紧 | 不批准则 PW-3a 退化为"仅命名、不复用登录态" |

---

## 6. 验收标准（整体系列）

1. PW-1：规范文件有版本头与 sha256；执行日志可见且仅一次；缺文件 fail-open 不影响执行。
2. PW-2：关键业务改断言被拒绝且原断言不变；非关键改动有完整来源与 diff；verdict 不被伪装成通过；旧报告结构兼容。
3. PW-3a：默认行为与 #180 一致；显式别名可复用登录态、不同别名并发不串；Cookie 不落库不进日志；secret 扫描覆盖新增敏感面。
4. 全量：`venv\Scripts\python.exe -m pytest apps/ui_automation/tests/ -q` 全绿；ruff/format 通过；涉及 testgen 时八环门禁 EXIT=0；CI 5/5。
5. 真机：至少一次完整 AICase 执行 + 一次闭环 + 一次别名会话复用，均留日志证据。

---

## 7. 后续任务表（本轮为方案，全部未开始）

| 优先级 | 任务 | 状态 | 未做项 |
|---|---|---|---|
| P1 | PW-1 Skill 规范层（文件 + 加载器 + prompt 接线 + 测试） | 未开始 | 全部，待 D1/D2 拍板 |
| P1 | PW-2 断言来源纪律（字段 + 关键业务识别 + 护栏 + 测试） | 未开始 | 全部，待 D3 拍板 |
| P2 | PW-3a 命名会话 + 登录态导入导出（含密钥卫生） | 未开始 | 全部，待 D4/D5 拍板 |
| P3 | PW-3b 常驻会话池 / attach 调试会话 | 未开始 | 不在本系列默认范围，待 PW-3a 验收后另拍 |
| P3 | PW-4 快照降 Token / PW-5 生成代码评审门禁 / PW-6 三段固化 / PW-7 密钥卫生扩展 / PW-8 attach / PW-9 CLI 备选 | 未开始 | 本次不动；PW-7 的会话状态扫描随 PW-3a 顺带做 |
| 阻塞 | #438 `.env.bak` 历史重写 | pending（需重新授权） | 与 PW 系列无关，保持暂缓 |

---

以上为方案，待你确认后我再逐批实施（含代码与自测）。
