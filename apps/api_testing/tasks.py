"""api_testing 定时任务执行（L8，Celery 任务）。

设计要点：
- **只传主键**：Celery 参数必须可序列化，且 worker 侧要重新取对象，避免跨进程
  携带陈旧状态；
- **执行逻辑单份**：worker 与「broker 不可用时的有界线程池兜底」共用
  `ScheduledTaskViewSet._run_task_sync`，不存在两套实现漂移的风险；
- **不抛业务异常**：失败已在 `_execute_and_record` 内落到执行日志，这里只返回结果，
  避免把堆栈塞进 Celery 结果后端（对齐 `apps/app_automation/tasks.py` 的做法）。
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def execute_scheduled_task(self, task_id, execution_log_id, user_id=None):
    """异步执行一条定时任务（`TEST_SUITE` / `API_REQUEST`）。

    :param task_id: `ScheduledTask` 主键
    :param execution_log_id: `TaskExecutionLog` 主键（调用方已创建，状态 PENDING）
    :param user_id: 触发人主键（定时调度场景可为 None）
    """
    # 延迟导入：views 模块会反过来导入本任务，放在函数内避免循环导入
    from .views import ScheduledTaskViewSet

    logger.info(
        "Celery 执行定时任务：task=%s log=%s user=%s", task_id, execution_log_id, user_id
    )
    return ScheduledTaskViewSet()._run_task_sync(task_id, execution_log_id)
