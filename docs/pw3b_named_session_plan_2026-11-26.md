# PW-3b：常驻会话池 / attach 调试会话 方案（含真机双角色并发验收）

> 文档日期：2026-11-26
> 来源：`docs/pw_skill_series_plan_2026-11-26.md`（PW-3 批次 3，明确 3b 另拍板）、browser-use 0.10.1 依赖层源码实测、`apps/ui_automation/ai_base.py` / `ai_agent.py` 现状核对。
> 性质：**方案文档，本轮不落地任何代码或测试改动**。按项目硬规则，待你确认后再逐批实施（含代码与自测）。
> 范围：仅 `C:\Users\EDY\WorkBuddy\testhub_platform` 的 `apps/ui_automation`；aiSitePilot 独立项目不纳入。

---

## 0. 结论先行（三个必须先说的判断）

1. **PW-3a 的登录态回写钩子大概率是坏的（前置阻塞）**。`ai_base.py:3351-3365` 在 `agent.run()` **返回后**才调 `agent.browser_session.export_storage_state()`；但 browser-use 在 `agent.run()` 的 `finally` 里已 `close() → kill() → reset()`（`agent/service.py:2022,2550-2558`），`reset()` 把 `_cdp_client_root=None`（`session.py:496`）。`export_storage_state()` 依赖 CDP 取 Cookie（`session.py:1172-1187,2875-2881`），因此真机大概率抛异常并被吞（只记异常类型），**登录态从未真正回写**。所有单测都 stub 了 `browser_session`，所以全绿但假通过。→ **PW-3b 第一批必须先修这个**，否则常驻/attach 复用是空中楼阁。

2. **"常驻会话池"不是加个开关就能做，是重做驱动层**。`agent.run()` 内部硬编码 `start()`/`close()`/`kill()`；`ai_agent.py:37-38` 每任务 `asyncio.run()` 新事件循环；`_stop_event_buses()`（`:8-34`）会停掉 loop 上所有常驻 EventBus。CDP WebSocket 绑定 loop，跨 loop 复用不可行。必须同时改 `BrowserProfile(keep_alive=True)` + `Agent(browser_session=已有)` + 专用长生命周期 loop 线程，**且要多进程（Django 线程 + Celery）池不共享的架构认知**。

3. **多角色并发"不串味"与 attach 单 Chrome 天然冲突**。browser-use 0.10.1 无 browser context 隔离（源码无 `createBrowserContext`），attach 到同一 Chrome 的所有任务共享 tab/Cookie/历史。真正的多角色并发只能"每角色一个独立 Chrome + 独立端口"，这又回到进程池。**建议真机验收采用"两个独立 profile 并发 + 各自 storage_state"路径（PW-3a 已具备能力），把 attach 定位为"人工只读调试后门"，不承担并发执行。**

---

## 1. 分层落地路线（每批可独立拍板、独立 PR）

| 批次 | 内容 | 风险 | 是否碰 #180 默认隔离 | 建议 |
|---|---|---|---|---|
| **3b-0** | 修复 PW-3a 回写：改用 browser-use 内建 `SaveStorageStateEvent(path=...)` 在 CDP 仍连时落盘，再读回 registry；补真回写测试 | 低 | 否（仅修回写时机） | **强制先做**，是后续一切的前置 |
| **3b-1** | 只读 attach 调试后门：`UI_CDP_URL` 配置 + 独立 debug 入口构造 `BrowserProfile(cdp_url=..., user_data_dir=None)`；不接多任务执行 | 低-中 | 否（默认关闭） | 建议做，验证 attach 能力，为调试用 |
| **3b-2** | 每 alias 固定 `user_data_dir`（保留 `browser-use-user-data-dir-` 前缀避开 `_copy_profile` 拷贝）；不做进程常驻 | 中 | 仅显式 alias 时退化 | 建议做，满足"长流程不重复登录"且不重做驱动层 |
| **3b-3** | 完整进程内池（keep_alive + 复用 session + 专用 loop + TTL/驱逐 + 多进程边界） | **高** | 是 | **前置 PoC 通过才启动，本方案不包含实施** |

**真机双角色并发验收放到 3b-2 完成后做**（3b-0 修好回写 + 3b-2 固定 alias profile 后，两个角色天然独立，最适合验收）。

---

## 2. 批次 3b-0：修复 PW-3a 登录态回写（前置必做）

### (a) 根因确认（源码级已核实）

