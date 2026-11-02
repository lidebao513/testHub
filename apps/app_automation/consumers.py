import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)


class AppExecutionConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        try:
            user = self.scope.get("user")
            if user is None or not getattr(user, "is_authenticated", False):
                # M9：未登录直接拒绝（不进入任何 group，杜绝匿名订阅）。
                await self.close(code=4401)
                return

            execution_id = self.scope["url_route"]["kwargs"]["execution_id"]
            if not await self._owns_execution(user, execution_id):
                # M9：非本人且非管理员 → 拒绝订阅，避免猜 id 读取他人执行结果。
                await self.close(code=4403)
                return

            self.execution_id = execution_id
            self.group_name = f"app_execution_{self.execution_id}"
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            logger.info(
                f"WebSocket 连接成功: execution_id={self.execution_id}, user={user}"
            )
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            await self.close()

    @database_sync_to_async
    def _owns_execution(self, user, execution_id):
        from .models import AppTestExecution

        if user.is_staff:
            # 管理员可订阅任意执行。
            return AppTestExecution.objects.filter(id=execution_id).exists()
        # 普通用户仅可订阅归属自己的执行（AppTestExecution.user）。
        return AppTestExecution.objects.filter(id=execution_id, user=user).exists()

    async def disconnect(self, close_code):
        try:
            if hasattr(self, 'group_name'):
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
                logger.info(f"WebSocket 断开: execution_id={self.execution_id}, code={close_code}")
        except Exception as e:
            logger.error(f"WebSocket 断开处理失败: {e}")

    async def execution_update(self, event):
        try:
            await self.send_json(event)
        except Exception as e:
            logger.error(f"WebSocket 推送消息失败: {e}")
