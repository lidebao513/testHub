"""SSRF 防护（H4 / M16）——纯标准库 + requests，不依赖 Django，便于单测。

设计取舍（重要）：
    本平台的**正当用途**就是测试用户自己的接口，其中大量在内网
    （192.168.x.x / 10.x.x.x / localhost）。因此不能像公网 SaaS 那样
    一律封禁私网段，否则直接废掉核心功能。

    分层策略：
    1. **始终拦截**（任何配置都拦）：非 http/https 协议、云元数据地址
       （169.254.169.254 / 100.100.100.200 / fd00:ec2::254 等）、链路本地、
       未指定地址、组播、保留段。这些没有任何合法测试用途，是凭据外泄主通道。
    2. **可配置拦截**：私网段与回环。默认放行以保留内网测试能力；把
       ``API_TESTING_SSRF_STRICT=1`` 打开即按最小权限收紧（多租户/公网部署必须打开）。
    3. **免检白名单**：``API_TESTING_SSRF_ALLOW_HOSTS``（逗号分隔，支持主机名与 CIDR）。
    4. **重定向逐跳校验**：30x 跳转后重新校验目标，防止
       ``公网URL → 302 → 169.254.169.254`` 绕过。跳数上限默认 5。
    5. **DNS 解析后校验全部 A/AAAA 记录**：任一地址落在禁段即拒绝，
       可挡住「一条记录公网、一条记录内网」的经典 rebound 手法。

残余风险：校验与 requests 实际连接之间存在 DNS rebinding 时间窗（需在
连接层 pin IP 才能彻底消除），已在整改文档登记。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

__all__ = [
    "SsrfBlocked",
    "SsrfPolicy",
    "get_policy",
    "reset_policy_cache",
    "validate_url",
    "safe_request",
    "redact_url",
    "mask_webhook_url",
    "is_url_safe",
]

DEFAULT_MAX_REDIRECTS = 5
ALLOWED_SCHEMES = frozenset({"http", "https"})

# 任何配置下都拦截：无人有正当理由在测试平台里访问这些地址
_ALWAYS_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.0.0/16",      # 链路本地 + AWS/GCP/Azure 元数据 169.254.169.254
        "168.63.129.16/32",    # Azure WireServer
        "100.100.100.200/32",  # 阿里云元数据
        "100.100.100.100/32",  # 阿里云内网 DNS/元数据旁路
        "fd00:ec2::254/128",   # AWS IPv6 元数据
        "0.0.0.0/32",
        "::/128",
        "224.0.0.0/4",         # 组播
        "240.0.0.0/4",         # 保留（含 255.255.255.255）
        "ff00::/8",
        "fe80::/10",           # IPv6 链路本地
    )
)

# 仅在 strict 模式拦截
_STRICT_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "192.0.0.0/24",
        "198.18.0.0/15",
    )
)

_STRICT_BLOCKED_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".home.arpa",
)
_STRICT_BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal"})

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# 敏感 query 参数（日志/落库前必须打码）
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "key",
        "secret",
        "sign",
        "signature",
        "timestamp",
        "token",
        "password",
        "pwd",
        "auth",
        "ticket",
    }
)


class SsrfBlocked(ValueError):
    """目标 URL 未通过 SSRF 校验。继承 ValueError，便于上层统一按参数错误处理。"""


@dataclass(frozen=True)
class SsrfPolicy:
    """SSRF 校验策略。

    strict: 是否连私网段/回环一起拦截（多租户、公网暴露场景应设为 True）。
    allow_hosts: 免检白名单，主机名（大小写不敏感）或 CIDR。
    max_redirects: 允许跟随的跳数上限。
    """

    strict: bool = False
    allow_hosts: frozenset[str] = field(default_factory=frozenset)
    max_redirects: int = DEFAULT_MAX_REDIRECTS


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_allow_hosts(raw: str) -> frozenset[str]:
    items = [s.strip().lower() for s in (raw or "").split(",")]
    return frozenset(s for s in items if s)


@lru_cache(maxsize=8)
def _policy_from_env(strict: bool, allow_hosts_raw: str, max_redirects: int) -> SsrfPolicy:
    return SsrfPolicy(
        strict=strict,
        allow_hosts=_parse_allow_hosts(allow_hosts_raw),
        max_redirects=max_redirects,
    )


def get_policy() -> SsrfPolicy:
    """从环境变量读取策略（默认非 strict，保留内网测试能力）。"""
    raw_max = os.environ.get("API_TESTING_SSRF_MAX_REDIRECTS", "").strip()
    try:
        max_redirects = max(0, int(raw_max)) if raw_max else DEFAULT_MAX_REDIRECTS
    except ValueError:
        max_redirects = DEFAULT_MAX_REDIRECTS
    return _policy_from_env(
        _env_flag("API_TESTING_SSRF_STRICT"),
        os.environ.get("API_TESTING_SSRF_ALLOW_HOSTS", ""),
        max_redirects,
    )


def reset_policy_cache() -> None:
    """测试辅助：环境变量变更后清缓存。"""
    _policy_from_env.cache_clear()


def _normalize_ip(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """把 IPv4-mapped IPv6（::ffff:169.254.169.254）还原成 IPv4 再判断。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _match_allow_entry(host: str, ips: Iterable, policy: SsrfPolicy) -> bool:
    """allow_hosts 命中判定：主机名精确匹配，或 CIDR 命中任一解析地址。"""
    if not policy.allow_hosts:
        return False
    if host in policy.allow_hosts:
        return True
    for entry in policy.allow_hosts:
        if "/" not in entry:
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        for ip in ips:
            if _normalize_ip(ip).version == net.version and _normalize_ip(ip) in net:
                return True
    return False


