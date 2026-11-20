# testgen 残留问题与隐含风险盘点（2026-11-19）

> 口径：只记录**能从代码逐行证实**的问题。已闭环项（卡片导航 auto 开态 E2E、批次 A/B/C/D、T5/T6、#305/#306）不再列入。
> 每条给出 `文件:行号` 证据 + 触发条件 + 影响 + 建议解法。本轮**只盘点、不实施**。

## 结论先行

- **P0（1 条）**：限流探针在 `allow_write=False`（默认）下仍对写接口发 8 次真实写请求，直接违反"写操作默认不执行"的安全红线。**默认配置即可触发**。
- **P1（7 条）**：异常类判定接受 2xx（系统性假阴性）、危险菜单未过滤（点"退出登录"腰斩会话）、页面级只断言前 20 元素、控制台基线口径错配 + 白屏阈值过低、4066 条串行无预算且单 page 复用、布尔配置非法值静默回落、两处测试盲区。
- **P2（12 条）**：click-through 点写动词按钮无熔断、卡片危险词表不全 + 事后熔断、前端 `path:` 泛提、鉴权模式误判、质量门禁证据形同虚设、选择器哈希 class 假 FAIL、store 对账三处不一致、配置漏校验、`shell=True` OTP、repo_facts O(n²)+3000 文件静默截断、`count()` 无等待慢页面假 FAIL、跨项目 run_batches 改挂。
- **P3（2 条）**：配置摘要缺 mobile 开关、`MAX_ELEMENTS_PER_PAGE` 重复定义（200/150）。

---

## A. 已核实的硬缺陷

### A1【P0】限流探针绕过写保护，默认发 8 次真实写请求
- 证据：`executor.py:454` `_special_probe` 在 `:457` `_not_executable` **之前**调用；`:502-542` `_probe_rate_limit` 内部**没有** `_not_executable`，直接按用例 `method` 循环 `_RATE_LIMIT_BURST=8`（`:68`）次 `_send`；`tp_expand.py:661` 把 RATE_LIMIT 附加到**所有** API 功能点（含写接口）；`_send` 对写方法带 `json={}`（`:266-267`）。
- 触发：默认 `ExecutorOptions.allow_write=False`（`:95`），普通写用例被 `:373` 诚实跳过，但其限流转 adjudication 用例却真发 8 次 POST/PUT/DELETE。
- 影响：污染被测环境数据，违背模块开头"写操作默认不执行"承诺（`executor.py:21`）。
- 测试盲区：`test_executor_g3g5.py:119,143-165` 限流用例默认 `method="GET"`，从未覆盖 POST。
- 解法：`_probe_rate_limit` 入口加 `_not_executable` 同款写方法判定；或让 `_special_probe` 在写维度上返回"诚实跳过"，与常规写用例口径统一。补 POST + `allow_write=False` 单测。

### A2【P1】异常类判定接受 2xx，与"期望 4xx"直接矛盾
- 证据：`executor.py:163` `_KIND_ABNORMAL: lambda s: 200 <= s < 500`；`:174` 文案却是"异常路径返回 4xx"；`tp_expand.py:535` 期望写"返回 4xx（404/409）"；`case_gen.py:274-276` 同。
- 影响：本应 404 的"资源不存在"接口错误返回 200 时判 PASS——系统性假阴性。
- 注意：需先确认设计意图（部分系统对异常输入返回 200 + 业务错误码属正常），但**至少判定谓词、成功文案、用例期望三者必须统一**。
- 解法：确定口径后收敛 `_PASS_PREDICATES[_KIND_ABNORMAL]` 与 `_VERDICT_DESC`/`_abnormal_expect`；若需兼容"200+业务错误码"，应检查响应体而非只看 status。

### A3【P1】危险菜单过滤函数存在但发现路径从未调用
- 证据：`_is_danger_label`（`runtime_ui.py:973`）仅在 `:1714`/`:1785`（面板 replay/absorb）被调用；真正的菜单发现 `_probe_menu_routes`（`:1129-1169`）遍历 `probe.labels[:limit]` 直接 `_click_label`，`_discover_menu_routes`（`:1172-1206`）汇入 `extra_labels` 也**未过滤**；`_click_label`（`:1090-1126`）自身无危险判断。
- 影响：点击"退出登录/注销/Logout"把会话踢下线，后续页面全抓成登录页 → 功能点被登录页元素污染、路由发现静默腰斩。
- 测试盲区：`test_runtime_ui_nav.py:22-34` 只把 `_is_danger_label` 当纯函数测，不验证发现路径真的调用。
- 解法：在 `_discover_menu_routes` 合并 labels 后统一过滤 `_is_danger_label`，并补"发现路径不含危险项"的集成单测。

