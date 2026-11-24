<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('bizModuleManage.title') }}</h1>
      <div class="header-actions">
        <el-button type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>
          {{ $t('bizModuleManage.newModule') }}
        </el-button>
      </div>
    </div>

    <div class="card-container">
      <div class="filter-bar">
        <el-row :gutter="20">
          <el-col :span="6">
            <el-input
              v-model="searchText"
              :placeholder="$t('bizModuleManage.searchPlaceholder')"
              clearable
              @input="handleFilter"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
          </el-col>
          <el-col :span="5">
            <el-select
              v-model="projectFilter"
              :placeholder="$t('bizModuleManage.project')"
              clearable
              @change="handleFilter"
            >
              <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
            </el-select>
          </el-col>
          <el-col :span="5">
            <el-select
              v-model="sourceFilter"
              :placeholder="$t('bizModuleManage.source')"
              clearable
              @change="handleFilter"
            >
              <el-option
                v-for="opt in sourceOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </el-col>
        </el-row>
      </div>

      <div class="table-container">
        <el-table v-loading="loading" :data="modules" style="width: 100%" height="100%">
          <el-table-column prop="name" :label="$t('bizModuleManage.name')" min-width="180" />
          <el-table-column :label="$t('bizModuleManage.source')" width="130">
            <template #default="{ row }">
              <el-tag :type="sourceTagType(row.source)">{{ sourceLabel(row.source) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="project_name" :label="$t('bizModuleManage.project')" width="160" />
          <el-table-column prop="namespace" :label="$t('bizModuleManage.namespace')" min-width="260" show-overflow-tooltip />
          <el-table-column :label="$t('bizModuleManage.caseCount')" width="110">
            <template #default="{ row }">
              <el-link type="primary" @click="goToCases(row.id)">{{ row.case_count }}</el-link>
            </template>
          </el-table-column>
          <el-table-column prop="description" :label="$t('bizModuleManage.description')" min-width="200" show-overflow-tooltip />
          <el-table-column :label="$t('bizModuleManage.createdAt')" width="180">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column :label="$t('project.actions')" width="150" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
              <el-button size="small" type="danger" @click="deleteModule(row)">{{ $t('common.delete') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[15, 25, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          @current-change="fetchModules"
          @size-change="handleFilter"
        />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? $t('bizModuleManage.editModule') : $t('bizModuleManage.newModule')"
      width="540px"
    >
      <el-form :model="form" label-width="100px">
        <el-form-item :label="$t('bizModuleManage.project')" required>
          <el-select v-model="form.project" filterable :placeholder="$t('bizModuleManage.selectProject')" style="width: 100%">
            <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('bizModuleManage.name')" required>
          <el-input v-model="form.name" :placeholder="$t('bizModuleManage.namePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('bizModuleManage.namespace')">
          <el-input v-model="form.namespace" placeholder="code:42 / runtime:https://example.com" />
        </el-form-item>
        <el-form-item :label="$t('bizModuleManage.source')">
          <el-select v-model="form.source" style="width: 100%">
            <el-option v-for="opt in sourceOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('bizModuleManage.description')">
          <el-input v-model="form.description" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="saveModule">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import api from '@/utils/api'
import dayjs from 'dayjs'

const { t } = useI18n()
const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const modules = ref([])
const projects = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(15)
const searchText = ref('')
const projectFilter = ref('')
const sourceFilter = ref('')
const dialogVisible = ref(false)
const editingId = ref(null)
const form = ref(emptyForm())

function emptyForm() {
  return { project: null, name: '', namespace: '', source: 'manual', description: '' }
}

const sourceOptions = [
  { value: 'manual', label: 'manual' },
  { value: 'code', label: 'code' },
  { value: 'runtime', label: 'runtime' },
  { value: 'requirement', label: 'requirement' }
]

function sourceLabel(source) {
  return sourceOptions.find((opt) => opt.value === source)?.label || source || '-'
}

function sourceTagType(source) {
  return { code: 'primary', runtime: 'success', requirement: 'warning' }[source] || 'info'
}

function formatDate(value) {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-'
}

async function fetchModules() {
  loading.value = true
  try {
    const params = { page: currentPage.value, page_size: pageSize.value }
    if (searchText.value) params.search = searchText.value
    if (projectFilter.value) params.project = projectFilter.value
    if (sourceFilter.value) params.source = sourceFilter.value
    const response = await api.get('/biz-modules/', { params })
    modules.value = response.data.results || []
    total.value = response.data.count || 0
  } catch (error) {
    ElMessage.error(t('bizModuleManage.fetchFailed'))
  } finally {
    loading.value = false
  }
}

async function fetchProjects() {
  try {
    const response = await api.get('/projects/')
    projects.value = response.data.results || response.data || []
  } catch (error) {
    ElMessage.error(t('bizModuleManage.fetchProjectsFailed'))
  }
}

function handleFilter() {
  currentPage.value = 1
  fetchModules()
}

function openCreate() {
  editingId.value = null
  form.value = emptyForm()
  if (projectFilter.value) form.value.project = projectFilter.value
  dialogVisible.value = true
}

function openEdit(row) {
  editingId.value = row.id
  form.value = {
    project: row.project,
    name: row.name,
    namespace: row.namespace || '',
    source: row.source || 'manual',
    description: row.description || ''
  }
  dialogVisible.value = true
}

async function saveModule() {
  if (!form.value.project || !form.value.name?.trim()) {
    ElMessage.warning(t('bizModuleManage.requiredFields'))
    return
  }
  saving.value = true
  try {
    const payload = {
      project: form.value.project,
      name: form.value.name.trim(),
      namespace: form.value.namespace.trim(),
      source: form.value.source,
      description: form.value.description
    }
    if (editingId.value) {
      await api.put(`/biz-modules/${editingId.value}/`, payload)
      ElMessage.success(t('bizModuleManage.updateSuccess'))
    } else {
      await api.post('/biz-modules/', payload)
      ElMessage.success(t('bizModuleManage.createSuccess'))
    }
    dialogVisible.value = false
    fetchModules()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || t('bizModuleManage.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function deleteModule(row) {
  try {
    const message = row.case_count
      ? t('bizModuleManage.deleteConfirmWithCases', { count: row.case_count })
      : t('bizModuleManage.deleteConfirm')
    await ElMessageBox.confirm(message, t('common.warning'), {
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
      type: 'warning'
    })
    await api.delete(`/biz-modules/${row.id}/`)
    ElMessage.success(t('bizModuleManage.deleteSuccess'))
    fetchModules()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(t('bizModuleManage.deleteFailed'))
  }
}

function goToCases(id) {
  router.push({ path: '/ai-generation/testcases', query: { module: id } })
}

onMounted(() => {
  fetchProjects()
  fetchModules()
})
</script>

<style lang="scss" scoped>
.page-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding: 20px;
  box-sizing: border-box;
  overflow: hidden;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-title {
  font-size: 22px;
  font-weight: 600;
  margin: 0;
}

.card-container {
  background: #fff;
  border-radius: 4px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.filter-bar {
  margin-bottom: 16px;
}

.table-container {
  flex: 1;
  min-height: 0;
}

.pagination-container {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}
</style>
