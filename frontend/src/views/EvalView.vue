<!-- 评测页：读取 GET /eval/report 的正式报告，展示结论、E1 分层消融、E2 快速通道和错例；没有报告时给出生成方法。 -->
<template>
  <main class="page eval">
    <p v-if="loading" class="empty">正在读取评测报告…</p>
    <section v-else-if="error" class="panel missing">
      <h2>{{ error.status === 404 ? '还没有正式评测报告' : '读取评测报告失败' }}</h2>
      <p v-if="error.status === 404">在仓库根目录运行 <code>python backend/evaluation/运行评测.py</code>，把结果目录里的 report.json 复制到 backend/evaluation/报告/ 后刷新。</p>
      <p v-else class="error-text">{{ error.message }}</p>
      <button @click="load"><AppIcon name="refresh" />重新读取</button>
    </section>

    <template v-else-if="report">
      <header class="eval-head">
        <h1>意图识别评测</h1>
        <p class="muted">{{ metaLine }}</p>
      </header>

      <ul class="findings">
        <li><b class="num">{{ formatPct(e1.layered.summary.intent_accuracy) }}</b>分层识别（关键词 + LLM）的主意图准确率，只用关键词时 {{ formatPct(e1.keyword_only.summary.intent_accuracy) }}</li>
        <li><b class="num">{{ formatPct(intent.coverage) }}</b>快速通道覆盖率，误命中率 {{ formatPct(intent.false_trigger) }}</li>
        <li v-if="e2e"><b class="num">{{ formatMs(e2e.on.p50_ms) }}</b>命中快速通道时的端到端延迟（P50），走完整链路要 {{ formatMs(e2e.off.p50_ms) }}</li>
        <li v-if="e2e"><b class="num">{{ formatPct(e2e.state_consistency) }}</b>两条通道改出的车辆状态一致</li>
      </ul>

      <div class="grid">
        <section class="panel">
          <div class="panel-head"><h2>E1 分层消融</h2></div>
          <div class="panel-body">
            <table>
              <thead><tr><th>组</th><th>主意图准确率</th><th>多意图完全匹配率</th><th>延迟 P50</th><th>延迟 P95</th></tr></thead>
              <tbody>
                <tr v-for="row in e1Rows" :key="row.key">
                  <td>{{ row.label }}</td>
                  <td class="num">{{ formatPct(row.s.intent_accuracy) }}</td>
                  <td class="num">{{ formatPct(row.s.multi_match) }}</td>
                  <td class="num">{{ formatMs(row.s.latency.p50_ms) }}</td>
                  <td class="num">{{ formatMs(row.s.latency.p95_ms) }}</td>
                </tr>
              </tbody>
            </table>
            <BarChart caption="主意图准确率" :items="accuracyBars" :max="1" />
            <p class="muted small">只关键词等同 LLM 不可用；只 LLM 没有紧急直达和关键词兜底。识别耗时只算识别器本身。</p>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><h2>E2 快速通道</h2></div>
          <div class="panel-body">
            <table>
              <thead><tr><th>快速通道</th><th>主意图准确率</th><th>端到端 P50</th><th>端到端 P95</th></tr></thead>
              <tbody>
                <tr v-for="mode in ['on', 'off']" :key="mode">
                  <td>{{ mode === 'on' ? '开' : '关' }}</td>
                  <td class="num">{{ formatPct(intent[mode].intent_accuracy) }}</td>
                  <td class="num">{{ e2e ? formatMs(e2e[mode].p50_ms) : '—' }}</td>
                  <td class="num">{{ e2e ? formatMs(e2e[mode].p95_ms) : '—' }}</td>
                </tr>
              </tbody>
            </table>
            <BarChart
              v-if="e2e" caption="端到端延迟 P50" :items="latencyBars" scale="log" :min="0.1" :max="10000"
              note="对数刻度：条长每多一格代表延迟大 10 倍。端到端只算编排器，不含 HTTP 和记忆读写。"
            />
            <dl class="facts">
              <dt>覆盖率</dt><dd>{{ formatPct(intent.coverage) }}<span class="muted">（{{ intent.fast_path_samples - intent.missed.length }}/{{ intent.fast_path_samples }}）</span></dd>
              <dt>误命中率</dt><dd>{{ formatPct(intent.false_trigger) }}<span class="muted">（误命中 {{ intent.false_hits.length }} 条）</span></dd>
              <template v-if="e2e">
                <dt>状态一致率</dt><dd>{{ formatPct(e2e.state_consistency) }}<span class="muted">（{{ e2e.samples }} 条 × {{ e2e.repeats }} 遍）</span></dd>
              </template>
            </dl>
          </div>
        </section>
      </div>

      <section class="panel errors">
        <div class="panel-head"><h2>错例</h2><span class="muted">任意一遍判错就列出</span></div>
        <div class="panel-body">
          <div v-for="row in e1Rows" :key="row.key" class="error-group">
            <h3>{{ row.label }}<span class="muted">{{ row.s.intent_errors.length + row.s.multi_errors.length }} 条</span></h3>
            <p v-if="!row.s.intent_errors.length && !row.s.multi_errors.length" class="muted">没有错例</p>
            <ul>
              <li v-for="err in row.s.intent_errors" :key="`i-${err.id}`">
                <span class="id num">{{ err.id }}</span>
                <span class="sample">{{ err.text }}</span>
                <span class="muted">应为 {{ INTENT_LABELS[err.expected] }}，判成 {{ predicted(err.predicted) }}</span>
              </li>
              <li v-for="err in row.s.multi_errors" :key="`m-${err.id}`">
                <span class="id num">{{ err.id }}</span>
                <span class="sample">{{ err.text }}</span>
                <span class="muted">应拆为 {{ intentsText(err.expected) }}，判成 {{ intentsText(err.predicted[0]) }}</span>
              </li>
            </ul>
          </div>
        </div>
      </section>
    </template>
  </main>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import BarChart from '../components/BarChart.vue'
