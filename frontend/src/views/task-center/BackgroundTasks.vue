<template>
  <div class="task-center">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <h2 class="page-title">{{ $t('task.title') }}</h2>
      </div>
      <div class="toolbar-right">
        <el-switch
          v-model="autoRefresh"
          :active-text="$t('task.autoRefresh')"
          inline-prompt
          class="mr"
        />
        <el-checkbox v-model="showClosed" class="mr" @change="fetchTasks">
          {{ $t('task.showClosed') }}
        </el-checkbox>
        <el-button type="primary" :icon="Refresh" :loading="loading" @click="fetchTasks">
          {{ $t('task.refresh') }}
        </el-button>
      </div>
    </div>

    <!-- 任务表格 -->
    <el-table
      v-loading="loading"
      :data="tasks"
      stripe
      border
      empty-text=" "
      style="width: 100%"
    >
      <el-table-column prop="name" :label="$t('task.name')" min-width="220" show-overflow-tooltip />
      <el-table-column :label="$t('task.typeLabel')" width="140">
        <template #default="{ row }">{{ typeLabel(row.task_type) }}</template>
      </el-table-column>
      <el-table-column :label="$t('task.statusLabel')" width="110">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" effect="light" size="small">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="$t('task.progressLabel')" width="160">
        <template #default="{ row }">
          <el-progress
            :percentage="Math.round((row.progress || 0) * 100)"
            :status="row.status === 'failed' ? 'exception' : (row.status === 'success' ? 'success' : '')"
            :stroke-width="12"
          />
        </template>
      </el-table-column>
      <el-table-column :label="$t('task.start')" width="170">
        <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
      </el-table-column>
      <el-table-column :label="$t('task.end')" width="170">
        <template #default="{ row }">{{ formatTime(row.finished_at) }}</template>
      </el-table-column>
      <el-table-column prop="created_by_username" :label="$t('task.creator')" width="120" show-overflow-tooltip />
      <el-table-column :label="$t('task.actions')" width="240" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="viewDetail(row)">
            {{ $t('task.detail') }}
          </el-button>
          <el-button
            link
            type="warning"
            size="small"
            :disabled="!canRetry(row)"
            @click="retryTask(row)"
          >
            {{ $t('task.retry') }}
          </el-button>
          <el-button
            link
            type="danger"
            size="small"
            :disabled="!canPause(row)"
            @click="pauseTask(row)"
          >
            {{ $t('task.pause') }}
          </el-button>
          <el-button
            link
            type="info"
            size="small"
            :disabled="row.is_closed"
            @click="closeTask(row)"
          >
            {{ $t('task.close') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-empty v-if="!loading && tasks.length === 0" :description="$t('task.noTask')" />

    <!-- 详情抽屉（在途任务自动轮询最新状态） -->
    <el-drawer
      v-model="detailVisible"
      :title="$t('task.detail')"
      size="46%"
      @closed="stopDetailPoll"
    >
      <template v-if="current">
        <el-descriptions :column="1" border>
          <el-descriptions-item :label="$t('task.name')">{{ current.name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('task.typeLabel')">{{ typeLabel(current.task_type) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('task.statusLabel')">
            <el-tag :type="statusTagType(current.status)" effect="light" size="small">
              {{ statusLabel(current.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('task.progressLabel')">
            <el-progress :percentage="Math.round((current.progress || 0) * 100)" :stroke-width="12" />
          </el-descriptions-item>
          <el-descriptions-item :label="$t('task.start')">{{ formatTime(current.started_at) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('task.end')">{{ formatTime(current.finished_at) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('task.creator')">{{ current.created_by_username || '-' }}</el-descriptions-item>
          <el-descriptions-item :label="$t('task.source')">{{ current.source || '-' }}</el-descriptions-item>
          <el-descriptions-item :label="$t('task.created')">{{ formatTime(current.created_at) }}</el-descriptions-item>
        </el-descriptions>

        <el-divider>{{ $t('task.payload') }}</el-divider>
        <pre class="json-box">{{ pretty(current.payload_summary) }}</pre>

        <template v-if="current.result_summary">
          <el-divider>{{ $t('task.result') }}</el-divider>
          <pre class="json-box">{{ pretty(current.result_summary) }}</pre>
        </template>

        <template v-if="current.error">
          <el-divider>{{ $t('task.error') }}</el-divider>
          <pre class="json-box error">{{ current.error }}</pre>
        </template>

        <div class="detail-actions">
          <el-button
            type="warning"
            :disabled="!canRetry(current)"
            @click="retryTask(current)"
          >
            {{ $t('task.retry') }}
          </el-button>
          <el-button
            type="danger"
            :disabled="!canPause(current)"
            @click="pauseTask(current)"
          >
            {{ $t('task.pause') }}
          </el-button>
          <el-button type="info" :disabled="current.is_closed" @click="closeTask(current)">
            {{ $t('task.close') }}
          </el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '@/utils/api'

const { t } = useI18n()

const tasks = ref([])
const loading = ref(false)
const autoRefresh = ref(true)
const showClosed = ref(false)
let listTimer = null

const detailVisible = ref(false)
const current = ref(null)
let detailTimer = null

const STATUS_TAG = {
  pending: 'info',
  running: 'warning',
  success: 'success',
  failed: 'danger',
  cancelled: 'info',
  paused: 'info',
}
const LIVE = ['pending', 'running']

function typeLabel(type) {
  return t(`task.type.${type}`) || type
}
function statusLabel(status) {
  return t(`task.status.${status}`) || status
}
function statusTagType(status) {
  return STATUS_TAG[status] || 'info'
}
function canRetry(row) {
  return row.task_type === 'generate' && ['failed', 'cancelled', 'paused'].includes(row.status)
}
function canPause(row) {
  return ['pending', 'running'].includes(row.status)
}
function formatTime(v) {
  if (!v) return '-'
  // 后端返回 ISO 字符串（含 T 与 Z），转为本地可读
  const d = new Date(v)
  if (isNaN(d.getTime())) return v
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}
function pretty(v) {
  if (!v) return '-'
  try {
    return JSON.stringify(JSON.parse(v), null, 2)
  } catch {
    return v
  }
}

async function fetchTasks() {
  loading.value = true
  try {
    const { data } = await api.get('/taskcenter/tasks/', {
      params: { include_closed: showClosed.value ? 1 : 0 },
    })
    tasks.value = data.tasks || []
  } catch (e) {
    // 列表读取失败不弹全局错误（api 拦截器已处理 401/5xx），仅清空
    tasks.value = []
  } finally {
    loading.value = false
  }
}

function startListPoll() {
  stopListPoll()
  listTimer = setInterval(() => {
    if (autoRefresh.value) fetchTasks()
  }, 5000)
}
function stopListPoll() {
  if (listTimer) {
    clearInterval(listTimer)
    listTimer = null
  }
}

async function viewDetail(row) {
  current.value = row
  detailVisible.value = true
  await refreshDetail(row.id)
  startDetailPoll(row.id)
}
async function refreshDetail(id) {
  try {
    const { data } = await api.get(`/taskcenter/tasks/${id}/`)
    current.value = data.task
    // 详情里若任务已终态，停止轮询
    if (!LIVE.includes(data.task.status)) stopDetailPoll()
  } catch {
    /* 忽略 */
  }
}
function startDetailPoll(id) {
  stopDetailPoll()
  detailTimer = setInterval(() => refreshDetail(id), 2000)
}
function stopDetailPoll() {
  if (detailTimer) {
    clearInterval(detailTimer)
    detailTimer = null
  }
}

async function retryTask(row) {
  try {
    await ElMessageBox.confirm(t('task.confirmRetry'), t('task.retry'), { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.post(`/taskcenter/tasks/${row.id}/retry/`)
    ElMessage.success(t('task.retry') + ' ' + row.name)
    await fetchTasks()
    if (detailVisible.value && current.value && current.value.id === row.id) {
      await refreshDetail(row.id)
    }
  } catch (e) {
    const msg = e.response?.data?.error || e.message
    ElMessage.error(msg)
  }
}

async function pauseTask(row) {
  try {
    await ElMessageBox.confirm(t('task.confirmPause'), t('task.pause'), { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.post(`/taskcenter/tasks/${row.id}/pause/`)
    ElMessage.success(t('task.pause') + ' ' + row.name)
    await fetchTasks()
    if (detailVisible.value && current.value && current.value.id === row.id) {
      await refreshDetail(row.id)
    }
  } catch (e) {
    const msg = e.response?.data?.error || e.message
    ElMessage.error(msg)
  }
}

async function closeTask(row) {
  try {
    await ElMessageBox.confirm(t('task.confirmClose'), t('task.close'), { type: 'info' })
  } catch {
    return
  }
  try {
    await api.post(`/taskcenter/tasks/${row.id}/close/`)
    ElMessage.success(t('task.close') + ' ' + row.name)
    await fetchTasks()
    if (detailVisible.value && current.value && current.value.id === row.id) {
      await refreshDetail(row.id)
    }
  } catch (e) {
    const msg = e.response?.data?.error || e.message
    ElMessage.error(msg)
  }
}

onMounted(() => {
  fetchTasks()
  startListPoll()
})
onUnmounted(() => {
  stopListPoll()
  stopDetailPoll()
})
</script>

<style scoped>
.task-center {
  background: #fff;
  padding: 16px;
  border-radius: 6px;
  min-height: 100%;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.page-title {
  font-size: 18px;
  font-weight: 600;
  margin: 0;
}
.toolbar-right {
  display: flex;
  align-items: center;
}
.mr {
  margin-right: 14px;
}
.json-box {
  background: #f6f8fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 10px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 280px;
  overflow: auto;
}
.json-box.error {
  background: #fef0f0;
  color: #f56c6c;
  border-color: #fde2e2;
}
.detail-actions {
  margin-top: 18px;
  display: flex;
  gap: 10px;
}
</style>
