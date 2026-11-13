<template>
  <section class="page" data-module="retry">
    <header class="page-head">
      <div>
        <h2>失败重试策略管理</h2>
        <p class="page-desc">维护采集失败的退避重试与兜底策略，支持重试预演、失败批次接纳与处理队列跟踪。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="showCreate = !showCreate">登记重试策略</button>
        <button class="btn" type="button" @click="exportRows">导出失败重试策略清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="showCreate" class="panel" @submit.prevent="submitCreate">
      <h3 class="panel-title">登记重试策略（判定条件、阈值范围、上限与优先级标准须逐项配置）</h3>
      <div class="form-grid">
        <label v-for="field in createFields" :key="field.key" class="filter-item">
          <span>{{ field.key }}</span>
          <input v-model="createForm[field.key]" :placeholder="field.hint" />
        </label>
      </div>
      <div class="panel-actions">
        <button class="btn primary" type="submit">提交登记</button>
        <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
      </div>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>策略编号</span>
        <input v-model="filters.keyword" placeholder="按策略编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无失败重试策略数据，可先登记重试策略</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <h3 class="panel-title">重试预演</h3>
      <p class="panel-desc">按采集日志的错误类型与已尝试次数生成后续时间点；已有留档的日志断线后从下一时间点继续，历史口径不随新配置改排。</p>
      <form class="filter-bar" @submit.prevent="submitPreview">
        <label class="filter-item">
          <span>日志编号</span>
          <input v-model="previewLogNo" placeholder="如 LOG-0002" />
        </label>
        <button class="btn primary" type="submit">生成 / 续接预演</button>
      </form>
      <div v-if="preview" class="preview-result">
        <p class="conclusion">{{ previewMessage }}</p>
        <div class="caliber-grid">
          <div v-for="item in caliberItems" :key="item.label" class="caliber-item">
            <span class="stat-label">{{ item.label }}</span>
            <span>{{ item.value }}</span>
          </div>
        </div>
        <table v-if="preview.时间点 && preview.时间点.length" class="data-table">
          <thead>
            <tr><th>重试次序</th><th>计划时间</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="point in preview.时间点" :key="point.次序">
              <td>第 {{ point.次序 }} 次</td>
              <td>{{ point.计划时间 }}</td>
              <td>{{ point.状态 }}</td>
            </tr>
          </tbody>
        </table>
        <p class="conclusion">预演结论：{{ preview.结论 }}</p>
      </div>
    </section>

    <section class="panel">
      <h3 class="panel-title">失败批次送入</h3>
      <p class="panel-desc">同一失败批次重复送入仅接纳一份；排除异常与可重试异常冲突时以排除异常优先，命中即停止重试。</p>
      <form class="filter-bar" @submit.prevent="submitBatch">
        <label v-for="field in batchFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="batchForm[field]" :placeholder="field === '失败时间' ? '如 2026-11-12 14:30' : `填写${field}`" />
        </label>
        <button class="btn primary" type="submit">送入批次</button>
      </form>
      <p v-if="batchMessage" class="conclusion">{{ batchMessage }}</p>
    </section>

    <section class="panel">
      <h3 class="panel-title">处理队列（即将执行的失败重试）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in queueColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in queue" :key="String(todo.id)">
            <td v-for="column in queueColumns" :key="column">{{ todo[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!queue.length">
            <td :colspan="queueColumns.length" class="empty-state">处理队列暂无待办，执行重试预演后自动生成</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条失败重试策略记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/retry'
const columns = ["策略编号", "适用任务", "最大重试", "重试间隔", "退避系数", "可重试异常", "排除异常", "最终动作", "判定条件", "阈值范围", "优先级标准"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const queueColumns = ["待办编号", "日志编号", "策略编号", "计划时间", "重试次序", "来源", "状态", "结论"]
const createFields = [
  { key: "策略编号", hint: "如 RETR-0004" },
  { key: "适用任务", hint: "如 TASK-0001" },
  { key: "最大重试", hint: "上限，1-10 的整数" },
  { key: "重试间隔", hint: "分钟，须落在阈值范围内" },
  { key: "退避系数", hint: "不小于 1，如 1、1.5、2" },
  { key: "可重试异常", hint: "顿号分隔，如 网络超时、连接重置" },
  { key: "排除异常", hint: "顿号分隔，如 鉴权失败" },
  { key: "最终动作", hint: "转人工处理 / 告警并终止 / 降级重采" },
  { key: "判定条件", hint: "如 错误类型命中可重试异常且已尝试次数小于最大重试" },
  { key: "阈值范围", hint: "分钟区间，如 1-120" },
  { key: "优先级标准", hint: "固定为 排除异常优先" },
]
const batchFields = ["批次号", "日志编号", "错误类型", "失败时间"]

const rows = ref<Row[]>([])
const total = ref(0)
const queue = ref<Row[]>([])
const errorMessage = ref('')
const filters = ref<Record<string, string>>({ keyword: '', status: '' })
const showCreate = ref(false)
const createForm = ref<Record<string, string>>({})
const previewLogNo = ref('')
const preview = ref<Row | null>(null)
const previewMessage = ref('')
const batchForm = ref<Record<string, string>>({})
const batchMessage = ref('')

const stats = ref([
  { label: "重试策略总数", value: 0 },
  { label: "待校验重试策略", value: 0 },
  { label: "已启用重试策略", value: 0 },
  { label: "待执行重试", value: 0 },
])

const caliberItems = computed(() => {
  const caliber = (preview.value?.口径快照 ?? {}) as Row
  return [
    { label: "最大重试", value: caliber.最大重试 ?? '—' },
    { label: "重试间隔（分钟）", value: caliber.重试间隔 ?? '—' },
    { label: "退避系数", value: caliber.退避系数 ?? '—' },
    { label: "可重试异常", value: caliber.可重试异常 ?? '—' },
    { label: "排除异常", value: caliber.排除异常 ?? '—' },
    { label: "最终动作", value: caliber.最终动作 ?? '—' },
    { label: "判定条件", value: caliber.判定条件 ?? '—' },
    { label: "阈值范围", value: caliber.阈值范围 ?? '—' },
    { label: "优先级标准", value: caliber.优先级标准 ?? '—' },
    { label: "口径来源", value: preview.value?.来源 ?? '—' },
  ]
})

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function post(path: string, values: Record<string, unknown>) {
  const response = await request(`${ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? '接口返回异常')
  }
  return payload
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const payload = await post('', { ...createForm.value })
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    showCreate.value = false
    createForm.value = {}
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试策略登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const payload = await post(`/${row.id}/actions`, { action })
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失败重试策略操作失败'
  }
}

async function submitPreview() {
  errorMessage.value = ''
  preview.value = null
  previewMessage.value = ''
  try {
    const payload = await post('/preview', { 日志编号: previewLogNo.value.trim() })
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    preview.value = payload.entry
    previewMessage.value = payload.message
    await reloadQueue()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试预演失败'
  }
}

async function submitBatch() {
  errorMessage.value = ''
  batchMessage.value = ''
  try {
    const payload = await post('/batches', { ...batchForm.value })
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    batchMessage.value = payload.message
    await reloadQueue()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失败批次送入失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value.keyword) query.set('keyword', filters.value.keyword)
  if (filters.value.status) query.set('status', filters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('重试策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => row.status === '待校验').length
    stats.value[2].value = rows.value.filter((row) => row.status === '已启用').length
    await reloadQueue()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失败重试策略列表读取失败'
  }
}

async function reloadQueue() {
  try {
    const response = await request(`${ENDPOINT}/queue`)
    if (!response.ok) {
      throw new Error('处理队列读取失败')
    }
    const payload = await response.json()
    queue.value = payload.items ?? []
    stats.value[3].value = queue.value.filter((todo) => todo.状态 === '待执行').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '处理队列读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin: 12px 0;
}
.panel-title {
  margin: 0 0 8px;
  font-size: 14px;
}
.panel-desc {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--muted);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}
.panel-actions {
  display: flex;
  gap: 8px;
}
.caliber-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
  margin: 10px 0;
}
.caliber-item {
  border: 1px dashed var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  word-break: break-all;
}
.conclusion {
  font-size: 13px;
  color: #1f2937;
}
.filter-item select,
.filter-item input {
  min-width: 160px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
</style>
