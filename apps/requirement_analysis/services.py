import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

try:
    from PyPDF2 import PdfReader
except ImportError:
    from PyPDF2 import PdfFileReader as PdfReader
    
try:
    import docx
except ImportError:
    docx = None

from .models import (
    AIModelConfig,
    AIModelService,
    AnalysisTask,
    BusinessRequirement,
    ClarificationItem,
    FunctionalInventory,
    GeneratedTestCase,
    PromptConfig,
    RequirementAnalysis,
    RequirementDocument,
    UIElementDescription,
)

logger = logging.getLogger(__name__)


class DocumentProcessor:
    """文档处理服务"""
    
    @staticmethod
    def extract_text_from_pdf(file_path: str) -> str:
        """从PDF文件提取文本"""
        try:
            text = ""
            with open(file_path, 'rb') as file:
                pdf_reader = PdfReader(file)
                for page in pdf_reader.pages:
                    text += page.extract_text() + "\n"
            return text.strip()
        except Exception as e:
            logger.error(f"PDF文本提取失败: {e}")
            return f"PDF文本提取失败: {e!s}"
    
    @staticmethod
    def extract_text_from_docx(file_path: str) -> str:
        """从Word文档提取文本"""
        try:
            doc = docx.Document(file_path)
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text.strip()
        except Exception as e:
            logger.error(f"Word文档文本提取失败: {e}")
            return f"Word文档文本提取失败: {e!s}"
    
    @staticmethod
    def extract_text_from_txt(file_path: str) -> str:
        """从文本文件提取文本"""
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                return file.read().strip()
        except UnicodeDecodeError:
            try:
                with open(file_path, 'r', encoding='gbk') as file:
                    return file.read().strip()
            except Exception as e:
                logger.error(f"文本文件读取失败: {e}")
                return f"文本文件读取失败: {e!s}"
        except Exception as e:
            logger.error(f"文本文件读取失败: {e}")
            return f"文本文件读取失败: {e!s}"
    
    @classmethod
    def extract_text(cls, document: RequirementDocument) -> str:
        """根据文档类型提取文本"""
        file_path = document.file.path
        
        if document.document_type == 'pdf':
            return cls.extract_text_from_pdf(file_path)
        elif document.document_type == 'docx':
            return cls.extract_text_from_docx(file_path)
        elif document.document_type == 'txt' or document.document_type == 'md':
            return cls.extract_text_from_txt(file_path)
        else:
            return "不支持的文档类型"


