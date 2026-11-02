# testgen 控制台 API 前缀可配置化 —— 彻底消除 `/api/v1/` 命名空间冲突

> 生成日期：2026-10-30　状态：**B1 + B2 已实施并验证通过**；B3（下线旧代理）待另行确认
> 关联：`docs/p2_batch3_plan_2026-10-30.md` §9.2（命名空间归属判定，已落地的**缓解**措施）
> 前置结论：`/api/v1/` 目前是「缓解」而非「根治」——平台侧靠归属判定挡住误入，但两个服务仍共用一个前缀。

---

## 1. 为什么要做

### 1.1 现状（已缓解，但没根治）

`backend/urls.py:47-49` 有一条全前缀兜底：

```python
re_path(r'^testgen/(?P<rest>.*)$', testgen_web_proxy),
re_path(r'^api/v1/(?P<rest>.*)$', testgen_api_proxy),
```

第二条存在的原因很具体：**sidecar 控制台的前端 JS 把 API 路径写死成 `/api/v1/` 绝对路径**（`service/web/index.html` 里 39 处），而平台 SPA 用 iframe 同源嵌入它，所以必须由 Django 把这个前缀转发出去。

问题是主平台自己的 API 全部**无版本前缀**（`/api/users/`、`/api/projects/` …），于是 `/api/v1/` 成了一个「看起来像平台、实际属 sidecar」的灰色命名空间。2026-10-30 已修复其最危险的表现（已登录时把 `/api/v1/users/login/` 的**明文密码原样转发给 sidecar**），做法是转发前判归属、平台路径返回 404。

但这仍是**补丁**：判定依赖「平台段清单 − sidecar 段清单」的差集，只要将来 sidecar 新增一个与平台 app 同名的顶层路由（`projects` 已经同名，靠硬编码优先级躲过），就会再次踩坑。

### 1.2 目标

| 目标 | 说明 |
|---|---|
| G1 | 控制台的 API 前缀**可配置**，不再写死 `/api/v1/` |
| G2 | 平台给控制台注入**专属前缀**（如 `/testgen-api/v1`），与平台 `/api/*` 完全分离 |
| G3 | **直连** sidecar（本地调试、`curl`、旧脚本）行为零变化：默认仍是 `/api/v1/` |
| G4 | 改造可回退：任一阶段出问题都能退到上一状态，不产生数据/接口不兼容 |
| G5 | 全部改动可被自动化用例覆盖（含 XSS 负向用例） |

### 1.3 非目标

- 不改 sidecar 的 Python API 路径（`/api/v1/generate` 等一律保留，避免破坏既有集成与文档）。
- 不做前端构建化（`index.html` 是 80KB 自包含单文件，引入打包链成本远超收益）。
- 不动 `apps/testgen_integration/client.py` 等**服务端直连** sidecar 的客户端（它们直连 8910，不经过代理，不受前缀影响）。

---

## 2. 现状盘点（决定成本的三个事实）

| 事实 | 证据 | 对方案的影响 |
|---|---|---|
| **① `api()` 是唯一请求收敛点** | `index.html:405`，39 处调用里 33 处走它 | 前缀只需在 `api()` 内部加一次，**33 处调用零改动** |
| **② 只有 5 处绕开 `api()`** | `736`（`report_link.href`）、`898`、`925`（表格里的报告 `<a href>`）、`1510`（`url` 变量）、`1644`（multipart 上传裸 `fetch`） | 需为这 5 处补一个 `apiUrl()` helper |
| **③ 页面是纯静态投放** | `app.py:1869-1872` `console_home()` 直接 `FileResponse(index.html)` | 前缀不能「构建期注入」，只能**运行期替换占位符** |

补充事实：

- `?embed=1&zone=` 由平台 iframe 传入（`frontend/src/views/requirement-analysis/TestgenConsole.vue:32`），但 **sidecar 前端根本没读它**（全文件搜不到 `embed`）。即 iframe 就是普通同源页面，无需为嵌入模式单开分支。
- 报告链接是 `target="_blank"` 的**浏览器直跳**，不是 XHR，因此必须落在可被代理的路径上 —— 这条最容易在改造中被漏掉。
- sidecar 无 CSP / `X-Frame-Options` 限制（`app.py` 只有 CORS + 一个自定义中间件），注入方案不会被安全头拦。

---

## 3. 方案对比

