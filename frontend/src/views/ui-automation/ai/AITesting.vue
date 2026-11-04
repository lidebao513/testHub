<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('uiAutomation.ai.title') }}</h1>
    </div>

    <div class="card-container">
      <el-row :gutter="20">
        <el-col :span="12">
          <!-- #184 统一智能输入框：一句话混合文本（URL/账号/密码/权限/动作/验证）解析后一键执行 -->
          <el-card shadow="never" class="smart-input-card" style="margin-bottom: 20px;">
            <template #header>
              <div class="section-title" style="margin: 0;">{{ $t('uiAutomation.ai.smartInput.title') }}</div>
            </template>
            <el-input
              v-model="smartText"
              type="textarea"
              :rows="4"
              :placeholder="$t('uiAutomation.ai.smartInput.placeholder')"
            />
            <div style="margin-top: 10px;">
              <el-button :loading="parsing" :disabled="!smartText.trim()" @click="handleSmartParse">
                <el-icon><MagicStick /></el-icon>
                {{ $t('uiAutomation.ai.smartInput.parseBtn') }}
              </el-button>
              <el-button type="primary" :disabled="!smartParsed || running" :loading="running" @click="handleSmartRun">
                <el-icon><VideoPlay /></el-icon>
                {{ $t('uiAutomation.ai.smartInput.runBtn') }}
              </el-button>
              <el-button type="success" :disabled="!smartParsed" @click="handleSmartFillBack">
                {{ $t('uiAutomation.ai.smartInput.fillBackBtn') }}
              </el-button>
            </div>
            <div v-if="smartParsed" style="margin-top: 12px;">
              <el-alert
                v-if="smartParsed.warnings && smartParsed.warnings.length"
                type="warning"
                :closable="false"
                style="margin-bottom: 8px;"
              >
                {{ smartParsed.warnings.join('；') }}
              </el-alert>
              <el-descriptions :column="2" size="small" border>
                <el-descriptions-item :label="$t('uiAutomation.ai.smartInput.url')">
                  {{ smartParsed.url || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="$t('uiAutomation.ai.smartInput.confidence')">
                  <el-tag :type="smartParsed.confidence === 'high' ? 'success' : 'warning'" size="small">
                    {{ smartParsed.confidence === 'high' ? $t('uiAutomation.ai.smartInput.confHigh') : $t('uiAutomation.ai.smartInput.confLow') }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item :label="$t('uiAutomation.ai.smartInput.username')">
                  {{ smartParsed.username || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="$t('uiAutomation.ai.smartInput.password')">
                  {{ smartParsed.password_masked || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="$t('uiAutomation.ai.smartInput.actions')" :span="2">
                  {{ (smartParsed.actions || []).join('；') || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="$t('uiAutomation.ai.smartInput.asserts')" :span="2">
                  {{ (smartParsed.asserts || []).join('；') || '-' }}
                </el-descriptions-item>
                <el-descriptions-item v-if="(smartParsed.notes || []).length" :label="$t('uiAutomation.ai.smartInput.notes')" :span="2">
                  {{ smartParsed.notes.join('；') }}
                </el-descriptions-item>
              </el-descriptions>
            </div>
          </el-card>

          <div class="section-title">{{ $t('uiAutomation.ai.taskInput') }}</div>
          <el-form :model="taskForm" label-position="top">
            <el-form-item :label="$t('uiAutomation.ai.taskDescription')" required>
              <el-input
                v-model="taskForm.description"
                type="textarea"
                :rows="10"
                :placeholder="$t('uiAutomation.ai.taskPlaceholder')"
                maxlength="2000"
                show-word-limit
              />
            </el-form-item>

            <el-form-item :label="$t('uiAutomation.ai.gifRecording')">
              <el-switch
                v-model="taskForm.enableGif"
                :active-text="$t('uiAutomation.ai.on')"
                :inactive-text="$t('uiAutomation.ai.off')"
              />
              <span style="margin-left: 10px; color: #909399; font-size: 12px;">
                {{ $t('uiAutomation.ai.gifTip') }}
              </span>
            </el-form-item>

            <el-form-item>
              <el-button
                type="primary"
                :loading="running"
                :disabled="!taskForm.description"
                @click="handleRun"
              >
                <el-icon><VideoPlay /></el-icon>
                {{ $t('uiAutomation.ai.startExecution') }}
              </el-button>
              <el-button
                v-if="running"
                type="danger"
                :disabled="!running || analyzing"
                @click="handleStop"
              >
                <el-icon><SwitchButton /></el-icon>
                {{ $t('uiAutomation.ai.stopExecution') }}
              </el-button>
              <el-button
                type="success"
                :disabled="!taskForm.description"
                @click="handleSaveAsCase"
              >
                <el-icon><DocumentAdd /></el-icon>
                {{ $t('uiAutomation.ai.saveAsCase') }}
              </el-button>
            </el-form-item>
          </el-form>

          <el-alert
            :title="$t('uiAutomation.ai.tip')"
            type="info"
            :closable="false"
            style="margin-top: 20px;"
          >
            <template #default>
              <div>{{ $t('uiAutomation.ai.tipContent1') }}</div>
              <div>{{ $t('uiAutomation.ai.tipContent2') }}</div>
            </template>
          </el-alert>

          <div class="section-title" style="margin-top: 20px;">{{ $t('uiAutomation.ai.executionLogs') }}</div>
          <div ref="logContainer" class="log-container">
            <div v-if="!logs && !running" class="empty-logs">
              {{ $t('uiAutomation.ai.noLogs') }}
            </div>
            <pre v-else class="log-content">{{ logs }}</pre>
          </div>
        </el-col>

        <el-col :span="12">
          <div class="section-title">{{ $t('uiAutomation.ai.taskDetails') }}</div>
          <div class="task-list-container">
            <div v-if="analyzing" class="analyzing-state">
              <el-icon class="is-loading"><Loading /></el-icon>
              <span>{{ $t('uiAutomation.ai.analyzing') }}</span>
            </div>
            <div v-else-if="plannedTasks.length > 0">
              <div
                v-for="task in plannedTasks"
                :key="task.id"
                class="task-item"
                :class="task.status"
              >
                <div class="task-status-icon">
                  <el-icon v-if="task.status === 'completed'" color="#67C23A"><CircleCheckFilled /></el-icon>
                  <el-icon v-else-if="task.status === 'in_progress'" class="is-loading" color="#409EFF"><Loading /></el-icon>
                  <el-icon v-else color="#909399"><CircleCheck /></el-icon>
                </div>
                <div class="task-content">
                  <span class="task-id">{{ task.id }}.</span>
                  <span class="task-desc">{{ task.description }}</span>
                </div>
              </div>
            </div>
            <div v-else class="empty-tasks">
              {{ $t('uiAutomation.ai.noTasks') }}
            </div>
          </div>
        </el-col>
      </el-row>
    </div>

    <!-- 保存为用例对话框 -->
    <el-dialog v-model="showSaveDialog" :title="$t('uiAutomation.ai.saveAsCaseTitle')" width="500px" :close-on-click-modal="false">
      <el-form ref="saveFormRef" :model="saveForm" :rules="saveRules" label-width="80px">
        <el-form-item :label="$t('uiAutomation.ai.caseName')" prop="name">
          <el-input v-model="saveForm.name" :placeholder="$t('uiAutomation.ai.caseNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.description')" prop="description">
          <el-input v-model="saveForm.description" type="textarea" :placeholder="$t('uiAutomation.ai.caseDescPlaceholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showSaveDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="confirmSaveCase">{{ $t('uiAutomation.common.save') }}</el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, nextTick, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { VideoPlay, DocumentAdd, CircleCheckFilled, CircleCheck, Loading, SwitchButton, MagicStick } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  runAdhocAITask,
  createAICase,
  getAIExecutionRecordDetail,
  stopAITask,
  parseSmartInput
} from '@/api/ui_automation'

const { t } = useI18n()

const running = ref(false)
const analyzing = ref(false)
const saving = ref(false)
const logs = ref('')
const plannedTasks = ref([])
const currentExecutionId = ref(null)
const logContainer = ref(null)

const taskForm = reactive({
  description: '',
  enableGif: true  // GIF录制开关，默认开启
})

const showSaveDialog = ref(false)
const saveForm = reactive({
  name: '',
  description: ''
})
const saveFormRef = ref(null)

// #184 统一智能输入框：解析预览 + 一键执行 + 拆分回填（只加不改，不动下方结构化入口）
const smartText = ref('')
const parsing = ref(false)
const smartParsed = ref(null)

// 智能解析：明文密码不出接口，仅回传打码值
const handleSmartParse = async () => {
  if (!smartText.value.trim()) return
  parsing.value = true
  try {
    const response = await parseSmartInput(smartText.value)
    smartParsed.value = response.data.parsed
    ElMessage.success(t('uiAutomation.ai.smartInput.parseSuccess'))
  } catch (error) {
    console.error('解析失败:', error)
    ElMessage.error(t('uiAutomation.ai.smartInput.parseFailed') + ': ' + (error.response?.data?.error || error.message))
  } finally {
    parsing.value = false
  }
}

// 一键执行：把原始混合文本交给后端现解析（凭据只在服务端组装进执行线程，不落库）
const handleSmartRun = async () => {
  running.value = true
  analyzing.value = true
  logs.value = t('uiAutomation.ai.messages.initAgent')
  plannedTasks.value = []
  try {
    const response = await runAdhocAITask({
      smart_input: smartText.value,
      execution_mode: 'text',
      enable_gif: taskForm.enableGif
    })
    currentExecutionId.value = response.data.execution_id
    ElMessage.success(t('uiAutomation.ai.messages.startSuccess'))
    pollLogs()
  } catch (error) {
    console.error('执行失败:', error)
    ElMessage.error(t('uiAutomation.ai.messages.startFailed') + ': ' + (error.response?.data?.error || error.message))
    running.value = false
    analyzing.value = false
  }
}

// 拆分回填：解析出的任务描述（不含凭据）写回现有表单，走原结构化路径
const handleSmartFillBack = () => {
  if (!smartParsed.value) return
  taskForm.description = smartParsed.value.task_description || ''
  ElMessage.success(t('uiAutomation.ai.smartInput.fillBackSuccess'))
}

const saveRules = computed(() => ({
  name: [{ required: true, message: t('uiAutomation.ai.rules.nameRequired'), trigger: 'blur' }]
}))

// 执行任务
const handleRun = async () => {
  running.value = true
  analyzing.value = true
  logs.value = t('uiAutomation.ai.messages.initAgent')
  plannedTasks.value = []

  try {
    const response = await runAdhocAITask({
      task_description: taskForm.description,
      execution_mode: 'text',  // 始终使用文本模式
      enable_gif: taskForm.enableGif  // 传递GIF录制开关状态
    })

    // analyzing.value = false // 移除过早设置，改为在轮询获取到任务列表后再取消

    currentExecutionId.value = response.data.execution_id
    ElMessage.success(t('uiAutomation.ai.messages.startSuccess'))

    // 开始轮询日志
    pollLogs()

  } catch (error) {
    console.error('执行失败:', error)
    ElMessage.error(t('uiAutomation.ai.messages.startFailed') + ': ' + (error.response?.data?.error || error.message))
    running.value = false
    analyzing.value = false
  }
}

// 停止任务
const handleStop = async () => {
  if (!currentExecutionId.value) return

  try {
    await stopAITask(currentExecutionId.value)
    ElMessage.warning(t('uiAutomation.ai.messages.stopping'))
    // 不立即设置 running = false，等待轮询检测到状态变化
  } catch (error) {
    console.error('停止失败:', error)
    ElMessage.error(t('uiAutomation.ai.messages.stopFailed'))
  }
}

// 轮询日志
const pollLogs = () => {
  const pollInterval = setInterval(async () => {
    if (!currentExecutionId.value) {
      clearInterval(pollInterval)
      return
    }
    
    try {
      const response = await getAIExecutionRecordDetail(currentExecutionId.value)
      const record = response.data
      
      logs.value = record.logs || ''
      plannedTasks.value = record.planned_tasks || []
      
      // 如果获取到了任务列表，则取消“分析中”状态
      if (plannedTasks.value.length > 0) {
        analyzing.value = false
      }
      
      // 滚动到底部
      nextTick(() => {
        if (logContainer.value) {
          logContainer.value.scrollTop = logContainer.value.scrollHeight
        }
      })
      
      if (record.status === 'passed' || record.status === 'failed' || record.status === 'stopped') {
        clearInterval(pollInterval)
        running.value = false
        analyzing.value = false // 确保结束时必然取消分析状态
        if (record.status === 'passed') {
          ElMessage.success(t('uiAutomation.ai.messages.executionSuccess'))
        } else if (record.status === 'stopped') {
          ElMessage.warning(t('uiAutomation.ai.messages.taskStopped'))
        } else {
          ElMessage.error(t('uiAutomation.ai.messages.executionFailed'))
        }
      }
    } catch (error) {
      console.error('获取日志失败:', error)
      // 不停止轮询，可能是临时网络问题
    }
  }, 2000) // 每2秒轮询一次
}

// 保存为用例
const handleSaveAsCase = () => {
  showSaveDialog.value = true
  saveForm.name = ''
  saveForm.description = ''
}

const confirmSaveCase = async () => {
  if (!saveFormRef.value) return

  await saveFormRef.value.validate(async (valid) => {
    if (valid) {
      saving.value = true
      try {
        await createAICase({
          name: saveForm.name,
          description: saveForm.description,
          task_description: taskForm.description
        })

        ElMessage.success(t('uiAutomation.ai.messages.saveSuccess'))
        showSaveDialog.value = false
      } catch (error) {
        console.error('保存失败:', error)
        ElMessage.error(t('uiAutomation.ai.messages.saveFailed'))
      } finally {
        saving.value = false
      }
    }
  })
}
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
}

.card-container {
  background-color: #fff;
  border-radius: 4px;
  padding: 20px;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
  min-height: calc(100vh - 140px);
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 15px;
  padding-left: 10px;
  border-left: 4px solid #409eff;
}

.task-list-container {
  background-color: #f5f7fa;
  border-radius: 4px;
  padding: 15px;
  margin-bottom: 20px;
  height: calc(100vh - 200px);
  overflow-y: auto;
  
  .task-item {
    display: flex;
    align-items: flex-start;
    padding: 10px;
    border-bottom: 1px solid #e4e7ed;
    transition: all 0.3s;
    
    &:last-child {
      border-bottom: none;
    }
    
    &.completed {
      background-color: #f0f9eb;
      .task-desc {
        color: #67c23a;
        text-decoration: line-through;
      }
    }
    
    &.in_progress {
      background-color: #ecf5ff;
      .task-desc {
        color: #409eff;
        font-weight: bold;
      }
    }
    
    .task-status-icon {
      margin-right: 10px;
      margin-top: 2px;
      font-size: 16px;
    }
    
    .task-content {
      flex: 1;
      line-height: 1.5;
      
      .task-id {
        font-weight: bold;
        margin-right: 5px;
      }
    }
  }
}

.empty-tasks {
  color: #909399;
  text-align: center;
  padding: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.analyzing-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #409eff;
  
  .el-icon {
    font-size: 24px;
    margin-bottom: 10px;
  }
}

.log-container {
  background-color: #1e1e1e;
  border-radius: 4px;
  height: 300px;
  overflow-y: auto;
  padding: 15px;
  color: #fff;
  font-family: 'Consolas', 'Monaco', monospace;
  
  .empty-logs {
    color: #909399;
    text-align: center;
    margin-top: 100px;
  }
  
  .log-content {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-size: 14px;
    line-height: 1.5;
  }
}
</style>
