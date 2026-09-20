# 跨项目调用 testgen 能力层（aiSitePilot 示例）

本文说明 **aiSitePilot** 如何复用 `testgen-service` 的能力层，而不引入 testgen 的
`projects/cases/runs` 存储依赖——这是模块化 P2 阶段的核心目标。

## 两条复用路径

| 路径 | 适用场景 | 存储依赖 |
| --- | --- | --- |
| **① 导入包** `testgen-capabilities` | 同机 / 同进程，想直接调纯函数（比对、对话模板） | 无（仅 re-export 纯能力模块） |
| **② HTTP 调用** `testgen-service` | 跨服务 / 跨仓库（aiSitePilot 远程调 testgen） | 无（能力由入参驱动，服务端自带存储） |

aiSitePilot 属于「② 跨服务」，通过 HTTP 调 testgen 的**纯能力端点**。

## 端点映射（已落地的真实端点）

> 任务清单 P2-2 提到 `/api/v1/extract`；该能力在 testgen 中实际落地为
> **`POST /api/v1/analyze`**（内部即 `fp_extract.extract_functional_points`）。

| 能力 | 端点 | 请求体（节选） | 返回（节选） |
| --- | --- | --- | --- |
| 功能点抽取（= extract） | `POST /api/v1/analyze` | `{local_path, project_name, include_business, extract_pages}` | `{files, counts, functional_points[], errors[]}` |
| 语义比对 | `POST /api/v1/compare` | `{case, actual, context}` 或 `{items:[{case,actual}]}` | `{verdict, confidence, reason, diff[]}` 或 `{verdicts[], count}` |

## 鉴权

所有端点经 `X-Auth-Token` 头校验（testgen 配置 `AUTH_TOKEN` 时才生效；未配置放行）：

```http
POST /api/v1/compare
X-Auth-Token: <你的 AUTH_TOKEN>
Content-Type: application/json
```

## 调用示例（零额外依赖）

`examples/aisitepilot_client.py` 用标准库 `urllib` 实现最小客户端：

```bash
# 先在本机启动 testgen 服务（另一终端）
venv/Scripts/python.exe -m cli.main serve --port 8000

# 再跑示例：抽取 + 比对
venv/Scripts/python.exe examples/aisitepilot_client.py \
    --base-url http://127.0.0.1:8000 \
    --auth-token <AUTH_TOKEN> \
    --analyze C:/path/to/aiSitePilot
```

## 红线与契约

- **不传凭证**：`/api/v1/analyze` 仅收代码路径；`/api/v1/compare` 仅收「预期/实际」文本。
  两端点天然不含账号密码类字段，安全红线由契约满足。
- **诚实降级**：`/api/v1/compare` 未配置 LLM 时仅规则判定，返回 `inconclusive` 而非假装通过；
  `verdict ∈ {pass, fail, partial, inconclusive}`。
- **依赖注入**：比对完全由入参驱动，aiSitePilot 自带其存储后端，testgen 不反向依赖调用方。

## 同进程复用（路径 ①）

若 aiSitePilot 与 testgen 同机，可直接 `pip install -e packages/testgen-capabilities`：

```python
from testgen_capabilities import compare_one, CompareInput, ComparatorOptions
v = compare_one(CompareInput(case=..., actual=...), ComparatorOptions(enabled=False))
```
