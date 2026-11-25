# B-5：describe 缓存命名空间收口方案（2026-11-25）

## 0. 结论先行

把 describe 缓存的命名空间从「调用方随手传的 `repo_url` 字符串」升级为由**一个共享构造函数**产出的显式命名空间：

- 远程仓：沿用 `repo_url` 原值（历史缓存继续命中，零失效）；
- 本地目录：`local:<规范绝对路径>`（修复 B-5 串味）；
- 运行时地址：`runtime:<归一 URL>`（与地址优先入口共用口径，但词键不冲突）；
- 多仓条目：条目自带命名空间，按仓隔离，不再靠「传空禁用」；
- 无任何身份信息：**真正禁用缓存**（不读、不写），兑现现有 docstring 的承诺。

**不改表结构、不做数据迁移**：命名空间仍落在 `file_descriptions.repo_url` 列（复合主键第一列），schema 保持 v7。

---

## 1. 现状事实（已逐项核对代码）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 缓存表 `file_descriptions(repo_url, file_path, symbols_sig, description, model, updated_at)`，主键 `(repo_url, file_path)`，schema v7 | `services/testgen/core/db.py:220-237` |
| 2 | 读：`get_file_descriptions(repo_url, paths)` 按 `repo_url` 精确匹配；写：`upsert_file_description(...)` 同键 upsert；清：`clear_file_descriptions("")` **清空整表** | `core/store.py:1812-1906` |
| 3 | describe 写入键 = `_describe_item_key(it)` = `path or name`；结构指纹 `symbols_sig` = 函数/类排序 sha1 | `service/routers/scan.py:133-149` |
| 4 | `DescribeRequest` 只有 `items` + `repo_url`；`DescribeItem` 只有 `path/name/kind/functions/classes`，**没有仓库身份字段** | `scan.py:81-105` |
| 5 | 本地目录模式前端传 `repo_url=""`（`local_path` 有值但没带给 describe）→ 所有本地项目共用 `""` 命名空间，不同项目同名文件互相命中。**这就是 B-5 原始缺陷** | `service/web/index.html:1890-1901, 1907, 2027`；`services/testgen/docs/code_review_2026-10-19.md:59-61` |
| 6 | 多仓模式前端注释写「传空 repo_url 禁用缓存」，但后端**并不禁用**，而是把全部仓的文件写进同一个 `""` 命名空间 → 注释与行为相反，跨仓串味更严重 | `index.html:2023-2027` vs `scan.py:332-333` |
| 7 | Django 代理 `TestgenDescribeView` → `client.describe(items=...)` 不传 `repo_url` → 该通道全部落 `""` | `apps/testgen_integration/views.py:379-386`、`client.py:257-267` |
| 8 | `DescribeRequest` docstring 声称「repo_url 为空则不做缓存，全部走 LLM」，实现却是共用 `""` 命名空间 | `scan.py:92-96` |
| 9 | 生成侧 `pipeline.load_file_context(opts.repo_url, fps)` 从同一张表读 describe 背景；`PipelineOptions` **同时有 `local_path` 与 `repo_url`**，可用同一函数算 ns | `engine/pipeline.py:119-149, 1033-1048, 1060` |
| 10 | 地址优先入口 `url_modules` 复用同表：ns = 归一 URL，键 = `runtime:<页面路径>`；`clear("")` 清整表会**误删运行时模块缓存** | `core/url_modules.py:4-7, 26-33, 90` |
| 11 | 现有 describe 测试均用非空 repo_url，未覆盖空 ns 串味 | `tests/test_m18_describe_concurrency.py`、`tests/e2e_p2_batch3.py` |

---

## 2. 目标与非目标

### 目标
1. 同一命名空间口径在 **写入（/describe）、读取（/describe 命中判断）、生成侧背景注入（pipeline）、清理（/describe/cache/clear）** 四处完全一致，由单一函数提供。
2. 本地目录不同项目、不同仓同名文件互不串味。
3. 无身份信息时真正禁用缓存（不读不写），与既有 docstring 一致。
4. 远程仓历史缓存继续命中，不因本次改造重新烧 LLM。

