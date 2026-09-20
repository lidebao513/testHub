"""类型化错误体系。

约定：
- 所有业务错误继承 `AppError`，携带**机器可读的 code** 与 **HTTP 状态码**；
- 全局错误处理器据此统一渲染规范化响应体，**绝不把堆栈或内部细节返回客户端**；
- 客户端只看到 `{code, message, detail?}` 结构。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务错误基类。"""

    code: str = "internal_error"
    http_status: int = 500
    message: str = "服务内部错误"

    def __init__(self, message: str | None = None, *, detail: Any = None) -> None:
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


# ---------------------------------------------------------------- 配置 / 启动
class ConfigError(AppError):
    """环境变量缺失或非法——启动期快速失败。"""

    code = "config_error"
    http_status = 500
    message = "配置错误"


# ---------------------------------------------------------------- 输入校验
class ValidationError(AppError):
    """请求入参非法。"""

    code = "validation_error"
    http_status = 422
    message = "入参校验失败"


class NotFoundError(AppError):
    """资源不存在。"""

    code = "not_found"
    http_status = 404
    message = "资源不存在"


class UnauthorizedError(AppError):
    """未通过鉴权。"""

    code = "unauthorized"
    http_status = 401
    message = "未授权"


# ---------------------------------------------------------------- 契约
class ContractViolation(AppError):
    """产物不满足数据契约 v1.0。"""

    code = "contract_violation"
    http_status = 500
    message = "产物不符合数据契约"


# ---------------------------------------------------------------- 工作区
class WorkspaceError(AppError):
    """工作区操作失败（路径非法、目录逃逸等）。"""

    code = "workspace_error"
    http_status = 400
    message = "工作区操作失败"


class WorkspaceEscapeBlocked(WorkspaceError):
    """目标目录逃逸到祖先仓库——已阻断，防误伤。"""

    code = "repo_escape_blocked"
    http_status = 400
    message = "目标目录逃逸到祖先仓库，已阻断"


class ReadOnlyViolation(WorkspaceError):
    """试图写入只读区。"""

    code = "readonly_violation"
    http_status = 403
    message = "只读工作区不允许写入"


class FetchError(AppError):
    """代码获取失败（克隆/更新/ref 解析）。"""

    code = "fetch_failed"
    http_status = 502
    message = "代码获取失败"


# ---------------------------------------------------------------- 引擎
class EngineError(AppError):
    """引擎执行失败。"""

    code = "engine_error"
    http_status = 500
    message = "引擎执行失败"


class LLMError(AppError):
    """大模型通道失败。"""

    code = "llm_error"
    http_status = 502
    message = "大模型调用失败"


class NotImplementedYet(AppError):
    """骨架阶段的占位实现尚未落地。"""

    code = "not_implemented"
    http_status = 501
    message = "该能力尚未实现"
