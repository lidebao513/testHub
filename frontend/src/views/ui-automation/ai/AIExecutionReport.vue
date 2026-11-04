<template>
  <el-dialog
    v-model="visible"
    :title="`${$t('uiAutomation.ai.executionReport.title')} - ${reportTypeDisplay}`"
    width="1000px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :modal="true"
    :destroy-on-close="false"
    @close="handleClose"
  >
    <div v-if="loading" class="report-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>{{ $t('uiAutomation.ai.executionReport.generatingReport') }}</span>
    </div>

    <div v-else-if="reportData" class="report-container">
      <!-- 报告类型切换 -->
      <div class="report-type-tabs">
        <el-radio-group v-model="currentReportType" @change="onReportTypeChange">
          <el-radio-button value="summary">{{ $t('uiAutomation.ai.executionReport.summary') }}</el-radio-button>
          <el-radio-button value="detailed">{{ $t('uiAutomation.ai.executionReport.detailed') }}</el-radio-button>
          <el-radio-button value="performance">{{ $t('uiAutomation.ai.executionReport.performance') }}</el-radio-button>
        </el-radio-group>
        <el-button v-if="reportData.gif_path" type="primary" size="small" @click="showGifDialog = true">
          <el-icon><VideoPlay /></el-icon>
          {{ $t('uiAutomation.ai.executionReport.viewGif') }}
        </el-button>
        <el-button v-else type="info" size="small" disabled>
          <el-icon><VideoPlay /></el-icon>
          {{ $t('uiAutomation.ai.executionReport.noGif') }}
        </el-button>
        <el-button type="success" size="small" @click="exportReport">
          <el-icon><Download /></el-icon>
          {{ $t('uiAutomation.ai.executionReport.exportReport') }}
        </el-button>
      </div>

      <!-- 摘要报告 -->
      <div v-if="currentReportType === 'summary'" class="report-content">
        <!-- 概览卡片 -->
        <div class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.overview') }}</h3>
          <div class="overview-cards">
            <div class="overview-card">
              <div class="card-label">{{ $t('uiAutomation.ai.executionReport.executionStatus') }}</div>
              <el-tag :type="reportData.overview.status_color" size="large">
                {{ reportData.overview.status }}
              </el-tag>
            </div>
            <div class="overview-card">
              <div class="card-label">{{ $t('uiAutomation.ai.executionReport.executionDuration') }}</div>
              <div class="card-value">{{ reportData.overview.duration_formatted }}</div>
            </div>
            <div class="overview-card">
              <div class="card-label">{{ $t('uiAutomation.ai.executionReport.completionRate') }}</div>
              <div class="card-value">{{ reportData.overview.completion_rate }}%</div>
            </div>
            <div class="overview-card">
              <div class="card-label">{{ $t('uiAutomation.ai.executionReport.executionSteps') }}</div>
              <div class="card-value">{{ reportData.overview.total_steps }} {{ $t('uiAutomation.ai.executionReport.steps') }}</div>
            </div>
          </div>
        </div>

        <!-- #178 AI 用量归因：模型 / token / 成本 / 预探索 -->
        <div v-if="reportData.ai_usage" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.aiUsage') }}</h3>
          <div class="performance-metrics usage-metrics">
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.routedModel') }}</div>
              <div class="metric-value metric-text">{{ reportData.ai_usage.routed_model || '-' }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.totalTokens') }}</div>
              <div class="metric-value">{{ formatTokens(reportData.ai_usage.total_tokens) }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.cost') }}</div>
              <div class="metric-value">{{ formatCost(reportData.ai_usage) }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.preExploreElements') }}</div>
              <div class="metric-value">{{ formatPreExploreCount(reportData.ai_usage.pre_explore) }}</div>
            </div>
          </div>

          <!-- 预探索清单：动手前页面有哪些可交互元素，作为「模型是否真的先看清页面」的审计证据 -->
          <div v-if="preExploreElements.length > 0" class="pre-explore-block">
            <div class="pre-explore-header">
              <span class="pre-explore-title">{{ $t('uiAutomation.ai.executionReport.preExploreTitle') }}</span>
              <span class="pre-explore-count">
                {{ $t('uiAutomation.ai.executionReport.preExploreCount', { count: reportData.ai_usage.pre_explore.count }) }}
              </span>
            </div>
            <div v-if="reportData.ai_usage.pre_explore.url" class="pre-explore-url">
              {{ $t('uiAutomation.ai.executionReport.preExploreUrl') }}: {{ reportData.ai_usage.pre_explore.url }}
            </div>
            <el-table :data="preExploreElements" size="small" max-height="240" stripe>
              <el-table-column prop="index" label="#" width="70" />
              <el-table-column prop="tag" :label="$t('uiAutomation.ai.executionReport.elementTag')" width="110" />
              <el-table-column prop="label" :label="$t('uiAutomation.ai.executionReport.elementText')" min-width="200" show-overflow-tooltip />
            </el-table>
          </div>
          <div v-else class="pre-explore-empty">
            {{ $t('uiAutomation.ai.executionReport.preExploreNotCaptured') }}
          </div>
        </div>

        <!-- 任务统计 -->
        <div v-if="reportData.statistics" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.taskStatistics') }}</h3>
          <div class="statistics-container">
            <div class="chart-wrapper">
              <div ref="pieChartRef" class="chart" style="height: 200px;"></div>
            </div>
            <div class="stats-table">
              <table class="stats-table-content">
                <tbody>
                  <tr>
                    <td>{{ $t('uiAutomation.ai.executionReport.totalTasks') }}</td>
                    <td class="stat-value">{{ reportData.statistics.total }}</td>
                  </tr>
                  <tr class="success-row">
                    <td>{{ $t('uiAutomation.ai.executionReport.completed') }}</td>
                    <td class="stat-value">{{ reportData.statistics.completed }}</td>
                  </tr>
                  <tr class="info-row">
                    <td>{{ $t('uiAutomation.ai.executionReport.pending') }}</td>
                    <td class="stat-value">{{ reportData.statistics.pending }}</td>
                  </tr>
                  <tr class="danger-row">
                    <td>{{ $t('uiAutomation.ai.executionReport.failed') }}</td>
                    <td class="stat-value">{{ reportData.statistics.failed }}</td>
                  </tr>
                  <tr class="warning-row">
                    <td>{{ $t('uiAutomation.ai.executionReport.skipped') }}</td>
                    <td class="stat-value">{{ reportData.statistics.skipped }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- 任务时间线 -->
        <div class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.taskTimeline') }}</h3>
          <div class="timeline-container">
            <el-timeline>
              <el-timeline-item
                v-for="task in reportData.timeline"
                :key="task.id"
                :timestamp="`${$t('uiAutomation.ai.executionReport.task')} ${task.id}`"
                placement="top"
                :type="getTimelineType(task.status)"
              >
                <el-card>
                  <div class="timeline-task-content">
                    <div class="task-description">{{ task.description }}</div>
                    <el-tag :type="getTaskStatusType(task.status)" size="small">
                      {{ task.status_display }}
                    </el-tag>
                  </div>
                </el-card>
              </el-timeline-item>
            </el-timeline>
          </div>
        </div>
      </div>

      <!-- 详细步骤报告 -->
      <div v-else-if="currentReportType === 'detailed'" class="report-content">
        <!-- 步骤列表 -->
        <div class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.stepDetails') }}</h3>
          <div class="steps-list">
            <el-card v-for="step in reportData.detailed_steps" :key="step.step_number" class="step-card">
              <div class="step-header">
                <span class="step-number">{{ $t('uiAutomation.ai.executionReport.step') }} {{ step.step_number }}</span>
                <el-tag :type="getStepStatusType(step.status)" size="small">
                  {{ step.status }}
                </el-tag>
              </div>
              <div class="step-content">
                <div class="step-action">
                  <strong>{{ $t('uiAutomation.ai.executionReport.action') }}:</strong> {{ step.action || '-' }}
                </div>
                <div v-if="step.element" class="step-element">
                  <strong>{{ $t('uiAutomation.ai.executionReport.element') }}:</strong> {{ step.element }}
                </div>
                <div v-if="step.thinking" class="step-thinking">
                  <strong>{{ $t('uiAutomation.ai.executionReport.thinking') }}:</strong> {{ step.thinking }}
                </div>
              </div>
            </el-card>
          </div>
        </div>

        <!-- 错误信息 -->
        <div v-if="reportData.errors && reportData.errors.length > 0" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.errorInfo') }}</h3>
          <div class="errors-list">
            <el-alert
              v-for="(error, index) in reportData.errors"
              :key="index"
              :type="error.type === 'error' ? 'error' : 'warning'"
              :title="error.message"
              :closable="false"
              show-icon
            />
          </div>
        </div>
      </div>

      <!-- 性能分析报告 -->
      <div v-else-if="currentReportType === 'performance'" class="report-content">
        <!-- 性能指标 -->
        <div v-if="reportData.metrics" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.performanceMetrics') }}</h3>
          <div class="performance-metrics">
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.avgStepDuration') }}</div>
              <div class="metric-value">{{ reportData.metrics.avg_step_duration }} {{ $t('uiAutomation.ai.executionReport.seconds') }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.maxStepDuration') }}</div>
              <div class="metric-value">{{ reportData.metrics.max_step_duration }} {{ $t('uiAutomation.ai.executionReport.seconds') }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.minStepDuration') }}</div>
              <div class="metric-value">{{ reportData.metrics.min_step_duration }} {{ $t('uiAutomation.ai.executionReport.seconds') }}</div>
            </div>
            <!-- #178 性能页同时看成本：慢和贵经常是同一处 -->
            <div v-if="reportData.ai_usage" class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.totalTokens') }}</div>
              <div class="metric-value">{{ formatTokens(reportData.ai_usage.total_tokens) }}</div>
            </div>
            <div v-if="reportData.ai_usage" class="metric-card">
              <div class="metric-label">{{ $t('uiAutomation.ai.executionReport.cost') }}</div>
              <div class="metric-value">{{ formatCost(reportData.ai_usage) }}</div>
            </div>
          </div>
        </div>

        <!-- 操作分布 -->
        <div v-if="reportData.action_distribution" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.actionDistribution') }}</h3>
          <div ref="barChartRef" class="chart" style="height: 250px;"></div>
        </div>

        <!-- 性能瓶颈 -->
        <div v-if="reportData.bottlenecks && reportData.bottlenecks.length > 0" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.performanceBottlenecks') }}</h3>
          <el-table :data="reportData.bottlenecks" stripe>
            <el-table-column prop="step_number" :label="$t('uiAutomation.ai.executionReport.step')" width="80" />
            <el-table-column prop="action" :label="$t('uiAutomation.ai.executionReport.action')" min-width="200" />
            <el-table-column prop="duration" :label="$t('uiAutomation.ai.executionReport.durationSeconds')" width="100" />
            <el-table-column prop="slower_than_avg_by" :label="$t('uiAutomation.ai.executionReport.slowerThanAvg')" width="100">
              <template #default="{ row }">
                {{ row.slower_than_avg_by }}%
              </template>
            </el-table-column>
          </el-table>
        </div>

        <!-- 优化建议 -->
        <div v-if="reportData.recommendations && reportData.recommendations.length > 0" class="report-section">
          <h3 class="section-title">{{ $t('uiAutomation.ai.executionReport.recommendations') }}</h3>
          <div class="recommendations-list">
            <el-alert
              v-for="(rec, index) in reportData.recommendations"
              :key="index"
              type="info"
              :title="rec"
              :closable="false"
              show-icon
            />
          </div>
        </div>
      </div>
    </div>

    <div v-else class="report-error">
      <el-empty :description="$t('uiAutomation.ai.executionReport.noReportData')" />
    </div>

    <template #footer>
      <span class="dialog-footer">
        <el-button @click="handleClose">{{ $t('uiAutomation.common.close') }}</el-button>
      </span>
    </template>

    <!-- GIF回放对话框 -->
    <el-dialog v-model="showGifDialog" :title="$t('uiAutomation.ai.executionReport.gifPlayback')" width="800px" append-to-body>
      <div v-if="reportData && reportData.gif_path" class="gif-container">
        <img :src="gifUrl" alt="Execution GIF" class="gif-image" />
      </div>
    </el-dialog>
  </el-dialog>
</template>

<script setup>
import { ref, watch, nextTick, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Loading, VideoPlay, Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { getAIExecutionReport, exportAIExecutionReportPDF } from '@/api/ui_automation'

const { t } = useI18n()

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false
  },
  recordId: {
    type: [Number, String],
    default: null
  }
})

const emit = defineEmits(['update:modelValue'])

const visible = ref(false)
const loading = ref(false)
const reportData = ref(null)
const currentReportType = ref('summary')
const showGifDialog = ref(false)
const pieChartRef = ref(null)
const barChartRef = ref(null)
let pieChart = null
let barChart = null

// 报告类型显示名称
const reportTypeDisplay = computed(() => {
  const map = {
    'summary': t('uiAutomation.ai.executionReport.summary'),
    'detailed': t('uiAutomation.ai.executionReport.detailed'),
    'performance': t('uiAutomation.ai.executionReport.performance')
  }
  return map[currentReportType.value] || t('uiAutomation.ai.executionReport.summary')
})

// ===== #178 用量归因的展示 helpers =====
// 统一约定：缺数据一律显示 '-' 或「未知」，绝不显示 0。

const formatTokens = (value) => {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString()
}

// 成本三档：provider 实报 / 价目表估算 / 未知（null）。
// 后端用 cost_known 显式区分「未知」与「真的为 0」，前端照此渲染，不要自己猜。
const formatCost = (usage) => {
  if (!usage) return t('uiAutomation.ai.executionReport.costUnknown')
  const known = usage.cost_known === true ||
    (usage.cost_known === undefined && usage.cost !== null && usage.cost !== undefined)
  const num = Number(usage.cost)
  if (!known || !Number.isFinite(num)) {
    return t('uiAutomation.ai.executionReport.costUnknown')
  }
  return `$${num.toFixed(6)}`
}

const formatPreExploreCount = (pre) => {
  if (!pre || !pre.count) return '-'
  return String(pre.count)
}

const preExploreElements = computed(() => {
  const pre = reportData.value?.ai_usage?.pre_explore
  const list = pre && Array.isArray(pre.elements) ? pre.elements : []
  return list
})

// GIF URL
const gifUrl = computed(() => {
  if (reportData.value && reportData.value.gif_path) {
    // gif_path格式：media/ai_recording/xxx.gif
    const path = reportData.value.gif_path
    // 如果路径已经包含media/，直接使用；否则添加media/
    if (path.startsWith('media/')) {
      return `/${path}`
    } else {
      return `/media/${path}`
    }
  }
  return ''
})

// 监听 modelValue 变化
watch(() => props.modelValue, (newVal) => {
  visible.value = newVal
  if (newVal && props.recordId) {
    currentReportType.value = 'summary'  // 确保设置为summary
    loadReport('summary')
  }
})

// 监听 visible 变化
watch(visible, (newVal) => {
  emit('update:modelValue', newVal)
  if (!newVal) {
    // 清理
    if (pieChart) {
      pieChart.dispose()
      pieChart = null
    }
    if (barChart) {
      barChart.dispose()
      barChart = null
    }
  }
})

// 加载报告数据
const loadReport = async (reportType = 'summary') => {
  if (!props.recordId) return

  loading.value = true
  try {
    const response = await getAIExecutionReport(props.recordId, { report_type: reportType })
    console.log('API Response:', response.data)
    if (response.data.success) {
      reportData.value = response.data.data
      console.log('Report Data:', reportData.value)
      await nextTick()
      // 等待DOM更新后再初始化图表
      setTimeout(() => {
        if (reportType === 'summary') {
          initPieChart()
        } else if (reportType === 'performance') {
          initBarChart()
        }
      }, 100)
    } else {
      ElMessage.error(response.data.error || t('uiAutomation.ai.executionReport.messages.loadFailed'))
    }
  } catch (error) {
    console.error('加载报告失败:', error)
    ElMessage.error(t('uiAutomation.ai.executionReport.messages.loadFailed'))
  } finally {
    loading.value = false
  }
}

// 报告类型切换
const onReportTypeChange = (value) => {
  currentReportType.value = value
  loadReport(value)
}

// 初始化饼图
const initPieChart = () => {
  if (!pieChartRef.value || !reportData.value) return

  // 确保统计数据存在
  if (!reportData.value.statistics) {
    console.warn('统计数据不存在')
    return
  }

  if (pieChart) {
    pieChart.dispose()
  }

  pieChart = echarts.init(pieChartRef.value)

  const stats = reportData.value.statistics
  const option = {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)'
    },
    legend: {
      orient: 'vertical',
      right: 10,
      top: 'center'
    },
    series: [
      {
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['35%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 10,
          borderColor: '#fff',
          borderWidth: 2
        },
        label: {
          show: false,
          position: 'center'
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 16,
            fontWeight: 'bold'
          }
        },
        labelLine: {
          show: false
        },
        data: [
          { value: stats.completed || 0, name: t('uiAutomation.ai.executionReport.completed'), itemStyle: { color: '#67C23A' } },
          { value: stats.pending || 0, name: t('uiAutomation.ai.executionReport.pending'), itemStyle: { color: '#909399' } },
          { value: stats.failed || 0, name: t('uiAutomation.ai.executionReport.failed'), itemStyle: { color: '#F56C6C' } },
          { value: stats.skipped || 0, name: t('uiAutomation.ai.executionReport.skipped'), itemStyle: { color: '#E6A23C' } }
        ].filter(item => item.value > 0)
      }
    ]
  }

  pieChart.setOption(option)
}

// 初始化柱状图
const initBarChart = () => {
  if (!barChartRef.value || !reportData.value) return

  // 确保性能数据存在
  if (!reportData.value.action_distribution) {
    console.warn('操作分布数据不存在')
    return
  }

  if (barChart) {
    barChart.dispose()
  }

  barChart = echarts.init(barChartRef.value)

  const distribution = reportData.value.action_distribution
  const data = [
    { name: t('uiAutomation.ai.executionReport.actions.click'), value: distribution.click || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.input'), value: distribution.input || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.scroll'), value: distribution.scroll || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.wait'), value: distribution.wait || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.switchTab'), value: distribution.switch_tab || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.navigate'), value: distribution.navigate || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.openTab'), value: distribution.open_tab || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.done'), value: distribution.done || 0 },
    { name: t('uiAutomation.ai.executionReport.actions.other'), value: distribution.other || 0 }
  ].filter(item => item.value > 0)

  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow'
      }
    },
    xAxis: {
      type: 'category',
      data: data.map(item => item.name)
    },
    yAxis: {
      type: 'value'
    },
    series: [
      {
        type: 'bar',
        data: data.map(item => item.value),
        itemStyle: {
          color: '#409EFF'
        }
      }
    ]
  }

  barChart.setOption(option)
}

