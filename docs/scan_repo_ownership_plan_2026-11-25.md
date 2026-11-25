# scan 返回携带仓库归属方案（P1，2026-11-25）

## 一、背景与问题

B-5（describe 缓存命名空间）三批次已全部合并：后端 `core.describe_namespace` 是单一事实源，`/describe` 已支持**条目级 `namespace`**（`DescribeItem.namespace` 优先于请求级），本地目录与远程仓的缓存串味已消除。

但**多仓场景仍被迫禁用缓存**：`services/testgen/service/web/index.html:2036-2039` 在多仓模式下故意只发 `{items}`（无任何身份），后端算出 `None` → 不读不写。原因是 `/api/v1/scan` 返回的 items 不含仓库归属，前端无法逐条填 `namespace`；若用弹窗单值 `lastRepoUrl` 代替，会把所有仓的文件塞进同一个命名空间，制造新的串味。

本项（P1）目标：**让 scan 的返回项带上仓库归属（后端算好的 `namespace`），前端 describe 逐条回填，恢复多仓缓存复用**。不改 describe 的分组逻辑（已具备），不改缓存表结构。

## 二、现状事实（已核实）

| 事实 | 位置 | 影响 |
|---|---|---|
| `/api/v1/scan` 用 `ScanRequest`，只有 `repo_url/code_source/ref/base_ref/local_path` | `service/routers/scan.py:124` | 请求无 `repo_key`/`project_id`，后端算不出 `multi:` 命名空间 |
| 同名 `ScanRequest` 另有一份门面定义 | `service/app.py:1093` | `tests/test_scan_git_repo.py:97,112,136` 用 `service_app.ScanRequest(...)` 直接构造；**两处必须同步加字段**，否则门面对象缺字段 |
| scan items 字段固定为 `path/name/ext/functions/classes/bytes/change_status` | `engine/remote_scan.py:251-261` | 无归属；三个扫描路径（本地/Codeup/git）共用 `_select_and_package` 打包 |
| `/api/v1/repos/refs` kind=scan 已按 `repo_key` 分组返回 | `service/routers/scan.py:455-516` | 归属在外层 `repos[key]`，items 内没有；可直接注入 |
| 后端 describe 已支持条目级 namespace 分组 | `scan.py:347-373`（`_describe_item_namespace` + `groups`） | 本项**不需要改 describe 分组逻辑** |
| 前端 `scanCode()` 只扫输入框的单仓 | `index.html:1904-1905`（`_scanRepoPayload`） | 多仓模式下 B 区扫描仍单仓；需从 `MULTI_REPO_SPECS` 匹配出归属 |
| 前端未调用 `/api/v1/repos/refs` | `index.html` 全文检索无该路径 | 批量扫描端点目前只有 API 消费者，本项**不接多仓批量扫描 UI** |
| `loadMultiRepos(pid)` 的 spec 不含 `project_id` | `index.html:845-856` | 需从 `#multi_pid` 取 project_id（或从项目注入点补） |
| 守护测试锁死旧的多仓「不带身份」写法 | `tests/test_b5_frontend_describe_params.py:37-41` 的 `? {items}`、`tests/test_multirepo_stage4.py:101` 的 `multiMode` | 本项必须同步更新，否则门禁 pytest 环失败 |
| items 新增字段对现有消费安全 | `index.html:2073-2149` 只读 `path/name/change_status/functions/classes`；`collectSelectedPaths()` 只读 `data-path` | 附加字段不会破坏勾选/渲染/生成 |
| `scan_code` 经 `A._dispatch_scan` 调用 | `scan.py:523` | 保持门面引用，`patch("service.app._dispatch_scan")` 契约不变 |

## 三、目标契约

### 3.1 请求侧（附加可选字段，兼容旧调用）

`ScanRequest`（**两处同步**）新增：

```
repo_key: str = ""                    # 多仓归属键（来自 repo_bindings）
project_id: int | None = None         # 项目 id，配合 repo_key 生成 multi:<pid>:<key>
namespace: str = ""                   # 显式覆盖（最高优先级，一般前端不传）
```

`RepoRefsRequest` 无需改（已有 `project_id`，逐仓 key 来自 bindings）。

### 3.2 响应侧（附加字段）

`/api/v1/scan`：

```
{
  ok, source, items, summary, count, total, truncated,   // 全部保留
  "namespace": "multi:30:backend" | "local:C:/..." | "https://...git" | null,
  "repo_key": "backend" | "",
  "project_id": 30 | null
}
```

每个 item 在原有字段上附加：

```
"namespace": "<后端算好的值>"   // 仅当有身份时写入；无身份时不写该键
"repo_key": "backend" | ""      // 仅当请求带 repo_key 时写入
```

`/api/v1/repos/refs` kind=scan：每个 `repos[key].items` 同样逐条带 `namespace=multi:<project_id>:<key>`（由 `describe_namespace` 计算），条目归属与外层 key 严格一致。

### 3.3 前端消费

- `scanCode()`：保存响应顶层 `r.namespace` 到 `lastScanNamespace`；items 自带 `namespace` 保留在 `scanItems` 中。
- `enrichScanDescriptions()`：构造 `items` 时逐条透传 `namespace: it.namespace || ""`；有 `lastScanNamespace` 时请求级也带上（双保险）；**删除 `multiMode ? {items} : ...` 的禁用缓存分支**。
- `clearDescCache()`：优先用 `lastScanNamespace` 精确清 `namespace`；为空时回退现有 `repo_url`/`local_path` 分支；全空才全清。
- `_scanRepoPayload()`：多仓模式（`MULTI_REPO_SPECS` 非空）下，用输入框的 `repo_url`/`local_path` 匹配 spec（按归一后的 URL / 规范路径比较），命中则请求带 `repo_key` + `project_id`（`#multi_pid`）；未命中保持原单仓字段，不强行造 `multi:`。

