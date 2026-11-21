# 页面假路径修复 + 运行时元素级 UI 用例验证（#305 / #306）

日期：2026-11-19 · 范围：`services/testgen`（引擎侧），不改 testhub 主平台
结论：**#305 修复完成（假 URL 121→0、重复 8 组→0）**；**#306 验证通过（184 条用例带真实页面地址 + 元素名 + 选择器，11 条带控制台基线），过程中另外修掉 4 个真实缺陷**。

---

## 一、结论摘要

| 项 | 修复前 | 修复后 | 证据 |
|---|---|---|---|
| PAGE 功能点数（本仓 `frontend/src`，204 文件） | 145 | 130 | `scripts/audit_page_fps.py` |
| 其中唯一 | 121 | **130（0 重复）** | 同上 |
| 用文件名伪造的 URL（`/Home.vue`、`/Dashboard.vue`） | **121** | **0** | 同上 |
| 同路由重复（`/home`×2、`/Dashboard.vue`×6 …） | 8 组 | **0** | 同上 |
| 元素级 UI 用例（`test_url=http://127.0.0.1:3900`） | 「定位交互点：`xxx.vue`」 | 「打开 http://…/页面，定位交互点：按钮「保存」（选择器：…）」 | project 49 |
| 带控制台基线的交互用例 | 0 | 11（有报错的页面才有基线） | project 49 |
| 含不稳定选择器 `el-id-*` 的用例 | 全量元素用例 | **0** | project 49 |
| 单测 | 892 passed | **895 passed, 1 skipped** | `pytest` |

---

## 二、#305：页面功能点重复与假路径

### 2.1 缺陷形态（实测）

旧实现在「屏幕文件（`pages/views/screens/` 下或 `*Page`/`*Screen` 命名）没写 `path:` 声明」时，
用**文件名**拼路由：

```python
name = f"/{stem}"          # views/Home.vue → /Home.vue
```

于是每个屏幕再展开 7 个维度（页面可达 / 未授权 / 极端视口 / 断网 / 慢网 / 5xx / 空数据），
等于**对着一条不存在的 URL 生成 7 条必然失败的用例**。实测本仓 `frontend/src`：

```
旧：145 条 page 功能点 / 121 唯一 —— 121 条是假 URL（`/Dashboard.vue` 6 次、`/ScheduledTasks.vue` 4 次）
新：130 条 page 功能点 / 130 唯一 —— 假 URL 0，重复 0（22 条真实路由 + 108 条无路由屏幕）
```

### 2.2 三道修复

1. **不伪造地址**（`_screen_fp`）：无 `path:` 声明的屏幕 → `name = 文件相对路径`、`has_route=False`；
   `tp_expand.plan_of` 对这类功能点只给「交互元素可用」一个维度，用例准备步写
   「定位交互点：`<文件>`」而不是「打开页面：`<不存在的地址>`」。
2. **同路由去重**（`_dedupe_pages`）：按 `name` 去重，冲突时**优先保留路由声明文件**那一条
   （`router/index.js` > 页面自身 > 菜单配置），只作用于 PAGE，不动业务函数/组件。
3. **`name:` 段落内配对**（`_page_label`）：只在「本条 `path:` 到下一个 `path:`」之间找 `name:`。
   旧实现 `_PAGE_NAME_RE.findall(text)[i]` 用全局下标对齐，菜单项/组件注册一多就整体错位
   （实测把 `/home` 标成 `Login`）。

### 2.3 回归守护

`services/testgen/tests/test_page_route_extract.py`（9 例）：假路径、真实路由、维度收敛（1 vs 7）、
文案（「定位交互点」不出现「打开页面」）、跨文件去重优先路由文件、去重不影响其它 ftype、
`name` 段落配对、无 `name` 退回文件名。
另加审计脚本 `scripts/audit_page_fps.py`（只读，可随时复算上表）。

---

## 三、#306：`test_url` 触发元素级 UI 用例

### 3.1 方法与隔离

