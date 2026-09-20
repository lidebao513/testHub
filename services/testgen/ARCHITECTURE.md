# ARCHITECTURE.md —— testgen-service 架构约束（单一事实来源）

> 本文件定义的规则由 `scripts/check_structure.py` **机器强制**，违反即质量门禁失败。
> 与 legacy(`work/test-accel`) 的规范同源；分层名按本服务调整。

---

## 1. 分层模型（核心约束）

```
                      cli/  (命令行，最外层)
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    service/       workspace/        output/
   (HTTP 薄壳)    (只读工作区)      (产物写出)
        │               │               │
        └───────┬───────┴───────┬───────┘
                ▼               ▼
             engine/          core/
          (6 模块引擎)   (契约/枚举/配置/DB/日志)
```

**依赖只能向下，不能向上**（门禁强制）：

| 层 | 可以依赖 | 禁止依赖 |
|---|---|---|
| `core` | 无（最底层） | engine / service / cli / workspace / output |
| `engine` | core | service / cli |
| `workspace` | core | service / cli / engine |
| `output` | core | service / cli / engine |
| `service` | core / engine / workspace / output | cli |
| `cli` | 全部 | — |

**为什么这么严**：引擎必须能脱离 HTTP 独立测试与复用（"引擎=库、壳=服务"双形态），
一旦 `engine` 反向依赖 `service`，就无法在不启动 Web 服务的情况下做影子双跑。

---

## 2. 目录结构约定

```
testgen-service/
├── core/          契约(contracts) / 枚举(enums) / 配置(config) / 错误(errors)
│                  日志(log) / 数据库(db) / 仓储(store)
├── engine/        scan / fp_extract / diff_tag / tp_expand / semantic_enrich
│                  / case_gen / pipeline
├── service/       app.py（HTTP 薄壳，唯一入口）
├── cli/           main.py（命令行入口）
├── workspace/     manager.py（隔离与生命周期） / readonly.py（只读加固）
├── output/        writer.py（产物写出）
├── tools/         独立工具（如 export_golden.py）
├── scripts/       质量门禁与钩子（同源复制自 legacy）
├── tests/         单元 + 集成测试
├── data/          运行时数据库（不入库）
└── outputs/       产物输出区（不入库）
```

**根目录禁止散落脚本**：一切入口走 `cli/` 或 `scripts/`（门禁 warning）。

---

## 3. 模块职责边界

| 模块 | 只做 | 绝不做 |
|---|---|---|
| `engine/scan.py` | 目录 → 文件集合；AST 只解析一次 | 提取功能点 |
| `engine/fp_extract.py` | 文件 → 功能点 FP | 展开测试点、碰数据库 |
| `engine/diff_tag.py` | git diff → 变更集与 hunk；打标签 | 决定测试点内容 |
| `engine/tp_expand.py` | 功能点 → 测试点（四维展开） | 生成用例 |
| `engine/semantic_enrich.py` | 规则/LLM 增强 + **防幻觉校验** | 直接落库 |
| `engine/case_gen.py` | 测试点 → 用例（八要素） | 写数据库（交给 store） |
| `engine/pipeline.py` | 编排 + 幂等落库 | 自己做解析细节 |
| `core/store.py` | 参数化读写、幂等对账 | 承载业务规则 |
| `service/app.py` | 解析请求、调引擎、格式化响应 | 写业务逻辑 |
| `workspace/*` | 隔离校验、只读加固、实测 | 分析代码 |

---

## 4. 不可动摇的铁律（被门禁强制）

1. **枚举单源**：所有枚举取值只在 `core/enums.py` 定义，其它文件禁止硬编码字面量（`check_enums`）。
2. **契约单源**：字段名、编号算法、八要素只在 `core/contracts.py` 声明（`tests/test_contract.py` 守护）。
3. **分层方向**：见 §1（`check_structure`）。
4. **密钥不入库**：真实密钥只在 `.env`；提交前 `check_secrets` 扫描。
5. **机器步只在 `steps[0]`**：执行器只读第一步。
6. **`cases.status` 只表生命周期**：执行结论写 `last_result`，不得混入。
7. **路径统一正斜杠**：与 git diff 路径形态一致。
8. **错误不外泄**：客户端只看到 `{code, message}`，绝不返回堆栈。
9. **输入边界校验**：所有入参在 API/CLI 边界校验后才进引擎。

---

## 5. 质量门禁（八环）

`scripts/gate_all.py` 一条命令跑完：

| # | 环节 | 作用 |
|---|---|---|
| 0 | secret scan | 密钥/明文扫描（最高优先级） |
| 1 | ruff lint | 风格 + 复杂度（单函数圈复杂度 ≤ 10） |
| 2 | ruff format | 格式一致性（只校验不改写） |
| 3 | check_enums | 枚举单源 + 孤儿反查 |
| 4 | check_structure | 分层方向 + 数据契约 + 根目录散落脚本 |
| 5 | mypy | 类型检查 |
| 6 | bandit | 安全扫描（MEDIUM 及以上） |
| 7 | pytest + coverage | 测试与覆盖率门槛 |

覆盖率门槛随阶段抬升：**P2 = 5% → P3 = 30%（目标）→ P4 = 50%（目标）**。

---

## 6. 与 legacy 的关系

- legacy(`work/test-accel`) 已冻结为只读基线（tag `legacy-pipeline-v1`），本服务**不修改它**；
- 本服务通过「**契约一致 + 资产移植**」承接其能力（见 `P1-2_资产盘点表.md`）；
- 两处若出现门禁配置分叉，由 `P1-3_契约升版策略.md` §4「防线 3」检出。

---

## 7. 已知技术债（登记，不隐瞒）

| # | 债务 | 影响 | 计划 |
|---|---|---|---|
| 1 | 服务端点**同步执行** | 大仓库请求会长时间占用连接 | 服务化改造阶段异步化 + webhook |
| 2 | 前端路由提取用正则 | 复杂路由写法可能漏提 | 后续引入 Tree-sitter 或前端 AST |
| 3 | LLM 通道默认关闭 | 语义增强能力未启用 | 配好 key 后开启并做质量对比 |
| 4 | Windows 下目录只读靠 ACL | `chmod` 对目录不生效 | 生产走容器 `:ro` 挂载 + 非 root |
