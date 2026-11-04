# 闭环 API/UI 触发入口（#200）

## 背景
#183 闭环内核与 #199 testgen→AICase 桥接已就绪，但闭环只能由运维命令 `run_closed_loop` 触发，
缺少"用户在界面上一键对选中 AICase 跑闭环"的入口。闭环跑真实浏览器（秒~分钟级），**必须异步**。

## 改动
1. `apps/ui_automation/tasks.py`（新增）：`run_closed_loop_task` Celery 任务
   - `@shared_task`，复用 #183 `ClosedLoopOrchestrator`（真实 `execute_aicase_real` / `reflow_aicase_real` 接线）；
   - 作业来源 `case_ids`（优先）或 `project_id`；结果与报告落 `data/closed_loop_reports/`（与命令同约定）。
2. `apps/ui_automation/views_closed_loop.py`（新增）：
   - `ClosedLoopRunView` `POST /api/ui-automation/closed-loop/run/`：鉴权 + 校验 `case_ids` 存在性 →
     `run_closed_loop_task.delay(...)` **fire-and-forget** 返回 `task_id`（不阻塞 HTTP）；
   - `ClosedLoopStatusView` `GET /api/ui-automation/closed-loop/<task_id>/status/`：查 Celery `AsyncResult` 状态与结果。
3. `apps/ui_automation/urls.py`：挂载上述两个端点（在 router include 之后）。
4. 前端 `frontend/src/api/ui_automation.js`：新增 `runClosedLoop` / `getClosedLoopStatus`。
5. 前端 `frontend/src/views/ui-automation/ai/AICaseList.vue`：
   - `el-table` 加多选列 + `@selection-change`；
   - 工具栏加"运行闭环（N）"按钮（无选中时禁用）；
   - 提交后 **每 3s 轮询状态**，SUCCESS 提示本轮信号/通过率并刷新列表（AICase.last_loop 已写回），FAILURE 提示错误。

## 验证
- 后端 `apps/ui_automation/tests/test_closed_loop_trigger.py`：**3 passed**
  - 任务层：mock 真实执行、保留真实 reflow，验证编排被调用 + AICase.last_loop 写回 `pass` + 报告落盘；
  - 端点层：mock `.delay` 验证入参与返回 task_id（不真跑任务）；不存在的 case_ids → 400。
- `pytest apps/ui_automation` 全量：**297 passed**（无回归；#199 未合并 main，合并后为 300）。
- 前端：`npx eslint` 改动文件 **0 error**；`npm run build` 通过（21.23s）。
- 新增文件 ruff 全绿。

## 异步说明
- 依赖本仓既有 Celery 配置（`CELERY_BROKER_URL=REDIS_URL`，即 Memurai :6379）；测试用
  `CELERY_TASK_ALWAYS_EAGER=True` 不连 broker。
- 真实环境需 Celery worker 常驻消费 `run_closed_loop_task`，否则 `.delay` 派发后无 worker 执行。

## 后续
- #184 统一智能输入框（设计优先，不落地）。