### 非目标
- 不改 `file_descriptions` 表结构、不升 schema、不写数据迁移（历史 `""` 孤儿缓存保留，见 §5）。
- 不调整 describe 的并发/预算/降级语义（M18 行为原样）。
- 不碰 A-4 stage 单表、B-4、C-3 等其它优化项。
- 不改变三通道口径与 quality KPI。

---

## 3. 目标模型

### 3.1 唯一入口：共享命名空间函数

新增 `services/testgen/core/describe_namespace.py`：

```python
def describe_namespace(
    *,
    repo_url: str = "",
    local_path: str = "",
    runtime_url: str = "",
    project_id: int | None = None,
    repo_key: str = "",
) -> str | None:
    """返回 describe 缓存命名空间；None = 禁用缓存（不读不写）。"""
```

判定优先级与产出：

| 优先级 | 条件 | 命名空间 | 理由 |
|---|---|---|---|
| 1 | `runtime_url` 非空 | `runtime:<normalize_url(runtime_url)["normalized"]>` | 与地址优先入口同口径；`runtime:` 前缀使文件键天然不与源码路径冲突 |
| 2 | `repo_key` 非空 | `multi:<project_id or 0>:<repo_key>` | 多仓按项目+仓键隔离；缺 project_id 时显式落 `0`，不静默退化成共享 ns |
| 3 | `repo_url` 非空 | `repo_url.strip()` **原值** | 兼容历史缓存，零失效 |
| 4 | `local_path` 非空 | `local:<os.path.realpath(os.path.expanduser(path))>` | 修复 B-5；Windows 用 `ntpath.normcase` 统一大小写与分隔符 |
| 5 | 全空 | `None` | 真正禁用缓存 |

说明：
- 输出直接写入 `file_descriptions.repo_url` 列；**不新增列**。
- `None` 不是一个可存储的命名空间，调用方必须显式短路，不能转成 `""`（`""` 是历史孤儿命名空间，且空字符串在 clear 接口有「清全部」语义）。
- 归一 URL 复用 `core/url_identity.py:normalize_url`，避免地址优先入口与 describe 两套口径。

### 3.2 请求契约扩展（向后兼容）

`DescribeRequest` 增加可选字段：`local_path`、`runtime_url`、`project_id`、`repo_key`、`namespace`（显式覆盖，最高优先级）。
`DescribeItem` 增加可选 `namespace` —— 多仓同批提交时每条可自带命名空间，服务端按条分组读写。

旧调用（只带 `repo_url`）走优先级 3，行为与今天一致；旧调用（什么都不带）从「共用 `""`」变为「禁用缓存」，这是**期望的行为修正**。

### 3.3 四处接入点

| 位置 | 改动 |
|---|---|
| `scan.py:_describe_cache_lookup` | 入参从 `repo_url: str` 改为 `namespace: str \| None`；`None` 时直接返回 `({}, {k: False ...}, items)` |
| `scan.py:_describe_persist` / `_describe_run_chunks` / `_describe_one_chunk` | 透传 namespace；`None` 时跳过 `upsert` |
| `scan.py:describe_features` | 调用共享函数解析 ns；`cached` 标记与 `cache_hits` 口径不变 |
| `engine/pipeline.py:load_file_context` | 签名改为接收 `PipelineOptions` 或 `(repo_url, local_path, runtime_url, project_id, repo_key)`，内部调用同一函数；`None` 时返回 `{}` |
| `scan.py:clear_describe_cache` | 新增可选 `namespace`（精确清）；保留 `repo_url` 兼容；`repo_url=""` 的「清全部」语义保留但补警示日志 |

### 3.4 调用方改造

| 调用方 | 改动 |
|---|---|
| `service/web/index.html:enrichScanDescriptions` | 去掉「多仓传空禁用」的假禁用；本地模式传 `local_path`，多仓模式按条目带 `repo_key`/`project_id` 或 `namespace`；清缓存按钮传当前 namespace 而非空串 |
| `apps/testgen_integration/client.py:describe` | 增加可选 `repo_url/local_path/namespace/runtime_url/project_id/repo_key`，透传给 sidecar |
| `apps/testgen_integration/views.py:TestgenDescribeView` | 从 body 透传上述字段（旧调用不变） |

