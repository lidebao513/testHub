# 测试专家系统 · 两通道异构 · 全环节大模型驱动（方案 v2）

> 适用范围：testgen-service（URL 通道 + 代码通道）。
> 目标：在「生成测试点 → 生成测试用例」的**每一个环节**都调用大模型做推理与生成；针对 URL 与代码两条通道，分别构建**职能与处理逻辑不同**的专家模块（PageExpert / CodeExpert）。
> 文档状态：v2 已根据下列 4 项决策定稿（2026-09-16）。

## 一、已确认决策（4 项，来自用户拍板）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 视觉输入 | **多模态（截图）**：PageExpert 以页面截图为主输入，DOM 文本作兜底。 |
| D2 | 实现顺序 | **先 Phase 1（URL / PageExpert）**：优先交付 URL 通道专家，代码通道（Phase 2）后置。 |
| D3 | expert_mode 默认 | **默认开、留开关**：`expert_mode=True`，CLI `--expert-off` 可整体关闭，降级到纯规则基线。 |
| D4 | 专家强度 | **单轮 LLM（低成本，本方案默认）**；自主 Agent 作为**可选配置**后置到后续任务（仅留开关，不本期实现）。 |

### D1/D4 的现实约束（必须诚实标注）
- 默认 `LLM_BASE_URL=https://api.deepseek.com`、模型 `deepseek-chat` 是**纯文本模型，不能读图**。
- 因此「多模态截图」路径**需要配置视觉模型 endpoint 才能真正吃图**；未配置视觉模型时，`use_vision` 自动降级为 DOM 文本并**显式 notes 标注**（沿用 `F10b` 不静默原则），绝不假装已读截图。
- 架构按「截图优先 + DOM 兜底」设计，切换零代码改动，只改配置。

---

## 二、问题回顾（为什么必须做）

- 现状 `case_gen.py` 是确定性规则引擎（不依赖 LLM），用例 1:1 由测试点派生 → 质量瓶颈在**上游测试点**。
- 现有 LLM 仅用于 `semantic_enrich` / `llm_design`，**只吃代码事实，不吃渲染**；URL 通道缺「专家看页面」视角。
- `101.43.2.52:9001` 实证：`runtime_ui._discover_menu_routes` 靠 CSS 类名启发式找菜单，本 SPA 不匹配 → `routes=0` → 仅探索落地页 1 页 → 99 条用例全派生自 1 页，覆盖严重偏少。
- **根因不是用例生成逻辑差，是探索器没遍历菜单**。专家模块须同时承担「治本（反推菜单驱动全站遍历）」与「提质（旅程/风险视角）」两项职能。

---

## 三、总体架构（两通道异构 + 全环节 LLM）

```
【URL 通道】
runtime_ui(探索) ─┬─ S0 PageExpert.propose_routes(读截图/DOM 反推菜单) → 回灌点击遍历↺  (治本)
                  └─ S1..S6 PageExpert.review_page(逐页读渲染·旅程/风险/边界/安全/异常/汇总)
   → 专家测试点 ∪ 规则测试点 → tp_expand → case_gen(确定性)

【代码通道】
fp_extract → S1..S6 CodeExpert.review_code(读函数体/调用图/状态机·分支/边界/异常/安全/汇总)
   → 专家测试点 ∪ 规则测试点 → tp_expand → case_gen
```

**两通道专家职能差异（核心）**

| 维度 | PageExpert（URL） | CodeExpert（代码） |
|---|---|---|
| 审视对象 | 页面**运行时渲染**（截图/DOM/元素/XHR） | 程序**静态语义**（函数体/AST/调用图/状态机） |
| 推理目标 | 用户旅程、业务规则、状态迁移、真实边界 | 分支覆盖、边界条件、异常路径、内部安全 |
| 治本职责 | S0 反推菜单驱动全站遍历 | 无（代码通道无探索问题） |
| 产出形态 | 带 `selector`/`route` 的可执行 UI 用例 + 风险洞察 | 带断言/数据/桩的内部正确性用例 + 覆盖缺口 |
| 主要风险 | 臆造 selector/路由 | 臆造 fp_id / 脱离真实函数 |

