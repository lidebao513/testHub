import request from '@/utils/api'

// 仪表盘相关API
export function getDashboardStats() {
  return request({
    url: '/api-testing/dashboard/stats/',
    method: 'get'
  })
}

// 获取定时任务列表
export function getScheduledTasks(params) {
  return request({
    url: '/api-testing/scheduled-tasks/',
    method: 'get',
    params
  })
}

// 创建定时任务
export function createScheduledTask(data) {
  return request({
    url: '/api-testing/scheduled-tasks/',
    method: 'post',
    data
  })
}

// 更新定时任务
export function updateScheduledTask(id, data) {
  return request({
    url: `/api-testing/scheduled-tasks/${id}/`,
    method: 'patch',
    data
  })
}

// 删除定时任务
export function deleteScheduledTask(id) {
  return request({
    url: `/api-testing/scheduled-tasks/${id}/`,
    method: 'delete'
  })
}

// 立即执行定时任务
export function runScheduledTask(id) {
  return request({
    url: `/api-testing/scheduled-tasks/${id}/run_now/`,
    method: 'post'
  })
}

// 获取执行日志
export function getExecutionLogs(taskId, params = {}) {
  return request({
    url: `/api-testing/scheduled-tasks/${taskId}/execution_logs/`,
    method: 'get',
    params
  })
}

// 获取测试套件列表
export function getTestSuites(params) {
  return request({
    url: '/api-testing/test-suites/',
    method: 'get',
    params
  })
}

// 获取API请求列表
export function getApiRequests(params) {
  return request({
    url: '/api-testing/requests/',
    method: 'get',
    params
  })
}

// 获取环境列表
export function getEnvironments(params) {
  return request({
    url: '/api-testing/environments/',
    method: 'get',
    params
  })
}

// 获取项目列表
export function getApiProjects(params) {
  return request({
    url: '/api-testing/projects/',
    method: 'get',
    params
  })
}

// 获取集合列表
export function getApiCollections(params) {
  return request({
    url: '/api-testing/collections/',
    method: 'get',
    params
  })
}

// 执行测试套件
export function executeTestSuite(id, data) {
  return request({
    url: `/api-testing/test-suites/${id}/execute/`,
    method: 'post',
    data
  })
}

// 执行API请求
export function executeApiRequest(id, data) {
  return request({
    url: `/api-testing/api-requests/${id}/execute/`,
    method: 'post',
    data
  })
}

// 获取执行结果
export function getExecutionResult(id) {
  return request({
    url: `/api-testing/executions/${id}/`,
    method: 'get'
  })
}

// 获取请求历史
export function getRequestHistory(params) {
  return request({
    url: '/api-testing/histories/',
    method: 'get',
    params
  })
}

// 删除请求历史
export function deleteRequestHistory(id) {
  return request({
    url: `/api-testing/histories/${id}/`,
    method: 'delete'
  })
}

// 批量删除请求历史
export function batchDeleteRequestHistory(ids) {
  return request({
    url: '/api-testing/histories/batch-delete/',
    method: 'post',
    data: { ids }
  })
}

// 获取用户列表
export function getUsers(params) {
  return request({
    url: '/api-testing/users/',
    method: 'get',
    params
  })
}
// 获取操作日志
export function getOperationLogs(params) {
  return request({
    url: '/api-testing/operation-logs/',
    method: 'get',
    params
  })
}

// ==================== P0-2 OpenAPI 导入 + 接口 Agent + AI 归因 ====================

// 解析 swagger/openapi 并落库为接口资产
export function importOpenApi(data) {
  return request({
    url: '/api-testing/spec-imports/import-openapi/',
    method: 'post',
    data
  })
}

// 单接口用例生成 Agent（生成草稿用例）
export function generateCases(requestId, data = {}) {
  return request({
    url: `/api-testing/requests/${requestId}/generate-cases/`,
    method: 'post',
    data
  })
}

// 场景用例生成 Agent（串联多个接口生成草稿套件）
export function generateScenario(data) {
  return request({
    url: '/api-testing/test-suites/scenario/generate/',
    method: 'post',
    data
  })
}

// 将 AI 生成的草稿套件转为正式套件
export function adoptTestSuite(id) {
  return request({
    url: `/api-testing/test-suites/${id}/adopt/`,
    method: 'post'
  })
}

// 获取执行记录列表
export function getExecutions(params) {
  return request({
    url: '/api-testing/test-executions/',
    method: 'get',
    params
  })
}

// 失败执行 AI 归因
export function getAttribution(executionId, data = {}) {
  return request({
    url: `/api-testing/test-executions/${executionId}/attribution/`,
    method: 'post',
    data
  })
}

// 获取 AI 生成的接口用例草稿列表
export function getGeneratedCases(params) {
  return request({
    url: '/api-testing/generated-cases/',
    method: 'get',
    params
  })
}

// 确认采纳一条草稿用例
export function adoptGeneratedCase(id) {
  return request({
    url: `/api-testing/generated-cases/${id}/adopt/`,
    method: 'post'
  })
}

// 丢弃一条草稿用例
export function discardGeneratedCase(id) {
  return request({
    url: `/api-testing/generated-cases/${id}/discard/`,
    method: 'post'
  })
}
