# testgen-service

**代码 → 测试点 → 测试用例** 的独立生成服务。契约版本 **v1.0**。

> 定位：覆盖「代码 → 测试点 → 测试用例 → 执行 → 报告」全链路。
> 生成（读懂代码产出测试点与用例）为默认核心能力；**执行**（runtime_ui 真实浏览器驱动 + 截图）与**报告导出**（pdf/docx/xlsx）为显式能力，分别由 `RUNTIME_UI_ENABLED` 与可选依赖开关控制（详见 `ARCHITECTURE.md`）。
> 与**被测代码**的关系：分析阶段**只读**——分析完即把代码目录置只读；执行阶段针对**已部署运行的被测服务**发起请求，不改动被测仓库。

---

## 快速开始

```bash
# 1) 建虚拟环境并装依赖（使用托管 Python，避免污染本机）
C:/Users/EDY/.workbuddy/binaries/python/versions/3.13.12/python.exe -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt

# 2) 配置（可选；不配也能跑，LLM 默认关闭）
cp .env.example .env

# 3) 跑一条流水线（全量）
venv/Scripts/python.exe -m cli.main pipeline --path ../targets/research-agent

# 4) 增量（只看 git 变更）
venv/Scripts/python.exe -m cli.main pipeline --path ../targets/research-agent \
    --mode incremental --base HEAD~1 --target HEAD

# 5) 起服务
venv/Scripts/python.exe -m cli.main serve --host 127.0.0.1 --port 8100
```

## 质量门禁（一条命令，八环）

```bash
sh scripts/install_hooks.sh        # 装 git 钩子（一次性）
python scripts/gate_all.py         # 手动全量跑门禁
```

## 产出物

```
outputs/<project_id>/test_points.json    机读：测试点
outputs/<project_id>/test_cases.json     机读：用例（八要素 + 追溯键）
outputs/<project_id>/TEST_CASES.md       人读：用例清单
outputs/<project_id>/summary.json        机读：本次运行摘要
```

## HTTP 接口

> 除 `/health`、`/ready` 外，全部 `API` 受 `require_auth` 保护（需 `AUTH_TOKEN`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针 |
| GET | `/ready` | 就绪探针（含脱敏配置摘要） |
| POST | `/api/v1/pipeline` | 一站式：代码 → 测试点 → 用例（默认 `execute=false`） |
| POST | `/api/v1/generate` | 生成-only 红线端点（带 `execute=true` 返回 422） |
| POST | `/api/v1/analyze` | 只做扫描 + 功能点提取 |
| POST | `/api/v1/pull` | 拉取/加深被测代码仓库 |
| POST | `/api/v1/parse-input` | 智能输入框（A1）：混合解析仓库/URL/账号密码 |
| POST | `/api/v1/compare` | 测试点/用例版本对比（全量 vs 更新） |
| GET | `/api/v1/dialogue/template` | 智能输入对话框模板 |
| POST | `/api/v1/dialogue/submit` | 智能输入提交 |
| POST | `/api/v1/execute` | 执行测试（真实浏览器 + 截图，异步） |
| GET | `/api/v1/tasks` | 任务列表 |
| GET | `/api/v1/tasks/{task_id}` | 任务状态/轮询 |
| POST | `/api/v1/verify/webhook` | 发布流水线门禁回调（异步 + 结果回传） |
| GET | `/api/v1/metrics` | Prometheus 指标 |
| GET | `/api/v1/projects` | 项目列表 |
| GET | `/api/v1/projects/{pid}/test-points` | 测试点查询（支持 scope 过滤） |
| GET | `/api/v1/projects/{pid}/cases` | 用例查询 |
| GET | `/api/v1/projects/{pid}/traceability` | 追溯体检（孤儿应为 0） |
| GET | `/api/v1/projects/{pid}/runs` | 执行批次列表 |
| GET | `/api/v1/projects/{pid}/runs/{batch_id}` | 单批次执行详情 |
| GET | `/api/v1/projects/{pid}/report` | 报告（md/html/json；导出 pdf/docx/xlsx） |
| GET | `/api/v1/projects/{pid}/coverage` | 覆盖率统计 |
| GET | `/api/v1/workspaces` | 工作区状态 |

## 文档

- `ARCHITECTURE.md` —— 分层铁律与门禁（**开工前必读**）
- `规范文档.md` —— 规范总览与通俗说明
- 仓库根 `P1-1_契约冻结说明v1.0.md` —— 数据契约
- 仓库根 `P1-2_资产盘点表.md` —— 与 legacy 的资产对应关系