### A4【P1】页面级用例只断言前 20 个元素
- 证据：`case_gen.py:46` `MAX_RUNTIME_ELEMENTS_IN_STEP=20`；`:309` `design_assertions` 只对 `visible_elements()[:20]` 生成 `assert_element_visible`。
- 影响：页面级用例目标元素排在发现顺序第 21 位以后，删除该元素仍全 PASS（元素级用例靠 `element_hint_from_path` 补单元素断言，不受影响）。
- 解法：页面级断言应对**该用例声明的目标元素**定点生成，而非按发现顺序截断；或对截断外的元素在 notes 里标注"未纳入断言"，避免假通过被误读为全覆盖。

### A5【P1】控制台基线口径错配 + 白屏阈值过低
- 证据：`case_gen.py:305-307` `baseline = len(info.console_errors)` 是**发现阶段整页错误数**；`ui_executor.py:499,505` 执行时取 `console_errors[mark:]`（本用例期间**新增**错误）与基线比，`:296-302` 判 `count > baseline+slack`；`ui_executor.py:55` `MIN_VISIBLE_TEXT=1`，`:277-278` 正文长度 ≥1 即"已渲染"。
- 影响：① 本仓基线非空（`:3900` vue-i18n 3 条），基线≥3 意味着允许新增 3+slack 条错误才判失败，放过真实回归；② 页面只剩"Error"/"Loading…"任意一字符即判非白屏。
- 解法：基线应取"发现阶段同类型错误的**去重样本数**"或干脆改为 0 + 明确白名单；`MIN_VISIBLE_TEXT` 提到有语义的阈值（如 ≥10）并排除纯错误/加载文案。

### A6【P1】4066 条串行、无总预算、单 page 复用无状态隔离
- 证据：`executor.py:661-687` `execute_all` 单会话、单 UI page、全量串行（`:681` 列表推导）；`ui_executor.py:50` 每条固定 `SETTLE_AFTER_GOTO_MS=1200`；`config.py:228` 单步默认 30s；`ui_executor.py:394` `console_errors` 跨用例累积。
- 影响：4066 条仅固定等待就 ≈81 分钟，超时路径最坏数十小时，无 deadline/分批；`ui_click=on` 时前一条的 localStorage/DOM 变更污染后一条 → 顺序相关的随机假结果。对比 `/describe` 有 `describe_budget_seconds`，执行链路无等价预算。
- 解法：给执行链加总时长预算 + 分批 checkpoint；每条 UI 用例前重置 page context（或至少清 localStorage/console 缓冲）。

### A7【P1】布尔配置非法值静默回落，违背"启动期快速失败"
- 证据：`config.py:1-6` 模块 docstring 承诺"非法值启动期抛 ConfigError"；但 `config.py:46-54` `_as_bool` 对无法识别的值（如 `ture`/`enabled`/`2`）直接 `return default`。所有 `RUNTIME_*`/`EXECUTOR_*`/`QUALITY_GATE` 开关都走它。
- 影响：一个拼写错误让 `RUNTIME_UI_ENABLED` 静默变 False → 整条链路不跑却无报错，"配了以为生效"。
- 解法：`_as_bool` 对非空且非白名单值抛 `ConfigError`；确需兼容空值（未配置）才回落默认。

### A8【P1】测试盲区（对应 A1/A3）
- `tests/test_executor_g3g5.py:119,143-165` 限流只测 GET，未覆盖写方法 + `allow_write=False`。
- `tests/test_runtime_ui_nav.py:22-34` 只测 `_is_danger_label` 纯函数，不测发现路径调用。
- 影响：两个安全关键分支从未被真实验证，全绿也不代表防护生效。

---

## B. 隐含风险（P2）

