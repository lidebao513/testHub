# -*- coding: utf-8 -*-
"""
AI模型配置和服务
"""

from django.db import models
from django.contrib.auth import get_user_model
import json
import httpx
import asyncio
from typing import Dict, Any, List
import logging

from apps.requirement_analysis import llm_budget

User = get_user_model()
logger = logging.getLogger(__name__)


class AIModelConfig(models.Model):
    """AI模型配置模型"""
    MODEL_CHOICES = [
        ('deepseek', 'DeepSeek'),
        ('qwen', '通义千问'),
        ('siliconflow', '硅基流动'),
        ('xiaomi', '小米'),
        ('xiaomi_coding_plan', '小米coding plan'),
        ('other', '其他'),
    ]
    
    ROLE_CHOICES = [
        ('writer', '测试用例编写专家'),
        ('reviewer', '测试评审专家'),
    ]
    
    name = models.CharField(max_length=100, verbose_name='配置名称')
    model_type = models.CharField(max_length=20, choices=MODEL_CHOICES, verbose_name='模型类型')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name='角色')
    api_key = models.CharField(max_length=200, verbose_name='API Key')
    base_url = models.URLField(verbose_name='API Base URL')
    model_name = models.CharField(max_length=100, verbose_name='模型名称')
    max_tokens = models.IntegerField(default=4096, verbose_name='最大Token数')
    temperature = models.FloatField(default=0.7, verbose_name='温度参数')
    top_p = models.FloatField(default=0.9, verbose_name='Top P参数')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'ai_model_config'
        verbose_name = 'AI模型配置'
        verbose_name_plural = 'AI模型配置'
        unique_together = ('model_type', 'role')  # 每种角色只能有一个活跃配置
    
    def __str__(self):
        return f"{self.get_model_type_display()} - {self.get_role_display()}"


class PromptConfig(models.Model):
    """提示词配置模型"""
    PROMPT_CHOICES = [
        ('writer', '用例编写提示词'),
        ('reviewer', '用例评审提示词'),
    ]
    
    name = models.CharField(max_length=100, verbose_name='配置名称')
    prompt_type = models.CharField(max_length=20, choices=PROMPT_CHOICES, verbose_name='提示词类型')
    content = models.TextField(verbose_name='提示词内容')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'prompt_config'
        verbose_name = '提示词配置'
        verbose_name_plural = '提示词配置'
        unique_together = ('prompt_type', 'is_active')  # 每种类型只能有一个活跃提示词
    
    def __str__(self):
        return f"{self.get_prompt_type_display()} - {self.name}"


class TestCaseGenerationTask(models.Model):
    """测试用例生成任务模型"""
    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('generating', '生成中'),
        ('reviewing', '评审中'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]
    
    task_id = models.CharField(max_length=50, unique=True, verbose_name='任务ID')
    title = models.CharField(max_length=200, verbose_name='任务标题')
    requirement_text = models.TextField(verbose_name='需求描述')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    progress = models.IntegerField(default=0, verbose_name='进度百分比')
    
    # 配置参数
    writer_model_config = models.ForeignKey(
        AIModelConfig, on_delete=models.SET_NULL, null=True, 
        related_name='writer_tasks', verbose_name='编写模型配置'
    )
    reviewer_model_config = models.ForeignKey(
        AIModelConfig, on_delete=models.SET_NULL, null=True,
        related_name='reviewer_tasks', verbose_name='评审模型配置'
    )
    writer_prompt_config = models.ForeignKey(
        PromptConfig, on_delete=models.SET_NULL, null=True,
        related_name='writer_tasks', verbose_name='编写提示词配置'
    )
    reviewer_prompt_config = models.ForeignKey(
        PromptConfig, on_delete=models.SET_NULL, null=True,
        related_name='reviewer_tasks', verbose_name='评审提示词配置'
    )
    
    # 生成结果
    generated_test_cases = models.TextField(blank=True, verbose_name='生成的测试用例')
    review_feedback = models.TextField(blank=True, verbose_name='评审反馈')
    final_test_cases = models.TextField(blank=True, verbose_name='最终测试用例')
    
    # 元数据
    generation_log = models.TextField(blank=True, verbose_name='生成日志')
    error_message = models.TextField(blank=True, verbose_name='错误信息')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    
    class Meta:
        db_table = 'testcase_generation_task'
        verbose_name = '测试用例生成任务'
        verbose_name_plural = '测试用例生成任务'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.title} - {self.get_status_display()}"


