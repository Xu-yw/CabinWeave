<!-- 系统页：快慢通道统计与规则命中排行、当前用户的驾驶员偏好、用车手册检索测试、已加载的 Skills。 -->
<template>
  <main class="page system">
    <div class="system-head">
      <h1>系统</h1>
      <button :disabled="loading" @click="load"><AppIcon name="refresh" />刷新</button>
    </div>

    <div class="grid">
      <section class="panel">
        <div class="panel-head"><h2>快慢通道</h2><span class="muted">服务启动以来</span></div>
        <div class="panel-body">
          <p v-if="errors.monitor" class="error-text">{{ errors.monitor }}</p>
          <template v-else-if="routes">
            <dl class="facts">
              <dt>请求数</dt><dd class="num">{{ routes.total }}</dd>
              <dt>快速通道</dt><dd><span class="num">{{ routes.fast }}</span><span class="muted">（{{ formatPct(routes.fast_ratio) }}）</span></dd>
              <dt>完整链路</dt><dd class="num">{{ routes.slow }}</dd>
              <dt>开关</dt><dd>{{ routes.enabled ? '已开启' : '已关闭' }}</dd>
            </dl>
            <BarChart v-if="ruleBars.length" caption="规则命中次数" :items="ruleBars" :max="ruleMax" />
            <p v-else class="muted">还没有命中快速通道的请求。</p>
          </template>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>驾驶员偏好</h2><span class="muted">用户 {{ session.userId }}</span></div>
        <div class="panel-body">
          <p v-if="errors.profile" class="error-text">{{ errors.profile }}</p>
          <p v-else-if="profile && !hasProfile" class="muted">还没有记住偏好。在座舱里说"我习惯空调开22度""我家在……"，完整链路结束后会在后台提炼。</p>
          <dl v-else-if="profile" class="facts">
            <template v-for="(value, key) in profile.preferences || {}" :key="key">
              <dt>{{ PREF_LABELS[key] || key }}</dt><dd>{{ prefValue(key, value) }}</dd>
            </template>
            <template v-for="(value, key) in profile.places || {}" :key="`p-${key}`">
              <dt>{{ key === 'home' ? '家' : '公司' }}</dt><dd>{{ value }}</dd>
            </template>
            <template v-if="profile.notes?.length">
              <dt>备注</dt><dd>{{ profile.notes.join('；') }}</dd>
            </template>
          </dl>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>用车手册检索</h2><span v-if="chunks !== null" class="muted">{{ chunks }} 个片段</span></div>
        <div class="panel-body">
          <form class="search" @submit.prevent="search">
            <input v-model="query" placeholder="例如：胎压报警怎么处理" aria-label="检索用车手册" />
            <button type="submit" :disabled="searching || !query.trim()"><AppIcon name="search" />{{ searching ? '检索中' : '检索' }}</button>
          </form>
          <p v-if="errors.search" class="error-text">{{ errors.search }}</p>
          <p v-else-if="results && !results.length" class="muted">没有检索到相关片段，可以换个说法再试。</p>
          <ol v-else-if="results" class="results">
            <li v-for="(r, i) in results" :key="i">
              <div class="result-head"><b>{{ r.title || '未命名片段' }}</b><span class="num muted">{{ r.score?.toFixed?.(2) }}</span></div>
              <p>{{ snippet(r.content) }}</p>
            </li>
          </ol>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Skills</h2>
          <button :disabled="reloading" @click="reload"><AppIcon name="refresh" />重新加载</button>
        </div>
        <div class="panel-body">
          <p v-if="errors.skills" class="error-text">{{ errors.skills }}</p>
          <ul v-else-if="skills" class="skills">
            <li v-for="s in skills.skills" :key="s.name">
              <div class="result-head"><b>{{ s.name }}</b><span class="muted">{{ (s.agents || []).map(a => AGENT_LABELS[a] || a).join('、') || '全部角色' }}</span></div>
              <p class="muted">{{ s.description }}</p>
            </li>
            <li v-for="(e, i) in skills.errors" :key="`e-${i}`" class="error-text">{{ e }}</li>
          </ul>
        </div>
      </section>
    </div>
  </main>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import BarChart from '../components/BarChart.vue'
import { api } from '../lib/api'
import { AGENT_LABELS, DRIVE_MODE_LABELS, FAST_RULE_LABELS, formatPct } from '../lib/labels'
import { session } from '../lib/session'

const PREF_LABELS = { climate_temp: '空调温度', drive_mode: '驾驶模式', music: '音乐', seat_heat: '座椅加热' }

const routes = ref(null)
const profile = ref(null)
const skills = ref(null)
const chunks = ref(null)
const results = ref(null)
const query = ref('')
const loading = ref(false)
const searching = ref(false)
const reloading = ref(false)
const errors = reactive({ monitor: '', profile: '', skills: '', search: '' })

const hasProfile = computed(() => profile.value && Object.keys(profile.value).length > 0)
const ruleBars = computed(() => Object.entries(routes.value?.rules || {})
  .sort((a, b) => b[1] - a[1])
  .map(([rule, n]) => ({ label: `${rule} ${FAST_RULE_LABELS[rule] || ''}`, value: n, display: String(n), tone: 'amber' })))
const ruleMax = computed(() => Math.max(1, ...ruleBars.value.map(b => b.value)))

async function capture(key, task) {
  errors[key] = ''
  try {
    return await task()
  } catch (ex) {
    errors[key] = ex.message
    return null
  }
}

async function load() {
  loading.value = true
  const [monitor, prof, sk, stats] = await Promise.all([
    capture('monitor', api.monitor),
    capture('profile', () => api.profile(session.userId)),
    capture('skills', api.skills),
    api.knowledgeStats().catch(() => null)
  ])
  routes.value = monitor?.route_stats || null
  profile.value = prof?.profile || null
  skills.value = sk
  chunks.value = stats ? stats.total_chunks : null
  loading.value = false
}

async function search() {
  searching.value = true
  const data = await capture('search', () => api.searchManual(query.value.trim()))
  results.value = data ? data.results || [] : null
  searching.value = false
}

async function reload() {
  reloading.value = true
  const data = await capture('skills', api.reloadSkills)
  if (data) skills.value = data
  reloading.value = false
}

function prefValue(key, value) {
  if (key === 'climate_temp') return `${value}℃`
  if (key === 'drive_mode') return `${DRIVE_MODE_LABELS[value] || value}模式`
  return Array.isArray(value) ? value.join('、') : String(value)
}

function snippet(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim()
  return clean.length > 120 ? `${clean.slice(0, 120)}…` : clean
}

onMounted(load)
</script>

<style scoped>
.system { display: flex; flex-direction: column; gap: 12px; max-width: 1280px; width: 100%; margin: 0 auto; }
.system-head { display: flex; align-items: center; justify-content: space-between; }
.system-head h1 { margin: 4px 0; font-size: 24px; font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 12px; }
.grid .panel-body { display: flex; flex-direction: column; gap: 12px; }

.facts { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 0; }
.facts dt { color: var(--ink-3); }
.facts dd { margin: 0; }

.search { display: flex; gap: 8px; }
.search input { flex: 1; }
.results, .skills { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.results li, .skills li { background: var(--raised); border-radius: var(--r-tile); padding: 8px 12px; }
.results p, .skills p { margin: 2px 0 0; font-size: 13px; line-height: 1.6; }
.result-head { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }

@media (max-width: 760px) {
  .grid { grid-template-columns: 1fr; }
}
</style>