| # | 问题 | 证据 | 影响 / 解法方向 |
|---|---|---|---|
| B1 | click-through 点击"创建/新增/上传/编辑"等写动词按钮且无熔断 | `runtime_ui.py:2353-2373` `_OPEN_VERBS`；`:2033,2044` 按文本命中直接 `h.click()`；`:2005-2150` 四组 click 无 dialog/写请求监听（对比卡片 `:1325-1335` 有） | 真点"一键创建"产生写操作。复用卡片熔断封装到公共 click 路径 |
| B2 | 卡片危险词表缺中文写动词 + 事后熔断 + 仅同源 | `runtime_ui.py:122-156` 不含"提交/保存/发布/新增/创建/上传"；`:1317-1335` 点击后才检测、`:1332` 只比 netloc | 写请求已发出才熔断（无法回滚）；跨域写漏检。扩词表 + 点击前预检 + 跨域也算 |
| B3 | 前端 `path:` 泛提为路由 | `fp_extract.py:43` `_PAGE_PATH_RE`；`:627-657` 任意匹配即 `has_route=True`，仅受文件级 `_is_screen` 约束 | 菜单配置/mock/表单字段的 `path:` 被当路由，生成不可达用例。限定路由声明上下文（routes 数组/Route 组件） |
| B4 | 丢失 `include_router` 前缀 | `fp_extract.py:249-267` `_router_prefix` 只认顶层 `APIRouter(prefix=...)` 的 `ast.Assign` | `app.include_router(router, prefix="/api/v1")` 的路径缺前缀 → 执行 404 假失败。补 `include_router` AST 解析 |
| B5 | 鉴权模式正则误判 | `auth_scan.py:64-69` 裸串 `"Authorization"`/`"Bearer"` 即计入；`:113` 精确路径匹配，`./` 或绝对路径匹配不上 | 误判 `ABSENT` → 安全用例归 `_KIND_PUBLIC`（2xx 即过）→ 鉴权缺失漏报。收紧模式 + 路径归一化 |
| B6 | 质量门禁证据校验形同虚设 | `quality.py:162-165` `del fps`（不参与判定）、`:182-183` 只判 `if not tp.evidence`；`:111,123-125` 三位数正则把"200 条""500ms"也算可观测对象 | 证据指向错文件/过期 hunk 也算有据 → KPI 虚高。校验 evidence 指向当前 hunk；可观测词表去纯数字 |
| B7 | 选择器只校验 id 不校验 class | `runtime_ui.py:1443-1474` `stableId` 只滤不稳定 id，class 直接取前 2 个 + `nth-of-type` | antd/emotion 哈希 class 每次构建变化 → 执行假 FAIL；`nth-of-type` 遇列表增删错位。哈希 class 降级为 tag+语义属性 |
| B8 | store 对账三处不一致 | `store.py:395-403` `_case_payload` 不含 fp_id/fp_contract_id/tc_no/module，`:363-364` reused 不刷新 → FP 重排后追溯链错乱且不自愈；`:339-362` 内容变化的 obsolete 置 `updated` 不计 `revived`；`:556-561` `last_result` 按 tp_id 更新所有行（含 obsolete） | 追溯错乱、复活数少算、同 tp_id 结论分裂。扩 payload + 统一 revived 计数 + UPDATE 加 `status!='obsolete'` |
| B9 | 配置漏校验 + OTP `shell=True` | `config.py:381-399` 只校验 5 项，`expert_*`/`describe_budget_seconds`/`console_slack` 未校；`:472` `playwright_channel` 不校验；`:388-389` base_url 无 scheme；`:485` + `runtime_ui.py:698-707` OTP 命令 `shell=True` | 错误配置运行中才炸；本地 `.env` 被污染可任意 shell。扩 `_validate` + channel 白名单 + base_url scheme 校验 |
| B10 | repo_facts O(n²) + 3000 文件静默截断 | `repo_facts.py:496-510` 每个 span 遍历所有 span；`:610-611` 超 `_MAX_IMPORT_FILES=3000` 按路径排序截断 | 巨型前端仓 CPU 爆炸、跨文件回归漏报（仅 stats 无门禁）。区间树优化 + 截断进门禁 |
| B11 | `count()` 无等待 | `ui_executor.py:286-289` `locator.count()`/`is_visible()` 无重试，仅依赖导航后固定 1200ms（`:553`）；发现侧自己承认菜单要等 12s（`runtime_ui.py:240,985`） | SPA 慢页面执行侧假 FAIL。改 `expect(locator).to_be_visible(timeout=...)` |
| B12 | 跨项目 run_batches 改挂 | `db.py:128` `batch_id TEXT PRIMARY KEY` 全局；`store.py:610-615` `ON CONFLICT` 改 `project_id`；`pipeline.py:246-252` 仅 6 位 hex | 不同项目同 batch_id 时旧 runs 留原项目、批次行改挂新项目。batch_id 复合主键（project_id+batch_id） |

---

## C. 低优先（P3）