```
POST /api/v1/pipeline
  local_path   = <工作区>/testHub/frontend        # 被测代码（本仓前端的克隆，207 文件）
  test_url     = http://127.0.0.1:3900            # Vite 开发服务器
  login_user   = e2etest（密码不落库、不入产物）
  runtime_ui   = true
  runtime_routes = ["/home", "/api-testing", "/ui-automation"]   # 显式路由优先级最高
  scopes=["正常"], include_business=false, persist=true
```

落库到**独立项目**（47/48/49 三轮，最终数据取 project 49，任务 `gen-22a02cd8d51f`），
不碰 project 30 的主证据（T6 的教训：对照实验必须隔离）。

### 3.2 实测数据（project 49）

| 指标 | 值 |
|---|---|
| 可达页面 / 元素 / 面板区域 | 27 / 637 / 5 |
| 用例总数（channel：本体 109 / 运行时 272） | 381 |
| 元素级用例**带真实页面地址** | 184 |
| 交互用例带**控制台基线** | 11 |
| 机器步带 `runtime` 结构化细节 | 28 |
| 仍是文件级（无路由屏幕） | 109 |
| 含 `el-id-*` 不稳定选择器 | 0 |

### 3.3 开 / 关对照（同一测试点，只切 `runtime_index`）

```
# 关（无运行时索引）
定位交互点：/api-testing#nav:AI接口生成|div.layout > section.el-container > aside.el-aside > ul.el-menu…:nth-of-type(10)

# 开（有运行时索引）
打开 http://127.0.0.1:3900/api-testing，定位交互点：导航项「AI接口生成」
  （选择器：div.layout > section.el-container > aside.el-aside > ul.el-menu…:nth-of-type(10)）
校验：交互后界面按预期变化；控制台错误不超过基线 3 条（基线示例：[console] Message compilation error: Invalid linked format）
```

页面级用例同样富化：

```
# 关：打开页面：/home
# 开：用浏览器打开被测环境地址 http://127.0.0.1:3900/home（页面标题：TestHub - AI一体化测试平台）
#     校验：…关键元素可见：按钮「AI用例生成 智能分析需求…」、按钮「接口测试…」…等 38 个元素
```

### 3.4 验证过程中抓到的 4 个真实缺陷（已修）

1. **登录提交按钮命中「登录方式 tab」**（`_SUBMIT_SELECTORS`）
   Playwright `:has-text` 是子串匹配，真实登录页 3 个按钮（密码登录 / 短信登录 / 登录）全命中，
   按 DOM 顺序取到 tab → 点 tab 只切换登录方式 → 被误判「账号密码错误 / 有风控」。
   修复：`form button:*`（表单内的才是提交按钮）排在全局文本之前，并给全局文本加 `:not(:has-text("密码"))` 等排除。
   对照实测：旧选择器命中「密码登录」，新选择器命中「登录」。
2. **匿名访问不打开页面**（`_login`）
   无凭证时直接 `return False`，页面停在 `about:blank`，发现通道把落地页记成 `path="blank"`、0 元素，
   功能点 `area="blank"` 再展开成「打开页面：blank」的假用例 —— 与 #305 同一类缺陷。
   修复：匿名也要 `goto(base_url)`，失败如实记 note。
3. **选择器用框架生成的临时 id**
   Element Plus 渲染 `#el-id-9432-0`（每次加载都变），写进用例必然不可复现。
   修复：`cssPath` 只接受**稳定 id**（排除 `el-id*` / `:r*` / `ember*` / `vue-*` / `mui-*` / `rc-*` / 含 4 位以上数字的 id），
   否则退回 `tag.class:nth-of-type` 链。实测刷新两次，选择器集合完全一致。
4. **元素级 area 复合键查不到运行时细节**（`case_gen`）
   元素功能点的 `area` 是 `/path#kind:text|selector` 复合键，`runtime_index` 以页面路径为键，
   永远查不到 → 正文只能照抄机器键，既没有页面地址也没有控制台基线。
   修复：解析出所属页面再查一次（只用于**正文**；`steps[0].runtime` 仍只认直命中，避免 259 条元素用例各挂一份整页元素清单）。