**关键设计原则（继承现有红线）**
- **Additive（只增不加）**：专家用例绝不删减规则基线；规则用例是稳定溯源基线（内容指纹 id 不变）。
- **不臆造护栏**：专家测试点 `area` 必须命中已抓取 `elements` / `discovered_routes` / `xhr_list`（URL）或真实 `fp_id`（代码），否则进 `rejected` 并公示。
- **不静默**：专家未启用 / 模型缺失 / 视觉模型未配 → 返回空 added + `notes` 明示（沿用 `F10b`）。
- **生成-only 红线不变**：`/api/v1/generate` 仍不触发执行。

---

## 四、PageExpert（URL 通道，多模态截图，S0–S6）

### 4.1 数据契约 `PageCapture`（复用 runtime_ui 字段 + 截图）
```python
@dataclass
class PageCapture:
    url: str
    path: str
    title: str
    screenshot_b64: str | None = None   # 多模态输入（D1）
    dom_text: str = ""
    elements: list[dict] = field(default_factory=list)   # [{selector,kind,text,visible}]
    console_errors: list[str] = field(default_factory=list)
    xhr_list: list[dict] = field(default_factory=list)
    auth_mode: str = "required"
    discovered_routes: list[str] = field(default_factory=list)
```

### 4.2 逐环节 LLM（每个 S 都调模型）
- **S0 `propose_routes(capture) -> list[str]`（治本）**：读截图/DOM 列出规则选择器漏掉的菜单/导航项文本，回灌 `_discover_menu_routes` 点击遍历。这是把 `routes=0` 修成真实菜单数的唯一低成本治本路径。
- **S1 `extract_focus`**：从渲染中识别核心业务对象与用户目标。
- **S2 `expand_journeys`**：产出用户主流程 E2E 旅程（登录→新建→输入→发送→查历史）。
- **S3 `risk_rules`**：业务规则组合（权限矩阵、状态机迁移、依赖顺序）。
- **S4 `boundaries`**：真实边界（页面临界值、空态/超长、移动视口）。
- **S5 `security`**：安全直觉（未授权直达、输入注入、越权读他人数据）。
- **S6 `summarize`**：汇总去重、按 scope 归类、给出 `confidence` 与 `rationale`。

每条专家测试点 JSON：`{area, category(正常|异常|安全|边界), dimension, title, expect, priority, verify_layer, rationale}`；`area` 必须命中真实元素/路由/XHR。

### 4.3 模块骨架 `engine/expert/page_expert.py`
```python
@dataclass
class PageExpertOptions:
    enabled: bool = True          # D3 默认开
    provider: str = "deepseek"
    model: str = ""               # 视觉模型 endpoint 才真正吃截图
    use_vision: bool = True       # D1 多模态；模型非视觉时自动降级 DOM 并 notes
    timeout: int = 60
    max_tps_per_page: int = 8
```

---

## 五、CodeExpert（代码通道，职能不同，S1–S6）

演进自 `llm_design` + `semantic_enrich`，但**职能不同于 PageExpert**：聚焦程序**内部正确性**。

### 5.1 逐环节 LLM
- **S1 `extract_fp_context`**：吃函数体/AST/签名/调用函数（不只功能点名）。
- **S2 `expand_branches`**：分支与条件组合覆盖。
- **S3 `boundaries`**：参数边界、空/超长/越界、类型边界。
- **S4 `exceptions`**：异常路径与错误回退（缺 `try`、吞异常、空输入回退）。
- **S5 `internal_security`**：内部越权/注入/鉴权缺口。
- **S6 `summarize`**：汇总 + 覆盖缺口清单（review 视角直接回应「生成内容与预期偏差」）。

### 5.2 两种模式
- `generate`：比 `llm_design` 多吃上下文，守 `fp_id` 真实护栏，产 `origin=expert_code` 测试点。
- `review`：专家审计规则测试点 → 输出 `coverage_gaps`（如「未测并发写」「越权只测了读没测写」）+ 可选补点；这份缺口报告本身即解释「为何生成与预期不同」。

### 5.3 模块骨架 `engine/expert/code_expert.py`
```python
def review_code(fps, tps, prd, options,
                mode: str = "both") -> ExpertCodeResult: ...
```

---

## 六、统一 LLM 基建 + 红线护栏