- **C1** `config.py:319-348` `public_dict()` 缺 `runtime_ui_mobile_enabled`（`:230` 定义），面板无法确认移动端发现是否开启。
- **C2** `runtime_ui.py:226` `MAX_ELEMENTS_PER_PAGE=200` 与 `:2346` 又定义 150（后者生效），注释/维护者易按 200 理解实际 150 静默截断。合成单一常量。

---

## D. 历史文档已登记、但尚未闭环（非本链路隐含缺陷）

来自既有文档，非本次代码扫描发现，列出以免遗漏：
- `docs/closed_loop_183_2026-11-04.md:83-84`：闭环的 API/UI 触发入口未做；testgen `TestCase` → `AICase` 自动桥接未做（闭环当前消费已存在 AICase）。
- `docs/m10_prompt_injection_2026-10-30.md:103-112`：DNS rebinding TOCTOU、302 重定向到元数据靠下一次动作兜底、`UI_PRIVATE_NET_GUARD=off` 属运维降级——均已登记为"已知局限"，未根治。
- `docs/code_review_pre_explore_177_2026-11-03.md:78-83`：无 run_task 级集成测试（prompt 是否真拼进 task 未单测证明）。
- 运维类（非代码）：`docs/security_env_bak_remediation_2026-10-30.md` 的 5 项凭据轮换 + 历史清理脚本待用户执行。

---

## E. 建议批次与检查点

| 批次 | 范围 | 理由 | 状态 |
|---|---|---|---|
| **E1（P0，立即）** | A1 限流写保护 + A8 对应的 POST 单测 | 默认配置即污染被测环境，安全红线，改动小（入口加判定） | ✅ 已完成（2026-11-19） |
| **E2（P1，正确性）** | A2 异常口径统一、A3 危险菜单过滤、A4 页面级断言定点化、A5 控制台基线 | 全是"生成/执行假通过假失败"，直接影响交付可信度 | ✅ 已完成（2026-11-19） |
| **E3（P1，规模化）** | A6 执行预算 + context 隔离、A7 配置快速失败 | 4066 条场景才爆发，改动面较大，需单独回归 | ✅ 已完成（2026-11-19） |
| **E4（P2）** | B1-B4 点击副作用与路径提取、B6 门禁证据、B8 store 对账 | 按"先副作用后一致性"顺序逐条收 | ✅ 已完成（2026-11-19） |
| **E5（P2/P3）** | B5/B7/B9-B12 + C1/C2 | 健壮性与可观测性，低风险 | ✅ 已完成（2026-11-19，B10 核实已闭环无需改） |

**检查点**
- [x] E1 后：`allow_write=False` 下对 POST 限流用例发 0 次真实写请求（单测断言 request 计数）
- [x] E2 后：点名元素排第 25 位仍在断言里；点"退出登录"不进 labels；异常接口返 200 时文案不再说 4xx；基线已知错误被文本抵扣
- [x] E3 后：4066 条有总 deadline（EXEC_BUDGET_SECONDS，超额如实 skipped）且逐条 UI 用例前 reset_between_cases 隔离；拼错布尔开关启动即抛 ConfigError（点名变量）
- [x] E4 后：危险写动词按钮点前被排除、跨域写熔断；include_router 前缀拼回；mock/form 的 path 不提为路由；纯数量不算状态码；稳定追溯列（fp_contract_id/tc_no/module）变化自愈但易变 fp_id 不参与比对（守幂等）、obsolete 复活数一致、结论不写 obsolete
- [x] E5 后：鉴权注释不算信号、`./` 路径归一化；哈希 class 过滤；非法 channel/base_url/expert 值启动期报错、OTP shell=False；元素断言带短等待；批次号带项目前缀+12位
- [x] 全量 pytest 不回退（E4/E5 后见 F 节；E3 基线 1001 passed）、ruff 绿

> 说明：B3 采用「排除法」（只排除明确非路由上下文），不强制命中路由关键词，避免误伤简洁路由声明。A2 口径经确认采用"文案对齐 200-499 判定"（不改判定行为，零回归）。B10 经代码核实已有 `repo_import_truncated` 统计进 stats+日志，盘点文档"静默截断"描述已过时，无需改。

---

## F. 实施记录

### E1（P0 限流写保护）— 2026-11-19 ✅
- **改动**：
  - `engine/executor.py` `_special_probe` 限流分支入口加 `_not_executable(case, step, opts)` 判定，写方法未放行时返回诚实跳过，口径与常规写用例完全一致；修复前该分支直接发 8 次突发写请求、绕过 `_probe_http` 中的写保护。
  - `tests/test_executor_g3g5.py` +2 例：`POST + allow_write=False` → SKIPPED 且 `sess._i == 0`（零真实请求）+ 跳过原因断言；`PUT + allow_write=True` → 正常发完整突发序列并 PASS。