### 3.5 副作用与口径变化（如实记录）

- 元素级功能点 **283 → 230**（project 47 → 49）。原因不是丢覆盖，而是去掉 `el-id` 后，
  框架「同一份下拉菜单克隆两份 DOM」的**重复节点**终于能被 `(kind, selector)` 去重掉。
  实测冲突率：`/home` 23 个元素 2 组冲突（均为 `el-scrollbar` 下拉克隆），`/api-testing`、`/ui-automation` 为 0。
- 用例总数 440 → 381 同样是这 53 条重复元素用例消失，`channel_runtime` 331 → 272。

### 3.6 残留与建议（未在本批处理）

1. **109 条文件级 UI 用例**：无路由屏幕（`has_route=False`）在运行时也没被发现时，
   正文只能是「定位交互点：`src/views/xxx.vue`」——元素名/选择器无从谈起。
   建议下一批做「文件 ↔ 路由」映射（`repo_facts` 已有 import/路由事实），命中则改写成路由级；
   或对已被运行时页面覆盖的模块**抑制**文件级交互点，避免同一屏两套用例。
2. **`area::<签名>` 面板区域**：执行器明确不支持（需要按内容签名重定位），当前只作发现证据；
   若要可执行，需要把「面板签名 → 导航点击路径」一起落库。
3. **`runtime_routes=0`**：本仓落地页是卡片式导航（click → `router.push`），既不在 `<a>` 也不在
   `[class*=menu-item]` 里，菜单点击发现拿不到路由；本批用显式 `runtime_routes` 绕过。
   通用做法（识别「卡片式导航」容器）属于启发式扩展，需单独评估误报。

### 3.7 批次 A（P0）：元素级机器断言 + 伪通过回归（已修）

量化残留时发现一条比上述三项更严重的问题：**project 49 的 381 条用例里 353 条（92.7%）
没有显式断言**。元素级用例的定位信息只写在 `steps[0].path`
（`/api-testing#nav:AI用例生成|aside.el-aside > …`）与正文里，`steps[0].assertions` 为空、
`runtime.elements` 也为空；执行器的兜底断言只有「页面有正文 + 控制台未超基线」，
于是**按钮被删掉仍会 PASS**。这与 F13 声称的「真的按 selector 断言元素可见」不符。

修法（`engine/ui_executor.py`，只改兜底路径）：

- 新增 `element_hint_from_path()`：从 `#kind:text|selector` 解析出 selector。
  只按**第一个** `#` 和**第一个** `|` 切分，因为 selector 自身可能含 `#id`；
  `kind` 仅限发现器真实产出过的七种，避免把 URL 片段误判成元素提示。
- `assertions_of()` 在无显式断言时追加 `assert_element_visible`；
  runtime 细节里已有同一 selector 时不重复追加；显式断言永远优先、逐字不改。

真机对照（project 49 的 `TP-02639ed3`，`http://127.0.0.1:3900` 真实登录会话）：

| 用例 | page_rendered | console | element_visible | 结论 |
|---|---|---|---|---|
| 原 selector | True | True（0 条） | True | **pass** |
| 替换为 `#__definitely_missing_element__` | True | True（0 条） | **False：未找到元素** | **fail**（截图已落） |

单测 7 例：CSS id selector 解析、普通路由 / 未知 kind / 空 selector 不加断言、
runtime selector 去重、显式断言不被改写、元素缺失必须 FAIL 且留证据、元素存在仍 PASS。
按 project 49 全部 381 条产物复算：带元素可见断言的用例 **28 → 227 条（新增 199）**；
剩余 154 条（109 条文件级 + 45 条纯路由页，均无 selector）仍按原逻辑降级——
这不是漏修，而是**没有元素可断言**，不能凭空造断言。

### 3.8 批次 B（P1）：路由别名绑定 + 文件级屏幕取址（已修）

`repo_facts` 旧实现只按组件名绑路由，且对象式路由表的 240 字符窗口会串到**下一条**路由
的 `component:`（`/ai-generation/assistant` 被错认成 `Layout`）。本仓 130 条 `path:`
只产出 18 条绑定，绝大多数是 `children` 里的相对子路由，全部被丢弃——这正是 109 条
文件级用例的来源。

