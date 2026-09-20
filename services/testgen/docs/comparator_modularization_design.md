# testgen-service 模块化与 LLM 结果比对器 · 设计文档

> 状态：设计阶段（已确认方向，未写代码）
> 确认决策：① 复用方式 = **HTTP 端点为主**；② 判定关系 = **规则为主 · LLM 增强（默认关闭）**；③ 范围 = **先出设计文档**

---

## 0. 决策记录（本次拍板）

| 维度 | 结论 | 含义 |
|---|---|---|
| 比对器对外复用 | HTTP 端点为主 | 新增 `POST /api/v1/compare`；本项目另支持 CLI / import，但跨项目首推 HTTP 零耦合调用 |
| 与现有规则判定的关系 | 规则为主 · LLM 增强 | `executor` 规则判定仍是主结论；`comparator` 作为执行后**可选阶段**，默认关闭，仅补充语义解释/diff |
| 本次落地范围 | 先出设计文档 | 本文档即交付物；代码改造待评审后执行 |

---

## 1. 背景与目标

现状：testgen-service 已有一套"生成 → 执行 → 验证"链路（`engine/` 各 stage 文件已分，但被 `pipeline.py` 以整体编排硬串；`executor`/`ui_executor` 的**对比逻辑是纯规则、零 LLM**）。

目标：
1. **新增 `comparator` 模块**：用大模型（当前 `qwen3.8-flash` via DashScope）对"实际执行结果"与"预期结果"做**语义级比对**，输出可解释结论。
2. **旧阶段模块化**：把生成/执行/验证等 stage 拆成带统一契约的独立可调用单元，调用方可跳过编排层单跑任一 stage。
3. **跨项目复用**：`comparator` 不依赖 DB / pipeline，可经 HTTP 被任意项目调用。

---

## 2. 现状盘点（落地依据）

| 项 | 现状 | 对方案的影响 |
|---|---|---|
| 各阶段文件 | `scan / fp_extract / diff_tag / tp_expand / semantic_enrich / case_gen / fp_merge / runtime_ui / executor / ui_executor / report` 已各自成文件 | 文件已分，但**职责边界与统一契约不清晰**，被 `pipeline.py` 以"整体编排"方式硬串 |
| 执行对比逻辑 | `executor._judge()` 用 `_PASS_PREDICATES`（状态码→5 维）+ `ui_executor` DOM 断言（`page_rendered`/`element_visible`/`console_within_baseline`/`interaction_ok`），**零 LLM 调用** | 这正是要补的"LLM 语义比对"层；`comparator` 不替代它，而是叠加在其上 |
| 预期结果来源 | `tp_expand._expect_of()` 产出自然语言 `expect` 文案 + `steps[].dimension` | 比对器"预期侧"输入已现成，直接喂给 LLM |
| LLM 客户端 | `engine/expert/llm.py` 已封装 `ExpertLLM`（`timeout` 已放宽到 300s），走 DashScope 兼容端点；`EXPERT_MODEL` 已切 `qwen3.8-flash` | 比对器**直接复用**，跨项目只需配 `EXPERT_API_KEY/EXPERT_BASE_URL/EXPERT_MODEL` |
| 服务入口 | `service/app.py` 已有 `/api/v1/generate`、`/execute`、`/verify/webhook`、`/projects/{pid}/runs` 等，统一 `require_auth` + 版本化前缀 | 加 `/api/v1/compare` 是平滑扩展，不破坏既有 |
| 凭证红线 | `executor` 已明确：密码/OTP/Token 只从调用方注入，不入库、不入日志 | `comparator` 须继承同一红线（见 §4.6） |

---

## 3. 总体架构（对应架构图）

```
接入层   CLI(testgen) · FastAPI /api/v1/* · Webhook(发布流水线)
   ↓
编排层   pipeline 编排器 + StageRegistry（可跳过，调用方直接调 Stage）
   ↓
能力层   9 个独立 Stage（统一契约 StageInput→StageOutput）
   scan / fp_extract / tp_expand / case_gen / runtime_ui
   executor(规则判定) · comparator(LLM比对·新增) · reporter · gate
   ↓                                                ↘
基础层   expert/llm 客户端 · db · config · contracts      外部项目/其他服务
                                                          (① HTTP ② CLI ③ import)
```

