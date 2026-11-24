<template>
  <div class="url-first-page">
    <el-alert type="info" :closable="false" show-icon class="page-banner">
      <template #title>地址优先生成 · 独立入口（当前批次：结果一键回流用例库）</template>
      <template #default>
        本入口与「代码生成（testgen）」菜单同级、彼此独立。
        <strong>现有代码扫描功能及其链路不做任何改动</strong>。项目查重/建项、模块读取/扫描、地址通道生成已接入；
        生成完成后可一键把本次结果幂等回流到 TestHub 用例库（排除作废用例，重复回流只更新不新增）。
        以被测地址+账号密码为主，变更文件经轻量取码桥辅助靶向（只取文本、不 clone、不展开代码用例）。
        发生冲突时一律走新功能，不回改旧功能。
      </template>
    </el-alert>

    <el-steps
      :active="genResult ? 3 : modules.length ? 2 : projectInfo ? 1 : 0"
      finish-status="success"
      simple
      class="steps"
    >
      <el-step title="输入被测地址" />
      <el-step title="识别 / 创建项目" />
      <el-step title="读取 / 扫描模块" />
      <el-step title="生成用例" />
    </el-steps>

    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <span>① 被测环境与凭证</span>
          </template>
          <el-form :model="form" label-width="104px">
            <el-form-item label="被测地址" required>
              <el-input
                v-model="form.baseUrl"
                placeholder="https://host:port/path（端口 80/443 可省略）"
                clearable
              />
            </el-form-item>
            <el-form-item label="登录账号">
              <el-input v-model="form.username" placeholder="可选，留空则匿名访问" clearable />
            </el-form-item>
            <el-form-item label="登录密码">
              <el-input v-model="form.password" type="password" show-password placeholder="可选" />
            </el-form-item>
            <el-form-item label="动态码 OTP">
              <el-input v-model="form.otp" placeholder="可选，30~60 秒有效，生成前再填" clearable />
            </el-form-item>
            <el-form-item label="变更文件">
              <el-input
                v-model="form.changedFiles"
                type="textarea"
                :rows="3"
                placeholder="可选，每行一个文件路径；后续作为靶向辅助证据，不产出代码用例"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="parsing" @click="parseTarget">解析地址</el-button>
              <el-button @click="resetForm">清空</el-button>
            </el-form-item>
          </el-form>

          <el-alert v-if="parseError" type="error" :closable="false" show-icon>
            {{ parseError }}
          </el-alert>

          <div v-if="preview" class="preview">
            <div><b>归一项目键：</b><code>{{ preview.normalized }}</code></div>
            <div>
              <b>Host：</b><code>{{ preview.host }}</code>
              &nbsp;·&nbsp;<b>Scheme：</b>{{ preview.scheme }}
            </div>
            <div class="hint">
              该键是项目查重依据；点右侧「识别项目」即可按此键复用或新建两侧项目。
              本处解析本身只在浏览器本地进行；凭证只停留在当前页面，不随解析发送、不落库。
            </div>
          </div>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never">
          <template #header><span>② 项目与模块</span></template>
          <el-alert v-if="recognizeError" type="error" :closable="false" show-icon>
            {{ recognizeError }}
          </el-alert>
          <div v-if="projectInfo" class="project-info">
            <div class="project-tag">
              <el-tag :type="projectInfo.created ? 'success' : 'info'" effect="dark">
                {{ projectInfo.created ? '新建项目' : '命中已有项目' }}
              </el-tag>
            </div>
            <div>
              <b>TestHub 项目：</b>{{ projectInfo.testhub_project.name }}
              （ID {{ projectInfo.testhub_project.id }}）
            </div>
            <div><b>testgen 项目：</b>ID {{ projectInfo.testgen_project.id }}</div>
            <div><b>归一地址：</b><code>{{ projectInfo.normalized_url }}</code></div>
          </div>
          <el-empty v-else description="填写地址后点「识别项目」：已有则读取，没有则新建两侧项目">
            <el-button type="primary" :loading="recognizing" @click="recognizeProject">
              识别项目
            </el-button>
          </el-empty>

          <div v-if="projectInfo" class="module-block">
            <div class="module-header">
              <b>模块清单</b>
              <span class="hint">
                共 {{ moduleSummary.total }} 项（扫描 {{ moduleSummary.scan }} · 既有 {{ moduleSummary.existing }}）
              </span>
              <el-button size="mini" :loading="modulesLoading" @click="loadModules">刷新</el-button>
              <el-button size="mini" type="primary" :loading="scanning" @click="scanModules">
                扫描模块
              </el-button>
            </div>
            <el-alert v-if="moduleError" type="error" :closable="false" show-icon>
              {{ moduleError }}
            </el-alert>
            <div v-if="scanInfo" class="scan-summary">
              本次发现 {{ scanInfo.pages }} 页 / {{ scanInfo.routes }} 路由 / {{ scanInfo.elements }} 元素，
              入库 {{ scanInfo.persisted }} 项；{{ scanInfo.logged_in ? '已登录' : '匿名访问' }}。
            </div>
            <el-table v-if="modules.length" :data="modules" size="mini" stripe max-height="300">
              <el-table-column prop="name" label="模块 / 页面" min-width="150" show-overflow-tooltip />
              <el-table-column label="来源" width="100">
                <template #default="{ row }">
                  <el-tag :type="sourceTagType(row.source)" size="mini">
                    {{ sourceLabel(row.source) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="fp_count" label="功能点" width="70" align="center" />
              <el-table-column prop="description" label="说明" min-width="140" show-overflow-tooltip />
            </el-table>
            <el-empty v-else-if="!modulesLoading" description="暂无模块：点「扫描模块」让浏览器发现页面/路由">
              <el-button type="primary" :loading="scanning" @click="scanModules">扫描模块</el-button>
            </el-empty>
            <div class="hint module-note">
              扫描只做浏览器发现（不生成、不执行用例），结果复用既有描述缓存按页面路径幂等入库；
              凭证只用于本次扫描，不落库。OTP 有效期短，请在点扫描前再填入。
            </div>
          </div>
        </el-card>

        <el-card shadow="never" class="mt">
          <template #header><span>③ 生成结果（地址通道）</span></template>
          <div class="gen-controls">
            <el-form :model="genForm" label-width="92px" size="mini" inline>
              <el-form-item label="生成范围">
                <el-select v-model="genForm.runtimeScope" style="width: 150px">
                  <el-option label="变更靶向（推荐）" value="targeted" />
                  <el-option label="全站巡检" value="full" />
                </el-select>
              </el-form-item>
              <el-form-item label="变更仓库">
                <el-input
                  v-model="genForm.repoUrl"
                  placeholder="可选，仅用于取文本做靶向，不 clone"
                  style="width: 240px"
                  clearable
                />
              </el-form-item>
            </el-form>
            <el-button
              type="primary"
              :loading="generating"
              :disabled="!projectInfo"
              @click="generateCases"
            >
              {{ generating ? '生成中…' : '生成用例' }}
            </el-button>
            <span class="hint">需先识别项目；OTP 请在点击生成前再填。</span>
          </div>

          <el-alert v-if="genError" type="error" :closable="false" show-icon class="mt">
            {{ genError }}
          </el-alert>

          <div v-if="genTask" class="gen-status">
            <span>任务 {{ genTask.task_id }}</span>
            <el-tag :type="taskTagType(genTask.state)" size="mini">{{ taskStateLabel(genTask.state) }}</el-tag>
            <el-progress
              :percentage="Math.round((genTask.progress || 0) * 100)"
              :stroke-width="12"
              style="margin-top: 8px"
            />
            <span class="hint">{{ genTask.stage || '排队中' }}</span>
          </div>

          <div v-if="genResult" class="gen-result">
            <el-divider content-position="left">生成概览</el-divider>
            <div class="kpi-row">
              <div class="kpi-item">
                <span class="kpi-label">功能点</span>
                <span class="kpi-value">{{ kpiOf('functional_points') }}</span>
              </div>
              <div class="kpi-item">
                <span class="kpi-label">测试点</span>
                <span class="kpi-value">{{ kpiOf('test_points') }}</span>
              </div>
              <div class="kpi-item">
                <span class="kpi-label">用例</span>
                <span class="kpi-value">{{ kpiOf('cases') }}</span>
              </div>
              <div v-if="bridgeInfo" class="kpi-item">
                <span class="kpi-label">取码桥</span>
                <span class="kpi-value">{{ bridgeInfo.fetched }} 文件</span>
              </div>
            </div>
            <div v-if="channels.length" class="channel-row">
              <span v-for="ch in channels" :key="ch.name" class="channel-chip">
                {{ ch.name }}：<b>{{ ch.tp }}</b> 测试点
              </span>
            </div>
            <div v-if="bridgeInfo && bridgeInfo.notes.length" class="hint bridge-notes">
              <div v-for="(note, i) in bridgeInfo.notes" :key="i">· {{ note }}</div>
            </div>

            <el-divider content-position="left">用例列表</el-divider>
            <div class="case-filter">
              <span class="hint">按通道：</span>
              <el-radio-group v-model="caseChannel" size="mini">
                <el-radio-button label="all">全部</el-radio-button>
                <el-radio-button label="body">代码</el-radio-button>
                <el-radio-button label="impact">影响面</el-radio-button>
                <el-radio-button label="runtime">运行时</el-radio-button>
              </el-radio-group>
              <span class="hint">共 {{ filteredCases.length }} 条</span>
            </div>
            <el-table :data="filteredCases.slice(0, 50)" size="mini" stripe max-height="360">
              <el-table-column prop="tc_no" label="编号" width="120" />
              <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
              <el-table-column prop="priority" label="优先级" width="80" align="center" />
              <el-table-column prop="module" label="模块" width="110" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="row.module">{{ row.module }}</span>
                  <span v-else class="hint">-</span>
                </template>
              </el-table-column>
              <el-table-column label="通道" width="90">
                <template #default="{ row }">
                  <el-tag :type="channelTagType(row.channel)" size="mini">{{ channelLabel(row.channel) }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <div v-if="filteredCases.length > 50" class="hint">仅展示前 50 条，共 {{ filteredCases.length }} 条。</div>

            <el-divider content-position="left">回流用例库</el-divider>
            <div class="sync-block">
              <div class="sync-copy">
                <b>一键回流本次生成结果</b>
                <span class="hint">
                  写入 TestHub 用例库；以 <code>tg:&lt;用例指纹&gt;</code> 幂等，重复点击只更新不新增，
                  sidecar 标记作废的用例不回流。
                </span>
              </div>
              <el-button
                type="success"
                :loading="syncing"
                :disabled="!genTask?.task_id || !projectInfo"
                @click="syncToLibrary"
              >
                {{ syncing ? '回流中…' : '一键回流用例库' }}
              </el-button>
            </div>
            <el-alert v-if="syncError" type="error" :closable="false" show-icon class="mt">
              {{ syncError }}
            </el-alert>
            <div v-if="syncResult" class="sync-summary">
              回流完成：共 {{ syncResult.total }} 条 · 新增 {{ syncResult.created }} ·
              更新 {{ syncResult.updated }} · 跳过 {{ syncResult.skipped }}
              <span v-if="syncResult.bridge" class="hint">
                （AICase 桥接：新增 {{ syncResult.bridge.created }} / 更新 {{ syncResult.bridge.updated }}）
              </span>
            </div>
          </div>

          <el-empty
            v-else-if="!generating"
            description="识别项目后可生成：以被测地址为主，变更文件经轻量取码桥辅助靶向"
          />
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt">
      <template #header><span>本批范围与隔离说明</span></template>
      <ul class="scope-list">
        <li>
          已实现：独立菜单项、独立路由、独立页面、地址解析预览、按归一 URL 查重/建项
          （TestHub + testgen 两侧项目，命中复用、未命中新建）、模块读取与扫描、地址通道生成。
        </li>
        <li>
          模块复用既有数据：扫描结果写入既有描述缓存（命名空间=归一地址、键=页面路径、带指纹幂等），
          既有模块来自功能点的 module 聚合，<strong>不新建模块实体表</strong>，避免同功能两套数据与重复计数。
        </li>
        <li>
          地址通道生成：强制地址通道与生成-only（不执行）；默认「变更靶向」，变更文件经轻量取码桥
          <strong>只取文本供靶向匹配</strong>（不 clone、不展开代码功能点），无仓库地址时退化为按文件名推断；
          「全站巡检」走既有地址通道。复用现有任务轮询/用例端点，幂等键含地址与变更集指纹。
        </li>
        <li>
          结果一键回流用例库：按本次生成任务取用例，排除 sidecar 作废行，复用既有
          <code>tg:&lt;用例指纹&gt;</code> 幂等写入 TestHub TestCase（重复回流更新而非新增），
          并沿用旧回流的 AICase 桥接语义（失败不影响主回流）。
        </li>
        <li>
          隔离保证：未修改「代码生成（testgen）」页面、其行为、现有 sidecar 业务函数与主平台代理逻辑；
          新增能力只通过新增文件、新增端点与本页面扩展。
        </li>
      </ul>
    </el-card>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/utils/api'

const form = reactive({
  baseUrl: '',
  username: '',
  password: '',
  otp: '',
  changedFiles: ''
})
const preview = ref(null)
const parseError = ref('')
const parsing = ref(false)
const recognizing = ref(false)
const projectInfo = ref(null)
const recognizeError = ref('')
const modules = ref([])
const modulesLoading = ref(false)
const scanning = ref(false)
const moduleError = ref('')
const scanInfo = ref(null)

// —— 地址通道生成 ——
const genForm = reactive({ runtimeScope: 'targeted', repoUrl: '' })
const generating = ref(false)
const genTask = ref(null)
const genResult = ref(null)
const genError = ref('')
const allCases = ref([])
const caseChannel = ref('all')
const syncing = ref(false)
const syncResult = ref(null)
const syncError = ref('')
let pollTimer = null

const moduleSummary = computed(() => ({
  total: modules.value.length,
  scan: modules.value.filter((m) => m.source === 'scan').length,
  existing: modules.value.filter((m) => m.source === 'existing').length
}))

function sourceLabel(source) {
  if (source === 'both') return '两者'
  return source === 'scan' ? '扫描' : '既有'
}

function sourceTagType(source) {
  if (source === 'both') return 'warning'
  return source === 'scan' ? 'success' : 'info'
}

function taskStateLabel(state) {
  return { success: '成功', failed: '失败', cancelled: '已取消', running: '运行中', pending: '排队中' }[state] || state || '排队中'
}
function taskTagType(state) {
  return { success: 'success', failed: 'danger', cancelled: 'info', running: 'warning' }[state] || 'info'
}
function channelLabel(ch) {
  return { body: '代码', impact: '影响面', runtime: '运行时' }[ch] || ch || '代码'
}
function channelTagType(ch) {
  return { body: 'info', impact: 'warning', runtime: 'success' }[ch] || 'info'
}
function kpiOf(key) {
  return genResult.value?.counts?.[key] ?? 0
}
function caseChannelOf(c) {
  const origin = c.case_type || c.test_type || ''
  if (String(origin).startsWith('impact')) return 'impact'
  if (String(c.source || '').startsWith('runtime:')) return 'runtime'
  return c.channel || 'body'
}
const channels = computed(() => {
  const q = genResult.value?.quality?.channels
  if (!q) return []
  return ['body', 'impact', 'runtime']
    .filter((name) => q[name])
    .map((name) => ({ name: channelLabel(name), tp: q[name].tp ?? 0 }))
})
const bridgeInfo = computed(() => genResult.value?.url_code_bridge || null)
const filteredCases = computed(() =>
  caseChannel.value === 'all'
    ? allCases.value
    : allCases.value.filter((c) => caseChannelOf(c) === caseChannel.value)
)

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function pollTask(taskId) {
  try {
    const { data } = await api.get(`/testgen/tasks/${encodeURIComponent(taskId)}`)
    genTask.value = data?.task || data || null
    const state = data?.state || data?.task?.state
    if (state === 'success' || state === 'failed' || state === 'cancelled') {
      stopPolling()
      generating.value = false
      if (state === 'success') {
        genResult.value = data.result || data.task?.result || null
        await loadCases(taskId)
      } else {
        genError.value = data.error || data.task?.error || '生成失败'
      }
      return
    }
    pollTimer = setTimeout(() => pollTask(taskId), 1500)
  } catch (err) {
    stopPolling()
    generating.value = false
    genError.value = err?.response?.data?.error || err?.message || '轮询任务状态失败'
  }
}

async function loadCases(taskId) {
  try {
    const { data } = await api.get(`/testgen/tasks/${encodeURIComponent(taskId)}/cases`)
    allCases.value = data?.cases || []
  } catch (err) {
    allCases.value = []
  }
}

async function syncToLibrary() {
  const taskId = genTask.value?.task_id
  if (!taskId || !projectInfo.value) {
    syncError.value = '请先生成用例并识别项目'
    return
  }
  try {
    await ElMessageBox.confirm(
      `将任务 ${taskId} 的生成结果回流到「${projectInfo.value.testhub_project.name}」用例库；重复回流只更新不新增。是否继续？`,
      '一键回流用例库',
      { type: 'warning', confirmButtonText: '确认回流', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  syncing.value = true
  syncError.value = ''
  syncResult.value = null
  try {
    const { data } = await api.post('/testgen/url-generation/sync', {
      task_id: taskId,
      project_id: projectInfo.value.testhub_project?.id,
      base_url: form.baseUrl.trim()
    })
    if (!data?.ok) throw new Error(data?.error || '回流失败')
    syncResult.value = data
    ElMessage.success(`回流完成：新增 ${data.created} 条，更新 ${data.updated} 条`)
  } catch (err) {
    syncError.value = err?.response?.data?.error || err?.message || '回流失败'
  } finally {
    syncing.value = false
  }
}

async function generateCases() {
  if (!projectInfo.value || !form.baseUrl.trim()) {
    genError.value = '请先识别项目'
    return
  }
  stopPolling()
  generating.value = true
  genError.value = ''
  genTask.value = null
  genResult.value = null
  allCases.value = []
  syncResult.value = null
  syncError.value = ''
  try {
    const { data } = await api.post('/testgen/url-generation/generate', {
      test_url: form.baseUrl.trim(),
      login_user: form.username,
      login_password: form.password,
      login_otp: form.otp,
      runtime_routes: [],
      changed_files: form.changedFiles
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
      repo_url: genForm.repoUrl.trim(),
      project_id: projectInfo.value.testgen_project?.id,
      runtime_scope: genForm.runtimeScope,
      scopes: []
    })
    if (!data?.ok && !data?.task_id) throw new Error(data?.error || '提交生成失败')
    const taskId = data.task_id
    genTask.value = data
    pollTimer = setTimeout(() => pollTask(taskId), 800)
  } catch (err) {
    generating.value = false
    genError.value = err?.response?.data?.error || err?.message || '提交生成失败'
  }
}

/** 纯前端归一：scheme + host(去默认端口) + path(去尾斜杠)，作为后续项目查重键的预览。 */
function normalizeUrl(raw) {
  const value = (raw || '').trim()
  if (!value) {
    throw new Error('请先填写被测地址')
  }
  const candidate = /^https?:\/\//i.test(value) ? value : `http://${value}`
  let parsed
  try {
    parsed = new URL(candidate)
  } catch {
    throw new Error('地址格式无法解析，请填写完整的 http(s) 地址')
  }
  if (!parsed.hostname) {
    throw new Error('地址缺少主机名')
  }
  const scheme = parsed.protocol.replace(':', '')
  const defaultPort = scheme === 'https' ? '443' : '80'
  const port = parsed.port && parsed.port !== defaultPort ? `:${parsed.port}` : ''
  const host = `${parsed.hostname}${port}`
  const path = parsed.pathname.replace(/\/+$/, '') || '/'
  return { scheme, host, normalized: `${scheme}://${host}${path}` }
}

function parseTarget() {
  parseError.value = ''
  preview.value = null
  parsing.value = true
  try {
    preview.value = normalizeUrl(form.baseUrl)
  } catch (err) {
    parseError.value = err?.message || '地址解析失败'
  } finally {
    parsing.value = false
  }
}

function resetForm() {
  stopPolling()
  form.baseUrl = ''
  form.username = ''
  form.password = ''
  form.otp = ''
  form.changedFiles = ''
  genForm.runtimeScope = 'targeted'
  genForm.repoUrl = ''
  preview.value = null
  parseError.value = ''
  projectInfo.value = null
  recognizeError.value = ''
  modules.value = []
  moduleError.value = ''
  scanInfo.value = null
  generating.value = false
  genTask.value = null
  genResult.value = null
  genError.value = ''
  allCases.value = []
  caseChannel.value = 'all'
  syncResult.value = null
  syncError.value = ''
  syncing.value = false
}

async function loadModules() {
  if (!projectInfo.value || !form.baseUrl.trim()) return
  modulesLoading.value = true
  moduleError.value = ''
  try {
    const { data } = await api.get('/testgen/url-projects/modules', {
      params: { base_url: form.baseUrl.trim() }
    })
    if (!data?.ok) throw new Error(data?.error || '读取模块失败')
    modules.value = data.modules || []
  } catch (err) {
    moduleError.value = err?.response?.data?.error || err?.message || '读取模块失败'
  } finally {
    modulesLoading.value = false
  }
}

async function scanModules() {
  if (!projectInfo.value || !form.baseUrl.trim()) {
    moduleError.value = '请先识别项目'
    return
  }
  scanning.value = true
  moduleError.value = ''
  try {
    const { data } = await api.post('/testgen/url-projects/modules/scan', {
      base_url: form.baseUrl.trim(),
      login_user: form.username,
      login_password: form.password,
      login_otp: form.otp,
      max_pages: 8,
      timeout: 8
    })
    if (!data?.ok) throw new Error(data?.error || '扫描模块失败')
    modules.value = data.modules || []
    scanInfo.value = data.scan || null
  } catch (err) {
    moduleError.value = err?.response?.data?.error || err?.message || '扫描模块失败'
  } finally {
    scanning.value = false
  }
}

async function recognizeProject() {
  if (!form.baseUrl.trim()) {
    recognizeError.value = '请先填写被测地址'
    return
  }
  recognizing.value = true
  recognizeError.value = ''
  projectInfo.value = null
  modules.value = []
  scanInfo.value = null
  try {
    const { data } = await api.post('/testgen/url-projects/ensure', {
      base_url: form.baseUrl.trim()
    })
    if (!data?.ok) throw new Error(data?.error || '识别失败')
    projectInfo.value = data
    await loadModules()
  } catch (err) {
    recognizeError.value = err?.response?.data?.error || err?.message || '识别项目失败'
  } finally {
    recognizing.value = false
  }
}
</script>

<style scoped>
.url-first-page {
  padding: 16px;
}
.page-banner {
  margin-bottom: 16px;
}
.steps {
  margin-bottom: 16px;
}
.mt {
  margin-top: 16px;
}
.preview {
  margin-top: 12px;
  padding: 12px 16px;
  background: #f5f7fa;
  border-radius: 4px;
  line-height: 1.9;
}
.preview code {
  color: #409eff;
}
.preview .hint {
  color: #909399;
  font-size: 12px;
}
.project-info {
  padding: 12px 14px;
  background: #f5f7fa;
  border-radius: 4px;
  line-height: 2;
}
.project-info code {
  color: #409eff;
}
.project-info .hint {
  color: #909399;
  font-size: 12px;
}
.project-tag {
  margin-bottom: 8px;
}
.module-block {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
}
.module-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.module-header .hint {
  flex: 1;
  color: #909399;
  font-size: 12px;
}
.scan-summary {
  margin-bottom: 8px;
  color: #606266;
  font-size: 12px;
}
.module-note {
  margin-top: 10px;
}
.scope-list {
  margin: 0;
  padding-left: 20px;
  line-height: 2;
  color: #606266;
}
.gen-controls {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.gen-controls .hint {
  color: #909399;
  font-size: 12px;
}
.gen-status {
  margin: 10px 0;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 13px;
}
.gen-status .hint {
  color: #909399;
  font-size: 12px;
}
.kpi-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin: 8px 0;
}
.kpi-item {
  flex: 1;
  min-width: 92px;
  padding: 10px;
  background: #f5f7fa;
  border-radius: 4px;
  text-align: center;
}
.kpi-label {
  display: block;
  color: #909399;
  font-size: 12px;
}
.kpi-value {
  display: block;
  margin-top: 4px;
  color: #303133;
  font-size: 20px;
  font-weight: 600;
}
.channel-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin: 8px 0;
  font-size: 12px;
  color: #606266;
}
.channel-chip {
  padding: 3px 10px;
  background: #ecf5ff;
  border-radius: 12px;
}
.bridge-notes {
  margin: 8px 0;
  line-height: 1.8;
}
.case-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.sync-block {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 4px;
}
.sync-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.sync-copy .hint {
  color: #909399;
  font-size: 12px;
}
.sync-copy code {
  color: #409eff;
}
.sync-summary {
  margin-top: 8px;
  padding: 8px 12px;
  background: #f0f9eb;
  border-radius: 4px;
  color: #529b2e;
  font-size: 13px;
}
.sync-summary .hint {
  color: #909399;
}
</style>