// 获取时间线类型
const getTimelineType = (status) => {
  const typeMap = {
    'completed': 'success',
    'pending': 'info',
    'failed': 'danger',
    'skipped': 'warning'
  }
  return typeMap[status] || 'info'
}

// 获取任务状态标签类型
const getTaskStatusType = (status) => {
  const typeMap = {
    'completed': 'success',
    'pending': 'info',
    'failed': 'danger',
    'skipped': 'warning'
  }
  return typeMap[status] || 'info'
}

// 获取步骤状态类型
const getStepStatusType = (status) => {
  const typeMap = {
    'completed': 'success',
    'pending': 'info',
    'failed': 'danger'
  }
  return typeMap[status] || 'info'
}

// 导出报告
const exportReport = async () => {
  if (!props.recordId) {
    ElMessage.error(t('uiAutomation.ai.executionReport.messages.missingRecordId'))
    return
  }

  try {
    ElMessage.info(t('uiAutomation.ai.executionReport.messages.generatingPdf'))

    const response = await exportAIExecutionReportPDF(props.recordId, {
      report_type: currentReportType.value
    })

    // 创建下载链接
    const blob = new Blob([response.data], { type: 'application/pdf' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url

    // 从响应头获取文件名，如果没有则使用默认名称
    const contentDisposition = response.headers['content-disposition']
    let filename = 'AI_Report.pdf'

    if (contentDisposition) {
      const filenameMatch = contentDisposition.match(/filename="(.+)"/)
      if (filenameMatch && filenameMatch[1]) {
        filename = decodeURIComponent(filenameMatch[1])
      }
    }

    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)

    ElMessage.success(t('uiAutomation.ai.executionReport.messages.exportSuccess'))
  } catch (error) {
    console.error('导出报告失败:', error)
    ElMessage.error(error.response?.data?.error || t('uiAutomation.ai.executionReport.messages.exportFailed'))
  }
}

// 关闭对话框
const handleClose = () => {
  visible.value = false
}
</script>

<style scoped>
.report-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 0;
  color: #909399;
  font-size: 16px;
}