class AIService:
    """AI服务类 - 模拟大模型调用"""
    
    @staticmethod
    async def analyze_requirements(text: str, document_title: str = "") -> dict[str, Any]:
        """
        先进的需求分析 - 使用新的智能分析引擎
        
        Args:
            text: 需求文档文本内容
            document_title: 文档标题
            
        Returns:
            Dict包含分析报告、结构化需求等信息
        """
        try:
            # 直接导入并使用先进分析器
            from apps.requirement_analysis.advanced_analyzer import advanced_analyzer
            
            logger.info(f"使用先进分析器分析需求，文档标题: {document_title}")
            
            # 使用先进分析器进行分析
            result = await advanced_analyzer.analyze_requirements_advanced(text, document_title)
            
            # 转换为原系统期望的格式
            analysis_report = result.get("analysis_report", "")
            structured_requirements = result.get("structured_requirements", {})
            requirements_list = structured_requirements.get("requirements", [])
            
            # 计算分析时间（模拟）
            import time
            analysis_time = time.time() % 10 + 2  # 2-12秒之间的模拟时间
            
            logger.info(f"先进需求分析完成，识别需求{len(requirements_list)}个")
            
            return {
                "analysis_report": analysis_report,
                "requirements": requirements_list,
                "requirements_count": len(requirements_list),
                "analysis_time": analysis_time,
                "quality_assessment": result.get("quality_assessment", {}),
                "risk_analysis": result.get("risk_analysis", {})
            }
            
        except Exception as e:
            logger.error(f"先进需求分析失败: {e}")
            logger.info("使用备用分析方法")
            # fallback到原来的分析逻辑
            return await AIService._fallback_analyze_requirements(text, document_title)
    
    @staticmethod
    async def _fallback_analyze_requirements(text: str, document_title: str = "") -> dict[str, Any]:
        """备用需求分析方法"""
        # 模拟AI分析过程
        await asyncio.sleep(2)
        
        # 这里应该调用真实的大模型API
        # 现在返回改进的模拟数据
        analysis_report = f"""
# 需求分析报告

## 文档概述
基于提供的需求文档"{document_title}"，共识别出以下主要需求模块和功能点。

## 主要功能模块
1. 用户管理模块
2. 数据处理模块  
3. 报告生成模块
4. 系统配置模块

## 详细需求分析
基于文档内容分析，识别出以下具体需求：

### 功能需求
- 用户认证和权限管理
- 数据录入和维护功能
- 业务流程处理
- 报表和统计功能

### 非功能需求
- 系统性能要求：响应时间 < 3秒
- 安全性要求：数据加密存储
- 可用性要求：99.5%系统可用率
- 兼容性要求：支持主流浏览器

## 风险评估
- 技术实现风险：中等
- 进度风险：低
- 资源风险：低

## 建议
建议采用敏捷开发模式，分阶段实施各功能模块。

分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        
        # 生成基础的结构化需求
        requirements = [
            {
                "requirement_id": "REQ-001",
                "requirement_name": "用户认证管理", 
                "requirement_type": "functional",
                "parent_requirement": None,
                "module": "用户管理",
                "requirement_level": "high",
                "reviewer": "admin",
                "estimated_hours": 16,
                "description": "作为一名系统用户，我希望通过用户名和密码登录系统，这样可以确保系统安全性并获得个性化服务。",
                "acceptance_criteria": "用户能够使用有效凭证成功登录系统，无效凭证登录失败，系统记录登录日志。"
            },
            {
                "requirement_id": "REQ-002",
                "requirement_name": "数据管理功能",
                "requirement_type": "functional", 
                "parent_requirement": None,
                "module": "数据管理",
                "requirement_level": "high",
                "reviewer": "admin",
                "estimated_hours": 24,
                "description": "作为一名数据操作员，我希望能够对系统数据进行增删改查操作，这样可以有效管理业务信息。",
                "acceptance_criteria": "数据操作功能正常，数据完整性得到保证，操作权限控制有效。"
            },
            {
                "requirement_id": "REQ-003",
                "requirement_name": "报表统计功能",
                "requirement_type": "functional",
                "parent_requirement": None,
                "module": "报表管理",
                "requirement_level": "medium", 
                "reviewer": "admin",
                "estimated_hours": 20,
                "description": "作为一名管理人员，我希望能够生成各类业务报表和统计图表，这样可以直观了解业务数据和趋势。",
                "acceptance_criteria": "系统能够生成多种格式的报表，数据准确，支持导出功能。"
            }
        ]
        
        return {
            "analysis_report": analysis_report,
            "requirements": requirements,
            "requirements_count": len(requirements)
        }
    
    @staticmethod
    async def generate_test_cases(requirement: BusinessRequirement, test_level: str, test_priority: str, count: int) -> list[dict[str, Any]]:
        """生成测试用例 - 大模型A"""
        # 模拟AI生成过程
        await asyncio.sleep(1)
        
        # 生成唯一case_id的辅助函数
        def generate_unique_case_id(req, base_index):
            """生成唯一的测试用例ID"""
            base_case_id = f"TC-{req.requirement_id}-{base_index:03d}"
            case_id = base_case_id
            counter = 1
            
            # 检查是否已存在，如果存在则添加后缀
            from .models import GeneratedTestCase
            while GeneratedTestCase.objects.filter(requirement=req, case_id=case_id).exists():
                case_id = f"{base_case_id}_{counter}"
                counter += 1
            
            return case_id
        
        # 获取该需求现有测试用例的数量，作为起始索引
        from .models import GeneratedTestCase
        existing_count = GeneratedTestCase.objects.filter(requirement=requirement).count()
        
        # 根据需求生成测试用例
        test_cases = []
        for i in range(count):
            case_id = generate_unique_case_id(requirement, existing_count + i + 1)
            
            # 根据需求类型生成不同的测试用例
            if "登录" in requirement.requirement_name:
                test_cases.append({
                    "case_id": case_id,
                    "title": "验证用户使用有效凭证登录系统的认证流程和权限获取",
                    "priority": test_priority,
                    "precondition": "系统正常运行，测试用户账号已创建",
                    "test_steps": "1. 打开登录页面\n2. 输入有效的用户名和密码\n3. 点击登录按钮\n4. 检查登录结果和页面跳转",
                    "expected_result": "用户成功登录系统，跳转到主页面，显示用户信息和相应权限功能"
                })
            elif "数据" in requirement.requirement_name:
                test_cases.append({
                    "case_id": case_id,
                    "title": "测试数据录入功能在各种输入场景下的验证机制和保存结果",
                    "priority": test_priority,
                    "precondition": "系统正常运行，用户已登录具备数据操作权限",
                    "test_steps": "1. 进入数据录入页面\n2. 填写必填字段信息\n3. 提交数据\n4. 验证数据保存结果",
                    "expected_result": "数据成功保存到数据库，页面显示保存成功提示，可以查询到新录入的数据"
                })
            elif "报告" in requirement.requirement_name:
                test_cases.append({
                    "case_id": case_id,
                    "title": "验证报告生成功能在不同格式和数据量下的处理能力和输出质量",
                    "priority": test_priority, 
                    "precondition": "系统正常运行，存在可用于生成报告的数据",
                    "test_steps": "1. 进入报告生成页面\n2. 选择报告类型和参数\n3. 点击生成报告\n4. 检查生成的报告内容和格式",
                    "expected_result": "报告成功生成，内容准确完整，格式符合要求，可以正常下载"
                })
            else:
                test_cases.append({
                    "case_id": case_id,
                    "title": f"验证{requirement.requirement_name}功能的基本操作流程和预期结果",
                    "priority": test_priority,
                    "precondition": "系统正常运行，用户已登录",
                    "test_steps": f"1. 访问{requirement.requirement_name}功能\n2. 执行主要操作步骤\n3. 验证操作结果",
                    "expected_result": f"{requirement.requirement_name}功能正常工作，操作结果符合预期"
                })
        
        return test_cases
    
    @staticmethod
    async def review_test_cases(test_cases: list[GeneratedTestCase], review_criteria: str) -> dict[str, Any]:
        """评审测试用例 - 大模型B"""
        # 模拟AI评审过程
        await asyncio.sleep(1.5)
        
        reviewed_cases = []
        for test_case in test_cases:
            # 模拟评审逻辑
            review_score = 85  # 模拟评分
            
            review_comments = f"""