- **验证**：`test_executor_g3g5.py` 12 passed；执行器相关（g3g5 + a3 + a3_e2e）35 passed；全量 **967 passed, 1 skipped, EXIT=0**（原 965，+2 为新增单测）；ruff check / format 全绿。
- **未提交**（按仓库约定）。

### E2（P1 正确性四项）— 2026-11-19 ✅
经确认采用"最小惊讶、零回归"口径（与本文 A2/A4/A5 原始建议略有不同，以本节为准）：
- **A3 危险菜单过滤**：`engine/runtime_ui.py` `_discover_menu_routes` 合并 labels 后统一过滤 `_is_danger_label` —— 修复此前该函数只在面板 replay/absorb 调用、真正发现路径从未过滤的缺陷（点"退出登录"会踢下线）。`tests/test_runtime_ui_nav.py` +2 例，验证**发现路径真调用**（explicit labels 与 extra_labels 均过滤）。
- **A2 异常口径**：不改判定谓词（仍 200-499 通过，兼容"200+业务错误码"软失败），只把成功/失败文案与期望文案改成与之一致（"异常被妥善处理，未返回 5xx"）。`executor.py:174`、`tp_expand.py:_abnormal_expect`；`test_executor_g3g5.py` +2 例锁定"200 通过时文案不再说 4xx"。
- **A4 断言截断**：`case_gen.design_assertions(info, named_text="")` —— 标题/期望点名的可见元素稳定排到前 20 之前，不受截断；仍守总量上限（payload 不膨胀）。`build_case` 传 `title+expect`；`test_ui_case_exec_contract.py` +2 例（第 25 位点名元素被断言 / 无点名时保持发现顺序）。
- **A5 控制台基线同期口径**：`_check_console` 增可选 `baseline_errors`（文本白名单），发现阶段已知错误按出现次数抵扣，只把**真正新增**错误计入；不传时旧 int 行为逐字不变。`case_gen` 断言写 `baseline_errors`，执行器透传。`test_ui_case_exec_contract.py` +5 例（抵扣/超限/全已知零 slack/旧 int 兼容/生成侧写字段）。**白屏阈值 MIN_VISIBLE_TEXT 未动**（用户口径聚焦基线，避免误伤短文案页面）。
- **验证**：相关测试 67 passed；全量 **978 passed, 1 skipped, EXIT=0**（原 967，+11）；ruff check / format 全绿。
- **未提交**。

### E3（P1 规模化两项）— 2026-11-19 ✅
- **A7 配置快速失败**：`core/config.py` `_as_bool` 对**非空且非白名单**值（如 `ture`/`enabled`/`2`）抛 `ConfigError` 并点名变量；只有 None/空串/纯空格才回落默认（兼容旧行为）。`load_settings` 全部布尔开关调用点补 `name` 参数使报错精确。三态 `card_nav` 兼容语义保留：非法新值仍回落 auto、非法 legacy 值宽容回落 off（不走严格版，避免升级时旧变量卡住启动）。`EXEC_BUDGET_SECONDS` 负值也快速失败。
- **A6 执行预算 + 逐条隔离**：
  - `Settings.exec_budget_seconds`（默认 0=不限）→ `pipeline._build_exec_options` 贯穿两条执行路径 → `ExecutorOptions.budget_seconds`。
  - `executor.execute_all` 由列表推导改为逐条循环 + `time.monotonic()` 总预算：超预算的未执行用例一律 `_skipped`（原因写明"不等于通过"），汇总加 `budget_exhausted`/`budget_seconds`，绝不静默丢弃或伪装通过；预算 0 保持旧行为。
  - `ui_executor.UiSession.reset_between_cases()`：每条 UI 用例执行前清 console 缓冲 + best-effort 清 localStorage/sessionStorage（测试桩无 `evaluate` 时静默跳过），解决"单 page 复用 + ui_click=on 时跨用例状态串味"的顺序相关假结果；不重登录、不关 page，保持性能。
- **验证**：新增 `tests/test_exec_budget_config_e3.py` 23 例（非法布尔抛错/空值回落/合法解析/card-nav 兼容/预算耗尽跳过/预算 0 全执行/预算贯穿/隔离清缓冲/桩容错/UI 逐条隔离）；受影响测试集 176 passed；全量 **1001 passed, 1 skipped, EXIT=0**（原 978，+23）；ruff check / format 全绿。
- **未提交**。

