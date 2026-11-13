/**
 * 需求分析模块相关 API
 */
import request from '@/utils/api'

// ==================== 生成行为配置 ====================

// 获取所有生成行为配置
export function getGenerationConfigs(params) {
  return request({
    url: '/requirement-analysis/generation-config/',
    method: 'get',
    params
  })
}

// 获取生成行为配置详情
export function getGenerationConfigDetail(id) {
  return request({
    url: `/requirement-analysis/generation-config/${id}/`,
    method: 'get'
  })
}

// 创建生成行为配置
export function createGenerationConfig(data) {
  return request({
    url: '/requirement-analysis/generation-config/',
    method: 'post',
    data
  })
}

// 更新生成行为配置
export function updateGenerationConfig(id, data) {
  return request({
    url: `/requirement-analysis/generation-config/${id}/`,
    method: 'put',
    data
  })
}

// 删除生成行为配置
export function deleteGenerationConfig(id) {
  return request({
    url: `/requirement-analysis/generation-config/${id}/`,
    method: 'delete'
  })
}

// 获取活跃的生成行为配置
export function getActiveGenerationConfig() {
  return request({
    url: '/requirement-analysis/generation-config/active/',
    method: 'get'
  })
}

// ==================== AI 模型配置 ====================

// 获取所有 AI 模型配置
export function getAIModelConfigs(params) {
  return request({
    url: '/requirement-analysis/ai-models/',
    method: 'get',
    params
  })
}

// 获取活跃的 AI 模型配置
export function getActiveAIModelConfig(modelType, role) {
  return request({
    url: '/requirement-analysis/ai-models/active/',
    method: 'get',
    params: { model_type: modelType, role }
  })
}

// 创建 AI 模型配置
export function createAIModelConfig(data) {
  return request({
    url: '/requirement-analysis/ai-models/',
    method: 'post',
    data
  })
}

// 更新 AI 模型配置
export function updateAIModelConfig(id, data) {
  return request({
    url: `/requirement-analysis/ai-models/${id}/`,
    method: 'put',
    data
  })
}

// 删除 AI 模型配置
export function deleteAIModelConfig(id) {
  return request({
    url: `/requirement-analysis/ai-models/${id}/`,
    method: 'delete'
  })
}

// ==================== 提示词配置 ====================

// 获取所有提示词配置
export function getPromptConfigs(params) {
  return request({
    url: '/requirement-analysis/prompts/',
    method: 'get',
    params
  })
}

// 获取活跃的提示词配置
export function getActivePromptConfig(promptType) {
  return request({
    url: '/requirement-analysis/prompts/active/',
    method: 'get',
    params: { prompt_type: promptType }
  })
}

// 创建提示词配置
export function createPromptConfig(data) {
  return request({
    url: '/requirement-analysis/prompts/',
    method: 'post',
    data
  })
}

// 更新提示词配置
export function updatePromptConfig(id, data) {
  return request({
    url: `/requirement-analysis/prompts/${id}/`,
    method: 'put',
    data
  })
}

// 删除提示词配置
export function deletePromptConfig(id) {
  return request({
    url: `/requirement-analysis/prompts/${id}/`,
    method: 'delete'
  })
}

// ==================== P0-1 需求三件套 ====================

// 获取需求文档列表
export function getRequirementDocuments(params) {
  return request({
    url: '/requirement-analysis/documents/',
    method: 'get',
    params
  })
}

// 针对文档生成需求三件套（功能清单/界面描述/待澄清项）
export function generateThreeProducts(docId) {
  return request({
    url: `/requirement-analysis/documents/${docId}/generate-products/`,
    method: 'post'
  })
}

// 获取文档的需求三件套
export function getThreeProducts(docId) {
  return request({
    url: `/requirement-analysis/documents/${docId}/products/`,
    method: 'get'
  })
}

// 将澄清回答喂回，修订原三件套产物（对话修订闭环）
export function clarifyRequirement(docId, data) {
  return request({
    url: `/requirement-analysis/documents/${docId}/clarify/`,
    method: 'post',
    data
  })
}
