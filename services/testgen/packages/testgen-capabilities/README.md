# testgen-capabilities

testgen 的**存储无关能力层门面包**。把 `testgen-service` 中「纯能力」模块
（`comparator` / `llm_fallback` / `dialogue`）重新导出为一个可 `pip install` 的包，
供跨项目（如 aiSitePilot）复用，而**不引入** testgen 的 `projects/cases/runs` 存储依赖。

## 红线

- 能力层**不读** testgen 的 `projects/cases/runs` 表。
- 所有能力由入参驱动（依赖注入）：例如 `comparator.compare_one(inp, opts)` 仅消费传入的
  `case/actual`，零存储、零副作用。

## 安装

方式一（可编辑安装，推荐本地联调）：在 `testgen-service` 根目录执行

```bash
pip install -e packages/testgen-capabilities
```

方式二（在其他项目中指向 testgen-service 根目录）：

```bash
set TESTGEN_SERVICE_ROOT=C:/.../work/testgen-service
python -c "import testgen_capabilities; print(testgen_capabilities.compare_one)"
```

包初始化时会自动把 `TESTGEN_SERVICE_ROOT` 或「向上回溯找到含 `engine/` 的目录」
加入 `sys.path`，因此无需手动配置 `PYTHONPATH`。

## 公开 API

```python
from testgen_capabilities import (
    compare_one, CompareInput, ComparatorOptions, redact,   # 语义比对
    chat_with_fallback,                                     # 统一 LLM 降级链
    default_dialogue_template, DialogueAgent, DialoguePlan, # 对话代理
)
```

## 自测（无网络 / 无存储）

```bash
PYTHONPATH=packages/testgen-capabilities/src \
  pytest packages/testgen-capabilities/tests -q
```

自测证明：比对器在 `enabled=False` 时仅做确定性规则判定、对话代理在自由文本兜底下
正确抽取 URL 并脱敏凭证——全程不触存储、不触外部网络。