class AIModelService:
    """AI模型服务类"""

    @staticmethod
    def model_chain(model_name: str) -> List[str]:
        """把配置里的 `a,b,c` 解析成**按顺序降级**的模型链；单模型时返回单元素。

        这样「已确认开了用完即停」的多个模型（含不同快照日期）可以填在一行里，
        前一个额度用尽会自动切下一个，无需改代码。
        """
        return [m.strip() for m in (model_name or '').split(',') if m.strip()]

    @staticmethod
    def _resolve_chat_url(base_url: str) -> str:
        """兼容用户填根路径 / 以 /v1 结尾 / 已写全 /chat/completions 三种写法。"""
        base = (base_url or '').rstrip('/')
        if base.endswith('/chat/completions'):
            return base
        if base.endswith('/v1'):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    @staticmethod
    def _is_model_unavailable(status: int, text: str) -> bool:
        """该模型是否「暂时用不了，值得换下一个」——额度耗尽/限流/模型下线。

        403/404/429 直接判定；另按文本兜底（百炼免费额度用尽会返回
        `AllocationQuota.FreeTierOnly`）。**401 不在此列**：同一 key 所有模型都会失败，
        换模型没有意义。
        """
        if status in (403, 404, 429):
            return True
        low = (text or '').lower()
        return any(k in low for k in ('quota', 'freetieronly', 'rate limit', 'insufficient'))

    @staticmethod
    async def call_openai_compatible_api(config: AIModelConfig, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """调用OpenAI兼容格式的API。

        两个关键行为：
        1. **按顺序降级**：`config.model_name` 支持逗号分隔多模型，前一个额度用尽/限流
           自动切下一个（顺序即配置顺序，不随机、不轮询）。
        2. **成本护栏**（`apps.requirement_analysis.llm_budget`）：付费档模型默认拒绝、
           单次输入超限拒绝、当日 token 到顶整链停止——避免免费额度耗尽后静默转按量计费。
        """
        chain = AIModelService.model_chain(config.model_name)
        if not chain:
            raise Exception('未配置模型名（model_name 为空）')

        headers = {
            'Authorization': f'Bearer {config.api_key}',
            'Content-Type': 'application/json'
        }
        url = AIModelService._resolve_chat_url(config.base_url)

        budget_cfg = llm_budget.load_settings()
        max_tokens = config.max_tokens or 0
        if budget_cfg.enforce and (not max_tokens or max_tokens > budget_cfg.max_output_tokens):
            # 输出单价通常是输入的 3 倍，未配或配得过大时统一压到护栏上限
            max_tokens = budget_cfg.max_output_tokens

        blocked: List[str] = []
        last_error = ''
        for model in chain:
            if budget_cfg.enforce:
                decision = llm_budget.check_call(model, messages, cfg=budget_cfg)
                if not decision.allowed:
                    if decision.scope == 'global':
                        # 输入过大 / 日预算到顶：换模型一样超，整链停止
                        logger.error(f"LLM 成本护栏拦截整链调用：{decision.reason}")
                        raise llm_budget.BudgetExceeded(decision.reason)
                    logger.warning(f"模型 {model} 被成本护栏跳过：{decision.reason}")
                    blocked.append(decision.reason)
                    continue

            data = {
                'model': model,
                'messages': messages,
                'max_tokens': max_tokens,
                'temperature': config.temperature,
                'top_p': config.top_p,
                'stream': False
            }

            try:
                # Increase timeout to 120s for long generation tasks
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(url, headers=headers, json=data)

                if response.status_code == 200:
                    body = response.json()
                    AIModelService._record_usage(model, messages, body, budget_cfg)
                    return body

                detail = response.text
                logger.error(f"API调用返回错误: model={model}, Status={response.status_code}, Body={detail[:500]}")
                if AIModelService._is_model_unavailable(response.status_code, detail):
                    last_error = f"{model} 返回 {response.status_code}"
                    continue  # 换下一个模型
                response.raise_for_status()

            except httpx.HTTPStatusError as e:
                provider_name = config.get_model_type_display()
                last_error = f"{provider_name} API返回错误 {e.response.status_code}: {e.response.text[:300]}"
                logger.error(last_error)
                if AIModelService._is_model_unavailable(e.response.status_code, e.response.text):
                    continue
                raise Exception(last_error)
            except httpx.TimeoutException as e:
                provider_name = config.get_model_type_display()
                logger.error(f"{provider_name} API请求超时: {repr(e)}")
                raise Exception(f"{provider_name} API请求超时，请稍后再试或检查网络连接")

        provider_name = config.get_model_type_display()
        reason = last_error or ('；'.join(blocked) if blocked else '未知原因')
        raise Exception(f"{provider_name} 全部模型均不可用（链：{','.join(chain)}）：{reason}")

    @staticmethod
    def _record_usage(
        model: str,
        messages: List[Dict[str, str]],
        body: Dict[str, Any],
        budget_cfg: llm_budget.BudgetSettings,
    ) -> None:
        """把本次真实用量记入护栏（优先上游 usage，缺失则本地估算）。记账失败不影响返回。"""
        try:
            usage = body.get('usage') or {}
            prompt_tokens = int(usage.get('prompt_tokens') or 0)
            completion_tokens = int(usage.get('completion_tokens') or 0)
            if not prompt_tokens:
                prompt_tokens = llm_budget.estimate_messages_tokens(messages)
            if not completion_tokens:
                choices = body.get('choices') or []
                content = ''
                if choices:
                    content = ((choices[0] or {}).get('message') or {}).get('content') or ''
                completion_tokens = llm_budget.estimate_tokens(str(content))
            llm_budget.record_usage(
                model, prompt_tokens, completion_tokens, channel='django', cfg=budget_cfg
            )
        except Exception as exc:  # noqa: BLE001 - 记账失败不能影响业务返回
            logger.warning(f"LLM 用量记账失败（不影响本次返回）：{exc}")


    @staticmethod
    async def call_deepseek_api(config: AIModelConfig, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """调用DeepSeek API (兼容OpenAI格式)"""
        return await AIModelService.call_openai_compatible_api(config, messages)
    
    @staticmethod
    async def call_qwen_api(config: AIModelConfig, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """调用千问API (兼容OpenAI格式)"""
        return await AIModelService.call_openai_compatible_api(config, messages)
    
    @staticmethod
    async def generate_test_cases(task: TestCaseGenerationTask) -> str:
        """生成测试用例"""
        writer_prompt = task.writer_prompt_config.content
        user_message = (
            f"请根据以下需求生成测试用例：\n"
            f"   - **编号必须连续，中间不能有遗漏**\n"
            f"   - **所有用例必须一次性完整输出，不能中断**\n"
            f"6. **⚠️ 特殊字符处理（关键）**：\n"
            f"   - **如果在表格内容（如操作步骤、预期结果）中出现管道符 '|'，必须转义为 '\|'**。\n"
            f"   - **否则会导致表格列错位，无法解析**。\n"
            f"   - 示例：应输入 'a\|b' 而不是 'a|b'。\n\n"
            f"【需求文档内容】\n{task.requirement_text}"
        )
        
        messages = [
            {"role": "system", "content": writer_prompt},
            {"role": "user", "content": user_message}
        ]
        
        # 所有支持的模型都使用兼容OpenAI的接口
        response = await AIModelService.call_openai_compatible_api(task.writer_model_config, messages)
        
        return response['choices'][0]['message']['content']
    
    @staticmethod
    async def review_test_cases(task: TestCaseGenerationTask, test_cases: str) -> str:
        """评审测试用例"""
        reviewer_prompt = task.reviewer_prompt_config.content
        user_message = f"请评审以下测试用例：\n\n{test_cases}"
        
        messages = [
            {"role": "system", "content": reviewer_prompt},
            {"role": "user", "content": user_message}
        ]
        
        # 所有支持的模型都使用兼容OpenAI的接口
        response = await AIModelService.call_openai_compatible_api(task.reviewer_model_config, messages)
        
        return response['choices'][0]['message']['content']
