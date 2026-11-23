"""引擎 · 编排（pipeline）：把六个模块串成一条可复现的流水线。

    扫描 → 功能点提取 → 差异打标 → 测试点展开 → 语义增强 → 用例生成
         →（可选）用例执行 → 落库（含执行留痕 F12）

设计要点：
- **每个阶段一个函数**：输入输出都是显式数据，便于单测与影子双跑比对；
- **全量 / 增量同一条链路**，差异只体现在「是否传入 diff 上下文」；
- 任一阶段失败都带上下文抛出（`EngineError`），不吞异常。
- **可选扩展阶段（P2/P3）默认关闭**：由 Settings 的 flag 守卫；其中 P3 运行时
  UI 发现失败**只记错误不阻断主链路**（见 `P3_UI生成_详细设计.md` §10）。
- **所有数据库写入集中在 `stage_persist`**：执行结论（F12）必须等用例对账完成后再落库，
  否则首次运行没有 `cases` 行可回填 `last_result`。

两条入口（A1 · 见 `两条生成流程_链路梳理与补齐方案.md`）
---------------------------------------------------------
1. **代码通道**：`local_path` 指向被测代码目录 → 静态扫描 → 功能点 → 测试点 → 用例；
2. **地址通道**：`target.base_url` + 账号密码 → 运行时 UI 发现 → 功能点 → 测试点 → 用例。
   两者可**同时提供**：地址通道发现的功能点按**语义等价**（层类型对齐 + 路径归一，
   口径见 `engine/fp_merge.py`）并入静态集合（运行时优先），
   用例正文由 A2 用真实路由 / 元素 / 控制台基线富化。

   `local_path` 因此改为**可选**：既没有代码目录、也没开运行时发现时，入口直接报错
   （宁快速失败，不做一次「什么都没分析」的空跑）。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from core import store
from core.config import get_settings
from core.contracts import CaseSpec, FunctionalPoint, TestPoint, fp_id_of
from core.db import init_db
from core.enums import DEFAULT_SCOPE, FType, RunBatchState, Tag, TPType, VerifyLayer
from core.errors import EngineError, WorkspaceEscapeBlocked
from core.log import get_logger, log_extra
from engine import (
    auth_scan,
    case_gen,
    diff_tag,
    executor,
    file_impact,
    fp_extract,
    fp_merge,
    llm_design,
    prd_ingest,
    pull,
    quality,
    repo_facts,
    runtime_ui,
    scan,
    semantic_enrich,
    tp_expand,
)
from engine.expert.page_expert import (
    PageCapture,
    PageExpert,
    PageExpertOptions,
    expert_to_test_point,
)
from engine.stage_registry import REGISTRY, Stage, StageKind  # P1-1：registry 组链驱动
from output import channel_writer  # 需求1：把「代码通道 / 地址通道」各自产出的用例分别落盘独立记录


log = get_logger(__name__)

ProgressFn = Callable[[str, dict[str, Any]], None]


# ============================================================================
# 输入 / 输出
# ============================================================================
@dataclass
class TargetRequest:
    """「测试地址 + 账号密码」通道的**请求级**参数（优先级高于环境变量）。

    为什么要有请求级：环境变量只适合「一个人一台机器跑固定环境」；一旦要接入
    HTTP 调用或流水线，地址与账号必须能**按次传入**。凭证只在本对象内传递，
    **不进日志、不进产物、不落库**（与 `core.config` 同一红线）。
    """

    enabled: bool = False  # 是否启用运行时 UI 发现（等价于请求级 RUNTIME_UI_ENABLED）
    base_url: str = ""
    login_url: str = ""
    login_user: str = ""
    login_password: str = ""
    login_otp: str = ""
    routes: list[str] = field(default_factory=list)  # 显式路由（优先级最高）
    # —— 执行器（A3）——
    execute: bool = False  # 请求级开启用例执行
    allow_write: bool = False  # 是否放行写操作（默认否）
    ui_click: bool = False  # F13：UI 层是否执行真实点击（会改状态，默认否）
    # 执行器专用的被测服务地址。为什么要单独一个字段：接口层执行只需要 HTTP 地址，
    # **不该被迫打开浏览器通道**（打开就意味着要装 Playwright、要登录态、要遍历页面）。
    # 未设置时回落到运行时发现的 base_url。
    exec_url: str = ""


@dataclass
class RepoSpec:
    """多仓改造（v1.7）：一个仓库的取码/对比输入。

    `repo_key` 是项目内稳定短名（backend/frontend/shared），决定 fp_id 的 repo 分量、
    存储归属（repo_binding_id）与报告分组。缺省空串时编排层回落到单仓路径（零回归）。
    """

    repo_key: str = ""
    local_path: str = ""
    repo_url: str = ""
    base: str | None = None
    target: str | None = None
    ref: str = ""
    codeup_token: str = ""
    pull_deepen: int = 0
    changed_files: list[str] = field(default_factory=list)
    selected_paths: list[str] = field(default_factory=list)


@dataclass
class PipelineOptions:
    """一次运行的完整输入。"""

    local_path: str = ""  # 可选：纯地址通道（运行时 UI 发现）不需要代码目录
    project_name: str = ""
    # 多仓改造（v1.7）：非空时走 run_multirepo_pipeline；空列表＝旧单仓行为（零回归）。
    repo_specs: list[RepoSpec] = field(default_factory=list)
    project_id: int | None = None
    # 子 opts（由多仓编排派生）：本仓 binding id + 归属短名。
    # 缺省 None/空串时 stage_register / stage_persist 走旧单仓分支。
    repo_binding_id: int | None = None
    repo_key: str = ""
    mode: str = "full"  # full | incremental
    base: str | None = None
    target: str | None = None
    # —— F1：取码（服务内获取被测代码）——
    # 给了仓库地址即先取码把代码准备到 local_path，再走后续阶段；
    # 未给 local_path 时按仓库名落到工作区根下（受管目录，便于只读加固）。
    repo_url: str = ""
    pull_deepen: int = 0  # 浅克隆加深的提交数（0=不加深）
    # 生成链路取码凭证：与扫描链路共用仓库配置里的有效 PAT（空则回退 env CODEUP_TOKEN）
    codeup_token: str = ""
    # —— F2：显式变更文件清单 ——
    # 上游（CI / 平台）已算好变更集时直接传入，**优先于**本地 git diff：
    # 服务再跑一次 diff 既慢又可能因浅克隆而失真。路径须为正斜杠相对路径。
    changed_files: list[str] = field(default_factory=list)
    # —— 按子集生成（控制台「扫描 → 勾选范围 → 生成」）——
    # 非空的相对路径清单（文件或目录）；仅对命中的文件产出功能点/测试点，空=全量。
    selected_paths: list[str] = field(default_factory=list)
    prd_source: str = ""  # P2：PRD 文件路径（启用 PRD 通道时读取）
    scopes: set[str] = field(default_factory=lambda: set(DEFAULT_SCOPE))
    review_status: str = "pending"
    include_business: bool = True
    extract_pages: bool = True
    llm: semantic_enrich.EnrichOptions = field(default_factory=semantic_enrich.EnrichOptions)
    persist: bool = True
    # 测试专家系统（D3）：默认开；CLI `--expert-off` 置 False → 降级纯规则基线
    expert_mode: bool = True
    # 增量 + 地址通道：是否把运行时发现的页面/接口收窄到「本次变更相关」（默认关，
    # 保持 CLI/测试的既有全站语义；HTTP 入口按请求的「地址通道范围」显式开启）。
    target_runtime: bool = False
    # 地址优先入口的「轻量取码桥」：只取变更文件的**文本**供靶向字面量匹配，
    # 不落盘、不展开代码功能点。默认空 → 靶向照旧读 result.files（旧链路零回归）。
    target_sources: dict[str, str] = field(default_factory=dict)
    target_hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    write_channel_records: bool = True  # 需求1：落库后是否把各通道独立记录写到 outputs/<pid>/
    # 「URL + 账号密码」通道的请求级参数（A1）
    target_req: TargetRequest = field(default_factory=TargetRequest)

    @property
    def is_incremental(self) -> bool:
        return self.mode == "incremental"


@dataclass
class PipelineResult:
    """一次运行的结果摘要（可序列化，供 API / CLI 输出）。"""

    project_id: int | None = None
    mode: str = "full"
    source_kind: str = ""  # code / url / code+url（本次用了哪条入口）
    run_batch_id: str = ""  # F12：本次执行批次号（未执行则为空）
    counts: dict[str, int] = field(default_factory=dict)
    scope_summary: dict[str, int] = field(default_factory=dict)
    tag_summary: dict[str, int] = field(default_factory=dict)
    case_stats: dict[str, Any] = field(default_factory=dict)
    traceability: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)  # A3：执行结论汇总
    pull_result: dict[str, Any] = field(default_factory=dict)  # F1：取码结论（未取码为空）
    auth_scan: dict[str, Any] = field(default_factory=dict)  # F8：鉴权接线画像
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 产物本体（不参与 to_dict 序列化，供调用方写出文件）
    functional_points: list[FunctionalPoint] = field(default_factory=list, repr=False)
    test_points: list[TestPoint] = field(default_factory=list, repr=False)
    cases: list[CaseSpec] = field(default_factory=list, repr=False)
    prd_doc: Any = None  # P2：解析后的 PRD（PrdDoc），未启用为 None
    runtime_ui: Any = None  # P3：运行时 UI 发现结果（RuntimeUiResult），未启用为 None
    merge_conflicts: list[dict] = field(
        default_factory=list
    )  # F5：合并冲突清单（结构化，供报告标注「冲突的测试用例」）
    # 需求1：按来源拆出的「代码通道 / 地址通道」独立记录（落库前由 _partition_channels 填充）
    code_fps: list[FunctionalPoint] = field(default_factory=list, repr=False)
    url_fps: list[FunctionalPoint] = field(default_factory=list, repr=False)
    code_tps: list[TestPoint] = field(default_factory=list, repr=False)
    url_tps: list[TestPoint] = field(default_factory=list, repr=False)
    code_cases: list[CaseSpec] = field(default_factory=list, repr=False)
    url_cases: list[CaseSpec] = field(default_factory=list, repr=False)
    channel_summary: dict[str, Any] = field(
        default_factory=dict
    )  # {"code": {...}, "url": {...}, "source_kind": ...}
    # 测试专家系统：本次运行专家(URL/PageExpert)贡献摘要（供报告/CLI 输出，#274）
    expert_summary: dict[str, Any] = field(default_factory=dict)
    # P1-1：共享编排上下文（中间产物），供各 stage 经统一签名 (opts, result, progress) 读写
    files: Any = None  # 扫描结果 dict[str, SourceFile]（仅代码通道）
    auth_profile: Any = None  # F8 鉴权接线画像
    tag_by_fp: dict[str, str] = field(default_factory=dict)  # 差异打标结果
    narrow_fp_ids: set[str] = field(default_factory=set)  # 增量收窄：需收敛维度的功能点 id
    repo_facts: Any = None  # T4：仓库级事实（静态路由表 + 前端 import 边），扫描期一次性构建
    # T5：生成后自检（门禁 + 三 KPI + 三通道），由 stage_quality 填充；报告与前端只读这一份
    quality: dict[str, Any] = field(default_factory=dict)
    # T5 的下游依赖：T1 的归属证据（改动函数 → 入口功能点）与 T3 的语义维度（fp_id → 维度）。
    # 必须留在 result 上：KPI 的分母要它们，而 report 阶段已经拿不到 hunk / 源文件了。
    attributed_evidence: list[dict[str, Any]] = field(default_factory=list, repr=False)
    semantic_dims: dict[str, list[tuple[str, str]]] = field(default_factory=dict, repr=False)
    diff_hunks: dict[str, list[tuple[int, int]]] = field(
        default_factory=dict
    )  # 取码阶段算好的 hunk
    runtime_options: Any = None  # 运行时 UI 选项
    has_code: bool = False  # 是否有代码目录入口
    runtime_enabled: bool = False  # 是否启用运行时 UI 发现
    # G-4：执行后安全裁判报告（生成引擎收尾处由 hooks.post_execution_security_judge 填充）
    security_judge: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "mode": self.mode,
            "source_kind": self.source_kind,
            "run_batch_id": self.run_batch_id,
            "counts": self.counts,
            "scope_summary": self.scope_summary,
            "tag_summary": self.tag_summary,
            "case_stats": self.case_stats,
            "traceability": self.traceability,
            "execution": self.execution,
            "pull": self.pull_result,
            "auth_scan": self.auth_scan,
            "notes": self.notes,
            "merge_conflicts": self.merge_conflicts,
            "channel_summary": self.channel_summary,
            "expert": self.expert_summary,
            "security_judge": self.security_judge,
            "quality": self.quality,
            "errors": self.errors,
        }


def _emit(progress: ProgressFn | None, stage: str, **info: Any) -> None:
    if progress:
        progress(stage, info)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_batch_id(project_id: int | None = None) -> str:
    """执行批次号：`RUN-p<项目>-<yyyymmdd-HHMMSS>-<12位随机>`。

    批次是**事件**（不是产物），因此用时间 + 随机后缀而非内容指纹——
    同一秒内连跑两次也必须能区分开，否则历史会互相覆盖。

    B12：带项目前缀 + 随机扩到 12 位。原 6 位随机在跨项目共享全局主键时，
    不同项目极概率撞号会导致 run_batches 的 ON CONFLICT 把批次改挂到另一项目、
    而旧 runs 留在原项目（结论分裂）。带前缀 + 扩位把碰撞概率降到可忽略，
    且无需改表结构/迁移（复合主键方案风险过高）。
    """
    prefix = f"p{project_id}-" if project_id is not None else ""
    return f"RUN-{prefix}{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:12]}"


def _count_by(items: list[Any], key: Callable[[Any], str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = key(it)
        out[k] = out.get(k, 0) + 1
    return out


# ============================================================================
# 阶段 -1（F1）：取码——把「给个仓库地址」变成服务内能力
# ============================================================================
def repo_dir_name(url: str) -> str:
    """从仓库地址推目录名（`.../foo.git` → `foo`）；推不出则用 `target-repo`。

    公开命名：CLI 的 `pull` 子命令与本模块共用同一套推导，避免两处各写一遍
    （「同一地址推出两个目录名」是取码类工具最常见的自相矛盾）。
    """
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    name = tail[:-4] if tail.endswith(".git") else tail
    return name or "target-repo"


def _pull_local_path(opts: PipelineOptions) -> str:
    """取码目标目录：显式 `--path` 优先；否则按仓库名落到工作区根下（受管目录）。"""
    if opts.local_path:
        return opts.local_path
    return str(get_settings().workspace_root / repo_dir_name(opts.repo_url))


def stage_pull(opts: PipelineOptions, result: PipelineResult, progress: ProgressFn | None) -> None:
    """F1：取码——克隆 / 更新远端仓库到 `local_path`，并（可选）算出变更集。

    设计要点：
    - **只在给了 `--repo-url` 时执行**：本地已有目录的用法（`--path` 直接指向代码）
      完全不受影响，行为不变；
    - 成功后**回填 `opts.local_path`**，让后续阶段（扫描 / diff / 执行）无感衔接；
    - 取码得到的变更集（`base..target` / 工作区）在**未显式给 `--changed-files`** 时
      作为增量通道的变更集使用——这一步把 F1 与 F2 接成一条链，
      避免「取码算了一次 diff、打标又算一次」的重复与潜在不一致（浅克隆下两次结果可能不同）；
    - 取码失败**不吞**：写入 `result.errors` 并抛 `EngineError`（宁快速失败，
      也不要拿一个空目录跑出一条「0 功能点」的假产物）。
    """
    if not opts.repo_url:
        return
    target_path = _pull_local_path(opts)
    _emit(progress, "pull", url=pull.mask_url(opts.repo_url), path=target_path)
    pulled = pull.pull(
        opts.repo_url,
        target_path,
        base=opts.base or "",
        target=opts.target or "",
        deepen=opts.pull_deepen,
        token=opts.codeup_token,
    )
    result.pull_result = pulled.to_dict()
    result.diff_hunks = dict(pulled.diff_hunks)  # 增量收窄用（取码时同 ref 算好，避免重跑 git）
    result.counts["pull_changed_files"] = pulled.changed_count
    result.notes.append(pulled.summary_line())
    result.errors.extend(pulled.errors[:5])
    if not pulled.success:
        raise EngineError(
            f"取码失败（{pulled.status}）：{pulled.errors[0] if pulled.errors else '未知原因'}"
        )
    opts.local_path = pulled.local_path
    if pulled.changed_files and not opts.changed_files:
        opts.changed_files = list(pulled.changed_files)
        result.notes.append(
            f"增量变更集取自取码结果（{pulled.changed_count} 个文件）；"
            "如需覆盖请显式给 --changed-files"
        )


# ============================================================================
# 阶段 0：项目注册
# ============================================================================
def _default_project_name(opts: PipelineOptions) -> str:
    """项目名兜底：优先代码目录名，纯地址通道取被测主机名（便于按环境区分）。"""
    if opts.local_path:
        return Path(opts.local_path).name
    host = urlparse(opts.target_req.base_url).netloc
    return host or "url-target"


def stage_register(
    opts: PipelineOptions, result: PipelineResult, progress: ProgressFn | None = None
) -> None:
    # 多仓改造（v1.7）：子 opts 已带 project_id（父编排统一注册过），直接复用，
    # 不因每个仓再 upsert 一次 project（否则同 project_name 反复更新、且丢失共享语义）。
    if opts.project_id is not None:
        result.project_id = opts.project_id
    elif opts.persist:
        init_db()
        name = opts.project_name or _default_project_name(opts)
        source = opts.local_path or opts.target_req.base_url
        result.project_id = store.upsert_project(name, source)
    if result.project_id and opts.repo_binding_id and opts.repo_key:
        # 子 opts 的 binding 由父编排创建；这里只做「缺失时补齐」的幂等兜底，
        # 让单独跑某个子 opts（如调试）也能自洽。用返回的真实 id 覆盖，保证
        # 后续 stage_persist 归属到正确的行。
        opts.repo_binding_id = store.upsert_repo_binding(
            result.project_id,
            opts.repo_key,
            local_path=opts.local_path,
            repo_url=opts.repo_url,
            base=opts.base or "",
            target=opts.target or "",
            ref=opts.repo_specs[0].ref if opts.repo_specs else "",
        )


# ============================================================================
# 阶段 1：扫描
# ============================================================================
def stage_scan(opts: PipelineOptions, result: PipelineResult, progress: ProgressFn | None) -> None:
    if not result.has_code:
        result.counts["files"] = 0
        return
    _emit(progress, "scan", path=opts.local_path)
    files = scan.Scanner(opts.local_path).index()
    # T4：仓库级事实（静态路由表 + 前端 import 边）必须在**子集过滤之前**构建——
    # 路由表（App.tsx）与 import 方（父页面）通常不在用户勾选的 5 个文件里，过滤后再算
    # 就只剩变更文件自己，跨文件关系永远为空。
    if files:
        result.repo_facts = repo_facts.build_repo_facts(files)
        stats = dict(getattr(result.repo_facts, "stats", {}) or {})
        result.counts.update(stats)
        if stats.get("repo_route_bindings"):
            result.notes.append(
                f"仓库事实：静态路由表 {stats['repo_route_bindings']} 条绑定"
                f"（{stats.get('repo_route_files', 0)} 个路由文件）、"
                f"前端 import {stats.get('repo_import_specs', 0)} 条"
                f"（{stats.get('repo_import_files', 0)} 个文件）"
            )
    # 按子集生成：仅保留命中 selected_paths 的文件（精确匹配或目录前缀）。
    if opts.selected_paths:
        sel = {s.strip().replace("\\", "/").rstrip("/") for s in opts.selected_paths if s.strip()}
        if sel:
            kept = {
                p: sf
                for p, sf in files.items()
                if any(p == s or p.startswith(s + "/") for s in sel)
            }
            dropped = len(files) - len(kept)
            files = kept
            result.notes.append(
                f"按子集生成：勾选 {len(sel)} 条路径，命中 {len(files)} 个文件"
                + (f"，滤除 {dropped} 个未勾选文件" if dropped else "")
            )
    result.files = files
    result.counts["files"] = len(files)
    log.info("扫描完成", extra=log_extra(files=len(files)))


# ============================================================================
# 阶段 2：功能点提取
# ============================================================================
def _apply_repo_key_to_fps(fps: list[FunctionalPoint], repo_key: str) -> list[FunctionalPoint]:
    """多仓改造（v1.7）：给本仓功能点注入 repo 分量并重算 fp_id。

    - `repo_key` 空 → 原样返回（单仓零回归）；
    - 仅重算「标准编号」`FP-<8位hex>` 的功能点（fp_extract 产出的）；非标准编号
      （如 runtime_ui 的 `FP-` 前缀自定义编号、手工构造）保留原 id，只标记 repo_key，
      避免破坏它们的既有语义。
    - 用 fp 上现有的 (ftype, file_path, name) 复现 fp_id_of 的哈希输入，只多带 repo_key。
    """
    if not repo_key:
        return fps
    import re as _re

    pattern = _re.compile(r"^FP-[0-9a-f]{8}$")
    for fp in fps:
        fp.repo_key = repo_key
        if pattern.match(fp.fp_id):
            fp.fp_id = fp_id_of(fp.ftype, fp.file_path, fp.name, repo_key=repo_key)
    return fps


def stage_extract(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    if not result.has_code:
        result.counts["functional_points"] = len(result.functional_points)
        result.notes.append("未提供被测代码目录：本次只走「测试地址 + 账号密码」通道")
        return
    files = result.files or {}
    _emit(progress, "fp_extract", files=len(files))
    cfg = get_settings()
    extracted = fp_extract.extract_functional_points(
        files,
        include_business=opts.include_business,
        extract_pages=opts.extract_pages,
        business_extract_mode=cfg.business_extract_mode,
        business_include_dirs=cfg.business_include_dirs,
    )
    fps = extracted.functional_points
    # F5：同源去重（真实后端 vs mock server 这类「同一路由声明两遍」）。
    # 必须在测试点展开之前完成——否则重复功能点会各自展开测试点与用例（重复计数）。
    if cfg.fp_semantic_merge:
        fps, merge_stats = fp_merge.dedupe_functional_points(fps)
        result.counts["fp_merged"] = int(merge_stats["deduped"])
        line = fp_merge.summary_line(merge_stats, "同源功能点语义去重")
        if line:
            result.notes.append(line)
    # F6：业务调用图吞并——只被本文件路由调用链用到的辅助函数不产出功能点。
    # 必须**如实上报**吞并数：否则「功能点变少了」会被误读成「扫描漏了」。
    absorbed = int(getattr(extracted, "business_absorbed", 0))
    if absorbed:
        result.counts["business_absorbed"] = absorbed
        examples = list(getattr(extracted, "business_absorbed_examples", []) or [])
        tail = f"（示例：{'、'.join(examples[:3])}）" if examples else ""
        result.notes.append(
            f"业务调用图吞并（F6）：{absorbed} 个辅助函数融入其调用方，未单独产出功能点{tail}"
        )
    # 多仓改造（v1.7）：给本仓产出的功能点 ID 注入 repo 分量。
    # fp_extract 内部的 fp_id_of 不带 repo（保持单仓契约冻结），这里在提取后统一重算——
    # 用 fp 上已有的 (ftype, file_path, name) 复现同一哈希输入，仅多带 repo_key。
    # 缺省 repo_key 为空时原样返回（零回归）。tp 展开在后续 stage 基于这里的 fp.fp_id，
    # 故 tp_id 会自动带上 repo 分量；不需要改 fp_extract 的 5 处调用签名。
    fps = _apply_repo_key_to_fps(fps, opts.repo_key)
    result.counts["functional_points"] = len(fps)
    result.errors.extend(extracted.errors[:20])
    result.functional_points = fps


# ============================================================================
# 阶段 3：差异打标
# ============================================================================
def _build_diff_context(opts: PipelineOptions) -> tuple[diff_tag.DiffContext, list[str]]:
    """构造差异上下文；**只在「未指定基线」时**才降级为全量并记录。

    优先级（F2）：
    1. **显式变更文件清单**（`--changed-files` / 请求体 `changed_files`）——上游已经
       算好了，服务不再跑 git；也只有文件级打标（无行号）。
    2. 本地 `git diff base..target`——需要 base/target 可解析。

    F7：`base/target` 明确给出却不可解析（ref 失效、不是本仓库对象、git 执行失败）
    必须**立刻报错**。原实现在此处 `except (ValueError, OSError)` 后降级为全量继续跑，
    于是「所有功能点都被标成全量」这一明显错误的结果会**静默**流到下游——
    历史上 `test-20260906/07` 两个 ref 失效正是这样被吞掉的。
    """
    notes: list[str] = []
    if not opts.is_incremental:
        notes.append("全量通道：不做 diff，全部标为『全量』")
        return diff_tag.DiffContext(), notes
    if opts.changed_files:
        ctx = diff_tag.context_from_files(opts.changed_files)
        notes.append(
            f"增量通道（显式变更集）：变更文件 {len(ctx.changed_files)} 个，"
            "仅做文件级打标（无 hunk 行号，未对齐到行）"
        )
        return ctx, notes
    if not opts.base or not opts.target:
        notes.append(
            "增量通道未指定基线：无法计算变更集，本次所有功能点均标『全量』"
            "（如需增量请显式给出 --base 与 --target，或 --target "
            f"{diff_tag.WORKTREE_TARGET} 与工作区比较）"
        )
        return diff_tag.DiffContext(), notes
    try:
        ctx = diff_tag.build_context(opts.local_path, opts.base, opts.target)
    except WorkspaceEscapeBlocked as exc:
        raise EngineError(f"增量通道被阻断：{exc.message}") from exc
    except ValueError as exc:
        raise EngineError(
            f"增量通道基线不可解析：{exc}。已显式给出 --base/--target 就必须能解析——"
            "此处静默降级为全量会让『更新』标签全部失真且无告警。"
            "请核对 ref（git rev-parse --verify <ref>），"
            f"或用 --target {diff_tag.WORKTREE_TARGET} 比较当前工作区。"
        ) from exc
    except OSError as exc:
        raise EngineError(f"增量通道不可用（git 执行失败）：{exc}") from exc
    notes.append(
        f"增量通道：变更文件 {len(ctx.changed_files)} 个，hunk 覆盖 {len(ctx.hunks)} 个文件"
    )
    if not ctx.is_incremental:
        notes.append("增量通道：基线到目标之间**无差异**（变更文件 0 个）")
    return ctx, notes


def stage_tag(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """差异打标：静态来源走 `git diff` 命中判定，运行时来源（F4）走显式分支。

    F4：`runtime:<url>` 功能点不对应任何 commit，**必须另路处理**。
    早期实现让它们落进 `tag_of_rel`：拿 "runtime:http://host/x" 去和变更文件集比对，
    永远 miss → 静默全标「全量」。结果虽与应然一致，但使用者无法从产物看出
    「这些功能点为什么没被增量打标」。现在改为显式计数 + 备注。
    """
    fps = result.functional_points
    _emit(progress, "diff_tag", mode=opts.mode)
    ctx, notes = _build_diff_context(opts)
    result.notes.extend(notes)
    tag_by_fp: dict[str, str] = {}
    url_sourced = 0
    for fp in fps:
        rel = str(fp.file_path or "")
        if diff_tag.is_runtime_source(rel):
            url_sourced += 1
        tag = diff_tag.tag_of_source(rel, ctx)
        tag_by_fp[fp.fp_id] = tag
        fp.commit_ref = (opts.target or "") if tag == Tag.UPDATE.value else ""
    if url_sourced:
        result.counts["fp_url_sourced"] = url_sourced
        result.notes.append(
            f"运行时（地址通道）功能点 {url_sourced} 条：无版本概念，"
            "增量通道对它们不做 diff，一律标『全量』（如需增量请以代码通道为准）"
        )
    result.tag_by_fp = tag_by_fp
    # 增量收窄紧贴打标之后执行（同属 diff 处理，语义内聚）；不单独注册为 registry
    # stage，避免为内部编排步骤新增对外契约。全量/无 hunk 时 stage_narrow 零改动。
    stage_narrow(opts, result, progress)


# ============================================================================
# 阶段 3.5：增量收窄（函数级，由 stage_tag 末尾调用）
# ============================================================================
def _hunks_from_codeup(opts: PipelineOptions) -> dict[str, list[tuple[int, int]]]:
    """用 Codeup compare HTTP API 拿 diff hunk（本地 git 不可用时的可靠来源）。"""
    try:
        from core import store as _store
        from engine import codeup_client, incremental_narrow

        creds = _store.find_repo_creds_by_url(opts.repo_url) or {}
        org_id = str(creds.get("org_id") or "")
        rid = codeup_client.parse_repo_to_repo_id(opts.repo_url)
        client = codeup_client.CodeupClient(
            token=opts.codeup_token, org_id=org_id, verify_ssl=False, timeout=25
        )
        diffs = client.compare(rid, from_ref=opts.base or "", to_ref=opts.target or "")
        return incremental_narrow.hunks_from_compare_diffs(diffs)
    except Exception:  # noqa: BLE001 - 收窄增强，失败返回空走 B 兜底
        log.warning("增量收窄：Codeup compare 取 hunk 失败，退化为维度收敛", extra=log_extra())
        return {}


def stage_narrow(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """增量模式把功能点收窄到变更函数，避免"改 2 个函数生成 800+ 用例"。

    - 后端有行号功能点：按函数体范围与 diff hunk 交集过滤未变更函数（方案 A）；
    - 前端/无行号功能点：保留，标记收敛维度（方案 B，交给 tp_expand）。
    - 全量模式 / 拿不到 hunk：零改动（向后兼容）。
    """
    if not opts.is_incremental or not result.has_code or not result.functional_points:
        return
    _emit(progress, "narrow", fp=len(result.functional_points))

    # hunk 行号来源（按可靠性排序，见 `_resolve_hunks`）：取码阶段的 diff_hunks →
    # Codeup compare HTTP API → 本阶段本地重算。都拿不到时不收窄后端，仅前端走 B 兜底。
    hunks = _resolve_hunks(opts, result)

    from engine import incremental_narrow

    cfg = get_settings()
    narrow = incremental_narrow.narrow_to_changed_functions(
        result.functional_points,
        result.files or {},
        hunks,
        changed_files=set(opts.changed_files),
        attribution=incremental_narrow.AttributionOptions(
            enabled=cfg.change_attribution,
            max_entries=cfg.change_attribution_max_entries,
        ),
    )
    result.functional_points = narrow.kept
    result.narrow_fp_ids = set(narrow.narrow_fp_ids)
    result.counts.update(narrow.stats)
    # T5：归属证据留在 result 上——改动函数覆盖率要按「归属到入口」这条路算覆盖，
    # 只靠 counts["fp_attributed"] 一个数字算不出来是谁覆盖了谁。
    result.attributed_evidence = list(narrow.attributed_evidence)
    if narrow.attributed_evidence:
        for ev in narrow.attributed_evidence:
            result.notes.append(
                f"改动归属：{ev['file_path']}:{ev['hunk_line']} 的改动落在无功能点函数 "
                f"{ev['via']}（私有/被调用图吞并），已按同文件调用图归属到入口 {ev['entry']}"
                f"（{ev['hops']} 跳）→ {ev['fp_id']}"
            )
        result.counts["fp_attributed"] = len(narrow.attributed_evidence)
    if narrow.dropped_fps:
        result.notes.append(
            f"增量收窄（函数级）：过滤 {len(narrow.dropped_fps)} 个未变更函数功能点，"
            f"保留 {len(narrow.kept)}/{narrow.stats['fp_before']} 个"
            f"（依据 {len(hunks)} 个文件的 diff hunk 行号）"
        )
    if narrow.narrow_fp_ids:
        result.notes.append(
            f"增量收窄（维度收敛）：{len(narrow.narrow_fp_ids)} 个无行号功能点（前端/无法定位函数）"
            "仅展开核心维度，避免泛化模板膨胀"
        )


# ============================================================================
# 阶段 4：测试点展开
# ============================================================================
def stage_auth_scan(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """F8：扫源码得到鉴权接线画像，供测试点展开与用例生成共用。

    **没有代码时返回 `None`**（而不是一个「什么都没扫到」的空画像）：
    空画像的 `mode_for()` 对一切接口都返回 `ABSENT`，会把「看不到代码」误当成
    「代码里没有鉴权接线」，进而把所有安全用例判成「公开接口可访问」——
    那是比不判定更危险的假结论。返回 `None` 时下游退回 v1.0 的保守口径（按需鉴权）。
    """
    files = result.files or {}
    _emit(progress, "auth_scan", files=len(files))
    if not files:
        result.notes.append(
            "未提供被测代码：跳过鉴权接线扫描（F8），安全用例退回保守口径（按需鉴权）"
        )
        return
    profile = auth_scan.scan_auth(files)
    result.auth_profile = profile
    result.auth_scan = profile.to_dict()
    result.counts["auth_wired_files"] = len(profile.files_with_auth)
    result.notes.append(profile.summary_line())
    if profile.evidence:
        # 证据只留前若干条（`AuthProfile.evidence` 已按类限量），供人工复核
        result.notes.append("鉴权接证据（示例）：" + "；".join(profile.evidence[:3]))


def build_expand_context(
    opts: PipelineOptions,
    auth_profile: auth_scan.AuthProfile | None,
    narrow_fp_ids: set[str] | None = None,
    semantic_dims: dict[str, list[tuple[str, str]]] | None = None,
) -> tp_expand.ExpandContext:
    """构造测试点展开上下文（范围 / 审核门 / F8 鉴权画像 / 增量维度收敛 / T3 语义维度）。

    为什么在组合根构造而不在 `stage_test_points` 内构造：上下文是**本次运行的显式输入**
    （范围来自 CLI/HTTP、鉴权画像来自源码扫描），在编排层组装才能一眼看清
    「这次展开用的是什么口径」；函数签名也因此不必随口径增长而膨胀。
    """
    return tp_expand.ExpandContext(
        scopes=set(opts.scopes),
        review_status=opts.review_status,
        auth_profile=auth_profile,
        narrow_fp_ids=set(narrow_fp_ids or ()),
        semantic_dims=dict(semantic_dims or {}),
        semantic_max_dims=get_settings().semantic_max_dims,
    )


def _scan_semantics_by_file(
    files: dict[str, Any],
    hunks: dict[str, list[tuple[int, int]]],
    sem_budget: int,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[str]]]:
    """逐文件识别变更语义，返回 ``(维度表, 语义名表)``，键统一为正斜杠相对路径。

    没有源文件（被 selected_paths 过滤掉/不是文本文件）或无命中的文件不产生条目——
    「没识别出语义」不等于「没有改动」，下游对这些文件走原有维度计划。
    """
    from engine import change_semantics as cs

    dims_by_file: dict[str, list[tuple[str, str]]] = {}
    tags_by_file: dict[str, list[str]] = {}
    for rel, ranges in hunks.items():
        norm = str(rel).replace("\\", "/")
        sf = files.get(rel) or files.get(norm)
        if sf is None:
            continue
        tags = cs.detect(cs.hunk_text(sf, ranges), cs.file_kind_of(norm))
        dims = cs.dimensions_of(tags, max_dims=sem_budget)
        if not dims:
            continue
        dims_by_file[norm] = dims
        tags_by_file[norm] = cs.tag_names(tags)
    return dims_by_file, tags_by_file


def _semantic_dims_by_fp(
    opts: PipelineOptions, result: PipelineResult
) -> dict[str, list[tuple[str, str]]]:
    """T3：按本次 diff 识别变更语义，映射成各功能点的语义维度。

    只在「增量 + 代码通道 + 灰度命中该项目」时生效。识别口径见
    `engine/change_semantics.py`：只扫 hunk 行、排除整行注释、每个语义需 ≥2 个
    关键词且含强信号。返回空字典时下游按旧逻辑展开（开关 off / 无 hunk 时与基线完全一致）。
    """
    if not opts.is_incremental or not result.has_code or not result.functional_points:
        return {}
    cfg = get_settings()
    if not cfg.semantic_dims_enabled(result.project_id):
        return {}
    hunks = _resolve_hunks(opts, result)
    if not hunks:
        return {}

    # 语义维度只占 max_dims-1 个槽位：留 1 个给该来源类型的核心维度（功能可用性）
    dims_by_file, tags_by_file = _scan_semantics_by_file(
        result.files or {}, hunks, max(1, cfg.semantic_max_dims - 1)
    )
    out: dict[str, list[tuple[str, str]]] = {}
    for fp in result.functional_points:
        dims = dims_by_file.get(str(fp.file_path or "").replace("\\", "/"))
        if dims:
            out[fp.fp_id] = list(dims)
    for rel, names in tags_by_file.items():
        result.notes.append(
            f"变更语义（{rel}）：识别到 {'、'.join(names)}，"
            f"按语义补 {len(dims_by_file[rel])} 个维度"
        )
    if out:
        result.counts["semantic_fp"] = len(out)
        result.counts["semantic_dims"] = sum(len(v) for v in out.values())
    # T5：维度覆盖率的理论值要把语义维度算进去，否则「实际 12 / 理论 13」会算出 >1
    result.semantic_dims = {k: list(v) for k, v in out.items()}
    return out


def _route_for_screen(facts: Any, rel: str) -> tuple[Any, str]:
    """无路由屏幕的取址证据：直接文件绑定 > 父页路由；都没有返回空。"""
    hit = facts.route_of_file(rel)
    if hit is not None:
        return hit, "file_bound"
    for spec in facts.importers_of(rel, exclude_router=False):
        hit = facts.route_of_file(spec.importer)
        if hit is not None:
            return hit, "parent_page"
    return None, ""


def _hint_file_bound_routes(opts: PipelineOptions, result: PipelineResult) -> None:
    """批次 B：给「路由表 import 证据已绑定」的无路由屏幕补可执行地址。

    与 `_infer_page_routes` 的分工必须说清：后者在**增量**模式把页面升级成完整路由页
    （放开 7 个地址类维度，量级大）；这里在**所有模式**只写 `route_hint`，
    `has_route` 保持 False —— 维度仍是「交互元素可用」一条，只是用例不再是
    「定位交互点：src/views/X.vue」这种无法执行的假地址。

    证据优先级：路由表直接绑定组件文件 > import 了它的父页已有路由。
    两者都没有就保持文件级口径并如实计数，绝不按文件名猜路由。
    """
    facts = result.repo_facts
    if facts is None or not result.has_code:
        return
    cfg = get_settings()
    if not cfg.route_file_hint:
        return
    direct = parent = unbound = 0
    for fp in result.functional_points:
        if fp.ftype != FType.PAGE.value or fp.has_route or fp.route_hint:
            continue
        rel = str(fp.file_path or "")
        hit, source = _route_for_screen(facts, rel)
        if hit is None:
            unbound += 1
            continue
        fp.route_hint = hit.path
        fp.route_source = source
        if source == "file_bound":
            direct += 1
        else:
            parent += 1
    if direct or parent or unbound:
        result.counts["route_file_hint"] = direct
        result.counts["route_parent_hint"] = parent
        result.counts["route_unbound_screens"] = unbound
        result.notes.append(
            f"文件级屏幕路由绑定：路由表直接命中 {direct} 条、经父页命中 {parent} 条，"
            f"仍无路由证据 {unbound} 条（保持文件级口径，不猜地址）"
        )


def _infer_page_routes(opts: PipelineOptions, result: PipelineResult) -> None:
    """T4-A：给「在 pages/ 下但本文件没写 path」的页面反查真实路由。

    只在增量 + 代码通道下生效。为什么不开全量：全量模式下每个无路由页面都会从
    「1 维交互」变成「3~7 维页面」（可达/未授权/极端视口/四种异常流），全站上百个页面
    一起放的量级变化属于对既有行为的大改（止损规则 3）；增量只处理本次改动到的少数
    页面，收益明确、影响面可控。

    推断必须被仓库路由表印证（组件名或文件名候选命中已声明的绝对路由），
    命中后写 `route_hint`（用例地址）与 `route_source=inferred`（来源可区分）。
    """
    facts = result.repo_facts
    if facts is None or not result.has_code or not opts.is_incremental:
        return
    cfg = get_settings()
    if not cfg.route_infer:
        return
    runtime_pages: list[str] = []
    ui = result.runtime_ui
    if ui is not None:
        try:
            runtime_pages = [str(p) for p in ui.reachable_pages()]
        except (AttributeError, TypeError):  # 运行时结果形态异常不影响生成主链路
            runtime_pages = []
    inferred = 0
    for fp in result.functional_points:
        if fp.ftype != FType.PAGE.value or fp.has_route:
            continue
        hit = repo_facts.infer_route(str(fp.file_path or ""), facts, runtime_pages=runtime_pages)
        if hit is None:
            continue
        fp.has_route = True
        fp.route_hint = hit.path
        fp.route_source = "inferred"
        inferred += 1
        result.notes.append(
            f"路由推断：{fp.file_path} 未在本文件声明 path，按仓库路由表反查到 {hit.path}"
            f"（依据 {hit.evidence()}），已放开该页面的地址类维度"
        )
    if inferred:
        result.counts["route_inferred"] = inferred


def _append_file_impact(  # noqa: PLR0911 - 逐个前置条件早退，比嵌套六层更好读
    opts: PipelineOptions,
    result: PipelineResult,
    tps: list[TestPoint],
) -> list[TestPoint]:
    """T4-B：前端文件级跨文件回归（import 边）。

    前端没有 AST 反向调用图，「父页面 import 了被改的子组件」在代码通道完全不可见。
    这里按 import 边补 ≤`FILE_IMPACT_MAX_TOTAL` 条文件级用例：只断言引用区域
    「能渲染、可交互、控制台无新增报错」，不断言具体元素（元素级断言需要运行时 DOM）。
    """
    if not opts.is_incremental or not result.has_code or not result.functional_points:
        return tps
    if TPType.NORMAL.value not in opts.scopes:
        return tps
    cfg = get_settings()
    if not cfg.file_impact_enabled or cfg.file_impact_max_total <= 0:
        return tps
    if result.repo_facts is None:
        return tps
    hunks = _resolve_hunks(opts, result)
    if not hunks:
        return tps

    items = file_impact.build_file_impact(
        result.repo_facts,
        hunks=hunks,
        files=result.files or {},
        fps=result.functional_points,
        max_total=cfg.file_impact_max_total,
        stats=result.counts,
    )
    result.counts["file_impact_items"] = len(items)
    if not items:
        return tps
    extra = file_impact.file_impact_test_points(items, review_status=opts.review_status)
    result.counts["file_impact_precise"] = sum(1 for i in items if i.precise)
    result.notes.append(
        f"文件级影响面：{len(items)} 个前端组件 import 了本次改动的文件，已补文件级回归用例"
        f"（上限 {cfg.file_impact_max_total}，符号级命中 {result.counts['file_impact_precise']} 条）："
        + "、".join(f"{i.importer} → {'/'.join(i.targets)}" for i in items)
    )
    return tps + extra


def _append_impact_points(
    opts: PipelineOptions,
    result: PipelineResult,
    tps: list[TestPoint],
) -> list[TestPoint]:
    """T2 影响面：为「被改函数的调用入口」补「未回归」测试点。

    只在增量 + 代码通道下生效；影响用例的行为维度归属「正常」（验的是主流程未回归），
    因此 scope 未含「正常」时同样过滤——口径与用户勾选保持一致，不搞特殊通道。
    """
    if not opts.is_incremental or not result.has_code or not result.functional_points:
        return tps
    if TPType.NORMAL.value not in opts.scopes:
        return tps
    cfg = get_settings()
    if not cfg.impact_enabled:
        return tps
    hunks = _resolve_hunks(opts, result)
    if not hunks:
        return tps

    from engine import impact

    plan = impact.build_impact_plan(
        result.files or {},
        hunks,
        result.functional_points,
        max_per_func=cfg.impact_max_per_func,
        max_total=cfg.impact_max_total,
    )
    result.counts.update(plan.stats)
    result.counts["impact_entry_coverage_pct"] = int(plan.coverage * 100)
    if plan.items:
        tail = f"，超预算截断 {plan.truncated} 个" if plan.truncated else ""
        result.notes.append(
            f"影响面回归：{len(plan.items)} 个入口受本次改动影响"
            f"（直接调用 {plan.stats['impact_direct']} / 数据契约 {plan.stats['impact_data']}），"
            f"已补影响测试点；入口覆盖 {len(plan.items)}/{plan.reachable_entries}{tail}"
        )
    elif plan.reachable_entries:
        result.notes.append(
            "影响面回归：可达入口均无可用追溯锚点（功能点缺失或归属失败），本次未新增影响用例"
        )
    return tps + impact.impact_test_points(plan)


def stage_test_points(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    # 批次 B：先给文件级屏幕补真实地址（只写 route_hint，不放开维度），再走 T4-A 增量推断
    _hint_file_bound_routes(opts, result)
    # T4-A：先给无路由页面反查真实路由（决定该页面能不能展开地址类维度），再展开
    _infer_page_routes(opts, result)
    # T3：先算语义维度（依赖本次 hunk + 源文件），再展开——顺序不能反，语义要跟着改动走
    semantic_dims = _semantic_dims_by_fp(opts, result)
    ctx = build_expand_context(opts, result.auth_profile, result.narrow_fp_ids, semantic_dims)
    fps = result.functional_points
    tag_by_fp = result.tag_by_fp or {}
    _emit(progress, "tp_expand", fp=len(fps))
    tps = tp_expand.expand_all(fps, ctx, tag_by_fp=tag_by_fp)
    # T2：影响面（被改函数的调用入口）紧随本体展开之后追加，不新增对外 stage
    tps = _append_impact_points(opts, result, tps)
    # T4-B：前端文件级跨文件兜底（import 边），再追加一类影响面用例
    tps = _append_file_impact(opts, result, tps)
    result.test_points = tps
    result.counts["test_points"] = len(tps)
    result.scope_summary = tp_expand.scope_summary(tps)
    result.tag_summary = _count_by(tps, lambda t: t.tag)


# ============================================================================
# 阶段 5：语义增强
# ============================================================================
def load_file_context(repo_url: str, fps: list[FunctionalPoint]) -> dict[str, str]:
    """第二步 C：从 describe 缓存取本次功能点所在文件的中文业务背景。

    只查本次 `fps` 涉及的 file_path（范围限定，不扫全库缓存），按 (repo_url, path) 命中；
    describe 未跑过 / 缓存缺失 / 查询异常时返回空字典，调用方零影响降级。
    describe 是**文件级**描述，作为提示词背景注入，不覆盖函数级 semantic 标题。
    """
    paths = sorted({fp.file_path for fp in fps if fp.file_path})
    if not paths:
        return {}
    try:
        cached = store.get_file_descriptions(repo_url, paths)
    except Exception:  # noqa: BLE001 - 缓存查询失败不影响生成主链路
        log.warning("describe 背景加载失败，本次不注入", extra=log_extra(repo=repo_url))
        return {}
    return {path: str(row["description"]) for path, row in cached.items() if row.get("description")}


def stage_enrich(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    tps = result.test_points
    fps = result.functional_points
    # 第二步 C：LLM 启用时把 describe 文件背景注入提示词；关闭/无缓存时零影响。
    if opts.llm.enabled:
        opts.llm.file_context = load_file_context(opts.repo_url, fps)
    _emit(progress, "semantic_enrich", enabled=opts.llm.enabled)
    enriched = semantic_enrich.enrich(tps, fps, opts.llm)
    result.test_points = enriched.test_points
    result.counts["test_points"] = len(enriched.test_points)
    result.counts["llm_added"] = len(enriched.added)
    result.counts["llm_rejected"] = len(enriched.rejected)
    result.notes.extend(enriched.notes)
    result.scope_summary = tp_expand.scope_summary(enriched.test_points)


# ============================================================================
# 阶段 5.5（P2）：PRD 通道 + LLM 用例设计
# ============================================================================
def stage_prd_ingest(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """P2：解析 PRD / OpenAPI 为结构化需求（真实实现）并并入主链路测试点。

    仅在 `prd_enabled` 且给定 `prd_source` 时生效；未对齐到功能点的需求**不臆造**
    测试点，只把提示记入 result.notes（见 prd_ingest）。
    """
    s = get_settings()
    if not (s.prd_enabled and opts.prd_source):
        return
    _emit(progress, "prd_ingest", source=opts.prd_source)
    doc = prd_ingest.ingest_prd(opts.prd_source)
    log.info("PRD 解析完成", extra=log_extra(fmt=doc.fmt, requirements=len(doc.requirements)))
    result.prd_doc = doc
    # 并入主链路
    prd_tps = prd_ingest.requirements_to_test_points(result.prd_doc, result.functional_points)
    result.notes.extend(result.prd_doc.notes)
    if not prd_tps:
        return
    result.test_points = [*result.test_points, *prd_tps]
    result.counts["prd_test_points"] = len(prd_tps)
    result.scope_summary = tp_expand.scope_summary(result.test_points)


def _design_options(settings: Any) -> llm_design.DesignOptions:
    """从全局配置构造 LLM 用例设计选项。"""
    return llm_design.DesignOptions(
        enabled=settings.llm_design_enabled,
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
        model_chain=settings.llm_model_chain,
    )


# ============================================================================
# 阶段 5.6（测试专家系统 · Phase 1 URL 通道）：PageExpert 审阅增补
# ============================================================================
def _expert_options(settings: Any) -> PageExpertOptions:
    """从全局配置构造 PageExpert 选项（D1/D3/D4）。"""
    configured = bool(
        settings.expert_api_key and settings.expert_base_url and settings.expert_model
    )
    return PageExpertOptions(
        enabled=bool(settings.expert_mode and configured),
        provider=settings.expert_provider,
        base_url=settings.expert_base_url,
        model=settings.expert_model,
        api_key=settings.expert_api_key,
        timeout=settings.expert_timeout,
        use_vision=settings.expert_vision,
        max_tps_per_page=settings.expert_max_tps_per_page,
        model_chain=settings.expert_model_chain,
    )


def _capture_from_runtime_page(page: Any, runtime_result: Any) -> PageCapture:
    """把一条已发现页面（UiPage）+ 运行时接口清单组装成 PageCapture（PageExpert 输入）。"""
    xhr = [{"method": ep.method, "path": ep.path} for ep in (runtime_result.api_endpoints or [])]
    return PageCapture(
        url=page.url,
        path=page.path,
        title=page.title,
        elements=[
            {"selector": e.selector, "kind": e.kind, "text": e.text, "visible": e.visible}
            for e in page.elements
        ],
        console_errors=list(page.console_errors),
        xhr_list=xhr,
        discovered_routes=list(runtime_result.discovered_routes or []),
    )


def _resolve_expert_area_to_fp(area: str, page_path: str, fps: list[FunctionalPoint]) -> str:
    """把专家测试点的 `area` 解析到真实功能点 fp_id（保证溯源不孤儿）。

    匹配优先级：页面路径 → 接口 `METHOD path` → UI 元素选择器（fp.name 末段 `|selector`）。
    都未命中返回空串（调用方据此 reject，绝不产生孤儿用例）。
    """
    a = (area or "").strip()
    for fp in fps:
        if fp.ftype == FType.PAGE.value and fp.name == a:
            return fp.fp_id
        if fp.ftype == FType.API.value and fp.name == a:
            return fp.fp_id
    for fp in fps:
        if fp.ftype == FType.UI.value:
            sel = fp.name.split("|", 1)[-1] if "|" in fp.name else ""
            if sel and sel == a:
                return fp.fp_id
    if a == page_path:
        for fp in fps:
            if fp.ftype == FType.PAGE.value and fp.name == page_path:
                return fp.fp_id
    return ""


def _review_one_page(
    page: Any,
    expert: PageExpert,
    fps: list[FunctionalPoint],
    result: PipelineResult,
) -> tuple[list[TestPoint], int, list[str], bool, str]:
    """专家审阅单页（提质 S1–S6），返回 (新增测试点, 拒绝数, 覆盖缺口, 是否用视觉, 视觉说明)。

    抽出独立函数以保持 `stage_expert_review` 复杂度达标；area 经护栏校验后才转契约，
    未命中真实锚点 / 未解析到功能点的进 rejected（不静默）。
    """
    cap = _capture_from_runtime_page(page, result.runtime_ui)
    res = expert.review_page(cap)
    result.notes.extend(res.notes)
    added: list[TestPoint] = []
    rejected_delta = len(res.rejected)
    for etp in res.added:
        fp_id = _resolve_expert_area_to_fp(etp.area, page.path, fps)
        if not fp_id:
            rejected_delta += 1
            continue
        fp = next((f for f in fps if f.fp_id == fp_id), None)
        if fp is None:
            rejected_delta += 1
            continue
        added.append(expert_to_test_point(etp, fp_id, fp.name, fp.module))
    return added, rejected_delta, list(res.coverage_gaps), bool(res.used_vision), res.vision_note


def _plan_expert_page(expert: Any, started: float, budget: int) -> bool:
    """页前预算判定；未超预算时把本页调用超时压进剩余预算。返回是否应停止。

    只做页前检查挡不住「单页跑 3 分钟」：模型链降级会叠加多次超时重试（实测 170s/页）。
    把 `expert.options.timeout` 动态压到剩余预算内，最坏情况只略微超出总预算。
    """
    if budget <= 0:
        return False
    elapsed = time.monotonic() - started
    if elapsed > budget:
        return True
    opts = getattr(expert, "options", None)
    if opts is not None:
        base = int(getattr(opts, "timeout", 60) or 60)
        opts.timeout = max(10, min(base, int(budget - elapsed)))
    return False


def stage_expert_review(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """测试专家系统 · URL 通道（PageExpert，S1–S6 提质）。

    对运行时发现的每个可达页面，让专家审视渲染并产出「专家增补测试点」；每条 area 经护栏
    命中真实锚点后才保留，再解析到功能点 fp_id 转成 TestPoint（origin=expert_page）。
    未启用 / 未配 LLM / 无运行时结果 → 降级纯规则，不静默（notes 说明）。
    """
    s = get_settings()
    if not (s.expert_mode and opts.expert_mode):
        result.expert_summary = {
            "enabled": False,
            "channel": "url",
            "expert": "PageExpert",
            "reason": "expert_mode 关闭（--expert-off 或配置关闭）",
        }
        return
    if result.runtime_ui is None:
        result.expert_summary = {
            "enabled": False,
            "channel": "url",
            "expert": "PageExpert",
            "reason": "未启用运行时 UI 通道（Phase 1 仅 URL 通道专家）",
        }
        result.notes.append("专家审阅跳过：未启用运行时 UI 通道（Phase 1 仅 URL 通道专家）")
        return
    expert_opts = _expert_options(s)
    if not expert_opts.enabled:
        result.expert_summary = {
            "enabled": False,
            "channel": "url",
            "expert": "PageExpert",
            "reason": "未配置 LLM（EXPERT_API_KEY/BASE_URL/MODEL 或缺省 LLM_*），已降级纯规则",
        }
        result.notes.append(
            "专家(URL)未启用：未配置 LLM（EXPERT_API_KEY/BASE_URL/MODEL 或缺省 LLM_*），"
            "已降级为纯规则用例"
        )
        return

    expert = PageExpert(expert_opts)
    fps = result.functional_points
    added_tps: list[TestPoint] = []
    rejected = 0
    coverage_gaps: list[str] = []
    pages_reviewed = 0
    vision_used = False
    vision_note = ""
    # 成本/时长护栏：每页一次大模型调用（实测 30~60s），几十页会把任务拖成「假死」。
    # 页数上限 + 总时间预算双重封顶，并在每页前上报进度（面板看得到「第 i/N 页」）。
    all_pages = list(result.runtime_ui.reachable_pages())
    max_pages = int(getattr(s, "expert_max_pages", 12) or 0)
    budget = int(getattr(s, "expert_time_budget_sec", 240) or 0)
    planned = all_pages[:max_pages] if max_pages > 0 else all_pages
    total = len(planned)
    started = time.monotonic()
    budget_exhausted = False
    for idx, page in enumerate(planned, start=1):
        if _plan_expert_page(expert, started, budget):
            budget_exhausted = True
            break
        _emit(progress, "expert_review", page=idx, total=total)
        page_added, page_rej, page_gaps, page_vis, page_vnote = _review_one_page(
            page, expert, fps, result
        )
        added_tps.extend(page_added)
        rejected += page_rej
        coverage_gaps.extend(page_gaps)
        pages_reviewed += 1
        vision_used = vision_used or page_vis
        if page_vnote:
            vision_note = page_vnote
    skipped = len(all_pages) - pages_reviewed
    if skipped > 0:
        why = "时间预算用尽" if budget_exhausted else f"页数上限 {max_pages}"
        result.notes.append(
            f"专家(URL)只审阅了 {pages_reviewed}/{len(all_pages)} 页（{why}）；"
            "剩余页面按规则基线出用例，可调 EXPERT_MAX_PAGES / EXPERT_TIME_BUDGET_SEC 放开"
        )
    if added_tps:
        result.test_points = [*result.test_points, *added_tps]
    result.counts["expert_page_tps"] = len(added_tps)
    result.counts["expert_page_rejected"] = rejected
    result.expert_summary = {
        "enabled": True,
        "channel": "url",
        "expert": "PageExpert",
        "pages_total": len(all_pages),
        "pages_planned": total,
        "pages_skipped": skipped,
        "budget_exhausted": budget_exhausted,
        "pages_reviewed": pages_reviewed,
        "tps_added": len(added_tps),
        "rejected": rejected,
        "vision_used": vision_used,
        "vision_note": vision_note,
        "coverage_gaps": coverage_gaps[:50],
    }
    if added_tps or rejected:
        result.notes.append(
            f"专家(URL)增补测试点 {len(added_tps)} 条（拒绝 {rejected} 条未命中真实锚点/未解析到功能点）"
        )


def stage_llm_design(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """P2 接入点：基于功能点 + 测试点 + PRD 上下文设计补充用例（F10b 已实现）。

    护栏 B（不静默）：开关已开但未产出任何新增用例（候选全被护栏拒绝 / LLM 返回空 /
    LLM 配置缺失）时，如实写进 `result.errors`，明确说明「本次未新增任何 LLM 用例」，
    杜绝 F10a 发现的「配了以为生效」静默陷阱。LLM 不可用或开关关闭时由 design_cases
    内部降级为规则产物，并在 notes 记录原因，不中断主链路。
    """
    if not get_settings().llm_design_enabled:
        return
    _emit(progress, "llm_design", enabled=True)
    design_opts = _design_options(get_settings())
    # 第二步 C：与 semantic_enrich 同口径，注入 describe 文件背景。
    design_opts.file_context = load_file_context(opts.repo_url, result.functional_points)
    designed = llm_design.design_cases(
        result.functional_points, result.test_points, result.prd_doc, design_opts
    )
    result.counts["llm_design_cases"] = len(designed.added)
    result.notes.extend(designed.notes)
    if designed.rejected:
        result.notes.append(f"LLM 设计拒绝 {len(designed.rejected)} 条臆造/无效候选")
    if designed.added:
        result.cases = [*result.cases, *designed.added]
        return
    # 护栏 B：开关已开但未新增任何用例 → 如实上报，绝不静默
    if design_opts.enabled:
        result.errors.append(
            "LLM 用例设计已开启但未新增任何 LLM 用例（候选全被护栏拒绝或 LLM 返回空）；"
            f"用例集仍为规则模板产物（{len(result.cases)} 条）"
        )


# ============================================================================
# 阶段 5.7（P3）：运行时 UI 发现 + 用例执行
# ============================================================================
def _static_page_paths(fps: list[FunctionalPoint]) -> list[str]:
    """路由优先级第 2 档：静态分析已提取的页面路径（形如 `/pc/tasks`）。"""
    return [
        str(fp.name)
        for fp in fps
        if fp.ftype in (FType.PAGE.value, FType.UI.value) and str(fp.name).startswith("/")
    ]


def _runtime_target(
    opts: PipelineOptions, settings: Any
) -> tuple[runtime_ui.RuntimeUiOptions, bool]:
    """合并「请求级参数」与「环境变量」得到运行时选项；返回 (选项, 是否启用)。

    优先级：**请求级 > 环境变量**。请求级只覆盖**非空**字段，避免把环境里的其它配置抹掉。
    启用却发现没有被测地址时直接报错——静默跳过会产出一份「看起来有 UI 用例但全是登录页」
    的假产物，比报错危险得多。
    """
    options = runtime_ui.options_from_settings(settings)
    req = opts.target_req
    for name in ("base_url", "login_url", "login_user", "login_password", "login_otp"):
        value = str(getattr(req, name, "") or "")
        if value:
            setattr(options, name, value)
    if req.routes:
        options.routes = [*req.routes, *options.routes]
    enabled = bool(req.enabled or settings.runtime_ui_enabled)
    if enabled and not options.base_url:
        raise EngineError("启用运行时 UI 发现必须提供被测地址（--url / RUNTIME_BASE_URL）")
    return options, enabled


def stage_runtime_ui(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """P3：运行时浏览器 UI 发现（真实实现，里程碑 M3.1–M3.4）。

    用 Playwright 打开被测环境、以账号密码（+ 动态口令）登录、遍历路由抓取真实可交互元素，
    结果挂到 `result.runtime_ui` 并计入 counts；并把发现的 UI 功能点**并入功能点集合**
    （M3.4 + F5，运行时优先），必须在 stage_tag 之前完成，否则运行时补入的页面不参与
    测试点展开与用例生成。失败只记错误、不阻断主链路（设计 §10）。
    凭证经「请求级参数 / .env」注入，不落库、不入产物。
    """
    if not result.runtime_enabled:
        return
    runtime_options = result.runtime_options
    _emit(progress, "runtime_ui", base_url=runtime_options.base_url)
    # 测试专家系统 · S0 治本：把专家反推的菜单文字并入遍历（治本 routes=0）。
    expert_opts = _expert_options(get_settings())
    try:
        found = runtime_ui.discover_ui(
            runtime_options,
            static_paths=_static_page_paths(result.functional_points),
            expert_options=expert_opts,
        )
    except EngineError as exc:
        result.errors.append(f"运行时 UI 发现失败：{exc.message}")
        return
    result.runtime_ui = found
    result.notes.extend(f"运行时 UI：{n}" for n in found.notes)
    result.counts["runtime_ui_pages"] = len(found.pages)
    result.counts["runtime_ui_reachable"] = sum(1 for p in found.pages if p.reachable)
    result.counts["runtime_ui_elements"] = len(found.elements)
    result.counts["runtime_ui_routes"] = len(found.discovered_routes)
    # M3.4：发现成功后把 UI 功能点**并入静态功能点集合**——必须在 stage_tag 之前，
    # 否则运行时补入的页面不参与测试点展开与用例生成。
    merged, stats = _merge_runtime_fps(
        result.functional_points,
        runtime_ui.to_functional_points(found),
        element_count=len(found.elements),
    )
    result.functional_points = merged
    result.counts["functional_points"] = len(merged)
    result.counts["runtime_ui_fp_added"] = int(stats["added"])
    result.counts["runtime_ui_fp_replaced"] = int(stats["replaced"])
    result.counts["runtime_ui_fp_deduped"] = int(stats["deduped"])
    result.notes.append(
        f"运行时补入 {stats['added']} 条 UI 功能点"
        f"（覆盖静态同名 {stats['replaced']} 条，语义收敛重复 {stats['deduped']} 条）"
    )
    examples = list(stats.get("examples") or [])
    if examples:
        result.notes.append("语义合并明细（示例）：" + "；".join(examples))
    result.merge_conflicts = list(stats.get("conflicts", []) or [])
    # 增量 + 地址通道：把刚并入的运行时功能点收窄到「本次变更相关」的页面/接口。
    # 与 stage_narrow 一样属于 diff 处理，不单独注册 registry stage（避免新增对外契约）。
    stage_target_runtime(opts, result, progress)


def _merge_runtime_fps(
    static_fps: list[FunctionalPoint],
    runtime_fps: list[FunctionalPoint],
    element_count: int | None = None,
) -> tuple[list[FunctionalPoint], dict[str, Any]]:
    """把运行时发现的 UI 功能点并入静态功能点集合（M3.4 + F5）。

    去重键**不是** `fp_id`：静态与运行时的 `file_path` 不同（静态是真实源码路径，
    运行时是 `runtime:<url>`），`fp_id` 必然不同，但语义上是同一个 UI 面
    （如同一路径 `/pc/tasks`）——只有按「层类型对齐 + 名称归一」才能正确判重。

    判定与保留规则见 `engine/fp_merge.py`（口径 v1.0）：同族内按归一化名称判等价，
    冲突时**运行时优先**（线上真实可达面 > 静态源码，静态可能过时）；
    解析不出语义键的项（component / business）原样保留，绝不误并。

    `element_count`：传入运行时元素数触发 G-13 量级守护（见 `merge_functional_points`）。
    """
    return fp_merge.merge_functional_points(static_fps, runtime_fps, element_count=element_count)


# 地址通道功能点的 file_path 已脱敏为 `runtime:<url>`（绝不泄露账号密码），
# 与 `fp_merge._RUNTIME_PREFIX` 同源；此处单独声明以避免跨模块依赖。
_RUNTIME_PREFIX = "runtime:"

# 运行时接口功能点的 name 形如 `GET /api/v1/threads`（方法 + 路径）。
_API_FP_NAME_RE = re.compile(r"^([A-Z]{3,7})\s+(\S+)$")


def _runtime_page_path(file_path: str) -> str:
    """从 `runtime:<url>` 取页面路径；`runtime:area::<签名>`（无 URL 归属）返回空串。"""
    rest = file_path[len(_RUNTIME_PREFIX) :] if file_path.startswith(_RUNTIME_PREFIX) else file_path
    if not rest.lower().startswith("http"):
        return ""
    try:
        return urlparse(rest).path or "/"
    except ValueError:  # pragma: no cover - urlparse 极少抛错，兜底不阻断
        return ""


def _runtime_fp_in_target(fp: FunctionalPoint, plan: Any) -> bool:
    """运行时功能点是否落在靶向内（接口按路径、页面/元素按所属页面）。

    `area::` 面板区域**没有 URL 归属**，其名称里混着 CSS 类名（`.flex.w-full` 之类），
    用关键词弱匹配会大面积误命中（实测把加班/年休假等表单区域全带进来），故一律不纳入
    靶向；需要它们时把「地址通道范围」改为全站巡检。
    """
    if fp.ftype == FType.API.value:
        m = _API_FP_NAME_RE.match(str(fp.name or ""))
        return bool(m and plan.match_api(m.group(2)))
    path = _runtime_page_path(str(fp.file_path or ""))
    return bool(path) and plan.match_page(path)


def _target_changed_files(opts: PipelineOptions) -> list[str]:
    """参与靶向的变更文件 = 变更集 ∩ 勾选子集（没勾选时用全量变更集）。"""
    changed = [str(c).replace("\\", "/") for c in (opts.changed_files or [])]
    sel = [
        s.strip().replace("\\", "/").rstrip("/") for s in (opts.selected_paths or []) if s.strip()
    ]
    if not sel:
        return changed
    return [c for c in changed if any(c == s or c.startswith(s + "/") for s in sel)]


def _resolve_hunks(opts: PipelineOptions, result: PipelineResult) -> dict[str, Any]:
    """取增量 hunk：取码结果 → Codeup compare → 本地重算；成功即回写缓存。

    三级顺序的原因：sidecar 进程里 `git diff` 可能因浅克隆/工作区状态返回空，
    而 Codeup compare 的 unified diff 稳定可用；反过来本地重算最便宜。
    """
    hunks: dict[str, Any] = dict(result.diff_hunks or {})
    if hunks:
        return hunks
    if opts.base and opts.target and opts.repo_url and opts.codeup_token:
        hunks = _hunks_from_codeup(opts)
    if not hunks and opts.base and opts.target and opts.local_path:
        try:
            hunks = diff_tag.compute_diff_hunks(opts.local_path, opts.base, opts.target)
        except Exception:  # noqa: BLE001 - 取 hunk 是增强，任何失败都不得阻断主链路
            log.warning("增量 hunk：无法计算 diff hunk，退化为全文处理", extra=log_extra())
            hunks = {}
    if hunks:
        # 回写缓存：增量收窄（stage_narrow）与变更靶向共用同一份 hunk，避免重复取码。
        result.diff_hunks = dict(hunks)
    return hunks


def _build_target_plan(opts: PipelineOptions, result: PipelineResult) -> Any:
    """构造变更靶向计划（变更文件 × diff hunk × 运行时发现的页面/接口）。"""
    from engine import change_targeting

    found = result.runtime_ui
    pages = [
        p.path
        for p in (found.reachable_pages() if found is not None else [])
        if str(p.path or "").startswith("/")
    ]
    endpoints = [(e.method, e.path) for e in (found.api_endpoints if found is not None else [])]
    # 轻量取码桥：地址优先入口只取变更文件的文本供字面量匹配（接口目标），
    # 优先级高于 result.files（纯地址下为空）。未注入时完全走旧口径。
    sources = opts.target_sources or result.files or {}
    hunks = opts.target_hunks or _resolve_hunks(opts, result)
    return change_targeting.build_target_plan(
        changed_files=_target_changed_files(opts),
        hunks=hunks,
        sources=sources,
        api_endpoints=endpoints,
        page_paths=pages,
    )


def stage_target_runtime(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """增量 + 地址通道：把运行时功能点收窄到「本次变更相关」的页面/接口。

    背景：增量模式只约束代码通道；一旦同时给了被测地址，运行时通道会把整站扫一遍，
    产出几百条与变更无关的用例（实测「改 5 个文件出 294 条」）。本阶段用
    `engine/change_targeting` 把变更映射成目标集合，剔除无关的页面/接口/元素。

    只在 `opts.target_runtime and opts.is_incremental` 时生效；计划为空（改的是样式/
    配置等无可映射目标）时同样剔除全部 URL 功能点，并如实写 notes，绝不静默。
    """
    if not (opts.target_runtime and opts.is_incremental):
        return
    if result.runtime_ui is None or not result.functional_points:
        return
    _emit(progress, "target_runtime", fp=len(result.functional_points))
    plan = _build_target_plan(opts, result)
    kept: list[FunctionalPoint] = []
    dropped = 0
    for fp in result.functional_points:
        if not str(fp.file_path or "").startswith(_RUNTIME_PREFIX):
            kept.append(fp)
            continue
        if _runtime_fp_in_target(fp, plan):
            kept.append(fp)
        else:
            dropped += 1
    result.functional_points = kept
    result.counts["functional_points"] = len(kept)
    result.counts["runtime_fp_targeted"] = sum(
        1 for fp in kept if str(fp.file_path or "").startswith(_RUNTIME_PREFIX)
    )
    result.counts["runtime_fp_skipped_by_target"] = dropped
    result.notes.extend(plan.notes)
    if dropped:
        result.notes.append(
            f"变更靶向：保留 {result.counts['runtime_fp_targeted']} 条与本次变更相关的运行时功能点，"
            f"剔除 {dropped} 条无关页面/接口/元素"
            "（如需全站巡检，把「地址通道范围」改为全站巡检）"
        )


def stage_partition_channels(
    opts: PipelineOptions, result: PipelineResult, progress: ProgressFn | None = None
) -> None:
    """按来源把合并后的产物拆回「代码通道 / 地址通道」两组（需求1：分别保存记录）。

    拆分依据（单一真值源）：功能点的 `file_path`——地址通道发现的项以 `runtime:` 前缀开头，
    其余为代码通道。合并（F5）已把同名项收敛为一条并保留存活方，故不会出现「同一条 FP
    既属 code 又属 url」的歧义：存活 FP 归属哪条通道，它派生出的 TP / Case 就归哪条通道。

    测试点 / 用例按 `fp_contract_id` 指回的「存活功能点」归属通道：
    - TP：看 `tp.fp_contract_id` 落在 code / url 哪组 fp_id 集合；
    - Case：看 `case.fp_contract_id` 落在哪组。
    纯代码 / 纯地址运行：对应通道为空组（仅保留非空通道的独立记录，见 channel_writer）。
    """
    fps = result.functional_points
    code_fp_ids = {fp.fp_id for fp in fps if not fp.file_path.startswith(_RUNTIME_PREFIX)}
    url_fp_ids = {fp.fp_id for fp in fps if fp.file_path.startswith(_RUNTIME_PREFIX)}
    result.code_fps = [fp for fp in fps if fp.fp_id in code_fp_ids]
    result.url_fps = [fp for fp in fps if fp.fp_id in url_fp_ids]

    tps = result.test_points
    result.code_tps = [tp for tp in tps if tp.fp_contract_id in code_fp_ids]
    result.url_tps = [tp for tp in tps if tp.fp_contract_id in url_fp_ids]

    cases = result.cases
    result.code_cases = [c for c in cases if c.fp_contract_id in code_fp_ids]
    result.url_cases = [c for c in cases if c.fp_contract_id in url_fp_ids]

    result.channel_summary = {
        "code": {
            "fp": len(result.code_fps),
            "tp": len(result.code_tps),
            "case": len(result.code_cases),
        },
        "url": {
            "fp": len(result.url_fps),
            "tp": len(result.url_tps),
            "case": len(result.url_cases),
        },
        "source_kind": result.source_kind,
    }


def _screenshot_dir(project_id: int | None) -> str:
    """UI 失败截图目录（`outputs/<pid>/screenshots/`）；无项目 ID 时不落盘。

    为什么不落临时目录：截图的唯一价值是「事后复盘」，临时目录会被清掉；
    落到产物目录下才能随报告一起交付。项目 ID 缺失时返回空串（`ui_executor`
    据此跳过截图，而不是写到某个不明所以的位置）。
    """
    if not project_id:
        return ""
    return str(get_settings().output_dir / str(project_id) / "screenshots")


def stage_execute(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """P3：执行已生成的用例（A3 接口层 + F13 UI 层均**真实执行**）。

    地址与凭证取「请求级 > 环境变量」同一套优先级：接口层执行用的是**被测服务地址**
    （`RUNTIME_BASE_URL`），不是登录页——登录页是浏览器通道才需要的概念。

    F13：UI 层用例由 `executor` 交给 `ui_executor` 用真浏览器执行——因此这里必须把
    **浏览器通道选项**（channel / headless / 截图目录 / 登录凭证 / 是否真实点击）一并传入，
    否则 UI 会话建不起来，UI 用例会全部如实 `skipped`（这是「配了却没生效」的典型形态，
    必须避免）。`ui_click` 默认关：真实点击会改被测环境状态，与接口层 `allow_write` 同思路。

    F12：本阶段只**产出**执行结论（含批次号与起止时间）并挂到 `result.execution`；
    真正落库在 `stage_persist`——必须等用例对账完成、`cases` 行就位后才能回填 `last_result`，
    否则首次运行「无处可写」。执行基础设施失败（如未装 requests）也会留下 `failed` 批次，**不静默**。
    """
    s = get_settings()
    if not (s.executor_enabled or opts.target_req.execute):
        return
    runtime_options = result.runtime_options
    _emit(progress, "execute", cases=len(result.cases))
    result.run_batch_id = _new_batch_id(result.project_id)
    started = _now_iso()
    exec_options = _build_exec_options(opts, runtime_options, result.project_id)
    try:
        summary = executor.execute_all(result.cases, exec_options)
    except EngineError as exc:
        # 批次留痕：执行没跑起来也要可见（state=failed），否则「开关开了却什么都没发生」
        result.execution = _failed_execution(
            result.run_batch_id, len(result.cases), started, exc.message
        )
        result.notes.append(f"执行批次 {result.run_batch_id} 标记为 failed：{exc.message}")
        result.errors.append(f"用例执行失败：{exc.message}")
        return
    _finalize_execution(result, summary, started)


def _build_exec_options(
    opts: PipelineOptions,
    runtime_options: runtime_ui.RuntimeUiOptions,
    project_id: int | None,
) -> executor.ExecutorOptions:
    """把流水线选项映射为执行器选项（stage_execute 与 run_execution 共用，避免两套分支）。"""
    s = get_settings()
    return executor.ExecutorOptions(
        base_url=opts.target_req.exec_url or runtime_options.base_url,
        auth_token=runtime_options.auth_token or s.runtime_auth_token,
        headless=runtime_options.headless,
        timeout=runtime_options.timeout,
        budget_seconds=float(s.exec_budget_seconds or 0),
        allow_write=bool(opts.target_req.allow_write or s.executor_allow_write),
        # —— F13：浏览器（UI 层）通道 ——
        # 仅在「真配置了运行时 UI（RUNTIME_UI_ENABLED=on 或指定了浏览器 channel 或显式开启）」
        # 时才启用 UI 层真实执行；否则不启动浏览器，UI 用例走「如实 skipped」分支。
        # 硬编码 True 会在无浏览器环境（测试 / 未配置部署）下尝试启动浏览器并挂起。
        ui_enabled=bool(runtime_options.channel or s.runtime_ui_enabled or opts.target_req.enabled),
        channel=runtime_options.channel,
        ui_click=opts.target_req.ui_click,
        screenshot_dir=_screenshot_dir(project_id),
        login_url=runtime_options.login_url,
        login_user=runtime_options.login_user,
        login_password=runtime_options.login_password,
        login_otp=runtime_options.login_otp,
    )


def _finalize_execution(result: PipelineResult, summary: dict[str, Any], started: str) -> None:
    """执行结论收口：补批次元信息、挂到 result、计数、备注（不与 stage_execute 重复实现）。"""
    summary.update(
        {
            "batch_id": result.run_batch_id,
            "state": RunBatchState.COMPLETED.value,
            "started_at": started,
            "finished_at": _now_iso(),
        }
    )
    result.execution = summary
    for key in ("total", "executed", "pass", "fail", "error", "skipped"):
        result.counts[f"exec_{key}"] = int(summary.get(key, 0))
    artifacts = sum(1 for r in summary.get("results", []) if r.get("screenshot_path"))
    if artifacts:
        result.counts["exec_screenshots"] = artifacts
    result.notes.append(
        f"执行结论（批次 {result.run_batch_id}）：共 {summary['total']} 条，"
        f"已执行 {summary['executed']} 条"
        f"（通过 {summary['pass']} / 失败 {summary['fail']} / 异常 {summary['error']}），"
        f"跳过 {summary['skipped']} 条（UI 层会话不可用、非 HTTP 来源或写操作未放行）"
    )


def _failed_execution(batch_id: str, total: int, started: str, error: str) -> dict[str, Any]:
    """执行基础设施失败时的批次留痕（没有任何单条结论，但批次必须可见）。

    注意键名：`error` 是**计数**（与 `executor.summarize` 同名），批次级错误文案用
    `batch_error`——两者同名会互相覆盖，是最隐蔽的一类「数据看起来对但其实错」。
    """
    return {
        "batch_id": batch_id,
        "state": RunBatchState.FAILED.value,
        "batch_error": error,
        "results": [],
        "total": total,
        "executed": 0,
        "pass": 0,
        "fail": 0,
        "error": 0,
        "skipped": 0,
        "started_at": started,
        "finished_at": _now_iso(),
    }


# ============================================================================
# 项目级执行（G-1）：对已有用例重新验证（不重新生成）
# ============================================================================
def _case_spec_from_row(row: dict[str, Any]) -> CaseSpec:
    """把 `cases` 表一行还原为 `CaseSpec`（执行器只认 CaseSpec）。

    只取执行所需字段；`steps` / `doc_steps` 在 `store.list_cases` 已被 JSON 解析为列表。
    """
    return CaseSpec(
        tc_no=str(row.get("tc_no") or ""),
        title=str(row.get("title") or ""),
        ctype=str(row.get("ctype") or "api"),
        steps=list(row.get("steps") or []),
        module=str(row.get("module") or ""),
        case_type=str(row.get("case_type") or "正常"),
        priority=str(row.get("priority") or "P2"),
        precondition=str(row.get("precondition") or ""),
        doc_steps=list(row.get("doc_steps") or []),
        tp_id=str(row.get("tp_id") or ""),
        fp_contract_id=str(row.get("fp_contract_id") or ""),
        fp_row_id=row.get("fp_row_id"),
        test_type=str(row.get("test_type") or "全量"),
        status=str(row.get("status") or "generated"),
        version=int(row.get("version") or 1),
        coverage_role=str(row.get("coverage_role") or ""),
    )


def _runtime_options_for_execute(opts: PipelineOptions) -> runtime_ui.RuntimeUiOptions:
    """为「仅执行」构造运行时选项：从环境配置起手，再用请求级参数覆盖。

    与 `_runtime_target` 的区别：不强制 `enabled → 必须有 base_url`——仅执行模式下
    UI 通道是否启用由 `ui_enabled` 透传给执行器，会话建不起来时 UI 用例会自动如实 skipped，
    不应因为没开浏览器就报错阻断整次执行。
    """
    s = get_settings()
    ro = runtime_ui.options_from_settings(s)
    req = opts.target_req
    for name in ("base_url", "login_url", "login_user", "login_password", "login_otp"):
        value = str(getattr(req, name, "") or "")
        if value:
            setattr(ro, name, value)
    # 接口层执行专用地址优先于运行时发现的 base_url
    if req.exec_url:
        ro.base_url = req.exec_url
    # 注：UI 通道是否启用由 `_build_exec_options` 据 `opts.target_req.enabled`
    # / `s.runtime_ui_enabled` 推导（RuntimeUiOptions 无 enabled 字段），
    # 会话建不起来时 UI 用例会如实 skipped，不在此重复设置。
    return ro


def run_execution(
    project_id: int,
    opts: PipelineOptions | None = None,
    *,
    progress: ProgressFn | None = None,
    tp_ids: list[str] | None = None,
) -> dict[str, Any]:
    """对**已有项目**的用例执行验证（不重新生成）。

    用途：用例已落库后，被测环境升级 / 部署 / 配置变更，跑一次真实验证，得到
    pass / fail / error / skipped 结论并落库（F12），无需重新生成用例。
    等价于 CLI `testgen execute --project <id>` 与服务端 `POST /api/v1/execute`
    （带 `project_id` 的调用）。执行策略与 `stage_execute` 完全一致（同一套 `_build_exec_options`）。

    `tp_ids` 非空时只重跑这些测试点对应的用例（用于 flaky 重跑治理）。
    """
    opts = opts or default_options()
    rows = store.list_cases(project_id, include_obsolete=False)
    if not rows:
        raise EngineError(f"项目 {project_id} 没有可执行的用例；请先生成用例")
    cases = [_case_spec_from_row(r) for r in rows]
    if tp_ids:
        wanted = set(tp_ids)
        cases = [c for c in cases if c.tp_id in wanted]
        if not cases:
            raise EngineError(f"项目 {project_id} 在给定 tp_ids 范围内没有可执行的用例")
    ro = _runtime_options_for_execute(opts)
    exec_options = _build_exec_options(opts, ro, project_id)
    batch_id = _new_batch_id(project_id)
    started = _now_iso()
    _emit(progress, "execute", cases=len(cases))
    try:
        summary = executor.execute_all(cases, exec_options)
    except EngineError as exc:
        # 基础设施失败也要留痕（state=failed），否则「开了却什么都没发生」
        summary = _failed_execution(batch_id, len(cases), started, exc.message)
        store.record_execution(
            project_id, summary, mode=opts.mode, source_kind="execute", conn=None
        )
        raise
    summary.update(
        {
            "batch_id": batch_id,
            "state": RunBatchState.COMPLETED.value,
            "started_at": started,
            "finished_at": _now_iso(),
        }
    )
    # G-4：执行引擎成功路径收尾 → 执行后安全裁判（fail-closed、非阻断）
    try:
        from engine import hooks as _hooks

        summary["security_judge"] = _hooks.post_execution_security_judge(cases=cases).to_dict()
    except Exception:  # noqa: BLE001 钩子异常绝不阻断主链路（fail-closed）
        summary["security_judge"] = {
            "verdict": "inconclusive",
            "error": "security judge hook failed",
        }
    written = store.record_execution(
        project_id, summary, mode=opts.mode, source_kind="execute", conn=None
    )
    _emit(
        progress,
        "done",
        exec_total=summary["total"],
        exec_pass=summary["pass"],
        exec_fail=summary["fail"],
        exec_error=summary["error"],
        exec_skipped=summary["skipped"],
    )
    summary["runs_written"] = int(written.get("runs", 0))
    summary["cases_backfilled"] = int(written.get("cases", 0))
    return summary


# ============================================================================
# 阶段 6：用例生成
# ============================================================================
def stage_cases(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    tps = result.test_points
    # 按 tp_id 去重（保留首条）：tp_id=md5(fp_id|category|area|method|dimension|ordinal)[:8]
    # 在真实代码下存在生日碰撞，不去重会让 generate_cases 产出重复 tp_id 的用例，
    # 触发 reconcile_cases 的 ContractViolation（设计约定「在生成侧修正编号，而非在落库去重」）。
    # 去重放在生成用例之前，使 cases 的 tp_id 天然唯一、下游对账守卫通过。
    _seen: set[str] = set()
    _deduped: list[TestPoint] = []
    for _tp in tps:
        if _tp.tp_id in _seen:
            continue
        _seen.add(_tp.tp_id)
        _deduped.append(_tp)
    tps = _deduped
    result.test_points = tps
    auth_profile = result.auth_profile
    _emit(progress, "case_gen", tp=len(tps))
    s = get_settings()
    # 已有 UI 覆盖的模块集合：用于判定接口层用例是否仅为「补充」
    ui_modules = {
        fp.module
        for fp in result.functional_points
        if fp.ftype in (FType.PAGE.value, FType.COMPONENT.value, FType.UI.value)
    }
    # A2：运行时发现的页面细节 → 用例正文富化（未启用运行时通道时为空字典，行为不变）
    runtime_index = runtime_ui.to_runtime_index(result.runtime_ui)
    cases = case_gen.generate_cases(
        tps,
        ui_modules=ui_modules,
        strategy=s.layer_strategy,
        drop_supplement=s.drop_supplement_cases,
        runtime_index=runtime_index,
        auth_profile=auth_profile,  # F8：安全用例期望值来自代码事实
    )
    result.counts["cases_with_runtime_detail"] = sum(
        1 for c in cases if c.steps and c.steps[0].get("runtime")
    )
    # UI 优先排序：UI 层用例置于接口层之前，同层按模块+编号稳定排序
    layer_rank = {VerifyLayer.UI.value: 0, VerifyLayer.INTERFACE.value: 1}
    cases.sort(
        key=lambda c: (
            layer_rank.get(str(c.steps[0].get("layer")) if c.steps else "", 1),
            c.module,
            c.tc_no,
        )
    )
    result.cases = cases
    result.counts["cases"] = len(cases)
    bad = [c.tc_no for c in cases if c.missing_elements()]
    if bad:
        result.errors.append(f"{len(bad)} 条用例八要素不全（示例 {bad[:3]}）")


# ============================================================================
# 阶段 6.9（T5）：生成后自检 —— 门禁 + 三 KPI + 三通道
# ============================================================================
def stage_quality(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """生成后自检：给测试点做命题门禁，并算出三个覆盖率 KPI。

    放在 `case_gen` / `llm_design` 之后：这两步才把测试点集合最终定下来（LLM 设计会补
    虚拟测试点、`stage_cases` 会去重），早于它们算出来的 KPI 是半个盘点。
    门禁**不删测试点**，只置 `unverified` + 记原因：删掉会让人误以为「本来就没这个维度」。
    """
    tps = result.test_points
    cfg = get_settings()
    _emit(progress, "quality", tp=len(tps))
    gate = quality.apply_gate(tps, enabled=cfg.quality_gate, fps=result.functional_points)
    hunks = _resolve_hunks(opts, result) if (result.has_code and opts.is_incremental) else {}
    kpis = quality.compute_kpis(
        tps,
        fps=result.functional_points,
        files=result.files or {},
        hunks=hunks,
        scopes=set(opts.scopes),
        semantic=result.semantic_dims,
        attributed=result.attributed_evidence,
        # T2/T4 的统计已并入 counts（impact_* / file_impact_*），这里直接复用同一份事实，
        # 不再重算一遍——重算必然与产点时的入参漂移，覆盖率就成了第二个真值源
        impact_stats=result.counts,
        file_impact_stats=result.counts,
    )
    channels = quality.channel_summary(tps)
    result.quality = {
        "enabled": cfg.quality_gate,
        "gate": gate,
        "kpis": kpis,
        "channels": channels,
        "summary": quality.summarize(tps),
    }
    result.counts.update(
        {
            "gate_blocked": gate["gate_blocked"],
            "gate_best_effort": gate[quality.GATE_BEST_EFFORT],
            "kpi_changed_func_pct": _pct(kpis["changed_func_coverage"]["value"]),
            "kpi_impact_entry_pct": _pct(kpis["impact_entry_coverage"]["value"]),
            "kpi_dimension_pct": _pct(kpis["dimension_coverage"]["value"]),
            # 分子/分母也进 counts：度量脚本与前端只读 counts 就能画「3/4」，
            # 不必去猜 result.quality 的嵌套结构（那结构会随 KPI 增删而变）
            "changed_func_covered": kpis["changed_func_coverage"]["covered"],
            "changed_func_total": kpis["changed_func_coverage"]["total"],
            "impact_entry_covered": kpis["impact_entry_coverage"]["covered"],
            "impact_entry_total": kpis["impact_entry_coverage"]["total"],
            "dimension_actual": kpis["dimension_coverage"]["actual"],
            "dimension_theory": kpis["dimension_coverage"]["theory"],
        }
    )
    cf = kpis["changed_func_coverage"]
    im = kpis["impact_entry_coverage"]
    dm = kpis["dimension_coverage"]
    if cfg.quality_gate:
        result.notes.append(
            f"质量门禁：{gate['quality_gated']} 条过检，拦下 {gate['gate_blocked']} 条"
            f"（无证据 {gate[quality.GATE_NO_EVIDENCE]} / 预期不可观测 "
            f"{gate[quality.GATE_NO_OBSERVABLE]} / 无来源标签 {gate[quality.GATE_NO_ORIGIN]}），"
            f"另有 {gate[quality.GATE_BEST_EFFORT]} 条需特殊环境（诚实标未验证，不算水货）"
        )
    else:
        result.notes.append("质量门禁：已关闭（QUALITY_GATE=off），测试点未做命题自检")
    result.notes.append(
        f"三 KPI：改动函数覆盖 {cf['covered']}/{cf['total']} = {_pct(cf['value'])}%；"
        f"影响入口覆盖 {im['covered']}/{im['total']} = {_pct(im['value'])}%"
        f"（后端 {im['backend']['covered']}/{im['backend']['total']}、"
        f"前端 {im['frontend']['covered']}/{im['frontend']['total']}）；"
        f"维度覆盖 {dm['actual']}/{dm['theory']} = {_pct(dm['value'])}%"
    )
    if cf["uncovered"]:
        result.notes.append(
            "改动函数未覆盖："
            + "、".join(f"{u['file']}::{u['symbol']}" for u in cf["uncovered"][:5])
        )
    result.counts["channel_body"] = channels[quality.CHANNEL_BODY]["tp"]
    result.counts["channel_impact"] = channels[quality.CHANNEL_IMPACT]["tp"]
    result.counts["channel_runtime"] = channels[quality.CHANNEL_RUNTIME]["tp"]


def _pct(value: float) -> int:
    """0~1 的比例 → 整数百分比（报告里统一用整数，避免 0.3333 这种噪声）。"""
    return round(float(value or 0) * 100)


# ============================================================================
# 阶段 7：落库（幂等）
# ============================================================================
def _persist_without_db(
    result: PipelineResult, tps: list[TestPoint], cases: list[CaseSpec]
) -> None:
    result.case_stats = {
        "created": len(cases),
        "updated": 0,
        "reused": 0,
        "obsolete": 0,
        "revived": 0,
    }
    result.traceability = case_gen.coverage_of(cases, tps)


def _persist_execution(pid: int, result: PipelineResult, conn: Any) -> None:
    """F12：把执行结论落库（逐条 `runs` + 回填 `cases.last_result` + 批次 `run_batches`）。

    必须在 `reconcile_cases` **之后**调用——`cases` 行要先就位，`last_result` 才有对象可回填。
    未执行（`result.execution` 为空）时什么都不做。
    """
    if not result.execution:
        return
    stats = store.record_execution(
        pid,
        result.execution,
        mode=result.mode,
        source_kind=result.source_kind,
        conn=conn,
    )
    if not stats["batches"]:
        return
    result.notes.append(
        f"执行留痕：批次 {result.run_batch_id} 写入 {stats['runs']} 条 runs，"
        f"回填 {stats['cases']} 条 cases.last_result"
    )


def stage_persist(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    """落库：读取 `result` 上已挂载的三层产物（功能点 / 测试点 / 用例）+ 执行留痕。"""
    fps = result.functional_points
    tps = result.test_points
    cases = result.cases
    if not (opts.persist and result.project_id):
        _persist_without_db(result, tps, cases)
        return

    pid = result.project_id
    bid = opts.repo_binding_id
    _emit(progress, "persist", project_id=pid, repo_binding_id=bid)
    with store.connect() as conn:
        init_db(conn)
        store.replace_functional_points(pid, fps, repo_binding_id=bid, conn=conn)
        store.replace_test_points(pid, tps, repo_binding_id=bid, conn=conn)
        fp_rows = store.fp_row_map(pid, conn)
        for case in cases:
            case.fp_row_id = fp_rows.get(case.fp_contract_id)
        result.case_stats = store.reconcile_cases(pid, cases, repo_binding_id=bid, conn=conn)
        result.traceability = store.traceability(pid, conn=conn)
        _persist_execution(pid, result, conn)
        store.log_change(
            pid,
            "pipeline",
            f"mode={opts.mode} fp={len(fps)} tp={len(tps)} case={len(cases)}",
            conn=conn,
        )


# ============================================================================
# 主入口
# ============================================================================
def stage_resolve_sources(
    opts: PipelineOptions, result: PipelineResult, progress: ProgressFn | None = None
) -> None:
    """判定本次运行用到哪条入口，把结果写回 `result`（has_code / runtime_options / runtime_enabled / source_kind）。

    校验规则（宁快速失败，不空跑）：
    - 给了 `local_path` 但它不是目录 → 报错（不静默当作「没有代码」）；
    - 两条入口都没有 → 报错，并给出两个可执行的修法。
    """
    s = get_settings()
    has_code = bool(opts.local_path) and Path(opts.local_path).is_dir()
    if opts.local_path and not has_code:
        raise EngineError(f"被测目录不存在：{Path(opts.local_path)}")
    runtime_options, runtime_enabled = _runtime_target(opts, s)
    if not has_code and not runtime_enabled:
        raise EngineError(
            "缺少被测来源：请提供被测代码目录（--path / local_path），"
            "或提供被测地址并启用运行时 UI 发现（--url + --runtime-ui / RUNTIME_UI_ENABLED=on）"
        )
    result.has_code = has_code
    result.runtime_options = runtime_options
    result.runtime_enabled = runtime_enabled
    result.source_kind = "+".join(
        [k for k, on in (("code", has_code), ("url", runtime_enabled)) if on]
    )


# ============================================================================
# P1-1：registry 组链驱动（去硬编码）
# ============================================================================
# 所有 stage_* 编排包装函数统一为 (opts, result, progress) 签名，并在模块加载时回注
# REGISTRY：适配层 6 项由 planned 占位填充为真实实现；能力层 11 项 fn 由「原始纯函数」
# 覆盖为「编排包装」。run_pipeline 因此退化为单一事实源驱动的纯顺序链。
_P1_STAGE_WIRING: tuple[tuple[str, StageKind, Callable[..., Any]], ...] = (
    ("pull", StageKind.ADAPTATION, stage_pull),
    ("resolve_sources", StageKind.ADAPTATION, stage_resolve_sources),
    ("register", StageKind.ADAPTATION, stage_register),
    ("scan", StageKind.ADAPTATION, stage_scan),
    ("auth_scan", StageKind.CAPABILITY, stage_auth_scan),
    ("extract", StageKind.CAPABILITY, stage_extract),
    ("runtime_ui", StageKind.CAPABILITY, stage_runtime_ui),
    ("tag", StageKind.CAPABILITY, stage_tag),
    ("tp_expand", StageKind.CAPABILITY, stage_test_points),
    ("semantic_enrich", StageKind.CAPABILITY, stage_enrich),
    ("expert_review", StageKind.CAPABILITY, stage_expert_review),
    ("prd_ingest", StageKind.CAPABILITY, stage_prd_ingest),
    ("case_gen", StageKind.CAPABILITY, stage_cases),
    ("llm_design", StageKind.CAPABILITY, stage_llm_design),
    # T5：门禁与 KPI 必须在「测试点集合已定稿」之后（llm_design 会补虚拟测试点、
    # case_gen 会去重），且在 persist 之前（报告要读 quality 快照）
    ("quality", StageKind.CAPABILITY, stage_quality),
    ("execute", StageKind.ADAPTATION, stage_execute),
    ("partition_channels", StageKind.ADAPTATION, stage_partition_channels),
    ("persist", StageKind.ADAPTATION, stage_persist),
)


def _wire_stages_to_registry() -> None:
    """把本模块 stage_* 编排包装回注 REGISTRY（P1-1 去硬编码的前置条件）。"""
    for _name, _kind, _fn in _P1_STAGE_WIRING:
        if REGISTRY.contains(_name):
            _st = REGISTRY.get(_name)
            _st.fn = _fn
            _st.kind = _kind
            _st.planned = False
        else:
            REGISTRY.register(Stage(name=_name, kind=_kind, fn=_fn, planned=False))


_wire_stages_to_registry()

# 流水线顺序（保持原编排语义）：
# resolve_sources 在 pull 后；runtime_ui 在 extract 后、tag 前（M3.4 并入须在打标前）；
# execute 在 cases 后、persist 前；partition_channels 在 persist 前。
PIPELINE_STAGE_ORDER: tuple[str, ...] = (
    "pull",
    "resolve_sources",
    "register",
    "scan",
    "auth_scan",
    "extract",
    "runtime_ui",
    "tag",
    "tp_expand",
    "semantic_enrich",
    "expert_review",
    "prd_ingest",
    "case_gen",
    "llm_design",
    "quality",
    "execute",
    "partition_channels",
    "persist",
)


def run_pipeline(
    opts: PipelineOptions,
    *,
    progress: ProgressFn | None = None,
) -> PipelineResult:
    """执行完整流水线（代码通道 / 地址通道 / 两者并用）。

    阶段顺序（经 REGISTRY 组链驱动，P1-1 去硬编码）：
      取码 → 来源解析 → 项目注册 → 扫描 → 鉴权扫描 → 功能点提取 → （运行时 UI 发现）
      → 差异打标 → 测试点展开 → 语义增强 → （专家审查）→ （PRD）→ 用例生成
      → （LLM 设计）→ （用例执行）→ 通道拆分 → 落库
    """
    result = PipelineResult(mode=opts.mode)
    for _name in PIPELINE_STAGE_ORDER:
        REGISTRY.get_fn(_name)(opts, result, progress)
    # 需求1：把各通道独立记录落盘（仅非空通道写文件）；e2e 等场景可置 False 后用自定义 base 重写出
    if opts.write_channel_records and result.project_id:
        channel_writer.write_channel_records(result.project_id, result, get_settings().output_dir)

    # G-4：生成引擎成功路径收尾 → 执行后安全裁判（fail-closed、非阻断）
    try:
        from engine import hooks as _hooks

        result.security_judge = _hooks.post_execution_security_judge(
            cases=result.cases,
            texts=[str(result.channel_summary.get("source_kind", ""))],
        ).to_dict()
    except Exception:  # noqa: BLE001 钩子异常绝不阻断主链路（fail-closed）
        result.security_judge = {
            "verdict": "inconclusive",
            "error": "security judge hook failed",
        }

    _emit(progress, "done", **result.counts)
    return result


# ============================================================================
# 多仓改造（v1.7）：逐仓子 opts + 共享 project + 结果聚合
# ============================================================================
def _derive_repo_opts(
    parent: PipelineOptions, spec: RepoSpec, project_id: int | None, binding_id: int | None
) -> PipelineOptions:
    """从父 opts + 仓库规格 + 共享 project/binding 派生一个单仓子 opts。

    子 opts 的 `repo_specs` 置空——避免 run_pipeline 内部再次进入多仓编排；
    `local_path/repo_url/base/target/changed_files/selected_paths` 用本仓值覆盖；
    `project_id/repo_binding_id/repo_key` 让 register/persist 共享容器、按仓隔离写入。
    """
    child = deepcopy(parent)
    child.local_path = spec.local_path
    child.repo_url = spec.repo_url
    child.base = spec.base
    child.target = spec.target
    child.changed_files = list(spec.changed_files)
    child.selected_paths = list(spec.selected_paths)
    child.pull_deepen = spec.pull_deepen or parent.pull_deepen
    child.codeup_token = spec.codeup_token or parent.codeup_token
    child.project_id = project_id
    child.repo_binding_id = binding_id
    child.repo_key = spec.repo_key
    child.repo_specs = []  # 子 opts 走单仓路径
    return child


def _merge_quality_facts(parent_result: PipelineResult, results: list[PipelineResult]) -> None:
    """把各仓 KPI 重算所需的事实字段并到父 result（hunks 按文件并集、其余按 key 取先者）。"""
    merged_files: dict[str, Any] = {}
    merged_hunks: dict[str, list[tuple[int, int]]] = {}
    merged_semantic: dict[str, list[tuple[str, str]]] = {}
    merged_attributed: list[dict[str, Any]] = []
    for r in results:
        for path, source_file in (r.files or {}).items():
            merged_files.setdefault(path, source_file)
        for path, spans in (r.diff_hunks or {}).items():
            target = merged_hunks.setdefault(path, [])
            for span in spans or []:
                if span not in target:
                    target.append(span)
        for fp_id, dims in (r.semantic_dims or {}).items():
            merged_semantic.setdefault(fp_id, dims)
        merged_attributed.extend(r.attributed_evidence or [])
    parent_result.files = merged_files
    parent_result.diff_hunks = merged_hunks
    parent_result.semantic_dims = merged_semantic
    parent_result.attributed_evidence = merged_attributed
    parent_result.has_code = any(r.has_code for r in results)


def _aggregate_multirepo_results(
    opts: PipelineOptions,
    parent_result: PipelineResult,
    results: list[PipelineResult],
) -> PipelineResult:
    """把逐仓子结果聚合到父结果：counts 求和、产物拼接、quality 在聚合后重算。

    多仓 quality 不能按字典合并各仓快照——顶层键相同会互相覆盖（只剩末仓），
    `kpi_*_pct` 会被 Counter 加成 300%。这里先把 KPI 重算所需的事实字段按仓合并，
    再对全量产物调用一次 `stage_quality` 重算：与单仓完全同一套口径，
    未来 KPI 增减自动生效，报告侧无需感知多仓。
    """
    parent_result.counts = dict(sum((Counter(r.counts) for r in results), Counter()))
    for attr in ("scope_summary", "tag_summary"):
        merged: Counter[str] = Counter()
        for r in results:
            merged.update(getattr(r, attr) or {})
        setattr(parent_result, attr, dict(merged))
    case_stats: Counter[str] = Counter()
    for r in results:
        case_stats.update(
            {k: int(v) for k, v in (r.case_stats or {}).items() if isinstance(v, (int, float))}
        )
    parent_result.case_stats = dict(case_stats)
    for attr in ("functional_points", "test_points", "cases"):
        setattr(parent_result, attr, [item for r in results for item in getattr(r, attr)])

    # KPI 重算所需的事实字段：按仓合并（见 _merge_quality_facts）。
    _merge_quality_facts(parent_result, results)

    # 分仓的 quality 说明由聚合版取代（数字是全项目口径，分仓版会误导读者）。
    parent_result.notes = [
        note
        for r in results
        for note in r.notes
        if not note.startswith(("质量门禁：", "三 KPI：", "改动函数未覆盖："))
    ]
    parent_result.errors = [err for r in results for err in r.errors]
    parent_result.traceability = {k: v for r in results for k, v in (r.traceability or {}).items()}

    # 在一次聚合后的全量 TP/FP 上重算：quality dict 与 counts 的 kpi_*_pct 一次性写对。
    # apply_gate 幂等（子仓已标的 unverified 不会被改回），stage_quality 只 emit + 记 note，
    # 这里的 progress 传 None 不向前端重复播报 quality 阶段。
    stage_quality(opts, parent_result, None)
    return parent_result


def run_multirepo_pipeline(
    opts: PipelineOptions,
    *,
    progress: ProgressFn | None = None,
) -> PipelineResult:
    """多仓改造（v1.7）主编排：一个 project 下对 N 个仓各跑一次完整流水线。

    与 `run_pipeline` 的关系：**不重写 18 阶段链**——每个仓派生一个单仓子 opts，
    复用同一条 `run_pipeline`，只是共享 project_id、按 repo_binding_id 隔离写入、
    产物 ID 带 repo 分量。结果聚合后返回。

    `repo_specs` 为空时直接回落 `run_pipeline`（零回归）。
    """
    specs = [s for s in opts.repo_specs if s.repo_key]
    if not specs:
        return run_pipeline(opts, progress=progress)

    result = PipelineResult(mode=opts.mode)
    # 1) 建共享 project（容器）——只注册一次。
    project_id = opts.project_id
    if project_id is None and opts.persist:
        init_db()
        name = opts.project_name or _default_project_name(opts)
        source = specs[0].local_path or specs[0].repo_url or opts.target_req.base_url
        project_id = store.upsert_project(name, source)
    result.project_id = project_id

    # 2) 为每个仓建 binding（幂等 upsert），拿到 binding id。
    results: list[PipelineResult] = []
    for spec in specs:
        binding_id: int | None = None
        if project_id is not None:
            binding_id = store.upsert_repo_binding(
                project_id,
                spec.repo_key,
                local_path=spec.local_path,
                repo_url=spec.repo_url,
                base=spec.base or "",
                target=spec.target or "",
                ref=spec.ref,
            )
        child = _derive_repo_opts(opts, spec, project_id, binding_id)
        _emit(progress, "repo", repo_key=spec.repo_key, binding_id=binding_id)
        sub = run_pipeline(child, progress=progress)
        sub.notes.insert(0, f"[{spec.repo_key}] 仓库生成完成")
        results.append(sub)

    # 3) 聚合（含 quality 重算，需用父 opts 的 scopes / mode 口径）。
    result.source_kind = "+".join(sorted({r.source_kind for r in results if r.source_kind}))
    return _aggregate_multirepo_results(opts, result, results)


def run_pipeline_auto(
    opts: PipelineOptions, *, progress: ProgressFn | None = None
) -> PipelineResult:
    """统一入口：按 opts.repo_specs 是否非空自动分派单仓 / 多仓编排。

    HTTP 同步 / 异步两条入口共用本函数，避免「一处支持多仓、另一处漏改」。
    """
    if opts.repo_specs:
        return run_multirepo_pipeline(opts, progress=progress)
    return run_pipeline(opts, progress=progress)


def default_options(local_path: str = "", *, mode: str = "full") -> PipelineOptions:
    """从环境配置构造默认选项（供 API / CLI 使用）。"""
    s = get_settings()
    return PipelineOptions(
        local_path=local_path,
        mode=mode,
        review_status="pending" if s.review_gate else "approved",
        llm=semantic_enrich.EnrichOptions(
            enabled=s.llm_enhance,
            provider=s.llm_provider,
            base_url=s.llm_base_url,
            model=s.llm_model,
            api_key=s.llm_api_key,
            timeout=s.llm_timeout,
            model_chain=s.llm_model_chain,
        ),
    )


# ============================================================================
# 统一智能输入框：解析结果 → 运行选项（A1）
# ============================================================================
def apply_auto_input(opts: PipelineOptions, parsed: Any) -> list[str]:
    """把智能输入框的解析结果填入运行选项，返回「本次实际采纳的字段名」清单。

    覆盖优先级：**显式 CLI/HTTP 参数 > 智能输入框 > 环境变量**。实现方式：调用方必须
    **先**调本函数、**后**应用显式参数——因此 `mode` / `scopes` 这类有默认值的字段
    允许被输入框覆盖（随后被显式参数再覆盖），而地址/路径只在为空时填充。
    凭证只写入内存中的选项对象，**不写日志、不入产物**。
    """
    adopted: list[str] = []
    if parsed is None:
        return adopted

    def take(field: str, attr: str, *, overwrite: bool = False) -> None:
        value = str(getattr(parsed, field, "") or "")
        if value and (overwrite or not getattr(opts, attr)):
            setattr(opts, attr, value)
            adopted.append(attr)

    take("project_name", "project_name")
    take("mode", "mode", overwrite=True)
    take("base", "base")
    take("target", "target")
    take("local_path", "local_path")
    if parsed.scopes:
        opts.scopes = set(parsed.scopes)
        adopted.append("scopes")

    # 解析结果用的是「目标环境」语义（url → base_url），此处做一次显式字段映射
    req = opts.target_req
    for parsed_field, attr in (
        ("url", "base_url"),
        ("login_url", "login_url"),
        ("user", "login_user"),
        ("password", "login_password"),
        ("otp", "login_otp"),
    ):
        value = str(getattr(parsed, parsed_field, "") or "")
        if value and not getattr(req, attr):
            setattr(req, attr, value)
            adopted.append(f"target.{attr}")
    if parsed.routes and not req.routes:
        req.routes = list(parsed.routes)
        adopted.append("target.routes")
    if req.base_url or req.login_user:
        req.enabled = True  # 给了地址/账号即视为要走地址通道
    return adopted
