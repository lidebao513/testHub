<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('uiAutomation.ai.executionRecords.title') }}</h1>
      <div class="header-actions">
        <el-button
          type="danger"
          :disabled="selectedRecords.length === 0"
          @click="batchDeleteRecords"
          :loading="isDeleting"
        >
          <el-icon><Delete /></el-icon>
          {{ $t('uiAutomation.common.batchDelete') }}
        </el-button>
      </div>
    </div>

    <div class="card-container">
      <el-table
        :data="records"
        v-loading="loading"
        style="width: 100%"
        @selection-change="handleSelectionChange"
        ref="tableRef"
      >
        <el-table-column type="selection" width="55" />
        <el-table-column :label="$t('uiAutomation.ai.executionRecords.serialNumber')" width="80">
          <template #default="{ $index }">
            {{ getSerialNumber($index) }}
          </template>
        </el-table-column>
        <el-table-column prop="case_name" :label="$t('uiAutomation.ai.executionRecords.caseName')" min-width="200" show-overflow-tooltip />

        <el-table-column prop="status" :label="$t('uiAutomation.ai.executionRecords.status')" width="120">
          <template #default="{ row }">
            <el-tag :type="getStatusTag(row.status)">
              {{ getStatusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration" :label="$t('uiAutomation.ai.executionRecords.durationSeconds')" width="120">
          <template #default="{ row }">
            {{ row.duration ? row.duration.toFixed(2) : '-' }}
          </template>
        </el-table-column>
        <!-- #178 用量归因：接口已返回，列表直接可见，避免逐条点进报告才知道花了多少 -->
        <el-table-column prop="routed_model" :label="$t('uiAutomation.ai.executionRecords.routedModel')" width="170" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.routed_model || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="total_tokens" :label="$t('uiAutomation.ai.executionRecords.totalTokens')" width="110" align="right">
          <template #default="{ row }">
            {{ formatTokens(row.total_tokens) }}
          </template>
        </el-table-column>
        <el-table-column prop="start_time" :label="$t('uiAutomation.ai.executionRecords.startTime')" width="180" :formatter="formatDate" />
        <el-table-column prop="executed_by.username" :label="$t('uiAutomation.ai.executionRecords.executor')" width="120" />
        <el-table-column :label="$t('uiAutomation.common.operation')" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="viewDetail(row)">
              {{ $t('uiAutomation.ai.executionRecords.viewDetail') }}
            </el-button>
            <el-button size="small" type="success" @click="viewReport(row)">
              {{ $t('uiAutomation.ai.executionRecords.viewReport') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="pagination.currentPage"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          :total="total"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </div>

    <!-- 详情对话框 -->
    <el-dialog v-model="showDetailDialog" :title="$t('uiAutomation.ai.executionRecords.executionDetail')" width="800px">
      <div v-if="currentRecord" class="record-detail">
        <div class="detail-item">
          <span class="label">{{ $t('uiAutomation.ai.executionRecords.caseName') }}:</span>
          <span class="value">{{ currentRecord.case_name }}</span>
        </div>

        <div class="detail-item">
          <span class="label">{{ $t('uiAutomation.ai.executionRecords.status') }}:</span>
          <el-tag :type="getStatusTag(currentRecord.status)">
            {{ getStatusText(currentRecord.status) }}
          </el-tag>
        </div>
        <div class="detail-item">
          <span class="label">{{ $t('uiAutomation.ai.executionRecords.startTime') }}:</span>
          <span>{{ formatDate(null, null, currentRecord.start_time) }}</span>
        </div>
        <div class="detail-item">
          <span class="label">{{ $t('uiAutomation.ai.executionRecords.duration') }}:</span>
          <span>{{ currentRecord.duration ? currentRecord.duration.toFixed(2) + ' ' + $t('uiAutomation.ai.executionRecords.seconds') : $t('uiAutomation.ai.executionRecords.unknown') }}</span>
        </div>

        <!-- #178 用量归因：模型 / token / 成本 / 预探索 -->
        <div class="usage-grid">
          <div class="usage-cell">
            <div class="usage-label">{{ $t('uiAutomation.ai.executionRecords.routedModel') }}</div>
            <div class="usage-value">{{ currentRecord.routed_model || '-' }}</div>
          </div>
          <div class="usage-cell">
            <div class="usage-label">{{ $t('uiAutomation.ai.executionRecords.totalTokens') }}</div>
            <div class="usage-value">{{ formatTokens(currentRecord.total_tokens) }}</div>
          </div>
          <div class="usage-cell">
            <div class="usage-label">{{ $t('uiAutomation.ai.executionRecords.costUsd') }}</div>
            <div class="usage-value">{{ formatCost(currentRecord.cost) }}</div>
          </div>
          <div class="usage-cell">
            <div class="usage-label">{{ $t('uiAutomation.ai.executionRecords.preExplore') }}</div>
            <div class="usage-value">{{ formatPreExploreCount(currentRecord.pre_explore) }}</div>
          </div>
        </div>

        <!-- 预探索元素清单（动手前抓到的可交互元素） -->
        <template v-if="preExploreElements.length > 0">
          <div class="detail-item mt-15">
            <span class="label">{{ $t('uiAutomation.ai.executionRecords.preExploreElements') }}:</span>
            <span class="pre-explore-meta">{{ preExploreMeta }}</span>
          </div>
          <div class="pre-explore-container">
            <div v-for="el in preExploreElements" :key="el.index" class="pre-explore-item">
              <span class="pre-explore-index">[{{ el.index }}]</span>
              <span class="pre-explore-tag">{{ el.tag }}</span>
              <span class="pre-explore-label">{{ el.label }}</span>
            </div>
          </div>
        </template>

        <!-- 任务描述 -->
        <div v-if="currentRecord.task_description" class="detail-item mt-15">
          <span class="label">{{ $t('uiAutomation.ai.executionRecords.taskDescription') }}:</span>
        </div>
        <div v-if="currentRecord.task_description" class="task-description-container">
          <div class="task-description-content">{{ currentRecord.task_description }}</div>
        </div>

        <!-- 执行日志 -->
        <div class="detail-item mt-15">
          <span class="label">{{ $t('uiAutomation.ai.executionRecords.executionLogs') }}:</span>
        </div>
        <div class="log-container">
          <pre>{{ currentRecord.logs }}</pre>
        </div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button type="success" @click="openReportFromDetail">{{ $t('uiAutomation.ai.executionRecords.viewReport') }}</el-button>
          <el-button @click="showDetailDialog = false">{{ $t('uiAutomation.common.close') }}</el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 报告对话框 -->
    <AIExecutionReport
      v-model="showReportDialog"
      :record-id="reportRecordId"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import { getAIExecutionRecords, batchDeleteAIExecutionRecords } from '@/api/ui_automation'
import AIExecutionReport from './AIExecutionReport.vue'

const { t } = useI18n()
const records = ref([])
const loading = ref(false)
const total = ref(0)
const pagination = reactive({
  currentPage: 1,
  pageSize: 20
})

const showDetailDialog = ref(false)
const currentRecord = ref(null)
let pollTimer = null

const selectedRecords = ref([])
const isDeleting = ref(false)
const tableRef = ref(null)

// 报告相关状态
const showReportDialog = ref(false)
const reportRecordId = ref(null)

// 加载记录列表
const loadRecords = async () => {
  loading.value = true
  try {
    const response = await getAIExecutionRecords({
      page: pagination.currentPage,
      page_size: pagination.pageSize
    })

    records.value = response.data.results || []
    total.value = response.data.count || 0
    // 清空选择
    if (tableRef.value) {
      tableRef.value.clearSelection()
    }
  } catch (error) {
    console.error('获取执行记录失败:', error)
    ElMessage.error(t('uiAutomation.ai.executionRecords.messages.loadFailed'))
  } finally {
    loading.value = false
  }
}

const handleSizeChange = () => {
  pagination.currentPage = 1
  loadRecords()
}

const handleCurrentChange = () => {
  loadRecords()
}

const viewDetail = (row) => {
  currentRecord.value = row
  showDetailDialog.value = true
}

// 查看报告
const viewReport = (row) => {
  reportRecordId.value = row.id
  showReportDialog.value = true
}

// 从详情页打开报告
const openReportFromDetail = () => {
  if (currentRecord.value) {
    reportRecordId.value = currentRecord.value.id
    showReportDialog.value = true
  }
}

const getStatusTag = (status) => {
  const map = {
    'pending': 'info',
    'running': 'warning',
    'passed': 'success',
    'failed': 'danger',
    'stopped': 'warning'
  }
  return map[status] || 'info'
}

const getStatusText = (status) => {
  const map = {
    'pending': t('uiAutomation.status.pending'),
    'running': t('uiAutomation.status.running'),
    'passed': t('uiAutomation.status.success'),
    'failed': t('uiAutomation.status.failed'),
    'stopped': t('uiAutomation.status.stopped')
  }
  return map[status] || status
}

const formatDate = (row, column, cellValue) => {
  if (!cellValue) return ''
  return new Date(cellValue).toLocaleString()
}

// ===== #178 用量归因的展示 helpers =====
// 统一约定：缺数据一律显示 '-'，绝不显示 0——0 会被读成「真的花了 0」。

const formatTokens = (value) => {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString()
}

// 成本三档：provider 实报 / 价目表估算 / 未知（null）。
// null 必须落到「未知」，不能落成 0 或「免费」。
const formatCost = (value) => {
  if (value === null || value === undefined || value === '') {
    return t('uiAutomation.ai.executionRecords.costUnknown')
  }
  const num = Number(value)
  if (!Number.isFinite(num)) return t('uiAutomation.ai.executionRecords.costUnknown')
  // 单次执行成本通常远小于 1 分钱，固定 4 位会把小额显示成 0.0000，故小额放到 6 位
  return `$${num < 0.01 ? num.toFixed(6) : num.toFixed(4)}`
}

const formatPreExploreCount = (pre) => {
  if (!pre || !pre.count) return t('uiAutomation.ai.executionRecords.preExploreNotCaptured')
  return `${pre.count}`
}

// 详情里最多列 20 项，与后端报告裁剪口径保持一致
const preExploreElements = computed(() => {
  const pre = currentRecord.value?.pre_explore
  const list = pre && Array.isArray(pre.elements) ? pre.elements : []
  return list.slice(0, 20)
})

const preExploreMeta = computed(() => {
  const pre = currentRecord.value?.pre_explore || {}
  const parts = []
  if (pre.title) parts.push(pre.title)
  if (pre.url) parts.push(pre.url)
  return parts.join(' | ')
})

// 获取序号
const getSerialNumber = (index) => {
  return (pagination.currentPage - 1) * pagination.pageSize + index + 1
}

// 处理选择变化
const handleSelectionChange = (selection) => {
  selectedRecords.value = selection
}

// 批量删除
const batchDeleteRecords = async () => {
  if (selectedRecords.value.length === 0) return

  try {
    await ElMessageBox.confirm(
      t('uiAutomation.ai.executionRecords.messages.batchDeleteConfirm', { count: selectedRecords.value.length }),
      t('uiAutomation.ai.executionRecords.messages.batchDeleteTitle'),
      {
        confirmButtonText: t('uiAutomation.common.confirm'),
        cancelButtonText: t('uiAutomation.common.cancel'),
        type: 'warning'
      }
    )

    isDeleting.value = true
    const ids = selectedRecords.value.map(item => item.id)
    await batchDeleteAIExecutionRecords(ids)

    ElMessage.success(t('uiAutomation.ai.executionRecords.messages.deleteSuccess'))

    // 如果当前页数据全部被删除，且不是第一页，则跳转到上一页
    if (records.value.length === ids.length && pagination.currentPage > 1) {
      pagination.currentPage--
    }

    loadRecords()
  } catch (error) {
    if (error !== 'cancel') {
      console.error('批量删除失败:', error)
      ElMessage.error(t('uiAutomation.ai.executionRecords.messages.batchDeleteFailed'))
    }
  } finally {
    isDeleting.value = false
  }
}



// 轮询更新状态
const startPolling = () => {
  pollTimer = setInterval(() => {
    // 只有在第一页且没有打开详情框且没有正在加载时才轮询
    if (pagination.currentPage === 1 && !showDetailDialog.value && !loading.value) {
      // 优化：检查当前列表是否有正在运行的任务，如果没有运行中的任务，则不轮询（或者降低频率）
      const hasActiveTasks = records.value.some(r => r.status === 'running' || r.status === 'pending')
      if (!hasActiveTasks) {
        return
      }

      // 静默刷新，不显示 loading
      getAIExecutionRecords({
        page: 1,
        page_size: pagination.pageSize
      }).then(response => {
        // 只有当没有选中项时才更新列表，避免干扰用户选择
        if (selectedRecords.value.length === 0) {
          records.value = response.data.results || []
          total.value = response.data.count || 0
        }
      }).catch(console.error)
    }
  }, 5000)
}

onMounted(() => {
  loadRecords()
  startPolling()
})

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
  }
})
</script>

<style lang="scss" scoped>
.page-container {
  padding: 20px;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;

  .page-title {
    font-size: 20px;
    font-weight: 600;
    margin: 0;
  }

  .header-actions {
    display: flex;
    align-items: center;
  }
}

.card-container {
  background-color: #fff;
  border-radius: 4px;
  padding: 20px;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

.record-detail {
  .detail-item {
    margin-bottom: 15px;
    .label {
      font-weight: bold;
      margin-right: 10px;
    }
  }

  .log-container {
    background-color: #1e1e1e;
    color: #fff;
    padding: 15px;
    border-radius: 4px;
    max-height: 400px;
    overflow-y: auto;
    font-family: monospace;

    pre {
      margin: 0;
      white-space: pre-wrap;
      word-wrap: break-word;
    }
  }

  .task-description-container {
    background-color: #f5f7fa;
    border: 1px solid #e4e7ed;
    border-radius: 4px;
    padding: 12px 15px;
    margin-top: 8px;

    .task-description-content {
      color: #606266;
      line-height: 1.6;
      white-space: pre-wrap;
      word-wrap: break-word;
    }
  }

  // #178 用量归因
  .usage-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-top: 15px;

    .usage-cell {
      background-color: #f5f7fa;
      border-radius: 4px;
      padding: 10px 12px;
      text-align: center;
    }

    .usage-label {
      font-size: 12px;
      color: #909399;
      margin-bottom: 6px;
    }

    .usage-value {
      font-size: 14px;
      font-weight: 600;
      color: #303133;
      word-break: break-all;
    }
  }

  .pre-explore-meta {
    color: #909399;
    font-size: 12px;
  }

  .pre-explore-container {
    background-color: #f5f7fa;
    border: 1px solid #e4e7ed;
    border-radius: 4px;
    padding: 10px 15px;
    margin-top: 8px;
    max-height: 220px;
    overflow-y: auto;

    .pre-explore-item {
      font-size: 13px;
      line-height: 1.8;
      color: #606266;
      display: flex;
      gap: 8px;
    }

    .pre-explore-index {
      color: #409eff;
      font-family: monospace;
      flex-shrink: 0;
    }

    .pre-explore-tag {
      color: #909399;
      flex-shrink: 0;
    }

    .pre-explore-label {
      word-break: break-all;
    }
  }
}

.mt-15 {
  margin-top: 15px;
}
</style>
