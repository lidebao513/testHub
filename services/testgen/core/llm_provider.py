"""LLM Provider 数据模型（跨账号降级链的基础单元）。

一个 LLMProvider 描述「一个**独立账号**下的一套可调用的模型凭证」：
- ``base_url`` + ``api_key`` 唯一标识一个账号（OpenAI 兼容网关）；
- ``model`` 是该账号下要调用的具体模型名；
- ``name`` 仅用于展示与日志（如 "deepseek-main" / "qwen-bak"）。

降级链 ``list[LLMProvider]`` 即「按优先级排列的多个账号/模型」：
当某个 provider 额度耗尽（403/404/429）、限流、瞬时网络错误、或鉴权失败（401，
且存在**不同账号**的下一个 provider）时，自动切换下一个 provider —— 实现
「某大模型额度用完 → 自动调用存入的其他大模型（不同 key）」的跨账号降级。

⚠️ 红线：``api_key`` 仅存在于内存/``.env``/``data/llm_config.json``，**绝不入库、
绝不写日志、绝不进入 public_dict**。本模块不依赖任何业务代码，供 ``core.config``
与 ``engine.llm_fallback`` 共用，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMProvider:
    """单个 LLM 调用凭证（一个账号 + 一个模型名）。"""

    name: str = ""  # 展示名（如 "deepseek-main"），空则回退到 model
    model: str = ""  # 模型名（如 deepseek-chat / qwen-plus）
    base_url: str = ""  # OpenAI 兼容网关地址
    api_key: str = ""  # 凭证（绝不入库/日志）
    timeout: int = 0  # 单次调用超时（秒），0=使用通道默认

    @property
    def identity(self) -> tuple[str, str]:
        """跨账号去重/判定键：同一 (base_url, api_key) 视为同一账号。

        用于 401 鉴权失败时决定「是否存在可接管的其它账号」——同账号换模型无意义，
        只有不同账号（不同 key）的 provider 才值得接管。
        """
        return (self.base_url, self.api_key)

    @property
    def label(self) -> str:
        """日志/展示用的短标签。"""
        return self.name or self.model or "(unnamed)"
