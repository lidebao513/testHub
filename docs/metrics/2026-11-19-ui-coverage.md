# UI 层全覆盖验证报告（2026-11-19）

对今日 A/B/C/D 四批 UI 改动做「单测覆盖 → 跨模块契约 → 端到端生成 → 真机执行」四层验证，
结论：**生成与执行链路均无问题**。

## 一、验证范围（今日改动的 UI 链路）

| 批次 | 改动环节 | 核心文件 |
|---|---|---|
| #305 | 页面功能点去重 / 假路由修复 | `engine/fp_extract.py` |
| #306 | test_url 元素级用例 | `engine/runtime_ui.py`、`engine/case_gen.py` |
| A（P0） | 元素级机器断言（防伪通过） | `engine/ui_executor.py` |
| B（P1） | 静态路由别名绑定 / 文件级取址 | `engine/repo_facts.py`、`engine/pipeline.py` |
| C（P2） | 面板父页坐标 + 签名重放 | `engine/runtime_ui.py`、`engine/case_gen.py`、`engine/ui_executor.py` |
| D（P3） | 卡片导航启发式（默认关 + 误报审计） | `engine/runtime_ui.py`、`core/config.py` |

## 二、第一层：单测覆盖

- 全量 `pytest`：**943 passed, 1 skipped**（批次 D 后为 935，本轮新增跨模块契约 8 例）。
- UI 相关测试文件：`test_page_route_extract`（9）、`test_repo_facts`（25）、
  `test_p3_runtime_ui`、`test_runtime_ui_nav`、`test_a2_runtime_cases`（14）、
  `test_panel_nav_c`（11）、`test_card_nav_d`（12）、`test_f13_ui_executor`（29）、
  `test_a3_executor`、`test_executor_g3g5`、`test_ui_first_strategy`、`test_target_runtime`。
- `ruff check` / `ruff format --check`：改动文件全绿。

## 三、第二层：跨模块契约（本轮新增）

单测各自覆盖了生成侧与执行侧内部，但 `case_gen.build_case` 的 `steps[0]` 是否被执行侧
`ui_executor` 正确消费此前无闭环测试——批次 C 曾踩过「元素级面板 selector 被整页
design_assertions 挤掉」的字段漂移坑。新增 `tests/test_ui_case_exec_contract.py`（8 例），
用**真实生成产物**喂给执行侧解析器，锁死四类用例契约：

| 契约 | 验证点 | 结果 |
|---|---|---|
| 页面级路由 | 生成 `runtime.url` → 执行 `target_url` + 默认断言 | pass |
| 元素级路由 | path `#kind:text\|selector` → 执行侧补元素断言（批次 A） | pass |
| 页面级面板 | 精简坐标 → `panel_target` 解析签名/父页/nav_path（批次 C） | pass |
| 元素级面板 | 坐标 + 元素 selector **都不丢**（A/C 交汇，最易漂移） | pass |
| 无路由文件级 | 不臆造 URL，执行侧目标为空 → 诚实 skip（批次 B） | pass |
| 显式断言优先 | 生成侧显式断言不被兜底改写 | pass |
| 文档/机器一致 | 正文写的元素 = 机器断言的元素 | pass |

## 四、第三层：端到端生成（隔离 project 52）

复用 project 51 参数（全量模式 + 前端代码 + `test_url`），新 project 52 独立生成，不覆盖主证据：

| 指标 | 结果 |
|---|---|
| 任务状态 | `success`，`error=None` |
| 用例总数 | 381 |
| 三口径一致性 | fp=tp=case=381，孤儿 0，`gate_blocked=0` |
| 运行时发现 | 27 页 / 637 元素 |
| 带运行时细节用例 | 51 |
| 结构分布 | 页面级路由 115 / 元素级路由 184 / 面板 20（15 元素级 + 5 页面级，20/20 带父页坐标）/ 文件级 16 |

与 project 51 完全一致，证明今日改动未改变既有产出形态。

## 五、第四层：真机执行（真实登录会话 + Playwright）

用 `UiSession` 在真实浏览器执行 project 52 多类用例（`channel=msedge`，`console_slack=10`
容纳本仓 vue-i18n 编译错误基线）：

| 用例类型 | 结果 | 断言数 | 说明 |
|---|---|---|---|
| 页面级路由 | **pass** | 2 | 真实导航 + 页面渲染/控制台基线 |
| 元素级路由 | **pass** | 3 | 元素 selector 断言命中 |
| 页面级面板 | **pass** | 23 | goto 父页 → 重放点击 → 签名断言 |
| 元素级面板 | **pass** | 4 | 坐标 + 元素 selector 双全 |

**反例（确认不是假通过）**：

| 场景 | 结果 | 说明 |
|---|---|---|
| selector 换成 `#__definitely_missing_element__` | **fail**（不是 pass） | 伪通过回归：元素被删必 FAIL |
| nav_path 塞入「退出登录」 | **error**（绝不 PASS） | 拒绝重放危险导航，写明「不等于通过」 |

## 六、结论

四层验证全部通过：单测 943 绿、跨模块契约闭合、端到端生成三口径一致、真机执行四类
UI 用例全 PASS 且两类反例判定正确。**用例生成与执行过程无问题**。

诚实说明：①卡片导航启发式（批次 D）默认关，主证据未开启，其真机验证仅做只读候选提取
（14 个候选，0 噪声），点击→URL 链路复用批次 C 已验证的 `_click_and_read_route`；
②本仓 `:3900` 存在 vue-i18n 编译错误基线（3 条），执行用 `console_slack=10` 容纳，
其他项目应按自身基线设置。