| 方案 | 做法 | 成本 | 根治 | 风险 | 结论 |
|---|---|---|---|---|---|
| **A（推荐）前缀可配置 + 平台专属前缀** | sidecar 引入 `API_PREFIX`（运行期注入，默认 `/api/v1`）；平台新增 `/testgen-api/v1/*` 代理并在转发 `/testgen/` 时注入前缀 | 中（sidecar 6 处 + 服务端 1 处 + 平台 3 处） | ✅ | 低（有默认值可回退） | **采用** |
| B 仅平台侧白名单前置 404 | 在代理前显式注册平台段 → 404 | 极低（已实现） | ❌ | 低 | 作为**过渡态保留**，已被 A 覆盖 |
| C 代理侧重写 HTML | 平台拿到 HTML 后字符串替换前缀 | 低（只改平台） | ✅ | 中：需处理 gzip/编码/缓存，且把「前端知识」放进平台 | 备选 |
| D 把平台 API 也搬到 `/api/v1/` | 统一版本化 | 高（要改所有前端调用） | ✅ | 高 | 否决（与本次目标无关，收益不成比例） |

**为什么选 A 而非 C**：C 让平台去猜 sidecar 的 HTML 结构（`<script>` 里的字符串），sidecar 一改版就可能静默失效；A 把前缀能力做成 sidecar 的**正式契约**，替换点在自己代码里，可测、可断言。

---

## 4. 方案 A 详细设计

### 4.1 sidecar：引入 `API_PREFIX`

#### 4.1.1 前端（`services/testgen/service/web/index.html`）

**改动 1：新增两个 helper（插在 `api()` 之前）**

```js
/* 控制台 API 前缀：由服务端注入（默认 /api/v1，直连 8910 时行为不变） */
const API_PREFIX = "__TESTGEN_API_PREFIX__";

/* 把硬编码的 /api/v1/... 改写成实际前缀下的路径；非 /api/v1 开头的原样返回 */
function apiUrl(path){
  const BASE = "/api/v1";
  return path.startsWith(BASE) ? API_PREFIX + path.slice(BASE.length) : path;
}
```

**改动 2：`api()` 内改一行（405 行）**

```diff
 async function api(path, opts){
-  const r = await fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, opts));
+  const r = await fetch(apiUrl(path), Object.assign({headers:{"Content-Type":"application/json"}}, opts));
```

> 33 处 `api("/api/v1/xxx")` 调用**完全不用改** —— 这是本方案成本可控的根本原因。

**改动 3：5 处绕开 `api()` 的地方**

| 行 | 原写法 | 改为 |
|---|---|---|
| 736 | ``$("report_link").href = `/api/v1/projects/${curPid}/report?format=html` `` | 外层套 `apiUrl(...)` |
| 898 | ``href="/api/v1/projects/${t.project_id}/report?format=html"`` | ``href="${apiUrl(`/api/v1/projects/${t.project_id}/report?format=html`)}"`` |
| 925 | 同上（`p.id`） | 同上 |
| 1510 | ``const url = `/api/v1/projects/${curPid}/report?format=html` `` | `const url = apiUrl(...)` |
| 1644 | ``fetch(`/api/v1/projects/${curPid}/import-test-points/upload`, {method:"POST", body: fd})`` | `fetch(apiUrl(...), ...)`（**multipart 不能走 `api()`**，它会覆盖 `Content-Type`，此点必须保留裸 fetch） |

**改动 4（可选）：`demo.html` 的 4 处**
演示页不在嵌入链路上，建议**保持原样**，仅在其顶部注释标注「演示页固定走 `/api/v1`，不参与前缀注入」，避免后人误改。

#### 4.1.2 服务端（`services/testgen/service/app.py:1869-1872`）

```python
_API_PREFIX_DEFAULT = "/api/v1"
# 白名单：只允许「/ 开头 + 路径字符」，杜绝把引号/尖括号写进 JS 字符串（XSS）
_API_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._~\-/]{0,120}$")


def _resolve_api_prefix(request: Request) -> str:
    """解析控制台 API 前缀：请求头 > query > 默认值；非法值一律回退默认。"""
    raw = (
        request.headers.get("X-Testhub-Api-Prefix")
        or request.query_params.get("api_prefix")
        or ""
    ).strip()
    if not raw:
        return _API_PREFIX_DEFAULT
    if not _API_PREFIX_RE.fullmatch(raw) or ".." in raw:
        log.warning("非法 api_prefix 被拒绝，回退默认", extra=log_extra(value=raw[:64]))
        return _API_PREFIX_DEFAULT
    return raw.rstrip("/") or _API_PREFIX_DEFAULT


@app.get("/", include_in_schema=False)
def console_home(request: Request) -> HTMLResponse:
    """控制台首页：返回 index.html，并把 API 前缀占位符替换为本次请求的前缀。"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__TESTGEN_API_PREFIX__", _resolve_api_prefix(request))
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store"},  # 前缀随请求变化，禁止缓存
    )
```