**关键解耦点**：`executor` 负责"跑出实际结果"（采集 status / assertions / DOM / console / screenshot）；`comparator` 负责"把实际结果和预期做语义比对"。二者通过统一数据结构衔接，互不耦合。

---

## 4. comparator 模块设计

### 4.1 定位与边界
- **纯函数模块**：输入"预期 + 实际执行结果"，输出"语义比对结论"。
- **不碰 DB、不碰 pipeline、不触发任何执行动作**（只读 executor 已产出的结果）。
- 依赖仅：`engine/expert/llm.py` 客户端 + 轻量数据结构。

### 4.2 数据契约（示意）

```python
@dataclass
class CompareInput:
    case: dict          # expected：id/title/steps/expect(自然语言期望)/dimension/severity
    actual: dict        # 来自 executor：status/assertions/response_body/DOM文本/console/screenshot_path/error
    context: dict = field(default_factory=dict)   # model 覆盖、语言、project_id 等

@dataclass
class CompareVerdict:
    verdict: str        # pass / fail / partial / inconclusive
    confidence: float   # 0~1
    reason: str         # 自然语言解释
    diff: list[str]     # 关键差异点
    rule_verdict: str   # 保留原规则判定，便于追溯
    model: str          # 实际使用的模型
```

### 4.3 比对流程与 Prompt
1. 取 `case.expect` + `case.steps` 作为"预期"。
2. 取 `actual` 摘要（status、关键 assertion 结果、DOM 文本片段、console error、screenshot 路径/可 OCR 文本）。
3. 调用 `ExpertLLM.complete()`，要求**结构化 JSON 输出** `{verdict, confidence, reason, diff}`。
4. 解析失败 → 走 §4.4 降级。
5. 增强逻辑默认关闭（`COMPARATOR_ENABLED=false`），仅在显式开启或按需调用 `/api/v1/compare` 时生效；规则判定结论始终保留在 `rule_verdict`。

### 4.4 诚实降级（延续项目一贯风格）
- LLM 超时 / 被墙 / 返回非 JSON / 解析失败 → 回退到 `executor` 原有规则判定，并标注 `verdict=inconclusive`、在 `reason` 注明降级原因。
- **绝不假装通过**：降级时绝不返回 `pass`。

### 4.5 对外接口（三种）

| 方式 | 形态 | 说明 |
|---|---|---|
| ① HTTP（主） | `POST /api/v1/compare` | body：`{case, actual}` 比单条，或 `{project_id, batch_id}` 比整批 |
| ② CLI | `testgen compare --project <pid> --batch <bid>` 或 `--case-json <f> --result-json <f>` | 本项目内联/脚本 |
| ③ import | `from engine.comparator import compare_one` | 同进程复用 |

HTTP 请求/响应示例（示意）：
```json
POST /api/v1/compare
{
  "project_id": 10,
  "batch_id": "RUN-20260917-xxxx"
}
→ 200 {
  "compared": 148,
  "verdicts": [ { "case_id":"...", "verdict":"fail", "confidence":0.92,
                 "reason":"页面返回 404，预期为正常渲染", "diff":["状态码 404 ≠ 预期 200"],
                 "rule_verdict":"fail", "model":"qwen3.8-flash" } ]
}
```

### 4.6 安全红线（必须继承）
- 比对输入/输出中若出现 `password / otp / api_key / token`，一律脱敏（参考 `executor`：仅记账号名，明文"密码与动态口令均不记录"）。
- `comparator` 不接收、不存储任何登录凭据；凭据注入只在 `executor` 侧完成。
- 服务端点沿用 `require_auth`，不开放匿名比对。

---

## 5. 旧阶段模块化方案

### 5.1 统一 Stage 契约
每个 stage 显式声明 I/O，替换当前隐式传参：
```python
@dataclass
class StageInput:
    project_id: int
    params: dict
    ctx: dict = field(default_factory=dict)

@dataclass
class StageOutput:
    ok: bool
    data: dict
    metrics: dict = field(default_factory=dict)

class Stage(Protocol):
    name: str
    def run(self, inp: StageInput, ctx) -> StageOutput: ...
```