三处修复（`engine/repo_facts.py`）：

1. `RouteBinding` 只增 `file` 字段；解析路由文件里的 `import X from '@/views/X.vue'` 别名
   与动态 `import()`，**只接受真实存在的文件**，解析不出就不产绑定；
2. 对象式路由按**下一条 `path:`** 截断窗口，取窗口内位置最早的组件声明（修串页）；
3. 按花括号嵌套还原 `children` 父前缀，`path: 'dashboard'` 拼成 `/api-testing/dashboard`
   ——不再「相对路由一律丢弃」，也不再拿文件名硬猜 `/dashboard`。

`pipeline` 新增 `_hint_file_bound_routes()`（配置 `ROUTE_FILE_HINT=on`）：给无路由屏幕
**只写 `route_hint`，不改 `has_route`**。这是有意的量级保护——`route_infer` 在增量模式会把
页面升级成 7 个地址类维度，全量放开会让全站爆炸；本批只让用例拿到可执行的 URL。

端到端（project 50，任务 `gen-85d8ca11d0bf`，与 project 49 同参数、仅项目名不同）：

| 指标 | 49（修复前） | 50（修复后） |
|---|---|---|
| 静态路由绑定 / 带文件 | 18 / 0 | **106 / 103** |
| 路由表直接命中屏幕 | — | **83** |
| 经父页命中屏幕 | — | **10** |
| 无路由证据（保持文件级） | 109 | **15** |
| 文件级用例 | **109** | **16** |
| 纯路由页用例 | 68 | **161（+93）** |
| 元素级用例 / 用例总数 | 199 / 381 | **199 / 381（不变）** |
| 不可执行目标 | — | 31（16 文件级 + 15 面板区） |

**偏差（如实记录）**：方案里写的「剩余 18 条抑制或挂父页」实际是 **10 条挂父页 + 15 条保留文件级**。
这 15 条既无路由表绑定、也无父页路由证据（配置页、对话框、未被路由引用的视图）。
直接删掉会静默丢覆盖，与本仓「不可执行如实 skipped」的契约冲突，因此保留并计数，
只把「猜测」这条路堵死。若要彻底抑制，需要另加开关，属于后续决策。

另一个残留：转换后的 93 条用例拿到了真实 URL，但运行时发现没覆盖到那些页面，
`steps[0].runtime` 为空 → 控制台基线按 0 算。被测应用若本身有全局控制台报错
（本仓 `:3900` 有 3 条 vue-i18n 错误），执行会报控制台失败。这是静态页用例的既有口径，
本批没有擅自放宽；实际执行可用 `console_slack` 或在后续批次补运行时基线回填。

### 3.9 批次 C（P2）：面板区域父页重放 + 签名断言（已修）

§3.6 第 2 项与批次 B 计数里的「15 条面板区」是同一批：`area::<签名>` 是状态驱动 SPA
点击菜单后 URL 不变、靠内容签名区分的区域，旧产物只留签名，执行器只能如实 skip。
根因是发现器 `_absorb_panel_areas()` 手里**同时握着** `start_url` 与点击的 `label`，
但交给 `_collect_current_page()` 时只传了 `area::<签名>`，父页坐标被丢掉。

三处修复：

1. **发现侧**（`engine/runtime_ui.py`）：`UiPage` / `RuntimePageInfo` 只增 `parent_url` /
   `nav_path` 两个字段；`_absorb_panel_areas()` 累计从起点出发点过的标签（含展开子菜单的
   无效点击——它们本就是复现路径的一部分），把 `parent_url=start_url` 与完整 `nav_path`
   写进面板页；`to_runtime_index()` 透传到索引。危险标签既跳过点击也不进路径。