**关键约束：`namespace` 一律由后端 `describe_namespace()` 计算并随 scan 回传，前端不自己拼字符串**。这样缓存键与 describe 写入、背景注入、清缓存四处永远同源，避免前后端漂移。

## 四、兼容与边界

1. **旧调用零变化**：不传 `repo_key`/`project_id` 时，单仓行为与 B-5 批次 3 完全一致（远程用 `repo_url` 原值、本地用 `local:`）；不传任何身份仍返回 `None`，describe 不读不写。
2. **`namespace` 为 `None` 时不写 item 键**：避免 JSON `null` 被前端误读成有效命名空间；前端 `it.namespace || ""` 自然为空。
3. **`project_id` 缺失**：`describe_namespace` 现有行为是 `multi:0:<key>`；本方案沿用，视为「无项目归属隔离桶」，不制造跨仓串味。
4. **`repo_key` 与 `repo_url` 同时存在**：按 `describe_namespace` 既有优先级，`repo_key` 胜出（`multi:` 命名空间）。多仓绑定有 key 时这是期望行为。
5. **不接多仓批量扫描 UI**：`/api/v1/repos/refs` 后端补齐归属即可，前端仍走单仓扫描；批量扫描 UI 另排期。
6. **不改 describe 分组**：`_describe_item_namespace` + `groups` 已支持同批多仓，跨仓同名路径的已知限制（响应 `{path: desc}` 后写覆盖）仍存在，本项不处理。

## 五、分批实施

### 批次 1：后端契约 + 守护测试

- `service/routers/scan.py`、`service/app.py`：`ScanRequest` 两处同步增字段；`scan_code` 用 `describe_namespace()` 计算顶层 namespace 并逐条注入 items；`list_repo_refs` 的 scan 分支逐条注入 `multi:<pid>:<key>`。
- 新增 `tests/test_scan_repo_ownership.py`：① 单仓远程 namespace 沿用 repo_url；② 单仓本地 `local:` 且双目录隔离；③ 多仓请求 `multi:<pid>:<key>` 且两仓隔离；④ 无身份 items 无 namespace 键；⑤ `/api/v1/repos/refs` kind=scan 逐仓归属正确；⑥ `_dispatch_scan` patch 契约仍生效。
- 自测：针对性 pytest + ruff；八环门禁。

### 批次 2：前端消费 + 守护测试更新

- `service/web/index.html`：按 §3.4 改 `_scanRepoPayload` / `scanCode` / `enrichScanDescriptions` / `clearDescCache`。
- 更新 `tests/test_b5_frontend_describe_params.py`（`test_multi_repo_mode_sends_no_identity` 改为锁定逐条透传 namespace）、`tests/test_multirepo_stage4.py:101`（锁定多仓识别仍在，但不锁死禁用缓存写法）。
- 新增前端静态守护：请求体带 `namespace: it.namespace`、不再有 `? {items}`、清缓存带 `namespace`。
- 自测：针对性 pytest + ruff + 八环门禁。

### 批次 3：真机验收 + 收尾

- 启动 sidecar（计划任务 `testhub-sidecar`），真实浏览器点击验收：单仓本地扫描 → 生成描述 → 抓 describe 请求体确认每条带 `local:` namespace；多仓模式载入 spec → 扫描 → 确认条目带 `multi:` namespace；清缓存精确作用域。
- 按常规：feature → commit → PR → CI 5/5 → 合并 → 同步 main；列自测结果与后续任务。

## 六、风险与验收口径

| 风险 | 缓解 |
|---|---|
| 两处 `ScanRequest` 不同步导致门面构造缺字段 | 批次 1 加「两处字段集合一致」守护测试 |
| 前端自己拼 namespace 与后端漂移 | 后端单一事实源计算并回传，前端只透传 |
| 多仓同名路径在 describe 响应里互相覆盖 | 已知限制，沿用「前端按仓分别提交」约定；本项不扩大改动 |
| 旧守护测试锁死禁用缓存写法 | 批次 2 同步更新，八环门禁拦截遗漏 |

验收判定：

- 单仓远程：scan items `namespace == repo_url` 原值，describe 命中历史缓存；
- 单仓本地：scan items `namespace == local:<规范路径>`，两个本地目录互不串味；
- 多仓：`repos/refs` kind=scan 与前端单仓扫描（命中 spec）均得到 `multi:<pid>:<repo_key>`，同路径不同仓隔离；
- 无身份：items 无 `namespace` 键，describe 不读不写（与 B-5 批次 3 一致）；
- `patch("service.app._dispatch_scan")` 契约不变、`/api/v1/scan` 旧字段不变；
- 八环门禁与 CI 5/5 全绿；真机抓包见请求体逐条 `namespace`。

## 七、备选方案（本项不采用）

- **B：只在响应顶层加归属，前端自己拼 `multi:` 字符串** —— 后端与前端各算一次，易漂移；不选。
- **C：改 `engine/remote_scan.py` 的 `_select_and_package` 给 items 加归属** —— 污染纯扫描引擎（本地/Codeup/git 三路径都要改），且归属上下文只在 API 层有；不选。
- **D：前端一次扫多仓（接 `/api/v1/repos/refs` 并合并 items）** —— 涉及 UI 大改与跨仓勾选，超出 P1 范围；另排期。

## 八、回滚

纯契约扩展（请求/响应附加可选字段 + 前端传参），不改表结构、无数据迁移；按批次单 commit revert 即可完全回滚。

以上为方案，待你确认后我再逐批实施（含代码与自测）。
