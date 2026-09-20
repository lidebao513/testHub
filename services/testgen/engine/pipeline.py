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

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from core import store
from core.config import get_settings
from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from core.db import init_db
from core.enums import DEFAULT_SCOPE, FType, RunBatchState, Tag, VerifyLayer
from core.errors import EngineError, WorkspaceEscapeBlocked
from core.log import get_logger, log_extra
from engine import (
    auth_scan,
    case_gen,
    diff_tag,
    executor,
    fp_extract,
    fp_merge,
    llm_design,
    prd_ingest,
    pull,
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
class PipelineOptions:
    """一次运行的完整输入。"""

    local_path: str = ""  # 可选：纯地址通道（运行时 UI 发现）不需要代码目录
    project_name: str = ""
    project_id: int | None = None
    mode: str = "full"  # full | incremental
    base: str | None = None
    target: str | None = None
    # —— F1：取码（服务内获取被测代码）——
    # 给了仓库地址即先取码把代码准备到 local_path，再走后续阶段；
    # 未给 local_path 时按仓库名落到工作区根下（受管目录，便于只读加固）。
    repo_url: str = ""
    pull_deepen: int = 0  # 浅克隆加深的提交数（0=不加深）
    # —— F2：显式变更文件清单 ——
    # 上游（CI / 平台）已算好变更集时直接传入，**优先于**本地 git diff：
    # 服务再跑一次 diff 既慢又可能因浅克隆而失真。路径须为正斜杠相对路径。
    changed_files: list[str] = field(default_factory=list)
    prd_source: str = ""  # P2：PRD 文件路径（启用 PRD 通道时读取）
    scopes: set[str] = field(default_factory=lambda: set(DEFAULT_SCOPE))
    review_status: str = "pending"
    include_business: bool = True
    extract_pages: bool = True
    llm: semantic_enrich.EnrichOptions = field(default_factory=semantic_enrich.EnrichOptions)
    persist: bool = True
    # 测试专家系统（D3）：默认开；CLI `--expert-off` 置 False → 降级纯规则基线
    expert_mode: bool = True
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
    runtime_options: Any = None  # 运行时 UI 选项
    has_code: bool = False  # 是否有代码目录入口
    runtime_enabled: bool = False  # 是否启用运行时 UI 发现

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
            "errors": self.errors,
        }


def _emit(progress: ProgressFn | None, stage: str, **info: Any) -> None:
    if progress:
        progress(stage, info)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_batch_id() -> str:
    """执行批次号：`RUN-<yyyymmdd-HHMMSS>-<6位随机>`。

    批次是**事件**（不是产物），因此用时间 + 随机后缀而非内容指纹——
    同一秒内连跑两次也必须能区分开，否则历史会互相覆盖。
    """
    return f"RUN-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


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
    )
    result.pull_result = pulled.to_dict()
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
    if opts.persist:
        init_db()
        name = opts.project_name or _default_project_name(opts)
        source = opts.local_path or opts.target_req.base_url
        result.project_id = store.upsert_project(name, source)
    elif opts.project_id is not None:
        result.project_id = opts.project_id


# ============================================================================
# 阶段 1：扫描
# ============================================================================
def stage_scan(opts: PipelineOptions, result: PipelineResult, progress: ProgressFn | None) -> None:
    if not result.has_code:
        result.counts["files"] = 0
        return
    _emit(progress, "scan", path=opts.local_path)
    files = scan.Scanner(opts.local_path).index()
    result.files = files
    result.counts["files"] = len(files)
    log.info("扫描完成", extra=log_extra(files=len(files)))


# ============================================================================
# 阶段 2：功能点提取
# ============================================================================
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
    opts: PipelineOptions, auth_profile: auth_scan.AuthProfile | None
) -> tp_expand.ExpandContext:
    """构造测试点展开上下文（范围 / 审核门 / F8 鉴权画像）。

    为什么在组合根构造而不在 `stage_test_points` 内构造：上下文是**本次运行的显式输入**
    （范围来自 CLI/HTTP、鉴权画像来自源码扫描），在编排层组装才能一眼看清
    「这次展开用的是什么口径」；函数签名也因此不必随口径增长而膨胀。
    """
    return tp_expand.ExpandContext(
        scopes=set(opts.scopes),
        review_status=opts.review_status,
        auth_profile=auth_profile,
    )


def stage_test_points(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    ctx = build_expand_context(opts, result.auth_profile)
    fps = result.functional_points
    tag_by_fp = result.tag_by_fp or {}
    _emit(progress, "tp_expand", fp=len(fps))
    tps = tp_expand.expand_all(fps, ctx, tag_by_fp=tag_by_fp)
    result.test_points = tps
    result.counts["test_points"] = len(tps)
    result.scope_summary = tp_expand.scope_summary(tps)
    result.tag_summary = _count_by(tps, lambda t: t.tag)


# ============================================================================
# 阶段 5：语义增强
# ============================================================================
def stage_enrich(
    opts: PipelineOptions,
    result: PipelineResult,
    progress: ProgressFn | None,
) -> None:
    tps = result.test_points
    fps = result.functional_points
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
    for page in result.runtime_ui.reachable_pages():
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
    if added_tps:
        result.test_points = [*result.test_points, *added_tps]
    result.counts["expert_page_tps"] = len(added_tps)
    result.counts["expert_page_rejected"] = rejected
    result.expert_summary = {
        "enabled": True,
        "channel": "url",
        "expert": "PageExpert",
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
    result.run_batch_id = _new_batch_id()
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
) -> dict[str, Any]:
    """对**已有项目**的用例执行验证（不重新生成）。

    用途：用例已落库后，被测环境升级 / 部署 / 配置变更，跑一次真实验证，得到
    pass / fail / error / skipped 结论并落库（F12），无需重新生成用例。
    等价于 CLI `testgen execute --project <id>` 与服务端 `POST /api/v1/execute`
    （带 `project_id` 的调用）。执行策略与 `stage_execute` 完全一致（同一套 `_build_exec_options`）。
    """
    opts = opts or default_options()
    rows = store.list_cases(project_id, include_obsolete=False)
    if not rows:
        raise EngineError(f"项目 {project_id} 没有可执行的用例；请先生成用例")
    cases = [_case_spec_from_row(r) for r in rows]
    ro = _runtime_options_for_execute(opts)
    exec_options = _build_exec_options(opts, ro, project_id)
    batch_id = _new_batch_id()
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
# 阶段 7：落库（幂等）
# ============================================================================
def _persist_without_db(
    result: PipelineResult, tps: list[TestPoint], cases: list[CaseSpec]
) -> None:
    result.case_stats = {"created": len(cases), "updated": 0, "reused": 0, "obsolete": 0}
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
    _emit(progress, "persist", project_id=pid)
    with store.connect() as conn:
        init_db(conn)
        store.replace_functional_points(pid, fps, conn=conn)
        store.replace_test_points(pid, tps, conn=conn)
        fp_rows = store.fp_row_map(pid, conn)
        for case in cases:
            case.fp_row_id = fp_rows.get(case.fp_contract_id)
        result.case_stats = store.reconcile_cases(pid, cases, conn=conn)
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

    _emit(progress, "done", **result.counts)
    return result


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