---

## 4. 分批实施

### 批次 1 — 共享函数与守护测试（不改调用方）
- 新增 `core/describe_namespace.py` 与 `tests/test_describe_namespace.py`：
  - 五条优先级全分支、`None` 禁用缓存、Windows 路径规范、URL 归一（默认端口/尾斜杠/query）、repo_url 原值兼容。
- 本批次**不改变任何读写行为**，可独立提交。

### 批次 2 — 后端四处接入 + Django 透传
- 按 §3.3/§3.4 改 `scan.py`、`pipeline.py`、`client.py`、`views.py`。
- 补集成测试：
  - 两个本地目录同名文件分别 describe → 描述互不命中；
  - 多仓两条同名文件带不同 `repo_key` → 互不命中；
  - 无任何身份 → 不读缓存、不写缓存（mock `get_file_descriptions`/`upsert` 断言未调用）；
  - 旧 `repo_url` 调用仍命中历史缓存；
  - `load_file_context` 与 `/describe` 取到同一 ns（守护测试，防止两处再次分叉）。
  - `clear` 精确清 namespace 不误删 `runtime:` 模块缓存。

### 批次 3 — 前端与验收收尾
- 改 `index.html` 的 describe 传参与清缓存作用域。
- 跑针对性测试 + ruff + 八环门禁；feature → PR → CI 5/5 → 合并 → 同步 main。
- 真机验收：本地两个目录同名文件描述不同；清缓存后 `runtime:` 模块缓存仍在。

---

## 5. 兼容与风险

| 风险 | 处置 |
|---|---|
| 历史 `""` 命名空间的缓存不再命中（一次性失效） | 保留原行不删、不迁移：`""` 无法判断属于哪个本地目录/仓，强行归属会制造新的串味。日志中如实提示「旧缓存未迁移」 |
| 远程仓缓存失效 | 不失效：ns 直接沿用 `repo_url` 原值 |
| 读写侧 ns 不一致 → 生成背景注入读不到 | 共享函数 + 守护测试强制同源（批次 2） |
| `clear("")` 误删 `runtime:` 模块缓存 | 前端改传精确 namespace；默认「清全部」保留兼容但补警示日志；守护测试覆盖 |
| 多仓前端未改完时按空 ns 提交 | 走 `None` 禁用缓存（多烧 LLM 但不串味），属于安全降级 |
| `runtime:` 前缀与代码文件路径冲突 | 命名空间层已隔离；代码文件名以 `runtime:` 开头极罕见，单测覆盖不构成误命中 |

---

## 6. 验收口径

1. **单测**：`tests/test_describe_namespace.py` 全绿；批次 2 新增 5 类集成测试全绿。
2. **守护**：`load_file_context` 与 `/describe` 同源断言通过；`clear` 不误删 `runtime:` 缓存。
3. **门禁**：testgen 八环（secret / ruff lint / ruff format / enums / structure / mypy / bandit / pytest）EXIT=0。
4. **CI**：PR 5/5 全绿，合并后 main 同步、工作区干净。
5. **真机**：本地两目录同名文件描述不串味；远程仓历史缓存命中；清缓存作用域正确。
6. **回滚**：无 schema 变更，按 commit revert 即可；回滚后旧 `""` 缓存仍在表中，行为可恢复。

---

## 7. 备选方案对比

| 方案 | 做法 | 评价 | 是否采用 |
|---|---|---|---|
| A（本方案） | 复用 `file_descriptions.repo_url` 列承载显式命名空间 | 无迁移、远程缓存零失效、可单测 | **采用** |
| B | 加 `namespace` 列 + schema v8 迁移 | 语义最干净，但需迁移/回滚脚本、双写兼容，风险与成本不匹配 | 否 |
| C | 所有 ns 统一加类型前缀（含 `git:<url>`） | 概念统一，但远程历史缓存全失效（重烧 LLM） | 否 |
| D | 仅修本地前缀，多仓继续「传空禁用」 | 改动最小，但多仓串味缺口保留，且假禁用仍在 | 否 |

---

以上为方案，待你确认后我再逐批实施（含代码与自测）。
