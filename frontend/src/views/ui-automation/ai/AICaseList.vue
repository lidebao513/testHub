<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('uiAutomation.ai.caseList.title') }}</h1>
    </div>

    <div class="card-container">
      <div class="filter-bar" style="display: flex; gap: 12px; align-items: center;">
        <el-input
          v-model="searchText"
          :placeholder="$t('uiAutomation.ai.caseList.searchPlaceholder')"
          clearable
          style="width: 300px;"
          @input="handleSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button
          type="warning"
          :disabled="selectedCases.length === 0"
          :loading="loopRunning"
          @click="runClosedLoopCases"
        >
          运行闭环（{{ selectedCases.length }}）
        </el-button>
      </div>

      <el-table v-loading="loading" :data="cases" style="width: 100%" @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="55" />
        <el-table-column prop="name" :label="$t('uiAutomation.ai.caseList.caseName')" min-width="200" show-overflow-tooltip />
        <el-table-column prop="description" :label="$t('uiAutomation.common.description')" min-width="200" show-overflow-tooltip />
        <el-table-column prop="task_description" :label="$t('uiAutomation.ai.caseList.taskDescription')" min-width="300" show-overflow-tooltip />
        <el-table-column prop="created_at" :label="$t('uiAutomation.common.createTime')" width="180" :formatter="formatDate" />
        <el-table-column :label="$t('uiAutomation.common.operation')" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="success" @click="runCase(row)">
              <el-icon><VideoPlay /></el-icon>
              {{ $t('uiAutomation.common.run') }}
            </el-button>
            <el-button size="small" type="primary" @click="editCase(row)">
              <el-icon><Edit /></el-icon>
              {{ $t('uiAutomation.common.edit') }}
            </el-button>
            <el-button size="small" type="danger" @click="deleteCase(row.id)">
              <el-icon><Delete /></el-icon>
              {{ $t('uiAutomation.common.delete') }}
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

    <!-- 编辑对话框 -->
    <el-dialog v-model="showEditDialog" :title="$t('uiAutomation.ai.caseList.editCase')" width="600px" :close-on-click-modal="false">
      <el-form ref="editFormRef" :model="editForm" :rules="formRules" label-width="100px">
        <el-form-item :label="$t('uiAutomation.ai.caseList.caseName')" prop="name">
          <el-input v-model="editForm.name" :placeholder="$t('uiAutomation.ai.caseNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.description')" prop="description">
          <el-input v-model="editForm.description" type="textarea" :placeholder="$t('uiAutomation.ai.caseDescPlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.ai.caseList.taskDescription')" prop="task_description">
          <el-input
            v-model="editForm.task_description"
            type="textarea"
            :rows="6"
            :placeholder="$t('uiAutomation.ai.taskPlaceholder')"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showEditDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="confirmEdit">{{ $t('uiAutomation.common.save') }}</el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, VideoPlay, Edit, Delete } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import {
  getAICases,
  updateAICase,
  deleteAICase,
  runAICase,
  runClosedLoop,
  getClosedLoopStatus
} from '@/api/ui_automation'

const { t } = useI18n()
const router = useRouter()
const cases = ref([])
const loading = ref(false)
const searchText = ref('')
const total = ref(0)
const pagination = reactive({
  currentPage: 1,
  pageSize: 20
})

const showEditDialog = ref(false)
const saving = ref(false)
const currentCaseId = ref(null)
const editForm = reactive({
  name: '',
  description: '',
  task_description: ''
})
const editFormRef = ref(null)

const formRules = computed(() => ({
  name: [{ required: true, message: t('uiAutomation.ai.rules.nameRequired'), trigger: 'blur' }],
  task_description: [{ required: true, message: t('uiAutomation.ai.caseList.rules.taskDescriptionRequired'), trigger: 'blur' }]
}))