- `ai_base.py:3351-3365`：回写发生在 `run_task` 末尾、`agent.run()` 已返回之后。
- browser-use `agent/service.py:2022`：`agent.run()` 的 `finally` 调 `self.close()`；`:2550-2558`：`close()` 中 `not keep_alive` 则 `browser_session.kill()`。
- `session.py:557-575`：`kill()` 先派发 `SaveStorageStateEvent`（此时 CDP 仍连），再 `BrowserStopEvent(force=True)` → `event_bus.stop(clear=True)` → `reset()`。
- `session.py:496`：`reset()` 把 `_cdp_client_root=None`，`export_storage_state()` 后续调用必然失败。
- `watchdogs/storage_state_watchdog.py:64-74`：`on_SaveStorageStateEvent(event)` 用 `event.path` 或 profile 默认路径落盘；`browser-use 0.10.1` 中 PW-3a 传的是 dict，watchdog 无默认路径可写 → 无人落盘。

### (b) 实施方案（二选一，建议 A）

- **方案 A（推荐，最小改动）**：在 `_create_browser_profile` 注入 `storage_state` 时，同时传**路径**而非 dict（`BrowserProfile.storage_state` 支持 `str | Path`）；由 `StorageStateWatchdog` 在 `SaveStorageStateEvent` 时自动把 state 写回该路径。执行后 `ai_base` 从该路径读回 → `SessionRegistry.save(alias, data)`。优点：复用 browser-use 内建机制、时机正确；缺点：多一次文件读写，且需保证路径唯一（alias 命名文件）。
- **方案 B**：在 `run_task` 的 `on_step_end` 回调里（CDP 仍连）周期性派发 `SaveStorageStateEvent(path=...)` 落盘；或包装 `browser_session.kill()` 在 `reset()` 前导出。优点：直接拿 dict；缺点：侵入 run 内循环、时机难精确。

### (c) 自测/e2e

- 新测试用**假 CDP session**（不再是 `object()` stub）断言：state 文件路径被 watchdog 写回、registry 拿到与写入一致的数据、CDP 断开后不再调 `export_storage_state()`。
- 真机最小验证：一次别名登录 → 落盘 → 第二次同 alias 执行免登录（这是 PW-3a 承诺但从未真实验证的能力）。

---

## 3. 批次 3b-1：只读 attach 调试后门

### (a) 配置与入口

- 新增 env `UI_CDP_URL`（默认空，必须显式）：形如 `http://127.0.0.1:9222`。
- 新增独立 debug 入口（如 `apps/ui_automation/debug_attach.py`）：构造 `BrowserProfile(cdp_url=url, user_data_dir=None, is_local=False, executable_path=None)`；只做只读探测（页面标题/URL/元素数），**不接 run_task、不执行 AI 用例**。
- 复用 `ai_base.py:1408-1413` 已实现的 `cdp_url` 透传 patch，无需改 patch。

### (b) 边界

- 默认关闭：未配置 `UI_CDP_URL` 时该入口不可用。
- 不承担多任务并发：attach 到单一 Chrome 的所有任务共享状态，仅人工调试用。
- 密钥卫生：`UI_CDP_URL` 可能含 token（如 `?token=xxx`），日志只记 host/port，不记完整 URL；不入 DB/测试夹具。

### (c) 自测/e2e

- 单测：配置解析、profile 字段（cdp_url 有值 / user_data_dir 为 None / is_local=False）、默认关闭。
- 真机：启动一个带 `--remote-debugging-port=<free>` 的 Edge/Chrome → debug 入口 attach → 读到页面标题，证明通路。

---

## 4. 批次 3b-2：每 alias 固定 profile + 真机双角色并发验收

### (a) 实现

- `ai_base.py:2665` 的 `user_data_dir = tempfile.mkdtemp(...)` 改为：命名会话生效时取 `SessionRegistry.profile_dir(alias)`，路径名含 `browser-use-user-data-dir-` 前缀（避开 `profile.py:787-827` 的 `_copy_profile` 每次拷贝到新临时目录），且确保 `profile.py:839` 非默认路径校验通过。
- 未启用命名会话时保持 `mkdtemp` 原逻辑（#180 零变化）。
- 僵尸清理 `_is_orphaned_chrome_cmdline`（`ai_base.py:2492-2510`）按"目录不存在判孤儿"→ 对持久目录失效，需增加"目录存在但进程已退出超过阈值"的兜底（或明确：持久 profile 目录**不参与**孤儿判定，靠 CDP 连接健康检查）。