2. **生成侧**（`engine/case_gen.py`）：页面级面板用例走完整 `_runtime_payload`（已含坐标）；
   元素级面板用例（`area::sig#kind:text|selector`，索引键是 `area::sig`）新增 `_panel_area_key()`
   反查索引，挂**精简** runtime（坐标 + 基线，不挂整页元素、不覆盖显式断言），避免把批次 A
   从 path 解析出的特定元素 selector 挤掉。文档步骤改用 `_panel_open_desc()`：写「打开父页 →
   依次点击导航项 → 切到面板」，不再冒充「打开 area::15条/页」。
3. **执行侧**（`engine/ui_executor.py`）：新增 `ASSERT_PANEL_SIGNATURE` 断言与 `panel_target()`
   ——只有 path 是 `area::` **且** `runtime.parent_url` 有值（新产物）才走面板分支：
   `goto(parent_url)` → `replay_panel_nav()` 按序重放点击 → 校验面板签名命中 → 跑常规断言。
   签名不匹配转 `fail`（不是假通过），导航失败转 `error`；旧产物无坐标继续 `skip`，
   文案写明「缺 parent_url / nav_path，不猜路由」。

端到端（project 51，与 49/50 同参数、仅项目名不同）：

| 指标 | 50（修复前） | 51（修复后） |
|---|---|---|
| 面板用例带父页坐标 | **0 / 20** | **20 / 20** |
| 带运行时细节的用例 | 36 | **51（+15）** |
| 用例总数 / 元素级 / 页面级 | 381 / 199 / — | 381 / 199 / 5（面板页） |
| 三口径一致性 | 一致 | 381=381=381，`gate_blocked=0` |

真机执行（真实登录会话 + Playwright，`console_slack=10`）：取 project 51 两条 `area::15条/页`
用例，均从旧产物的 `skipped` 变为 **`pass`**——`panel_signature` 命中「15条/页」、
`page_rendered` / `console_within_baseline` / `element_visible` 全 True；故意改错签名则
`fail`（单测覆盖）。nav_path 实测最长 7 个标签（含展开子菜单的点击），全量重放后签名稳定命中。

单测 +11（`tests/test_panel_nav_c.py`）：发现坐标累计 / 危险标签不入路径 / 索引透传 /
页面级+元素级生成坐标 / `panel_target` 解析 / 旧产物 skip / 真签名通过 / 错签名 fail /
拒绝重放危险导航。`pytest` 全量 **923 passed, 1 skipped**（批次 B 为 912）；ruff 绿。

残留（如实记录）：①`nav_path` 是发现器「不在两面板间回起点」的累计路径，若被测应用菜单
在跳转后重建且同名标签语义漂移，重放可能落到错误面板——此时签名断言会 `fail` 而非假通过，
属可接受的诚实失败；②卡片式导航（P3）见 §3.10，本批后已提供可选启发式。

### 3.10 批次 D（P3）：卡片式导航启发式（三态 off/auto/on，默认 auto）

接上批残留②。现象：首页/工作台的卡片（`div.card` 点击 → `router.push`）既不是 `<a href>`
也不是 `menu-item`，`_nav_labels` / `_harvest_nav_labels` 都看不到，导致 `runtime_ui_routes=0`
只能靠显式 `runtime_routes` 绕过。真正的受害面是「只有地址、没有代码」模式：静态路由为空、
菜单发现为 0，只能看到落地页。

**三态开关（无需手动切换）**：`RUNTIME_UI_CARD_NAV_MODE`（`Settings.runtime_ui_card_nav_mode`
→ `RuntimeUiOptions.card_nav_mode`），默认 `auto`；旧布尔 `RUNTIME_UI_CARD_NAV_ENABLED` 保留兼容
（仅新变量未设时生效，true→on / false→off）。
- `off` 强制关 / `on` 强制开 / `auto` **由系统按发现结果自动判定**：
  - **自动关**：显式 `runtime_routes`、菜单发现、静态路由、首页 `<a>` 链接四者任一有结果
    （说明已有确定性来源，点卡片只会徒增「删除」类副作用）；
  - **自动开**：四者全空（只有地址、无代码）才点卡片兜底。