.report-loading .el-icon {
  font-size: 32px;
  margin-bottom: 16px;
}

.report-container {
  max-height: 600px;
  overflow-y: auto;
  padding-right: 10px;
}

.report-type-tabs {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 10px;
  border-bottom: 1px solid #E4E7ED;
}

.report-content {
  padding: 10px 0;
}

.report-section {
  margin-bottom: 30px;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 2px solid #E4E7ED;
}

.overview-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.overview-card {
  background: #F5F7FA;
  border-radius: 8px;
  padding: 16px;
  text-align: center;
}

.card-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}

.card-value {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
}

.statistics-container {
  display: flex;
  gap: 30px;
  align-items: center;
}

.chart-wrapper {
  flex: 1;
  max-width: 300px;
}

.chart {
  width: 100%;
  height: 100%;
}

.stats-table {
  flex: 1;
}

.stats-table-content {
  width: 100%;
  border-collapse: collapse;
}

.stats-table-content td {
  padding: 10px 16px;
  border-bottom: 1px solid #E4E7ED;
}

.stats-table-content tr:last-child td {
  border-bottom: none;
}

.stat-value {
  font-weight: 600;
  text-align: right;
  font-size: 16px;
}

.success-row {
  background-color: #F0F9FF;
}

.info-row {
  background-color: #F5F7FA;
}

