"""PW-3a：命名会话注册表（storage_state 导入/导出，纯模块不依赖 Django）。

设计原则（#180 隔离语义优先）：

- **默认关闭**：``UI_SESSION_ALIAS_ENABLED`` 必须显式为真，且 ``UI_SESSION_STATE_DIR``
  必须显式指定目录；两者缺一即视为未启用，行为回退到 #180 每任务临时 profile。
- **凭据不落日志/不入库**：本模块只把 storage_state 写到文件系统指定目录，
  日志仅记 alias 与文件是否存在，绝不记 Cookie / localStorage 内容。
- **alias 白名单**：``^[A-Za-z0-9_-]{1,64}$``，拒绝 ``..`` / 路径分隔符 / 绝对路径穿越。
- **损坏即降级**：state 文件缺失 / JSON 损坏 / 结构不合法 → ``load()`` 返回 ``None``，
  调用方据此起全新会话，不抛异常、不污染执行结果。
- **权限收紧**：POSIX 上文件 0600；Windows 上尽力 chmod，目录建议非共享（见 README）。

与 PW-7 密钥卫生呼应：``storage_state*.json`` / ``*session-state*`` 由 secret 扫描
与 .gitignore 覆盖；测试夹具一律使用假 Cookie，真实登录态永不入库、永不提交。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import uuid
from pathlib import Path

logger = logging.getLogger("django")

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATE_FILE_SUFFIX = ".storage_state.json"
_REQUIRED_KEYS = ("cookies", "origins")


class SessionRegistryError(ValueError):
    """命名会话参数不合法（alias 越界 / 目录未配置等）。"""


def alias_enabled() -> bool:
    """``UI_SESSION_ALIAS_ENABLED``：仅显式真值才开启，缺省关闭。"""
    raw = (os.getenv("UI_SESSION_ALIAS_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def configured_state_dir() -> str:
    """``UI_SESSION_STATE_DIR``：缺省空串，必须显式指定。"""
    return (os.getenv("UI_SESSION_STATE_DIR") or "").strip()


def validate_alias(alias: object) -> str:
    """校验会话别名，拒绝路径穿越与空值。"""
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
        raise SessionRegistryError("无效会话别名：仅允许 1-64 位字母/数字/下划线/连字符")
    return alias


def is_named_session_active(alias: object | None) -> bool:
    """命名会话是否生效：开关 + 目录 + 合法 alias 三者同时满足。"""
    if not alias_enabled() or not configured_state_dir() or alias is None:
        return False
    try:
        validate_alias(alias)
    except SessionRegistryError:
        return False
    return True


class SessionRegistry:
    """按 alias 管理 storage_state 文件的读写，不接触 DB、不输出凭据。"""

    def __init__(self, state_dir: str | Path):
        raw = str(state_dir or "").strip()
        if not raw:
            raise SessionRegistryError("命名会话未配置 UI_SESSION_STATE_DIR，禁止使用缺省目录")
        self.state_dir = Path(raw).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, alias: object) -> Path:
        safe = validate_alias(alias)
        path = (self.state_dir / f"{safe}{STATE_FILE_SUFFIX}").resolve()
        # 双层防护：即便将来放宽 alias 规则，也不允许越过 state_dir
        if path.parent != self.state_dir:
            raise SessionRegistryError("会话状态路径越界")
        return path

    @staticmethod
    def _valid_state(data: object) -> bool:
        return isinstance(data, dict) and all(k in data for k in _REQUIRED_KEYS)

    @staticmethod
    def _atomic_write(path: Path, storage_state: dict) -> None:
        """原子写入 + 0600（Windows 尽力而为）。只写文件，不打日志。"""
        payload = json.dumps(storage_state, ensure_ascii=False, separators=(",", ":"))
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            try:
                tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600；Windows 尽力而为
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    @classmethod
    def _read_validated(cls, path: Path) -> dict | None:
        """读取并校验 state；缺失/损坏/结构不合法均返回 None。"""
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not cls._valid_state(data):
                raise ValueError("结构不合法")
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return data

    def save(self, alias: object, storage_state: dict) -> Path:
        """原子写入 storage_state，返回落盘路径。只记 alias，不记内容。"""
        path = self._path_for(alias)
        if not self._valid_state(storage_state):
            raise SessionRegistryError("storage_state 结构不合法（需含 cookies / origins）")
        self._atomic_write(path, storage_state)
        logger.info("命名会话状态已保存 alias=%s exists=True", alias)
        return path

    def load(self, alias: object) -> dict | None:
        """读取 storage_state；缺失/损坏/结构不合法均返回 None（降级全新会话）。"""
        path = self._path_for(alias)
        exists = path.is_file()
        logger.info("命名会话状态读取 alias=%s exists=%s", alias, exists)
        data = self._read_validated(path)
        if exists and data is None:
            logger.warning("命名会话状态损坏，降级为全新会话 alias=%s", alias)
        return data

    # ---------------------------------------------------------------- 3b-0 --
    # browser-use 0.10.1 的 StorageStateWatchdog 只在 storage_state 为 str/Path 时
    # 才会加载（on_BrowserConnectedEvent → LoadStorageStateEvent）和写回（kill 时
    # SaveStorageStateEvent）；传 dict 两端都会被跳过（watchdog 源码 177-181/239-241）。
    # 因此每次执行物化一个"临时 state 文件"交给 profile：watchdog 在 CDP 存活期
    # 加载 seed 并写回，执行后再按需归档到正式 alias 文件并清理临时文件。
    _RUNTIME_PREFIX = ".runtime-"

    def runtime_state_path(self, alias: object, nonce: str | None = None) -> Path:
        """本次执行专用 state 路径（含 alias 与随机 nonce，防并发互写）。"""
        safe = validate_alias(alias)
        suffix = nonce or uuid.uuid4().hex[:12]
        # 文件名仍以 .storage_state.json 结尾，沿用 .gitignore / CI secret 规则。
        name = f"{self._RUNTIME_PREFIX}{safe}-{suffix}.storage_state.json"
        path = (self.state_dir / name).resolve()
        if path.parent != self.state_dir:
            raise SessionRegistryError("运行时会话状态路径越界")
        return path

    def prepare_runtime_state(self, alias: object, data: dict | None) -> Path:
        """把正式 state（或空 state）物化到 runtime 路径，返回该路径。"""
        path = self.runtime_state_path(alias)
        payload = data if self._valid_state(data) else {"cookies": [], "origins": []}
        self._atomic_write(path, payload)
        logger.info("命名会话运行时状态已准备 alias=%s exists=True", alias)
        return path

    def read_runtime_state(self, path: str | Path) -> dict | None:
        """读回 watchdog 写回的 runtime state；缺失/损坏/结构非法均 None。"""
        return self._read_validated(Path(path))

    def cleanup_runtime_state(self, path: str | Path) -> None:
        """删 runtime 文件及 watchdog 旁支（.json.bak/.json.tmp），幂等。"""
        base = Path(path).resolve()
        if base.parent != self.state_dir:
            raise SessionRegistryError("运行时会话状态路径越界")
        for candidate in (
            base,
            base.with_suffix(".json.bak"),
            base.with_suffix(".json.tmp"),
        ):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.warning("命名会话运行时状态清理失败 name=%s", candidate.name)
        logger.info("命名会话运行时状态已清理 exists=False")

    def clear(self, alias: object) -> bool:
        """删除指定 alias 的 state；不存在也算成功（幂等）。"""
        path = self._path_for(alias)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("命名会话状态删除失败 alias=%s", alias)
            return False
        logger.info("命名会话状态已清理 alias=%s exists=False", alias)
        return True

    def has(self, alias: object) -> bool:
        """仅判断存在性，供日志/测试使用，不读内容。"""
        return self._path_for(alias).is_file()