import { api } from '../lib/api'
import { INTENT_LABELS, formatMs, formatPct } from '../lib/labels'

const E1_LABELS = { keyword_only: '只关键词', llm_only: '只 LLM', layered: '关键词 + LLM' }

const report = ref(null)
const loading = ref(true)
const error = ref(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    report.value = await api.evalReport()
  } catch (ex) {
    error.value = ex
  } finally {
    loading.value = false
  }
}
onMounted(load)

const ds = computed(() => report.value.dataset)
const cfg = computed(() => report.value.config)
const e1 = computed(() => report.value.e1)
const intent = computed(() => report.value.e2.intent)
const e2e = computed(() => report.value.e2.end_to_end || null)
const metaLine = computed(() => {
  const time = new Date(report.value.generated_at).toLocaleString('zh-CN', { hour12: false })
  const commit = (report.value.git?.commit || '').slice(0, 7)
  return `${ds.value.total} 条样本（多意图 ${ds.value.multi} 条，应走快速通道 ${ds.value.fast_path} 条），` +
    `模型 ${cfg.value.intent_model}，调用模型的组各跑 ${cfg.value.repeats} 遍取均值；${time} 生成，代码 ${commit}。`
})

const e1Rows = computed(() => Object.entries(E1_LABELS)
  .filter(([key]) => e1.value[key])
  .map(([key, label]) => ({ key, label, s: e1.value[key].summary })))

const accuracyBars = computed(() => e1Rows.value.map(row => ({
  label: row.label, value: row.s.intent_accuracy, display: formatPct(row.s.intent_accuracy)
})))

const latencyBars = computed(() => [
  { label: '快速通道开', value: e2e.value.on.p50_ms, display: formatMs(e2e.value.on.p50_ms), tone: 'amber' },
  { label: '快速通道关', value: e2e.value.off.p50_ms, display: formatMs(e2e.value.off.p50_ms) }
])

function predicted(list) {
  const counts = {}
  for (const intentName of list) counts[intentName] = (counts[intentName] || 0) + 1
  return Object.entries(counts)
    .map(([name, n]) => (list.length > 1 ? `${INTENT_LABELS[name] || name}（${n}/${list.length} 遍）` : INTENT_LABELS[name] || name))
    .join('、')
}

function intentsText(item) {
  const names = item.intents.map(name => INTENT_LABELS[name] || name).join(' + ')
  return `${names}（${item.execution === 'sequential' ? '串行' : '并行'}）`
}
</script>

<style scoped>
.eval { display: flex; flex-direction: column; gap: 12px; max-width: 1280px; width: 100%; margin: 0 auto; }
.eval > .panel { flex: none; }
.eval-head h1 { margin: 4px 0 2px; font-size: 24px; font-weight: 600; }
.eval-head p { margin: 0; max-width: 60em; }

.findings { list-style: none; margin: 4px 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px 24px; }
.findings li { color: var(--ink-2); line-height: 1.5; }
.findings b { display: block; font-size: 30px; font-weight: 600; color: var(--ink); line-height: 1.2; }

.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 12px; }
.grid .panel-body { display: flex; flex-direction: column; gap: 14px; }

table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font-weight: 500; color: var(--ink-3); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
td { padding: 7px 8px; border-bottom: 1px solid var(--line); }
tr:last-child td { border-bottom: none; }

.facts { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; margin: 0; }
.facts dt { color: var(--ink-3); }
.facts dd { margin: 0; }
.small { font-size: 12px; margin: 0; }

.error-group h3 { font-size: 14px; margin: 4px 0; display: flex; gap: 8px; align-items: baseline; }
.error-group ul { list-style: none; margin: 0 0 10px; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.error-group li { display: grid; grid-template-columns: 3em minmax(10em, 22em) 1fr; gap: 10px; font-size: 13px; align-items: baseline; }
.id { color: var(--ink-3); }

.missing { padding: 24px; gap: 10px; align-items: flex-start; max-width: 640px; }
.missing h2 { margin: 0; font-size: 18px; }
.missing p { margin: 0; }
code { font-family: Consolas, "Cascadia Mono", monospace; color: var(--ice); }

@media (max-width: 760px) {
  .grid { grid-template-columns: 1fr; }
  .error-group li { grid-template-columns: 3em 1fr; }
  .error-group li .muted { grid-column: 2; }
}
</style>