- 判定函数 `_card_nav_decision()` 在 `_sweep_pages` 菜单发现后运行（此时四类来源已就绪），
  返回 `(是否启用, 理由)`；理由原样写进 `card_nav_audit["decision"]` 与报告 `notes`，
  **系统为何开/关全程可追溯**，用户不必手动配置。

四道护栏（任何开启态都过）：
① 白名单容器 `_CARD_SCOPE_SELECTOR`（main/content）＋排除导航/侧栏/页头/页脚/弹窗/表单
（含 `[class*=header]` / `[class*=footer]`，真机验证前 3 个 header 噪声已据此剔除）；
② 危险标签过滤 `_CARD_DANGER_HINTS`（删除/支付/退出/停用…33 词），JS 侧先滤 + Python 侧二次滤；
③ 点击预算 `_CARD_CLICK_BUDGET=12`，且 `≤ max_pages`，每次点击前回起点隔离副作用；
④ URL 变化确认：`_click_and_read_route()` 内建 pathname+search 变化判定，未变即**误报、不采信**。

误报审计 `RuntimeUiResult.card_nav_audit`（`mode`/`enabled`/`decision`/候选/危险过滤/点击/
确认路由/`no_nav_labels`），`_record_summary` 以 `[卡片导航 P3]` 段沉淀到报告：
`auto`/`on` 态必写判定理由（即便未启用），启用态再补一行误报明细；`off` 态旧产物零输出。

验证：单测 +25（`tests/test_card_nav_d.py`，批次 D 原 12 + 三态判定/配置解析 13）：
三态 off/on 强制、auto 有来源自动关/无来源自动开、非法值回落 auto、sweep 判定原样生效并入审计、
配置层 5 例（新变量/默认 auto/旧 bool 兼容/新优先/非法回落）、判定理由进报告 + 原四道护栏。
全量 **956 passed, 1 skipped**；ruff 绿。
**只读真机探针**（真实登录 `:3900`，不点击、零副作用）：首页抓到 **14 个卡片式导航候选**
（AI用例生成/接口测试/UI自动化测试/Bug缺陷管理…），全部是 `_nav_labels` 看不到的目标；排除
header 后 0 噪声、危险过滤 0 误伤。**端到端（默认 auto，project 53）**：有显式路由 3 条 +
静态路由 22 条时，系统自动关闭卡片探测，报告写明
「模式=auto，未启用：auto：已有其它路由来源（显式路由 3 条、静态路由 22 条），跳过卡片探测避免副作用」，
用例数 381 与之前一致（零副作用）。点击→URL 链路复用批次 C 已真机验证的 `_click_and_read_route`。

残留（原）：auto 态「只有地址无代码」分支的自动开启此前仅单测覆盖，受真机点击副作用约束
未做开启态 E2E。**该残留已闭环，见 §3.11。**

### 3.11 残留闭环：auto 自动开的真机 E2E + 判定口径校准（三批）

针对 §3.10 残留，分三批补齐（均落地、未提交）：

**批次 1 — 本地受控 SPA + 真浏览器 E2E（`tests/test_card_nav_e2e.py`，4 例）**
- 夹具是只含 `div.card` 的最小 SPA（点击仅 `history.pushState` 改 URL，零副作用），
  刻意不放静态路由 / 菜单 / `<a>` / 显式 routes → auto 判「四来源全空 → 自动开」，
  解决了「真机 :3900 有静态路由必判关、无法触发开分支」的死结。
- 4 例覆盖：①auto 判开 + 真点 3 张导航卡采信路由 + 危险卡（`删除任务`）JS 侧过滤
  + URL 不变的误报卡记入 `no_nav_labels`；②点击预算受 `max_pages` 约束 + 每次点击前回起点；
  ③同夹具加一条静态路由 → auto 自动关、零点击（证明系统按实际自动开关）；
  ④**完整闭环**：发现侧真点出的 `/cases` → `case_gen.build_case` → `ui_executor` 真浏览器执行 PASS。
- 复用批次 C 已验证的 `_click_and_read_route`，未新增点击实现。真 Chromium（153）headless 跑通。