.danger-row {
  background-color: #FEF0F0;
}

.warning-row {
  background-color: #FDF6EC;
}

.timeline-container {
  padding: 10px 0;
}

.timeline-task-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.task-description {
  flex: 1;
  margin-right: 12px;
  font-size: 14px;
  color: #303133;
}

.steps-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.step-card {
  border-left: 3px solid #409EFF;
}

.step-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.step-number {
  font-weight: 600;
  color: #409EFF;
}

.step-content > div {
  margin-bottom: 8px;
  font-size: 14px;
}

.step-thinking {
  color: #909399;
  font-style: italic;
}

.performance-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 16px;
}

.metric-card {
  background: #F5F7FA;
  border-radius: 8px;
  padding: 20px;
  text-align: center;
}

.metric-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}

.metric-value {
  font-size: 24px;
  font-weight: 600;
  color: #409EFF;
}

/* #178 用量归因 */
.usage-metrics {
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: 16px;
}

.usage-metrics .metric-text {
  font-size: 14px;
  line-height: 1.6;
  word-break: break-all;
}

.pre-explore-block {
  margin-top: 8px;
}

.pre-explore-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.pre-explore-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.pre-explore-count {
  font-size: 12px;
  color: #909399;
}

.pre-explore-url {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
  word-break: break-all;
}

.pre-explore-empty {
  margin-top: 8px;
  font-size: 13px;
  color: #909399;
  background: #F5F7FA;
  border-radius: 4px;
  padding: 12px 16px;
}

.gif-container {
  text-align: center;
}

.gif-image {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
}

.report-error {
  padding: 40px 0;
  text-align: center;
}

/* 自定义滚动条 */
.report-container::-webkit-scrollbar {
  width: 6px;
}

.report-container::-webkit-scrollbar-track {
  background: #F5F7FA;
  border-radius: 3px;
}

.report-container::-webkit-scrollbar-thumb {
  background: #DCDFE6;
  border-radius: 3px;
}

.report-container::-webkit-scrollbar-thumb:hover {
  background: #C0C4CC;
}
</style>