### 5.2 StageRegistry
新增注册表，9 个 stage 登记入册；`pipeline` 改为"按 registry 组链"。调用方亦可单跑任一 stage（跳过编排层）。

### 5.3 各 Stage 改造清单
| Stage | 现状 | 改造 |
|---|---|---|
| scan / fp_extract / tp_expand / case_gen / runtime_ui | 文件已分，I/O 隐式 | 补 `StageInput/Output`，函数签名显式化 |
| executor（执行） | `execute_all` 已聚合 HTTP+UI | 拆清"规则判定"与"采集实际结果"，后者作为 comparator 输入源 |
| **comparator（验证·新）** | 无 | 新建 `engine/comparator/`（见 §4） |
| reporter（验证·报告） | `report.py` 已有 | 补 CLI/端点出口 |
| gate（质量门禁） | `scripts/gate_all.py` | 补 `testgen gate` 子命令，纳入 registry |

### 5.4 编排层改造
- `pipeline.run_pipeline` 改为按 `StageRegistry` 顺序组链。
- 保留 `upsert_project`（按 name+base_url 复用同一 project，避免重复建项）。

---

## 6. 跨项目调用示例（HTTP 为主）

其他项目（如 aiSitePilot、督办系统 v2 的测试接入）无需引入 testgen 代码，只需：
1. 向 testgen-service 发起 `POST /api/v1/compare`，携带自身执行结果 + 预期。
2. 拿到结构化 `verdicts` 并入自己的报告。

```
其他项目执行引擎 ──(实际结果+预期)──> testgen /api/v1/compare ──> LLM 语义比对 ──> 结构化结论
```

 credential 与鉴权：沿用 `require_auth`；比对不涉及任何被测系统凭据。

---

## 7. 分阶段实施计划

| 阶段 | 内容 | 门禁 |
|---|---|---|
| **P0** | 建 `engine/comparator/`（compare_one/compare_batch + 复用 expert/llm）+ 单测（含降级分支）；服务加 `POST /api/v1/compare` + CLI `testgen compare` | 八环门禁全绿，cov≥60% |
| **P1** | 9 个 stage 补统一 `StageInput/Output` 契约 + `StageRegistry`；`pipeline` 改按 registry 组链；reporter/gate 增加 CLI/端点出口 | 八环门禁全绿 |
| **P2** | 把 `comparator` 抽成可独立 `pip install` 的轻量包（仅依赖 expert/llm 客户端），供其他项目 import | 包内单测 + 门禁 |

> 复验动作（与本次切换模型相关，独立于模块化）：用 `qwen3.8-flash` 重跑 project 10 生成并执行，确认 routes≥15、专家 LLM 不再超时降级、无纯规则回退；OTP `260909` 已确认仍有效（`logged_in=true`）。

---

## 8. 质量门禁与验证
- 每次改动走 `scripts/gate_all.py`（secret/lint/format/enum/structure/type/security/test，cov≥60%）。
- `comparator` 单测必须覆盖：正常 LLM 比对、JSON 解析失败降级、超时降级、`inconclusive` 绝不误判为 pass。
- 凭证零泄露：DB / 产物 / 接口响应抽样检查（password/otp/api_key 不出现）。

---

## 9. 风险与开放问题
- **LLM 一致性**：同一对"实际/预期"多次比对结论可能浮动 → 固定 temperature、要求结构化输出、保留规则判定做锚点。
- **成本/时延**：比对全量用例会放大 LLM 调用；建议默认只对"规则判定 fail/partial"的用例做 LLM 增强（按需、可批量限流）。
- **跨项目模型配置**：独立包需自带最小 `expert/llm` 客户端配置，避免强绑 testgen 的 `config.py`。

---

## 10. 下一步
- 本文档评审通过后，按 §7 的 P0 → P1 → P2 顺序执行，每阶段跑八环门禁。
- 优先实现 P0（comparator 模块 + `/api/v1/compare` + CLI），即可立即在 project 10 上做"规则 + LLM 增强"对比验证。