### E4（P2 六项）— 2026-11-19 ✅
口径经确认：B6 选「去重计数+去纯数字」（不强制 evidence 指向 hunk，避免前端无行号用例被打成不可信）。
- **B8 store 对账**：`_case_payload` 纳入稳定追溯列 `fp_contract_id/tc_no/module`（FP 重排能自愈），**刻意不含 `fp_id`**（自增行 id 每次生成都变，纳入比对会破坏幂等——曾致 `test_pipeline_is_idempotent` 全量失败，已修并加守护测试）；updated 分支对作废行补计 `revived`；`last_result` 回填加 `status!='obsolete'` 防结论分裂。
- **B4 include_router 前缀**：`_router_prefix` 回退识别同文件 `app.include_router(router, prefix="/api/v1")`，APIRouter(prefix=) 仍优先；跨文件装配不追求（需全仓调用图，显式 routes 兜底）。
- **B6 门禁**：`_STATUS_RE` 排除"200 条/500ms"等纯数量单位；`apply_gate` 增 `evidence_total/unique/duplicate` 统计（KPI 虚高可观测），不改门禁判定、`fps` 仍 del。
- **B1/B2 点击副作用**：`_CARD_DANGER_HINTS` 补中文写动词（提交/保存/发布/新增/创建/上传，未加"确认/确定"避免误伤只读筛选）；卡片写请求熔断去掉 netloc 限制（跨域写也熔断）；click-through 触发器 JS 增点前危险词过滤（`__DANGER__`），`_click_triggers` 传入词表。
- **B3 path 泛提**：`_is_route_path_context` 用排除法（form/mock/fixture/schema/upload/filePath 等上下文的 `path:` 不提为路由），前 160 字符窗口；不用"必须命中路由关键词"以免误伤简洁路由声明。
- **验证**：新增 `tests/test_e4_fixes.py` 14 例；相关测试集 159+19 passed；全量见 E5 记录。ruff 绿。

### E5（P2/P3）— 2026-11-19 ✅
口径经确认：B9 OTP 改 `shell=False`+元字符拒绝；B12 batch_id 带项目前缀扩位（不改表结构/迁移）。
- **B5 鉴权**：`_norm` 去 `./` 前缀+统一分隔符（mode_for 精确匹配得上）；`_count` 前 `_strip_comments` 去整行/块注释，注释里的 Authorization/Bearer 不再算接线信号（与 `_evidence_of` 口径一致）。
- **B7 选择器**：两处 `cssPath` 增 `stableClass` 过滤 CSS-in-JS 哈希 class（`css-*`/`emotion-*`/`sc-*`/`_hash`），无稳定 class 退化 tag+nth，杜绝构建号变化导致执行假 FAIL。
- **B9 配置+OTP**：`_validate_b9` 补校验 describe_budget/expert_* 正数、playwright_channel 白名单（空/msedge/chrome）、base_url http(s) scheme；OTP 刷新改 `shlex.split`+`shell=False`，含 `|&;<>$` 等 shell 元字符的命令直接拒绝（返回空串由调用方回退静态 OTP）。
- **B11 元素等待**：`_check_element_visible` 增 `wait_for(state=visible, timeout≤5s)`，locator 支持则等待（SPA 慢渲染不假 FAIL），测试桩无该方法自动回退 count/is_visible。
- **B12 批次号**：`_new_batch_id(project_id)` 生成 `RUN-p<id>-<时间>-<12位随机>`，两处调用点传 project_id；不改表结构、零迁移，碰撞概率可忽略。
- **C1/C2**：`public_dict` 补 `runtime_ui_mobile_enabled`；`MAX_ELEMENTS_PER_PAGE` 合成单一常量（150，删掉重复的 200 定义）。
- **B10（核实无需改）**：代码已有 `repo_import_truncated` 进 stats+日志（`:638`），盘点文档"静默截断"描述过时。
- **验证**：新增 `tests/test_e5_fixes.py` 22 例；相关集 182 passed；**全量 1037 passed, 1 skipped, EXIT=0**（E3 基线 1001，+36 = E4 14 + E5 22）；ruff check/format 全绿。
- **未提交**（按仓库约定）。

E1–E5 全部交付。多仓改造待全部批次完成后按 `docs/multirepo_refactor_plan_2026-11-19.md` 执行。