**批次 2 — 判定口径结构化 + 只读探针固化**
- `_card_nav_decision` 把「首页链接」并入四来源统一判定（此前是判定后单独覆盖），
  新增 `sources_out` 就地写入四来源计数 → `card_nav_audit["sources"]`
  （`显式路由/菜单发现/静态路由/首页链接` 各自条数），报告 notes 增「判定来源计数」行。
- 固化 `scripts/probe_card_nav.py`：只读抓候选 / 危险过滤 / 导航文本 / 首页链接，
  **不点击、零副作用**，输出 JSON（如 `docs/metrics/card-nav-probe-2026-11-19.json`）。
  多 SPA 实测校准流程写进脚本 docstring：收集各站四来源证据 → 对照 §3.10 口径 → 再决定是否调判定。
- 单测 +2（首页链接作为来源、`sources` 结构化）。

**批次 3 — 护栏⑤：副作用熔断（`runtime_ui.py`）**
- 卡片点击期间监听 `dialog` 与同源写请求（POST/PUT/DELETE/PATCH），一旦触发即
  `dialog.dismiss()` 并**熔断后续点击**，审计记 `side_effect_tripped`、报告 notes 写明原因。
  GET / 跨域请求不熔断，正常导航（pushState）零影响。
- 单测 +3（dialog 熔断、同源 POST 熔断、普通 GET 不熔断）。

验证：`test_card_nav_d.py` 30 例 + `test_card_nav_e2e.py` 4 例全绿；
探针在受控 SPA 抓到 4 候选（三导航 + 一误报）、导航/链接为空，复现「四来源全空」证据；
ruff 改动文件全绿。

---

## 四、验证与状态

- 单测：`services/testgen` 全量 **965 passed, 1 skipped**（EXIT=0；#305/#306 批次为 895，
  批次 A +7、批次 B +10、批次 C +11、批次 D 原 +12、三态 auto 增强 +13，
  跨模块契约 `tests/test_ui_case_exec_contract.py` +8，§3.11 残留闭环 +9
  = E2E 4 + 判定口径 2 + 副作用熔断 3）。#305/#306 批次新增/改写 4 例：
  `test_submit_selector_skips_login_mode_tabs`、`test_login_skipped_without_credentials_but_opens_page`、
  `test_element_area_case_uses_page_url_element_and_baseline`、
  `test_element_area_parts_parsing_and_legacy_fallback`。
  批次 A 另加 7 例（见 `tests/test_f13_ui_executor.py` 的「批次 A（P0）」段）；
  批次 C 另加 11 例（`tests/test_panel_nav_c.py`）；
  批次 D + 三态 auto 共 25 例（`tests/test_card_nav_d.py`）；
  §3.11 残留闭环另加 `tests/test_card_nav_e2e.py` 4 例（真 Chromium，受控 SPA）
  + `test_card_nav_d.py` 再 +5 例（首页链接来源/sources 结构化 + 熔断 3）。
- `ruff check`：改动文件全绿。
- 端到端：project 49（`gen-22a02cd8d51f`）/ 50 / 51 / 52 三口径一致，`gate_blocked=0`；
  project 51 面板用例 20/20 带父页坐标，真机两条从 skip 变 pass（见 §3.9）；
  project 52 四类 UI 用例真机全 pass（见 `docs/metrics/2026-11-19-ui-coverage.md`）；
  project 53 验证默认 auto 在有显式/静态路由时自动关闭卡片探测、判定理由进报告（见 §3.10）。
- **所有改动未提交**（本轮涉及 `engine/runtime_ui.py`、`engine/case_gen.py`、`engine/fp_extract.py`、
  `engine/ui_executor.py`、`core/config.py`、`.env.example`、`tests/test_p3_runtime_ui.py`、
  `tests/test_a2_runtime_cases.py`、`tests/test_f13_ui_executor.py`、新增 `scripts/audit_page_fps.py`、
  `tests/test_panel_nav_c.py`、`tests/test_card_nav_d.py`、
  §3.11 新增 `tests/test_card_nav_e2e.py`、`scripts/probe_card_nav.py`、
  `docs/metrics/card-nav-probe-2026-11-19.json`）。
