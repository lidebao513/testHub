<template>
  <div class="ai-interface-generation">
    <div class="page-header">
      <div>
        <h2>{{ $t('apiTesting.aiInterfaceGeneration.title') }}</h2>
        <p class="subtitle">{{ $t('apiTesting.aiInterfaceGeneration.subtitle') }}</p>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="main-tabs">
      <!-- ============ OpenAPI 导入 ============ -->
      <el-tab-pane :label="$t('apiTesting.aiInterfaceGeneration.importTab')" name="import">
        <el-card shadow="never">
          <p class="section-desc">{{ $t('apiTesting.aiInterfaceGeneration.importDesc') }}</p>
          <el-form label-width="110px" class="import-form">
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.project')">
              <el-select v-model="importForm.project" filterable :placeholder="$t('apiTesting.aiInterfaceGeneration.project')" style="width: 320px">
                <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.importName')">
              <el-input v-model="importForm.name" :placeholder="$t('apiTesting.aiInterfaceGeneration.importName')" style="width: 320px" />
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.source')">
              <el-radio-group v-model="importForm.source">
                <el-radio value="paste">{{ $t('apiTesting.aiInterfaceGeneration.sourcePaste') }}</el-radio>
                <el-radio value="file">{{ $t('apiTesting.aiInterfaceGeneration.sourceFile') }}</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="importForm.source === 'file'" :label="$t('apiTesting.aiInterfaceGeneration.specContent')">
              <input type="file" accept=".json,.yaml,.yml,.txt" @change="onSpecFile" />
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.specFormat')">
              <el-select v-model="importForm.spec_format" style="width: 160px">
                <el-option label="JSON" value="json" />
                <el-option label="YAML" value="yaml" />
              </el-select>
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.specContent')">
              <el-input
                v-model="importForm.raw_spec"
                type="textarea"
                :rows="10"
                :placeholder="$t('apiTesting.aiInterfaceGeneration.specPlaceholder')"
                :disabled="importForm.source === 'file' && !!importFileName"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="importing" @click="doImport">
                {{ importing ? $t('apiTesting.aiInterfaceGeneration.importing') : $t('apiTesting.aiInterfaceGeneration.startImport') }}
              </el-button>
              <span v-if="importForm.source === 'file' && importFileName" class="file-hint">已选文件：{{ importFileName }}</span>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="importResult"
            :title="$t('apiTesting.aiInterfaceGeneration.importSuccess')"
            type="success"
            :closable="false"
            show-icon
          >
            <div class="import-result">
              <div>{{ $t('apiTesting.aiInterfaceGeneration.importedCount') }}：{{ importResult.imported_count }}</div>
              <div>ID：{{ importResult.id }} | {{ $t('apiTesting.aiInterfaceGeneration.status') }}：{{ importResult.status }}</div>
              <div v-if="importResult.error_message" class="err-msg">{{ importResult.error_message }}</div>
            </div>
          </el-alert>
        </el-card>
      </el-tab-pane>

      <!-- ============ 接口用例生成 Agent ============ -->
      <el-tab-pane :label="$t('apiTesting.aiInterfaceGeneration.agentTab')" name="agent">
        <el-card shadow="never">
          <p class="section-desc">{{ $t('apiTesting.aiInterfaceGeneration.agentDesc') }}</p>
          <el-form inline class="filter-bar">
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.selectProject')">
              <el-select v-model="agentProjectId" filterable clearable :placeholder="$t('apiTesting.aiInterfaceGeneration.selectProject')" style="width: 240px" :loading="loadingRequests" @change="loadRequests">
                <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.selectRequest')">
              <el-select v-model="selectedRequestId" filterable :placeholder="$t('apiTesting.aiInterfaceGeneration.requestPlaceholder')" style="width: 360px" :loading="loadingRequests" @change="loadGeneratedCases">
                <el-option v-for="r in requests" :key="r.id" :label="(r.method || '') + ' ' + r.name" :value="r.id" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="!selectedRequestId" :loading="generatingCases" @click="doGenerateCases">
                {{ generatingCases ? $t('apiTesting.aiInterfaceGeneration.generatingCases') : $t('apiTesting.aiInterfaceGeneration.generateCases') }}
              </el-button>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="requestsError"
            type="error"
            :closable="false"
            show-icon
            class="load-error"
          >
            {{ $t('apiTesting.aiInterfaceGeneration.requestsLoadFailed') }}
            <el-button size="small" text type="primary" @click="loadRequests">{{ $t('apiTesting.aiInterfaceGeneration.retry') }}</el-button>
          </el-alert>

          <el-alert
            v-if="casesError"
            type="error"
            :closable="false"
            show-icon
            class="load-error"
          >
            {{ $t('apiTesting.aiInterfaceGeneration.casesLoadFailed') }}
            <el-button size="small" text type="primary" @click="loadGeneratedCases">{{ $t('apiTesting.aiInterfaceGeneration.retry') }}</el-button>
          </el-alert>

          <div v-if="!generatedCases.length && !loadingCases && !casesError" class="empty-tip">{{ $t('apiTesting.aiInterfaceGeneration.noDraft') }}</div>
          <el-table v-else-if="!casesError" v-loading="loadingCases" :data="generatedCases" border stripe size="small">
            <el-table-column prop="api_request_name" :label="$t('apiTesting.aiInterfaceGeneration.selectRequest')" min-width="160" />
            <el-table-column prop="name" :label="$t('apiTesting.aiInterfaceGeneration.caseName')" min-width="180" show-overflow-tooltip />
            <el-table-column :label="$t('apiTesting.aiInterfaceGeneration.category')" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="categoryTag(row.category)">{{ row.category_display || row.category }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="description" :label="$t('apiTesting.aiInterfaceGeneration.caseDesc')" min-width="200" show-overflow-tooltip />
            <el-table-column :label="$t('apiTesting.aiInterfaceGeneration.status')" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'adopted' ? 'success' : (row.status === 'discarded' ? 'info' : 'warning')">
                  {{ row.status_display || row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="$t('apiTesting.aiInterfaceGeneration.operation')" width="260" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="viewCase(row)">{{ $t('apiTesting.aiInterfaceGeneration.view') }}</el-button>
                <el-button size="small" type="success" :disabled="row.status !== 'draft'" :loading="row._busy" @click="adoptCase(row)">
                  {{ $t('apiTesting.aiInterfaceGeneration.adopt') }}
                </el-button>
                <el-button size="small" type="info" :disabled="row.status !== 'draft'" :loading="row._busy" @click="discardCase(row)">
                  {{ $t('apiTesting.aiInterfaceGeneration.discard') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 场景生成 ============ -->
      <el-tab-pane :label="$t('apiTesting.aiInterfaceGeneration.scenarioTab')" name="scenario">
        <el-card shadow="never">
          <p class="section-desc">{{ $t('apiTesting.aiInterfaceGeneration.scenarioDesc') }}</p>
          <el-form label-width="110px">
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.selectProject')">
              <el-select v-model="agentProjectId" filterable clearable :placeholder="$t('apiTesting.aiInterfaceGeneration.selectProject')" style="width: 240px" :loading="loadingRequests" @change="loadRequests">
                <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.selectRequest')">
              <el-select v-model="scenarioRequestIds" multiple filterable :placeholder="$t('apiTesting.aiInterfaceGeneration.requestPlaceholder')" style="width: 520px" :loading="loadingRequests">
                <el-option v-for="r in requests" :key="r.id" :label="(r.method || '') + ' ' + r.name" :value="r.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.scenarioName')">
              <el-input v-model="scenarioName" :placeholder="$t('apiTesting.aiInterfaceGeneration.scenarioNamePlaceholder')" style="width: 360px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="!scenarioRequestIds.length" :loading="generatingScenario" @click="doGenerateScenario">
                {{ generatingScenario ? $t('apiTesting.aiInterfaceGeneration.generatingScenario') : $t('apiTesting.aiInterfaceGeneration.generateScenario') }}
              </el-button>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="requestsError"
            type="error"
            :closable="false"
            show-icon
            class="load-error"
          >
            {{ $t('apiTesting.aiInterfaceGeneration.requestsLoadFailed') }}
            <el-button size="small" text type="primary" @click="loadRequests">{{ $t('apiTesting.aiInterfaceGeneration.retry') }}</el-button>
          </el-alert>

          <el-alert
            v-if="scenarioResult"
            :title="$t('apiTesting.aiInterfaceGeneration.scenarioGenerated')"
            type="success"
            :closable="false"
            show-icon
          >
            <div class="import-result">
              <div>ID：{{ scenarioResult.id }} | {{ scenarioResult.name }} | is_draft：{{ scenarioResult.is_draft }}</div>
              <el-button size="small" type="success" :disabled="!scenarioResult.is_draft" @click="adoptSuite(scenarioResult.id)">
                {{ $t('apiTesting.aiInterfaceGeneration.adoptSuite') }}
              </el-button>
            </div>
          </el-alert>
        </el-card>
      </el-tab-pane>

      <!-- ============ 失败归因 ============ -->
      <el-tab-pane :label="$t('apiTesting.aiInterfaceGeneration.attributionTab')" name="attribution">
        <el-card shadow="never">
          <p class="section-desc">{{ $t('apiTesting.aiInterfaceGeneration.attributionDesc') }}</p>
          <el-form inline class="filter-bar">
            <el-form-item :label="$t('apiTesting.aiInterfaceGeneration.selectExecution')">
              <el-select v-model="selectedExecutionId" filterable :placeholder="$t('apiTesting.aiInterfaceGeneration.executionPlaceholder')" style="width: 420px" :loading="loadingExecutions" @visible-change="onExecSelectOpen">
                <el-option
                  v-for="ex in failedExecutions"
                  :key="ex.id"
                  :label="'# ' + ex.id + ' · ' + (ex.test_suite_name || ex.project_name || '') + ' · ' + (ex.failed_requests || 0) + ' failed'"
                  :value="ex.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="!selectedExecutionId" :loading="attributing" @click="doAttribution">
                {{ attributing ? $t('apiTesting.aiInterfaceGeneration.attributing') : $t('apiTesting.aiInterfaceGeneration.runAttribution') }}
              </el-button>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="execError"
            type="error"
            :closable="false"
            show-icon
            class="load-error"
          >
            {{ $t('apiTesting.aiInterfaceGeneration.execLoadFailed') }}
            <el-button size="small" text type="primary" @click="loadFailedExecutions">{{ $t('apiTesting.aiInterfaceGeneration.retry') }}</el-button>
          </el-alert>

          <div v-if="!failedExecutions.length && !loadingExecutions && !execError" class="empty-tip">{{ $t('apiTesting.aiInterfaceGeneration.noFailedExec') }}</div>

          <el-card v-if="attributionResult" class="attribution-card" shadow="never">
            <template #header>{{ $t('apiTesting.aiInterfaceGeneration.attributionResult') }}</template>
            <pre class="attribution-json">{{ prettyJson(attributionResult) }}</pre>
          </el-card>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 用例详情 / Diff 预览抽屉 -->
    <el-drawer
      v-model="detailVisible"
      :title="$t('apiTesting.aiInterfaceGeneration.detailTitle')"
      size="480px"
      direction="rtl"
    >
      <div v-if="detailCase" class="case-detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="ID">{{ detailCase.id }}</el-descriptions-item>
          <el-descriptions-item :label="$t('apiTesting.aiInterfaceGeneration.caseName')">{{ detailCase.name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('apiTesting.aiInterfaceGeneration.category')">
            <el-tag size="small" :type="categoryTag(detailCase.category)">{{ detailCase.category_display || detailCase.category }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('apiTesting.aiInterfaceGeneration.caseDesc')">{{ detailCase.description || '—' }}</el-descriptions-item>
        </el-descriptions>

        <h4 class="detail-h">{{ $t('apiTesting.aiInterfaceGeneration.requestOverrides') }}</h4>
        <div class="override-block">
          <div v-for="(val, key) in (detailCase.request_overrides || {})" :key="key" class="override-row">
            <span class="override-key">{{ key }}</span>
            <pre class="override-val">{{ formatOverride(val) }}</pre>
          </div>
          <div v-if="!detailCase.request_overrides || !Object.keys(detailCase.request_overrides).length" class="empty-tip">{{ $t('apiTesting.aiInterfaceGeneration.noOverride') }}</div>
        </div>

        <h4 class="detail-h">{{ $t('apiTesting.aiInterfaceGeneration.assertions') }}</h4>
        <div class="assert-list">
          <div v-for="(a, i) in (detailCase.assertions || [])" :key="i" class="assert-card">
            <div class="assert-card-head">
              <span class="assert-name">{{ a.name || ('断言 ' + (i + 1)) }}</span>
              <el-tag size="small" type="info">{{ assertionTypeLabel(a.type) }}</el-tag>
            </div>
            <pre class="assert-body">{{ formatAssertion(a) }}</pre>
          </div>
          <div v-if="!detailCase.assertions || !detailCase.assertions.length" class="empty-tip">{{ $t('apiTesting.aiInterfaceGeneration.noAssertion') }}</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '@/utils/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getApiProjects,
  getApiRequests,
  importOpenApi,
  generateCases,
  getGeneratedCases,
  adoptGeneratedCase,
  discardGeneratedCase,
  generateScenario,
  adoptTestSuite,
  getExecutions
} from '@/api/api-testing'

const { t } = useI18n()

const activeTab = ref('import')
const projects = ref([])
const requests = ref([])

// 导入
const importForm = reactive({ project: null, name: '', source: 'paste', spec_format: 'json', raw_spec: '' })
const importing = ref(false)
const importResult = ref(null)
const importFileName = ref('')

// 加载态 / 错误重试
const loadingProjects = ref(false)
const loadingRequests = ref(false)
const loadingCases = ref(false)
const loadingExecutions = ref(false)
const projectsError = ref(false)
const requestsError = ref(false)
const casesError = ref(false)
const execError = ref(false)

// 详情抽屉（diff 视图）
const detailVisible = ref(false)
const detailCase = ref(null)

// Agent
const agentProjectId = ref(null)
const selectedRequestId = ref(null)
const generatedCases = ref([])
const generatingCases = ref(false)

// 场景
const scenarioRequestIds = ref([])
const scenarioName = ref('')
const generatingScenario = ref(false)
const scenarioResult = ref(null)

// 归因
const failedExecutions = ref([])
const selectedExecutionId = ref(null)
const attributing = ref(false)
const attributionResult = ref(null)

async function loadProjects() {
  loadingProjects.value = true
  projectsError.value = false
  try {
    const res = await getApiProjects({ page_size: 200 })
    projects.value = res.data.results || res.data || []
    if (projects.value.length && !importForm.project) importForm.project = projects.value[0].id
  } catch (e) {
    projectsError.value = true
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    loadingProjects.value = false
  }
}

async function loadRequests() {
  loadingRequests.value = true
  requestsError.value = false
  try {
    const params = { page_size: 500 }
    if (agentProjectId.value) params.project = agentProjectId.value
    const res = await getApiRequests(params)
    requests.value = res.data.results || res.data || []
  } catch (e) {
    requestsError.value = true
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    loadingRequests.value = false
  }
}

function onSpecFile(e) {
  const file = e.target.files && e.target.files[0]
  if (!file) return
  importFileName.value = file.name
  const reader = new FileReader()
  reader.onload = () => {
    importForm.raw_spec = String(reader.result || '')
  }
  reader.readAsText(file)
}

async function doImport() {
  if (!importForm.raw_spec.trim()) {
    ElMessage.warning(t('apiTesting.aiInterfaceGeneration.specPlaceholder'))
    return
  }
  importing.value = true
  importResult.value = null
  try {
    const payload = {
      raw_spec: importForm.raw_spec,
      spec_format: importForm.spec_format,
      name: importForm.name || undefined,
      source: importForm.source,
      project_id: importForm.project || undefined
    }
    const res = await importOpenApi(payload)
    importResult.value = res.data
    if (res.data.status === 'failed') {
      ElMessage.warning(res.data.error_message || t('common.error'))
    } else {
      ElMessage.success(t('apiTesting.aiInterfaceGeneration.importSuccess'))
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    importing.value = false
  }
}

async function loadGeneratedCases() {
  if (!selectedRequestId.value) {
    generatedCases.value = []
    casesError.value = false
    return
  }
  loadingCases.value = true
  casesError.value = false
  try {
    const res = await getGeneratedCases({ api_request: selectedRequestId.value, page_size: 200 })
    generatedCases.value = res.data.results || res.data || []
  } catch (e) {
    casesError.value = true
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    loadingCases.value = false
  }
}

async function doGenerateCases() {
  generatingCases.value = true
  try {
    const res = await generateCases(selectedRequestId.value, {})
    ElMessage.success(t('apiTesting.aiInterfaceGeneration.casesGenerated') + '：' + (res.data.count || 0))
    await loadGeneratedCases()
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    generatingCases.value = false
  }
}

function viewCase(row) {
  detailCase.value = row
  detailVisible.value = true
}

function formatOverride(val) {
  if (val === null || val === undefined || val === '') return '—'
  if (typeof val === 'string') return val
  try {
    return JSON.stringify(val, null, 2)
  } catch {
    return String(val)
  }
}

function assertionTypeLabel(type) {
  const map = {
    status_code: t('apiTesting.interface.assertionTypes.statusCode'),
    response_time: t('apiTesting.interface.assertionTypes.responseTime'),
    contains: t('apiTesting.interface.assertionTypes.contains'),
    json_path: t('apiTesting.interface.assertionTypes.jsonPath'),
    header: t('apiTesting.interface.assertionTypes.header'),
    equals: t('apiTesting.interface.assertionTypes.equals')
  }
  return map[type] || type || '—'
}

function formatAssertion(a) {
  if (!a || typeof a !== 'object') return '—'
  const clone = {}
  for (const k of Object.keys(a)) {
    if (k === 'name' || k === 'type') continue
    const v = a[k]
    if (v === null || v === undefined || v === '') continue
    clone[k] = v
  }
  if (!Object.keys(clone).length) return '（无附加参数）'
  try {
    return JSON.stringify(clone, null, 2)
  } catch {
    return String(clone)
  }
}

async function adoptCase(row) {
  try {
    await ElMessageBox.confirm(
      t('apiTesting.aiInterfaceGeneration.adoptConfirmContent', { name: row.name }),
      t('apiTesting.aiInterfaceGeneration.adoptConfirmTitle'),
      { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') }
    )
  } catch {
    return
  }
  row._busy = true
  try {
    await adoptGeneratedCase(row.id)
    ElMessage.success(t('apiTesting.aiInterfaceGeneration.adopted'))
    await loadGeneratedCases()
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    row._busy = false
  }
}

async function discardCase(row) {
  row._busy = true
  try {
    await discardGeneratedCase(row.id)
    ElMessage.success(t('apiTesting.aiInterfaceGeneration.discarded'))
    await loadGeneratedCases()
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    row._busy = false
  }
}

async function doGenerateScenario() {
  generatingScenario.value = true
  scenarioResult.value = null
  try {
    const res = await generateScenario({
      request_ids: scenarioRequestIds.value,
      project_id: agentProjectId.value || undefined,
      name: scenarioName.value || undefined
    })
    scenarioResult.value = res.data
    ElMessage.success(t('apiTesting.aiInterfaceGeneration.scenarioGenerated'))
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    generatingScenario.value = false
  }
}

async function adoptSuite(id) {
  try {
    await ElMessageBox.confirm(
      t('apiTesting.aiInterfaceGeneration.adoptSuiteConfirmContent'),
      t('apiTesting.aiInterfaceGeneration.adoptSuiteConfirmTitle'),
      { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') }
    )
  } catch {
    return
  }
  try {
    await adoptTestSuite(id)
    ElMessage.success(t('apiTesting.aiInterfaceGeneration.adoptSuite') + ' ✓')
    if (scenarioResult.value) scenarioResult.value.is_draft = false
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  }
}

async function loadFailedExecutions() {
  loadingExecutions.value = true
  execError.value = false
  try {
    const res = await getExecutions({ status: 'FAILED', page_size: 200 })
    failedExecutions.value = res.data.results || res.data || []
  } catch (e) {
    execError.value = true
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    loadingExecutions.value = false
  }
}

function onExecSelectOpen(visible) {
  if (visible && !failedExecutions.value.length) loadFailedExecutions()
}

async function doAttribution() {
  attributing.value = true
  attributionResult.value = null
  try {
    const res = await getAttribution(selectedExecutionId.value, {})
    attributionResult.value = res.data.attribution
    ElMessage.success(t('apiTesting.aiInterfaceGeneration.attributionResult'))
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    attributing.value = false
  }
}

function categoryTag(category) {
  if (category === 'normal') return 'success'
  if (category === 'boundary') return 'warning'
  return 'danger'
}

function prettyJson(obj) {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

onMounted(() => {
  loadProjects()
  loadRequests()
})
</script>

<style scoped>
.ai-interface-generation { padding: 16px; }
.page-header { margin-bottom: 12px; }
.page-header h2 { margin: 0; }
.subtitle { color: #888; margin: 4px 0 0; font-size: 13px; }
.section-desc { color: #666; margin: 0 0 16px; font-size: 13px; }
.filter-bar { margin-bottom: 16px; }
.empty-tip { color: #999; padding: 30px; text-align: center; }
.import-result { line-height: 1.9; font-size: 13px; }
.file-hint { margin-left: 12px; color: #409eff; font-size: 13px; }
.attribution-card { margin-top: 16px; }
.attribution-json { background: #f5f7fa; padding: 12px; border-radius: 6px; max-height: 420px; overflow: auto; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
.load-error { margin-bottom: 12px; }
.load-error .el-button { margin-left: 8px; vertical-align: baseline; }
.case-detail { font-size: 13px; }
.detail-h { margin: 18px 0 10px; font-size: 14px; font-weight: 600; color: #303133; border-left: 3px solid #409eff; padding-left: 8px; }
.override-block { display: flex; flex-direction: column; gap: 8px; }
.override-row { display: flex; flex-direction: column; gap: 4px; }
.override-key { font-weight: 600; color: #606266; text-transform: uppercase; font-size: 12px; }
.override-val { background: #f5f7fa; padding: 8px 10px; border-radius: 6px; margin: 0; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 260px; overflow: auto; }
.assert-list { display: flex; flex-direction: column; gap: 10px; }
.assert-card { border: 1px solid #ebeef5; border-radius: 6px; padding: 10px; }
.assert-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.assert-name { font-weight: 600; color: #303133; }
.assert-body { background: #f5f7fa; padding: 8px 10px; border-radius: 6px; margin: 0; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
</style>