评审意见:
1. 测试用例标题清晰明确，能够准确描述测试目的
2. 测试步骤详细具体，具有良好的可执行性
3. 预期结果明确，便于验证
4. 建议补充异常场景的测试覆盖

评审分数: {review_score}/100
评审状态: 通过
"""
            
            reviewed_cases.append({
                "test_case_id": test_case.id,
                "review_score": review_score,
                "review_comments": review_comments.strip(),
                "status": "reviewed" if review_score >= 80 else "rejected"
            })
        
        return {
            "reviewed_cases": reviewed_cases,
            "overall_score": sum(case["review_score"] for case in reviewed_cases) / len(reviewed_cases),
            "pass_rate": len([case for case in reviewed_cases if case["status"] == "reviewed"]) / len(reviewed_cases) * 100
        }


class RequirementAnalysisService:
    """需求分析服务"""
    
    @classmethod
    def create_analysis_task(cls, document: RequirementDocument, task_type: str) -> AnalysisTask:
        """创建分析任务"""
        task_id = f"{task_type}_{uuid.uuid4().hex[:8]}"
        
        task = AnalysisTask.objects.create(
            task_id=task_id,
            task_type=task_type,
            document=document,
            status='pending'
        )
        
        return task
    
    @classmethod
    async def process_document_analysis(cls, document: RequirementDocument) -> RequirementAnalysis:
        """处理文档分析"""
        # 创建分析任务
        task = cls.create_analysis_task(document, 'requirement_analysis')
        
        try:
            # 更新任务状态
            task.status = 'running'
            task.started_at = datetime.now()
            task.progress = 10
            task.save()
            
            # 提取文档文本
            if not document.extracted_text:
                document.extracted_text = DocumentProcessor.extract_text(document)
                document.save()
            
            task.progress = 30
            task.save()
            
            # 调用AI分析
            start_time = time.time()
            analysis_result = await AIService.analyze_requirements(
                document.extracted_text, 
                document.title
            )
            analysis_time = time.time() - start_time
            
            task.progress = 70
            task.save()
            
            # 创建分析记录
            analysis = RequirementAnalysis.objects.create(
                document=document,
                analysis_report=analysis_result['analysis_report'],
                requirements_count=analysis_result['requirements_count'],
                analysis_time=analysis_time
            )
            
            # 保存需求数据
            for req_data in analysis_result['requirements']:
                BusinessRequirement.objects.create(
                    analysis=analysis,
                    **req_data
                )
            
            # 更新文档状态
            document.status = 'analyzed'
            document.save()
            
            # 完成任务
            task.status = 'completed'
            task.completed_at = datetime.now()
            task.progress = 100
            task.result = analysis_result
            task.save()
            
            return analysis
            
        except Exception as e:
            logger.error(f"文档分析失败: {e}")
            
            # 更新任务状态
            task.status = 'failed'
            task.error_message = str(e)
            task.completed_at = datetime.now()
            task.save()
            
            # 更新文档状态
            document.status = 'failed'
            document.save()
            
            raise
    
    @classmethod
    async def generate_test_cases_for_requirements(cls, requirement_ids: list[int], test_level: str, test_priority: str, test_case_count: int) -> list[GeneratedTestCase]:
        """为需求生成测试用例"""
        generated_cases = []
        
        for req_id in requirement_ids:
            try:
                requirement = BusinessRequirement.objects.get(id=req_id)
                
                # 调用AI生成测试用例
                test_cases_data = await AIService.generate_test_cases(
                    requirement, test_level, test_priority, test_case_count
                )
                
                # 保存生成的测试用例
                for case_data in test_cases_data:
                    test_case = GeneratedTestCase.objects.create(
                        requirement=requirement,
                        case_id=case_data['case_id'],
                        title=case_data['title'],
                        priority=case_data['priority'],
                        precondition=case_data['precondition'],
                        test_steps=case_data['test_steps'],
                        expected_result=case_data['expected_result'],
                        generated_by_ai='AI-A'
                    )
                    generated_cases.append(test_case)
                    
            except BusinessRequirement.DoesNotExist:
                logger.error(f"需求ID {req_id} 不存在")
                continue
            except Exception as e:
                logger.error(f"为需求 {req_id} 生成测试用例失败: {e}")
                continue
        
        return generated_cases
    
    @classmethod
    async def review_test_cases(cls, test_case_ids: list[int], review_criteria: str) -> dict[str, Any]:
        """评审测试用例"""
        test_cases = GeneratedTestCase.objects.filter(id__in=test_case_ids)
        
        # 调用AI评审
        review_result = await AIService.review_test_cases(list(test_cases), review_criteria)
        
        # 更新测试用例状态
        for case_review in review_result['reviewed_cases']:
            try:
                test_case = GeneratedTestCase.objects.get(id=case_review['test_case_id'])
                test_case.status = case_review['status']
                test_case.review_comments = case_review['review_comments']
                test_case.reviewed_by_ai = 'AI-B'
                test_case.save()
            except GeneratedTestCase.DoesNotExist:
                logger.error(f"测试用例ID {case_review['test_case_id']} 不存在")
                continue
        
        return review_result


def _extract_json(content: str) -> dict[str, Any]:
    """从 LLM 返回文本中稳健地解析 JSON。

    兼容 ```json 围栏、多余前后文本、以及字符串内/嵌套的括号（deepseek
    偶发输出），用平衡括号扫描兜底，避免简单 rfind('}') 截错导致 0 条。
    """
    if content is None:
        return {}
    text = content.strip()

    # 去 ```json ... ``` 围栏（正则兼顾无闭合围栏的情况）
    fence = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # 优先直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        # 顶层是数组等非对象：三件套契约要求 dict，标记无法解析
        logger.warning("三件套 LLM 返回非对象 JSON（top-level=%s）", type(parsed).__name__)
        return None
    except json.JSONDecodeError:
        pass

    # 平衡括号扫描：优先提取首个完整 {…}（三件套契约要求 dict）
    obj = _extract_balanced(text, '{', '}')
    if isinstance(obj, dict):
        return obj

    # 仅当文本确实以 [ 开头时，才退回到顶层数组（避免把内层数组误当顶层结果）
    if text.lstrip().startswith('['):
        arr = _extract_balanced(text, '[', ']')
        if isinstance(arr, list):
            logger.warning("三件套 LLM 返回顶层数组而非对象")
            return None

    logger.warning("三件套 LLM 返回无法解析为 JSON")
    return None


def _extract_balanced(text: str, open_c: str, close_c: str) -> Any:
    """从 text 中扫描出首个配平 open_c/close_c 包裹的片段并尝试解析为 JSON。"""
    start = text.find(open_c)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                cand = text[start:i + 1]
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    return None
    return None


def _default_system_prompt(product: str) -> str:
    """三件套各类产物的兜底系统提示词（当 PromptConfig 未配置时使用）。"""
    base = (
        "你是一名资深需求分析师与测试架构师。请严格基于给定的需求文档内容进行分析，"
        "只输出与文档内容相符、可验证的结论，不要臆造文档中不存在的功能。"
        "你必须只返回一个 JSON 对象，不要输出任何解释性文字或 Markdown 之外的格式。"
    )
    mapping = {
        'functional_inventory': base + (
            " 输出字段 functional_inventory：数组，每个元素含 seq(整数序号,从1开始)、"
            "name(功能名称)、summary(一句话功能摘要)、source_ref(文档来源章节/页码,可留空字符串)。"
        ),
        'ui_description': base + (
            " 输出字段 ui_descriptions：数组，每个元素含 page(页面/界面名称)、"
            "element(元素名称)、desc(元素描述)、interaction(交互行为,可留空字符串)。"
        ),
        'clarification': base + (
            " 输出字段 clarifications：数组，每个元素含 question(待澄清问题)、"
            "impact(该问题未澄清的影响)、suggestion(给产品/开发的建议)。"
        ),
    }
    return mapping.get(product, base)


def _system_prompt_for(product: str) -> str:
    """优先取 PromptConfig 中该类型 active 提示词，否则走兜底。"""
    cfg = PromptConfig.objects.filter(prompt_type=product, is_active=True).first()
    if cfg and cfg.content and cfg.content.strip():
        return cfg.content.strip()
    return _default_system_prompt(product)


def _get_active_llm_config() -> AIModelConfig:
    """选取一个可用的 AI 模型配置（优先 writer 角色，其次任意 active）。"""
    cfg = AIModelConfig.objects.filter(is_active=True, role='writer').first()
    if not cfg:
        cfg = AIModelConfig.objects.filter(is_active=True).first()
    if not cfg:
        raise RuntimeError('未配置可用的 AI 模型，请先在「AI模型配置」中添加并启用一个配置')
    return cfg


def _build_user_prompt(document: RequirementDocument, scope: str) -> str:
    text = document.extracted_text or ''
    if len(text) > 12000:
        text = text[:12000] + '\n...（文档已截断）'
    return (
        f"需求文档标题：{document.title}\n\n"
        f"需求文档内容：\n{text}\n\n"
        f"请针对「{scope}」生成结构化结果，严格按系统提示要求的 JSON 字段输出。"
    )


class RequirementThreeProductsService:
    """需求三件套（功能清单 / 界面描述 / 待澄清项）+ 对话修订闭环服务"""

    PRODUCT_SYSTEM_PROMPT = (
        "你是一名资深需求分析师。请基于给定的需求文档，一次性产出三套结构化产物，"
        "只返回 JSON，不要任何额外说明。JSON 结构必须包含三个数组字段："
        "1) functional_inventory：元素含 seq(int,从1递增)、name(功能名称)、summary(功能摘要)、source_ref(来源章节,可空串)；"
        "2) ui_descriptions：元素含 page(页面/界面)、element(元素名称)、desc(元素描述)、interaction(交互行为,可空串)；"
        "3) clarifications：元素含 question(待澄清问题)、impact(未澄清的影响)、suggestion(建议)。"
        "所有结论必须基于文档内容，不臆造。"
        "【严格控制长度】summary/desc/suggestion/impact 每项不得超过 25 个汉字，"
        "不要展开长段落、不要重复、不要示例性废话；整段输出应紧凑，避免被 max_tokens 截断。"
    )

    @classmethod
    def generate_three_products(cls, document: RequirementDocument) -> dict[str, int]:
        """调用 LLM 生成三件套并落库（先清旧再落新，保证幂等）。

        Returns: 各产物的创建数量字典。
        """
        if not document.extracted_text:
            document.extracted_text = DocumentProcessor.extract_text(document)
            document.save()

        config = _get_active_llm_config()
        messages = [
            {"role": "system", "content": cls.PRODUCT_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(document, '需求三件套（功能清单/界面描述/待澄清项）')},
        ]

        # 首次生成（max_tokens=8192）；若被截断则提升上限重试一次，避免三件套静默 0 条
        result = cls._call_llm_raw(config, messages, timeout=120.0, max_tokens=8192)
        content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        if result.get('choices', [{}])[0].get('finish_reason') == 'length':
            logger.warning("三件套首次生成被截断，提升至 max_tokens=16384 重试一次")
            result = cls._call_llm_raw(config, messages, timeout=180.0, max_tokens=16384)
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        data = _extract_json(content) or {}

        fi_list = data.get('functional_inventory') or []
        ui_list = data.get('ui_descriptions') or []
        cl_list = data.get('clarifications') or []

        # 幂等落库：清掉该文档旧的三件套，再写入
        FunctionalInventory.objects.filter(document=document).delete()
        UIElementDescription.objects.filter(document=document).delete()
        ClarificationItem.objects.filter(document=document).delete()

        fi_count = 0
        for idx, item in enumerate(fi_list, start=1):
            if not isinstance(item, dict):
                continue
            FunctionalInventory.objects.create(
                document=document,
                seq=int(item.get('seq') or idx),
                name=str(item.get('name') or f'功能{idx}'),
                summary=str(item.get('summary') or ''),
                source_ref=str(item.get('source_ref') or ''),
            )
            fi_count += 1

        ui_count = 0
        for item in ui_list:
            if not isinstance(item, dict):
                continue
            UIElementDescription.objects.create(
                document=document,
                page=str(item.get('page') or ''),
                element=str(item.get('element') or ''),
                desc=str(item.get('desc') or ''),
                interaction=str(item.get('interaction') or ''),
            )
            ui_count += 1

        cl_count = 0
        for item in cl_list:
            if not isinstance(item, dict):
                continue
            ClarificationItem.objects.create(
                document=document,
                question=str(item.get('question') or ''),
                impact=str(item.get('impact') or ''),
                suggestion=str(item.get('suggestion') or ''),
                status='open',
            )
            cl_count += 1

        return {
            'functional_inventory': fi_count,
            'ui_descriptions': ui_count,
            'clarifications': cl_count,
        }

    @classmethod
    def _call_llm_raw(cls, config: AIModelConfig, messages: list[dict[str, str]], timeout: float = 120.0, max_tokens: int = 4096) -> dict[str, Any]:
        """调用 LLM 并返回完整响应字典（含 finish_reason，便于截断重试）。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                asyncio.wait_for(
                    AIModelService.call_openai_compatible_api(config, messages, max_tokens=max_tokens),
                    timeout=timeout,
                )
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: S110  # 忽略事件循环清理异常
                pass
            loop.close()
        return result or {}

    @classmethod
    def _call_llm(cls, config: AIModelConfig, messages: list[dict[str, str]], timeout: float = 120.0, max_tokens: int = 4096) -> str:
        result = cls._call_llm_raw(config, messages, timeout=timeout, max_tokens=max_tokens)
        # 输出被 max_tokens 截断时给出告警，便于调参
        try:
            if result.get('choices', [{}])[0].get('finish_reason') == 'length':
                logger.warning("LLM 输出被 max_tokens(%s) 截断，建议提高上限", max_tokens)
        except Exception:  # noqa: S110  # 忽略 finish_reason 检查异常
            pass
        return result.get('choices', [{}])[0].get('message', {}).get('content', '')

    @classmethod
    def revise_with_clarification(
        cls, document: RequirementDocument, clarification_id: int, answer: str
    ) -> dict[str, Any]:
        """把澄清回答喂回，修订原三件套产物。

        流程：
        1. 定位 ClarificationItem，写入 answer 并置为 answered；
        2. 将现有三件套 + 该澄清问答交给 LLM，产出修订后的三件套；
        3. 幂等替换三件套，并把本次修订记入 revision_log，澄清项置 resolved。
        """
        clarification = ClarificationItem.objects.get(id=clarification_id, document=document)
        captured_question = clarification.question

        config = _get_active_llm_config()

        # 组装现有产物上下文
        existing_fi = [
            {'seq': fi.seq, 'name': fi.name, 'summary': fi.summary, 'source_ref': fi.source_ref}
            for fi in FunctionalInventory.objects.filter(document=document).order_by('seq')
        ]
        existing_ui = [
            {'page': ui.page, 'element': ui.element, 'desc': ui.desc, 'interaction': ui.interaction}
            for ui in UIElementDescription.objects.filter(document=document)
        ]
        existing_cl = [
            {'question': c.question, 'impact': c.impact, 'suggestion': c.suggestion, 'status': c.status, 'answer': c.answer}
            for c in ClarificationItem.objects.filter(document=document)
        ]
        context = json.dumps(
            {'functional_inventory': existing_fi, 'ui_descriptions': existing_ui, 'clarifications': existing_cl},
            ensure_ascii=False,
        )

        user_prompt = (
            f"需求文档标题：{document.title}\n\n"
            f"当前已有的三件套产物：\n{context}\n\n"
            f"刚刚澄清的问题：{captured_question}\n"
            f"用户的澄清回答：{answer}\n\n"
            f"请根据该澄清回答，修订并更新三件套产物，修正受影响的条目、补充遗漏、消除歧义。"
            f"仍只返回包含 functional_inventory / ui_descriptions / clarifications 三个数组的 JSON，"
            f"字段要求与生成时一致；clarifications 中该条应保留 question/answer，并体现最新状态。"
        )

        content = cls._call_llm(
            config,
            [
                {"role": "system", "content": cls.PRODUCT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        data = _extract_json(content) or {}

        fi_list = data.get('functional_inventory') or existing_fi
        ui_list = data.get('ui_descriptions') or existing_ui
        cl_list = data.get('clarifications') or existing_cl

        # 幂等替换
        FunctionalInventory.objects.filter(document=document).delete()
        for idx, item in enumerate(fi_list, start=1):
            if not isinstance(item, dict):
                continue
            FunctionalInventory.objects.create(
                document=document,
                seq=int(item.get('seq') or idx),
                name=str(item.get('name') or ''),
                summary=str(item.get('summary') or ''),
                source_ref=str(item.get('source_ref') or ''),
            )

        UIElementDescription.objects.filter(document=document).delete()
        for item in ui_list:
            if not isinstance(item, dict):
                continue
            UIElementDescription.objects.create(
                document=document,
                page=str(item.get('page') or ''),
                element=str(item.get('element') or ''),
                desc=str(item.get('desc') or ''),
                interaction=str(item.get('interaction') or ''),
            )

        ClarificationItem.objects.filter(document=document).delete()
        for item in cl_list:
            if not isinstance(item, dict):
                continue
            c_status = str(item.get('status') or 'open')
            if c_status not in ('open', 'answered', 'resolved'):
                c_status = 'open'
            ClarificationItem.objects.create(
                document=document,
                question=str(item.get('question') or ''),
                impact=str(item.get('impact') or ''),
                suggestion=str(item.get('suggestion') or ''),
                status=c_status,
                answer=str(item.get('answer') or ''),
            )

        # 记录修订日志，并标记被澄清项 resolved
        log_entry = {
            'clarification_id': clarification_id,
            'question': captured_question,
            'answer': answer,
            'revised_at': datetime.now().isoformat(timespec='seconds'),
        }
        # 被澄清项已被上面的重建覆盖，按问题文本定位并挂上修订记录
        target = ClarificationItem.objects.filter(
            document=document, question__exact=captured_question
        ).first()
        if target:
            logs = target.revision_log or []
            if not isinstance(logs, list):
                logs = []
            logs.append(log_entry)
            target.revision_log = logs
            target.status = 'resolved'
            target.answer = answer
            target.save(update_fields=['answer', 'status', 'revision_log', 'updated_at'])

        return {
            'clarification_id': clarification_id,
            'status': target.status if target else 'open',
            'functional_inventory': len(fi_list),
            'ui_descriptions': len(ui_list),
            'clarifications': len(cl_list),
        }