要点：

- **默认值 `/api/v1`** ⇒ 直连 8910 的一切旧行为不变（G3）。
- `Cache-Control: no-store`：不加会让浏览器缓存上一次注入的前缀，切换代理后出现「时好时坏」。
- 每请求读一次 80KB HTML + replace：本机可忽略；如需优化可加 `lru_cache` 缓存**原文**（不是渲染结果），替换仍按请求做。

#### 4.1.3 自测（`services/testgen/tests/test_api_prefix.py`，新增）

| 用例 | 断言 |
|---|---|
| 无任何参数访问 `/` | HTML 内 `const API_PREFIX = "/api/v1"` |
| 带 `X-Testhub-Api-Prefix: /testgen-api/v1` | 注入该值 |
| 带 `?api_prefix=/testgen-api/v1` | 同上（query 兜底） |
| 头部与 query 同时存在 | **头部优先** |
| `?api_prefix=/</script><script>alert(1)</script>` | 回退 `/api/v1`，响应体不含 `alert(1)` |
| `?api_prefix=../etc` | 回退默认 |
| `/static/*` 静态资源 | 不受影响（仍 200） |

---

### 4.2 平台：专属前缀 + 注入

#### 4.2.1 新增路由（`backend/urls.py`，放在现有 `/api/v1/` 之前）

```python
# testgen 控制台专属命名空间：与平台 /api/* 彻底分离
re_path(r'^testgen-api/v1/(?P<rest>.*)$', testgen_api_proxy),
```

#### 4.2.2 代理视图（`apps/testgen_integration/proxy.py`）

- `testgen_web_proxy`：转发 `/testgen/...` 时**附加请求头** `X-Testhub-Api-Prefix: /testgen-api/v1`（仅注入 HTML 首页即可，其他静态资源多带一个头无害）。
- `testgen_api_proxy`：保持现有归属判定**不变**（作为双保险，旧 `/api/v1/` 期间仍需它）。

```python
_CONSOLE_API_PREFIX = "/testgen-api/v1"

def testgen_web_proxy(request, rest=""):
    return _do_proxy(request, rest, extra_headers={"X-Testhub-Api-Prefix": _CONSOLE_API_PREFIX})
```

`_do_proxy` 增加可选参数 `extra_headers`（原地合并，不改变现有调用）。

> 用**请求头**而不是 query 传递前缀：URL 干净、不会被误分享/误收藏，也不进浏览器历史。query 仅保留给手工调试。

#### 4.2.3 iframe（`frontend/src/views/requirement-analysis/TestgenConsole.vue:32`）

**无需改动** —— src 仍是 `/testgen/?embed=1&zone=...`，前缀在代理层注入。这是本方案相对「改前端 src」的优势：前端不需要知道 sidecar 的 API 前缀。

---

### 4.3 收尾：旧 `/api/v1/` 代理的去留

| 阶段 | 状态 |
|---|---|
| 现在 | `/api/v1/*` 是控制台**唯一**可用通道 |
| 改造后 | `/testgen-api/v1/*` 为主，`/api/v1/*` 保留 |
| 观察 1~2 周 | `/api/v1/*` 命中时打 `WARNING` 并计数（确认无真实调用） |
| 确认后 | 移除 `/api/v1/*` 路由 + `_SIDECAR_TOP_SEGMENTS` 判定逻辑，命名空间冲突**物理消失** |

移除前必须确认的调用方（已核对）：

- ✅ 平台 SPA：只用 `/api/*`（无前缀），不受影响
- ✅ sidecar 控制台：改造后走 `/testgen-api/v1`
- ✅ 服务端客户端 `apps/testgen_integration/client.py`：直连 8910，不经过代理
- ⚠️ 本地调试脚本（`tests/e2e_p2_batch3.py` 等）直连 8910，用 `/api/v1/`，**不受影响**

---

## 5. 分批实施计划

### B1：sidecar 前缀机制（零行为变化，可独立验证）

- 范围：`index.html`（4 处改动）+ `app.py`（`_resolve_api_prefix` / `console_home`）+ 新增 `tests/test_api_prefix.py`
- 关键：默认值 `/api/v1`，**此时平台完全不改，线上行为等价于改造前**
- 自测：新增 7 项；sidecar 全量 `pytest tests/` 全绿（当前 511 项）
- 回滚：`git revert` 单批即可，无外部依赖

### B2：平台切换（真正分层）

