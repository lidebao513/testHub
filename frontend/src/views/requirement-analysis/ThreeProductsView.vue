<template>
  <div class="three-products">
    <div class="page-header">
      <div>
        <h2>{{ $t('threeProducts.title') }}</h2>
        <p class="subtitle">{{ $t('threeProducts.subtitle') }}</p>
      </div>
    </div>

    <el-card class="doc-bar" shadow="never">
      <el-select
        v-model="selectedDocId"
        :placeholder="$t('threeProducts.docPlaceholder')"
        filterable
        style="width: 420px"
        @change="onDocChange"
      >
        <el-option
          v-for="doc in documents"
          :key="doc.id"
          :label="doc.title + '（' + (doc.status_display || doc.status) + '）'"
          :value="doc.id"
        />
      </el-select>
      <el-button
        type="primary"
        :disabled="!selectedDocId"
        :loading="generating"
        @click="onGenerate"
      >
        {{ generating ? $t('threeProducts.generating') : $t('threeProducts.generate') }}
      </el-button>
    </el-card>

    <div v-if="!selectedDocId" class="empty-tip">
      {{ $t('threeProducts.emptyDoc') }}
    </div>

    <div v-else-if="!hasProducts" class="empty-tip">
      {{ $t('threeProducts.notGenerated') }}
    </div>

    <div v-else class="products-grid" v-loading="loadingProducts">
      <!-- 功能清单 -->
      <el-card class="product-col" shadow="never">
        <template #header>
          <span class="col-title">{{ $t('threeProducts.functionalInventory') }}</span>
          <el-tag size="small" type="info">{{ products.functional_inventory.length }}</el-tag>
        </template>
        <el-table :data="products.functional_inventory" size="small" border stripe>
          <el-table-column type="index" :label="$t('threeProducts.seq')" width="50" />
          <el-table-column prop="name" :label="$t('threeProducts.funcName')" min-width="120" />
          <el-table-column prop="summary" :label="$t('threeProducts.funcSummary')" min-width="180" show-overflow-tooltip />
          <el-table-column prop="source_ref" :label="$t('threeProducts.funcSource')" min-width="120" show-overflow-tooltip />
        </el-table>
      </el-card>

      <!-- 界面元素描述 -->
      <el-card class="product-col" shadow="never">
        <template #header>
          <span class="col-title">{{ $t('threeProducts.uiDescriptions') }}</span>
          <el-tag size="small" type="info">{{ products.ui_descriptions.length }}</el-tag>
        </template>
        <el-table :data="products.ui_descriptions" size="small" border stripe>
          <el-table-column prop="page" :label="$t('threeProducts.page')" min-width="110" show-overflow-tooltip />
          <el-table-column prop="element" :label="$t('threeProducts.element')" min-width="110" show-overflow-tooltip />
          <el-table-column prop="desc" :label="$t('threeProducts.uiDesc')" min-width="150" show-overflow-tooltip />
          <el-table-column prop="interaction" :label="$t('threeProducts.interaction')" min-width="130" show-overflow-tooltip />
        </el-table>
      </el-card>

      <!-- 待澄清项 -->
      <el-card class="product-col" shadow="never">
        <template #header>
          <span class="col-title">{{ $t('threeProducts.clarifications') }}</span>
          <el-tag size="small" type="warning">{{ products.clarifications.length }}</el-tag>
        </template>
        <el-table :data="products.clarifications" size="small" border stripe>
          <el-table-column prop="question" :label="$t('threeProducts.question')" min-width="180" show-overflow-tooltip />
          <el-table-column prop="impact" :label="$t('threeProducts.impact')" min-width="120" show-overflow-tooltip />
          <el-table-column prop="suggestion" :label="$t('threeProducts.suggestion')" min-width="120" show-overflow-tooltip />
          <el-table-column :label="$t('threeProducts.status')" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">{{ row.status_display || row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('threeProducts.operation')" width="90" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="primary" :disabled="row.status === 'revised'" @click="openClarify(row)">
                {{ $t('threeProducts.clarify') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <!-- 澄清答复对话框 -->
    <el-dialog
      v-model="clarifyVisible"
      :title="$t('threeProducts.clarifyDialogTitle')"
      width="560px"
      :close-on-click-modal="false"
    >
      <div v-if="currentClarification" class="clarify-question">
        <p><strong>{{ $t('threeProducts.question') }}：</strong>{{ currentClarification.question }}</p>
        <p><strong>{{ $t('threeProducts.suggestion') }}：</strong>{{ currentClarification.suggestion || '—' }}</p>
        <p v-if="currentClarification.answer">
          <strong>{{ $t('threeProducts.answer') }}：</strong>{{ currentClarification.answer }}
        </p>
      </div>
      <el-input
        v-model="clarifyAnswer"
        type="textarea"
        :rows="4"
        :placeholder="$t('threeProducts.answerPlaceholder')"
      />
      <template #footer>
        <el-button @click="clarifyVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitClarify">
          {{ $t('threeProducts.submitAnswer') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '@/utils/api'
import { ElMessage } from 'element-plus'
import {
  getRequirementDocuments,
  generateThreeProducts,
  getThreeProducts,
  clarifyRequirement
} from '@/api/requirement-analysis'

const { t } = useI18n()

const documents = ref([])
const selectedDocId = ref(null)
const loadingDocs = ref(false)
const generating = ref(false)
const loadingProducts = ref(false)
const products = reactive({
  functional_inventory: [],
  ui_descriptions: [],
  clarifications: []
})
const hasProducts = computed(() =>
  products.functional_inventory.length ||
  products.ui_descriptions.length ||
  products.clarifications.length
)

const clarifyVisible = ref(false)
const currentClarification = ref(null)
const clarifyAnswer = ref('')
const submitting = ref(false)

async function loadDocuments() {
  loadingDocs.value = true
  try {
    const res = await getRequirementDocuments({ page_size: 200 })
    documents.value = res.data.results || res.data || []
    if (!selectedDocId.value && documents.value.length) {
      selectedDocId.value = documents.value[0].id
      await loadProducts()
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    loadingDocs.value = false
  }
}

async function loadProducts() {
  if (!selectedDocId.value) return
  loadingProducts.value = true
  try {
    const res = await getThreeProducts(selectedDocId.value)
    products.functional_inventory = res.data.functional_inventory || []
    products.ui_descriptions = res.data.ui_descriptions || []
    products.clarifications = res.data.clarifications || []
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('common.error'))
  } finally {
    loadingProducts.value = false
  }
}

function onDocChange() {
  loadProducts()
}

async function onGenerate() {
  generating.value = true
  try {
    const res = await generateThreeProducts(selectedDocId.value)
    if (res.data.success) {
      ElMessage.success(res.data.message || t('common.success'))
      await loadProducts()
    } else {
      ElMessage.warning(res.data.message || t('common.error'))
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.message || e.response?.data?.error || t('common.error'))
  } finally {
    generating.value = false
  }
}

function statusTagType(status) {
  if (status === 'revised') return 'success'
  if (status === 'answered') return 'primary'
  return 'warning'
}

function openClarify(row) {
  currentClarification.value = row
  clarifyAnswer.value = row.answer || ''
  clarifyVisible.value = true
}

async function submitClarify() {
  if (!clarifyAnswer.value.trim()) {
    ElMessage.warning(t('requirement.threeProducts.answerPlaceholder'))
    return
  }
  submitting.value = true
  try {
    const res = await clarifyRequirement(selectedDocId.value, {
      clarification_id: currentClarification.value.id,
      answer: clarifyAnswer.value.trim()
    })
    if (res.data.success) {
      ElMessage.success(res.data.message || t('common.success'))
      clarifyVisible.value = false
      await loadProducts()
    } else {
      ElMessage.warning(res.data.message || t('common.error'))
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.message || e.response?.data?.error || t('common.error'))
  } finally {
    submitting.value = false
  }
}

onMounted(loadDocuments)
</script>

<style scoped>
.three-products { padding: 16px; }
.page-header { margin-bottom: 12px; }
.page-header h2 { margin: 0; }
.subtitle { color: #888; margin: 4px 0 0; font-size: 13px; }
.doc-bar { margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }
.empty-tip { color: #999; padding: 40px; text-align: center; }
.products-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  align-items: start;
}
.col-title { font-weight: 600; margin-right: 8px; }
.clarify-question { background: #f5f7fa; padding: 12px; border-radius: 6px; margin-bottom: 12px; font-size: 13px; line-height: 1.8; }
@media (max-width: 1100px) {
  .products-grid { grid-template-columns: 1fr; }
}
</style>