def _is_ip_blocked(ip, policy: SsrfPolicy) -> bool:
    normalized = _normalize_ip(ip)
    for net in _ALWAYS_BLOCKED_NETS:
        if normalized.version == net.version and normalized in net:
            return True
    if policy.strict:
        for net in _STRICT_BLOCKED_NETS:
            if normalized.version == net.version and normalized in net:
                return True
    return False


def _split_host_port(parts) -> tuple[str, int]:
    host = parts.hostname
    if not host:
        raise SsrfBlocked("目标 URL 缺少主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise SsrfBlocked(f"目标 URL 端口非法：{exc}") from exc
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, port


def _resolve(host: str, port: int) -> list:
    """解析域名，返回去重后的 IP 列表；解析失败视为不可信并拒绝。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise SsrfBlocked(
            f"目标域名无法解析，已阻止（无法确认其非内网地址）：{host}"
        ) from exc
    seen, ips = set(), []
    for info in infos:
        addr = info[4][0]
        if addr in seen:
            continue
        seen.add(addr)
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    if not ips:
        raise SsrfBlocked(f"目标域名无可用地址，已阻止：{host}")
    return ips


def validate_url(url: str, policy: SsrfPolicy | None = None,
                 resolve: bool = True) -> str:
    """校验出站 URL，通过则返回原 URL，不通过抛 SsrfBlocked。

    注意：只有 http/https，且解析到的**全部**地址都不得落在禁段。

    resolve=False 用于**写入口的静态预检**（表单/序列化器）：此时不做 DNS
    解析，只校验协议、主机名字面量与 IP 字面量，避免因 DNS 临时不可用
    把用户挡在保存之外；真正出站前仍会走 resolve=True 的完整校验。
    """
    if not url or not isinstance(url, str):
        raise SsrfBlocked("目标 URL 为空")
    policy = policy or get_policy()

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise SsrfBlocked(f"目标 URL 格式非法：{exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SsrfBlocked(
            f"仅允许 http/https 协议，已阻止 {scheme or '(无协议)'}：{redact_url(url)}"
        )

    host, port = _split_host_port(parts)
    host_l = host.lower().rstrip(".")

    if policy.strict:
        if host_l in _STRICT_BLOCKED_HOSTS or any(
            host_l.endswith(suffix) for suffix in _STRICT_BLOCKED_SUFFIXES
        ):
            if not _match_allow_entry(host_l, [], policy):
                raise SsrfBlocked(f"strict 模式下禁止访问本机/内部域名：{host}")

    if not resolve:
        # 静态预检：只对 IP 字面量做禁段判断
        try:
            literal = ipaddress.ip_address(host_l)
        except ValueError:
            return url
        if _is_ip_blocked(literal, policy) and not _match_allow_entry(
            host_l, [literal], policy
        ):
            raise SsrfBlocked(f"目标地址落在禁止网段，已阻止：{host}")
        return url

    ips = _resolve(host, port)
    if _match_allow_entry(host_l, ips, policy):
        return url

    for ip in ips:
        if _is_ip_blocked(ip, policy):
            raise SsrfBlocked(
                f"目标地址落在禁止网段（元数据/链路本地/私网），已阻止：{host} → {ip}"
            )
    return url


def is_url_safe(url: str, policy: SsrfPolicy | None = None) -> bool:
    """布尔版校验，便于串口/表单层直接复用。"""
    try:
        validate_url(url, policy)
        return True
    except SsrfBlocked:
        return False


def safe_request(method: str, url: str, *, policy: SsrfPolicy | None = None,
                 max_redirects: int | None = None, **kwargs) -> requests.Response:
    """带 SSRF 校验的 requests.request 替代品。

    - 每一跳（含重定向目标）都重新校验；
    - 303 / 301 / 302 把非 GET 降级为 GET 并丢弃 body（与浏览器一致）；
    - 超过跳数上限返回最后一跳响应，不再继续跟随。
    """
    policy = policy or get_policy()
    limit = policy.max_redirects if max_redirects is None else max(0, max_redirects)
    method = (method or "GET").upper()

    kwargs["allow_redirects"] = False
    current = url
    resp = None

    for hop in range(limit + 1):
        validate_url(current, policy)
        resp = requests.request(method, current, **kwargs)
        if resp.status_code not in _REDIRECT_CODES:
            return resp
        location = resp.headers.get("Location")
        if not location:
            return resp
        if hop >= limit:
            return resp
        nxt = urljoin(resp.url or current, location)
        if resp.status_code == 303 or (
            resp.status_code in (301, 302) and method not in ("GET", "HEAD")
        ):
            method = "GET"
            kwargs.pop("json", None)
            kwargs.pop("data", None)
        kwargs.pop("params", None)  # query 只对首跳生效
        current = nxt

    return resp


def redact_url(url: str) -> str:
    """打码 URL 中的敏感信息（userinfo 与敏感 query 值），用于日志。"""
    if not url or not isinstance(url, str):
        return "<empty>"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"

    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(
            [(k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v) for k, v in pairs]
        )

    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def mask_webhook_url(url: str) -> str:
    """webhook 地址脱敏：保留 scheme+host，路径与 query 全部打码。

    企业微信的 key、飞书的 hook uuid、钉钉的 access_token 都落在
    路径或 query 里，属完整凭据，不能落日志/落库。
    """
    if not url or not isinstance(url, str):
        return "<empty>"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme or 'http'}://{host}/***" if host else "<invalid-url>"