`engine/expert/llm.py`（统一包装，替换散落的 `LLMClient` 调用）：
- 支持**多模态**：消息体可带 `image_url`（截图）；纯文本模型自动剥离图片并 notes。
- 结构化输出：强制 JSON；失败重试一次；超时降级到规则基线。
- 统一故障语义：`{"added": [], "rejected": [...], "notes": "未启用/模型缺失/已降级"}`。

`engine/expert/base.py`（红线护栏，两专家共用）：
- `validate_area(area, capture_or_fps)`：命中真实元素/路由/XHR 或 fp_id，否则 reject。
- `no_silent(notes)`：未产出必须显式说明原因。
- `graceful_degrade()`：模型失败返回空 added，不影响规则基线。

---

## 七、契约 Additive 扩展（`core/contracts.py`，指纹不变）

```python
ORIGIN_RULE = "rule"
ORIGIN_LLM_ENRICH = "llm_enrich"
ORIGIN_EXPERT_PAGE = "expert_page"
ORIGIN_EXPERT_CODE = "expert_code"
ORIGIN_LLM_DESIGN = "llm_design"
```
`TestPoint`/`CaseSpec` 增 `origin` / `expert` / `confidence` 字段（旧消费方忽略；内容指纹 md5 不变，溯源零孤儿）。

---

## 八、配置与开关（D3）

- `settings.expert_mode = True`（默认开）；`expert_vision`、`expert_model`、`expert_code_mode` 可配。
- CLI：`--expert-off` 整体关闭 → 降级纯规则基线（D3 留开关）。
- `expert_agent_mode`（D4）：**本期仅留配置开关，不实现**；实现放到后续任务。

---

## 九、分阶段落地

| 相位 | 内容 | 范围 |
|---|---|---|
| **Phase 1（先做）** | 统一 LLM 包装 + 红线护栏；PageExpert（S0–S6，多模态截图）；导航治本接线（S0 反推菜单）；URL 流水线接入；契约 `origin`；配置/CLI 开关 | **URL 通道全链路** |
| **Phase 2** | CodeExpert（S1–S6，generate/review）；代码流水线接入（替代/叠加 `llm_design`） | 代码通道 |
| **Phase 3** | 报告增强（origin 专家覆盖增量 + 覆盖缺口清单）；八环门禁补 expert 检查（origin 必填 / 拒绝率阈值） | 横切 |
| **Phase 4（可选·后置）** | 自主 Agent：专家升级为 ReAct Agent，自带浏览器工具自主「看页面→决定测什么→执行探针→补点」；仅留开关，实现后续 | 演进 |

---

## 十、风险与对策

| 风险 | 对策 |
|---|---|
| 多模态：deepseek-chat 非视觉模型（D1/D4 现实） | `use_vision` 自动降级 DOM 文本 + 显式 notes；真正吃截图需配视觉模型 endpoint |
| 非确定性（每次输出不同） | 专家用例标 `unverified` + `review_status=pending`；规则基线仍稳定 |
| 臆造 selector/路由/fp_id | 护栏：`area` 必命中真实元素/路由/XHR/fp_id，否则 `rejected` 公示 |
| 成本/延迟（多页×多调用） | 每页一次批量；`max_tps_per_page` 封顶；仅对有菜单页启用 |
| 覆盖仍不全（专家不驱动探索） | 强制 S0 先落地，否则 URL 专家只看到 1 页 |
| 静默失效 | 沿用 `F10b`：未启用/缺模型 → 空 added + notes |

---

## 十一、验收标准（Phase 1 完成判据）

1. 对 `101.43.2.52:9001` 重跑：路由数 `routes` 从 **0 → 真实菜单数（预期 10+）**，`runtime_ui_pages` 显著跳升。
2. 用例量从 99 显著上升且**跨模块**（不再只派生自 1 页）。
3. 报告出现「专家增补覆盖」段，按 `origin` 展示增量与覆盖缺口。
4. `expert_mode=True` 默认生效；`--expert-off` 可整体降级到纯规则基线且产物无损。
5. 八环门禁全绿（含 expert 相关新增检查）。

---

## 十二、需后续拍板（非阻塞）

- 视觉模型 endpoint 是否提供（决定 D1 截图路径是否真吃图，否则走 DOM 兜底）。
- Phase 4 自主 Agent 的具体形态与限流策略（本期仅留开关）。