- 范围：`backend/urls.py`（新路由）+ `proxy.py`（`extra_headers` 注入）
- 自测：
  - `test_proxy_routing.py` 扩展：`/testgen-api/v1/projects` 转发成功；`/testgen/` 响应头带前缀
  - 真实链路：iframe 内「生成任务 / 报告链接 / 项目列表 / LLM 配置」逐个点开
- 回滚：仅删新路由 + 注入头，`/testgen/` 回到默认 `/api/v1`，秒级恢复

### B3：观察与下线（可选，需另行确认）

- 给旧代理加计数日志 → 观察期 → 移除路由与判定逻辑
- **B3 单独确认后再做**，因为它会移除一条现存的兼容通道

---

## 6. 验收用例

| 编号 | 场景 | 期望 |
|---|---|---|
| V1 | 浏览器直连 `http://127.0.0.1:8910/` | 控制台功能全部正常（默认前缀） |
| V2 | 平台 iframe 打开控制台 | 所有请求命中 `/testgen-api/v1/*`（DevTools 网络面板核对） |
| V3 | 控制台内点「报告」（`target=_blank`） | 新标签正常打开报告页（**最易漏的一处**） |
| V4 | 上传测试点文件（multipart） | 上传成功（验证裸 `fetch` 也被改写） |
| V5 | `POST /api/v1/users/login/`（改造前那条错误路径） | 仍 404（旧代理未移除时）；B3 之后为 404 |
| V6 | `?api_prefix=/</script>` | 回退默认，无脚本注入 |
| V7 | 平台 / sidecar 全量回归 | `pytest tests/ apps/testgen_integration/tests/` 239 passed / 10 skipped；sidecar 511 全绿 |
| V8 | UI e2e | `ui_e2e_dev2.py` 30/30 PASS（含 testgen iframe 用例） |

---

## 7. 风险与回退

| 风险 | 影响 | 缓解 |
|---|---|---|
| 漏改非 `api()` 调用 | 报告链接/上传 404 | 已穷举 5 处并逐条列在 §4.1.1；V3/V4 专测 |
| 前缀注入 XSS | 高 | 白名单正则 + 拒绝 `..` + 非法即回退默认；V6 负向用例 |
| 浏览器缓存旧 HTML | 切换后时好时坏 | `Cache-Control: no-store` |
| sidecar 有第二个控制台入口（`demo.html`） | 局部失效 | 明确不改并加注释，避免被误认为遗漏 |
| 旧 `/api/v1/` 仍有外部调用 | B3 会打断 | B3 前先加计数观察，确认后再删 |
| 代理 `_do_proxy` 新增参数影响既有调用 | 回归 | 参数带默认值，现有三处调用零改动 |

---

## 8. 待确认事项

1. **专属前缀用哪个**：`/testgen-api/v1`（推荐，语义清晰）还是 `/api/testgen-proxy/v1`（更接近现有命名风格）？
2. **B3 是否一并规划**：即是否接受「观察期后下线 `/api/v1/` 代理」。若你的环境还有别的系统直连平台 8900 的 `/api/v1/`，则 B3 需要额外确认调用方清单。
3. **是否同步改 `demo.html`**：默认不改（非嵌入链路）。

---

---

## 9. 实施记录（2026-10-30 完成 B1 + B2）

用户确认「按推荐执行」：前缀名采用 `/testgen-api/v1`，`demo.html` 不改，B3 暂不执行。

### 9.1 改动清单

| 批次 | 文件 | 改动 |
|---|---|---|
| B1 | `services/testgen/service/web/index.html` | 新增 `API_PREFIX`（占位符 `__TESTGEN_API_PREFIX__`，未替换时回退 `/api/v1`）与 `apiUrl()`；`api()` 内改 1 行 `fetch(apiUrl(path))`（覆盖 33 处调用）；5 处绕开 `api()` 的调用套 `apiUrl()`（行 748/910/937/1522/1656，其中 1656 是 multipart 上传，**必须保留裸 fetch**）；`chart.umd.min.js` 改**相对路径** |
| B1 | `services/testgen/service/app.py` | `console_home` 改 `HTMLResponse` + 占位符替换 + `Cache-Control: no-store`；新增 `_resolve_api_prefix()`（头 > query > 默认）+ `_API_PREFIX_RE` 白名单（拒绝 `..` 与特殊字符）；移除不再使用的 `FileResponse` 导入 |
| B2 | `backend/urls.py` | 新增 `re_path(r'^testgen-api/v1/(?P<rest>.*)$', testgen_api_proxy, {'dedicated': True})` |
| B2 | `apps/testgen_integration/proxy.py` | `_do_proxy(..., extra_headers=None)`；`testgen_web_proxy` 注入 `X-Testhub-Api-Prefix: /testgen-api/v1`，并对无尾斜杠的页面根 `302 → /testgen/`；`testgen_api_proxy(..., dedicated=False)`，专属前缀跳过归属判定；`x-testhub-api-prefix` 加入 `_HOP_BY_HOP`（客户端不可自选前缀） |