// 加载用例列表
const loadCases = async () => {
  loading.value = true
  try {
    const response = await getAICases({
      page: pagination.currentPage,
      page_size: pagination.pageSize,
      search: searchText.value
    })

    cases.value = response.data.results || []
    total.value = response.data.count || 0
  } catch (error) {
    console.error('获取用例列表失败:', error)
    ElMessage.error(t('uiAutomation.ai.caseList.messages.loadFailed'))
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  pagination.currentPage = 1
  loadCases()
}

const handleSizeChange = () => {
  pagination.currentPage = 1
  loadCases()
}

const handleCurrentChange = () => {
  loadCases()
}

// 编辑用例
const editCase = (row) => {
  currentCaseId.value = row.id
  editForm.name = row.name
  editForm.description = row.description
  editForm.task_description = row.task_description
  showEditDialog.value = true
}

const confirmEdit = async () => {
  if (!editFormRef.value) return

  await editFormRef.value.validate(async (valid) => {
    if (valid) {
      saving.value = true
      try {
        await updateAICase(currentCaseId.value, {
          name: editForm.name,
          description: editForm.description,
          task_description: editForm.task_description
        })

        ElMessage.success(t('uiAutomation.ai.caseList.messages.updateSuccess'))
        showEditDialog.value = false
        loadCases()
      } catch (error) {
        console.error('更新失败:', error)
        ElMessage.error(t('uiAutomation.ai.caseList.messages.updateFailed'))
      } finally {
        saving.value = false
      }
    }
  })
}

// 删除用例
const deleteCase = async (id) => {
  try {
    await ElMessageBox.confirm(
      t('uiAutomation.ai.caseList.messages.deleteConfirm'),
      t('uiAutomation.messages.confirm.tip'),
      {
        confirmButtonText: t('uiAutomation.common.confirm'),
        cancelButtonText: t('uiAutomation.common.cancel'),
        type: 'warning'
      }
    )

    await deleteAICase(id)
    ElMessage.success(t('uiAutomation.ai.caseList.messages.deleteSuccess'))
    loadCases()
  } catch (error) {
    if (error !== 'cancel') {
      console.error('删除失败:', error)
      ElMessage.error(t('uiAutomation.ai.caseList.messages.deleteFailed'))
    }
  }
}

// 执行用例
const runCase = async (row) => {
  try {
    await runAICase(row.id)
    ElMessage.success(t('uiAutomation.ai.caseList.messages.runSuccess'))
    // 跳转到执行记录页面
    router.push('/ai-intelligent-mode/execution-records')
  } catch (error) {
    console.error('执行失败:', error)
    ElMessage.error(t('uiAutomation.ai.caseList.messages.runFailed'))
  }
}

const formatDate = (row, column, cellValue) => {
  if (!cellValue) return ''
  return new Date(cellValue).toLocaleString()
}

// 多选 + 闭环触发（#200）
const selectedCases = ref([])
const loopRunning = ref(false)
let loopTimer = null

const handleSelectionChange = (rows) => {
  selectedCases.value = rows
}

const runClosedLoopCases = async () => {
  if (selectedCases.value.length === 0) return
  const ids = selectedCases.value.map((c) => c.id)
  loopRunning.value = true
  try {
    const resp = await runClosedLoop({ case_ids: ids, max_retries: 0 })
    const taskId = resp.data.task_id
    ElMessage.success(`已提交闭环任务，任务ID：${taskId}`)
    pollLoopStatus(taskId)
  } catch (error) {
    console.error('提交闭环任务失败:', error)
    ElMessage.error(t('uiAutomation.ai.caseList.messages.runFailed'))
    loopRunning.value = false
  }
}

const pollLoopStatus = (taskId) => {
  if (loopTimer) clearInterval(loopTimer)
  loopTimer = setInterval(async () => {
    try {
      const resp = await getClosedLoopStatus(taskId)
      const state = resp.data.state
      if (state === 'SUCCESS' || state === 'FAILURE') {
        clearInterval(loopTimer)
        loopTimer = null
        loopRunning.value = false
        if (state === 'SUCCESS') {
          const summary = resp.data.result?.summary || {}
          ElMessage.success(
            `闭环完成：信号 ${summary.signal || '-'}（通过 ${summary.done || 0}/${summary.total || 0}）`
          )
          loadCases() // 刷新列表（AICase.last_loop 已更新）
        } else {
          ElMessage.error(`闭环失败：${resp.data.error || '未知错误'}`)
        }
      }
    } catch (e) {
      clearInterval(loopTimer)
      loopTimer = null
      loopRunning.value = false
      console.error('查询闭环状态失败:', e)
      ElMessage.error(t('uiAutomation.ai.caseList.messages.runFailed'))
    }
  }, 3000)
}

onMounted(() => {
  loadCases()
})

onUnmounted(() => {
  if (loopTimer) clearInterval(loopTimer)
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
}

.card-container {
  background-color: #fff;
  border-radius: 4px;
  padding: 20px;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
}

.filter-bar {
  margin-bottom: 20px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}
</style>