### (b) 真机双角色并发验收（你明确要求的部分）

**目标**：验证 `admin` 与 `user` 两个角色并发执行时，登录态、Cookie、tab 状态互不交叉；关闭开关后回退临时 profile 需重新登录。

**环境**：`scripts/dev_stack.py start` 起 Memurai/sidecar:8910/Django:8900/Celery/Vite:3900；被测站点用 `ui_verify` 脚本风格（Edge `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，API `http://127.0.0.1:8900`，账号 `uiverify/UiVerify@2026`）或平台内已有 `Project.base_url`（`models.py:20`）。

**步骤**：
1. 配 `UI_SESSION_ALIAS_ENABLED=true`、`UI_SESSION_STATE_DIR=<临时目录>`；建两个测试账号（admin/user，假数据）。
2. **预热**：用 `admin` 别名跑一次登录任务 → 确认 `storage_state` 文件落盘且含 admin Cookie；用 `user` 别名跑一次 → 落 user state。
3. **并发**：同时提交两条任务（admin 查 A 页、user 查 B 页），用 `ThreadPoolExecutor` 或平台两个浏览器会话并发；断言各自页面身份/数据归属正确，无串味。
4. **免登录复用**：各角色再跑一条任务，断言不再出现登录页（storage_state 生效）。
5. **开关关闭回退**：关 `UI_SESSION_ALIAS_ENABLED`，再跑一次 → 断言走临时 profile、出现登录页（#180 语义保留）。
6. **Cookie 卫生**：抓执行日志与 DB，断言 Cookie 值不出现。

**证据**：截图 + 日志 + state 文件清单（假 Cookie）+ 两条任务的身份断言输出，落 `ui_verify/evidence/`（.gitignore 已排除，不入库）。

### (c) 自测

- 单测：alias→固定目录映射、前缀保留、跨 alias 目录不同、关闭开关走 mkdtemp、僵尸清理对持久目录的兼容。
- 回归：ui_automation 全量（当前基线 464 passed / 1 skip）。

---

## 5. 批次 3b-3：完整进程内池（本方案**不实施**，仅列 PoC 门禁）

启动前必须通过 PoC：
1. 同一事件循环内 `BrowserProfile(keep_alive=True)` + `Agent(browser_session=已有)` 连续跑两个 `Agent.run()` 均成功。
2. 池内 EventBus 不被 `ai_agent._stop_event_buses()` 误停。
3. 多进程（Django 线程 + Celery solo）下池不共享的边界有明确产品决策（例如"池仅进程内、每 worker 独立"）。
4. idle TTL / 异常任务后 session 可用性 / stop 端点 / alias 维度隔离全部有测试。

任一失败 → 止步于 3b-2，不强上。

---

## 6. 待你拍板（建议已给）

| 编号 | 决策 | 建议 | 理由 |
|---|---|---|---|
| E1 | 是否先做 3b-0 修复 PW-3a 回写？ | **是，强制先做** | 回写钩子大概率失效，不修则后续复用无意义 |
| E2 | 3b-1 只读 attach 后门是否做？ | **做，默认关闭** | 验证 attach 能力、供人工调试，风险低 |
| E3 | 3b-2 每 alias 固定 profile + 真机双角色并发验收是否做？ | **做** | 满足"多角色不重复登录"，且不重做驱动层；真机验收在此批 |
| E4 | 3b-3 完整进程池是否启动？ | **否，先 PoC** | 高风险重做驱动层，收益不确定，前置 PoC 全通过再拍 |
| E5 | 真机验收被测站点用哪个？ | **平台内 Project.base_url（优先）/ ui_verify 测试账号（兜底）** | 平台内更贴近真实用户路径 |

---

## 7. 风险与不做项

- 不改 `#180` 默认隔离语义：所有能力默认关闭，显式 alias 才生效。
- 不把 attach 当作并发执行通道：browser-use 无 context 隔离，attach 单 Chrome 多任务必然串味。
- 不把 Cookie/Credential 写入 DB、日志、测试夹具；state 文件受 `.gitignore` + CI secret-scan 双重覆盖（PW-3a 已落地规则）。
- 不在本方案内实施 PW-4~PW-9；PW-7 密钥卫生已随 PW-3a/3b 顺带做。

---

以上为方案，待你确认后我再逐批实施（含代码与自测）。