### 9.2 实施中发现并修掉的既有缺陷

**控制台静态资源在平台内一直 404**：`index.html` 用 `src="/static/chart.umd.min.js"` 绝对路径，而平台只代理 `/testgen/*` 与 `/api/v1/*`，`/static/*` 落到 Django 自己的静态路由 → 图表库从未在平台 iframe 内加载成功。

修法：改为**相对路径** `static/chart.umd.min.js` —— 直连 8910 解析为 `/static/...`，平台内解析为 `/testgen/static/...`（走既有 `/testgen/` 代理）。同时给 `/testgen`（无尾斜杠）加 302，保证相对路径正确解析。此缺陷由 `tests/e2e_testgen_prefix.py` 的「无 4xx/5xx」断言捕获。

### 9.3 验证

| 项 | 结果 |
|---|---|
| sidecar 单测 | `tests/test_api_prefix.py` **12 项通过**（默认值/头部/query/优先级/5 种非法值回退/no-store/静态资源） |
| 平台单测 | `apps/testgen_integration/tests/test_proxy_routing.py` **19 项通过**（+5：专属前缀不判归属、注入头、客户端伪造被丢弃、`/testgen` 302、静态资源转发） |
| 直连 sidecar `/` | `const injected = "/api/v1"`（旧行为不变） |
| 头部 / query 注入 | 均得 `/testgen-api/v1` |
| 平台 `/testgen/?embed=1` | 注入 `/testgen-api/v1` |
| `/testgen-api/v1/projects` | 200 + 项目列表 |
| 旧 `/api/v1/projects` | 200（兼容保留） |
| 旧 `/api/v1/users/login/` | 404 + 正确路径提示 |
| **UI 真实链路** `tests/e2e_testgen_prefix.py` | **6/6 PASS**：前缀已注入、4 个请求走专属前缀、**0 个**走旧前缀、项目列表渲染 19 行、「报告」href 带新前缀、无 4xx/5xx |
| 主仓全量 | **244 passed, 10 skipped** |
| sidecar 全量 | 全绿（EXIT=0） |
| UI e2e 二开 | **30/30 PASS** |
| lint | 新增/改动文件全过；`backend/urls.py` 3 项、`backend/settings.py` 1 项为 HEAD 既有问题 |

### 9.4 命中计数日志（B3 的观察窗，已实施）

旧代理每次命中都会记账，用于判断「是否还有调用方依赖旧通道」：

- 实现：`proxy._record_legacy_hit(rest, misroute=)`，模块级 `dict + Lock`（Django 多线程安全），只记 `/api/v1/`（`dedicated=False`）分支，专属前缀不记；
- 输出节奏：**每累计 50 次**或**距上次满 300 秒**输出一条 `INFO` 汇总（避免每请求一条刷屏），内容为「本窗口增量 / 累计 / 归属纠正次数 / 按顶层路径 Top8」；
- 计数随进程重启归零，观察期内 grep `旧 /api/v1/ 通道命中` 即可；
- 实测（重启后真实请求 50 次）：

```
INFO 旧 /api/v1/ 通道命中：本窗口 +50 次（累计 50，其中归属纠正 0）；按路径：tasks=50。
     控制台已改走 /testgen-api/v1，增量长期为 0 即可下线该兼容路由。
```

- 单测：`test_legacy_hit_counter_emits_window_summary`（阈值/窗口增量/路径聚合）、`test_legacy_proxy_records_hit_but_dedicated_does_not`（只记旧通道、专属前缀不记、归属纠正标记 misroute）。`test_proxy_routing.py` 现 **21 项通过**。

### 9.5 尚未执行（B3）

旧 `/api/v1/` 代理仍在（含归属判定），当前是**双通道**：

- 控制台已全部走 `/testgen-api/v1/*`（e2e 断言 0 泄漏）
- `/api/v1/*` 仅供未知的外部直连调用

下线判据：持续观察日志，若「本窗口 +N」长期为 0（只剩归属纠正的 404），即删除 `backend/urls.py` 的 `^api/v1/(?P<rest>.*)$` 路由与 `testgen_api_proxy` 的 `dedicated=False` 分支。**该步骤会移除一条现存兼容通道，需你单独确认。**

---

以上为方案，已按第 5 节 B1 + B2 实施完毕（含代码与自测）；B3 待你确认后再做。
