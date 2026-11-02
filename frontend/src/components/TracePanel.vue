<!-- 链路追踪：按请求链路的先后顺序展示选中那条回复的处理过程——通道、意图识别、子任务派发、工具调用、状态变化与拦截。 -->
<template>
  <section class="panel trace">
    <div class="panel-head">
      <h2>链路追踪</h2>
      <button class="icon-button" title="收起链路追踪" aria-label="收起链路追踪" @click="$emit('collapse')"><AppIcon name="panel" /></button>
    </div>
    <div class="panel-body">
      <p v-if="!data" class="empty">发一条指令后，这里按顺序展示它经过的每一步。</p>
      <ol v-else class="steps">
        <li :class="data.route">
          <h3>通道</h3>
          <p v-if="data.route === 'fast'">
            快速通道命中 <b>{{ data.fast_rule }} {{ FAST_RULE_LABELS[data.fast_rule] }}</b>，规则匹配后直接调用工具，没有调用模型。
          </p>
          <p v-else>快速通道未命中，进入完整链路。</p>
        </li>

        <li>
          <h3>意图识别</h3>
          <p>
            <b>{{ INTENT_LABELS[data.intent] || data.intent }}</b>
            <span class="muted">（置信度{{ CONFIDENCE_LABELS[data.intent_confidence_level] || '无' }}，{{ DECIDED_BY_LABELS[src.decided_by] || src.decided_by }}）</span>
          </p>
          <dl v-if="data.route !== 'fast'" class="kv">
            <template v-if="src.emergency_phrase">
              <dt>紧急短语</dt><dd>{{ src.emergency_phrase }}</dd>
            </template>
            <dt>关键词层</dt>
            <dd>
              <template v-if="src.keyword_intent">{{ INTENT_LABELS[src.keyword_intent] }}<span class="muted">（命中：{{ (src.keyword_hits || []).join('、') }}）</span></template>
              <span v-else class="muted">没有候选</span>
            </dd>
            <dt>LLM 层</dt>
            <dd>
              <template v-if="src.llm_intent">{{ INTENT_LABELS[src.llm_intent] }}</template>
              <span v-else class="muted">{{ LLM_STATUS_LABELS[src.llm_status] || src.llm_status }}</span>
            </dd>
          </dl>
        </li>

        <li v-if="data.route !== 'fast'">
          <h3>子任务与派发</h3>
          <ul v-if="data.sub_tasks.length" class="tasks">
            <li v-for="(t, i) in data.sub_tasks" :key="i">
              <span class="tag">{{ INTENT_LABELS[t.intent] || t.intent }}</span>{{ t.text }}
            </li>
          </ul>
          <p v-if="data.sub_tasks.length" class="muted">{{ data.execution === 'sequential' ? '串行：后一步用到前一步的结果' : '并行执行' }}</p>
          <p>派发 <b>{{ (data.agent_types.length ? data.agent_types : [data.agent_type]).map(a => AGENT_LABELS[a] || a).join('、') }}</b></p>
        </li>

        <li>
          <h3>工具调用</h3>
          <p v-if="toolCalls === null" class="muted">读取中…</p>
          <p v-else-if="!toolCalls.length" class="muted">没有调用工具</p>
          <ul v-else class="tools">
            <li v-for="(call, i) in toolCalls" :key="i" :class="{ blocked: blockedTools.has(call.tool_name), failed: call.error }">
              <div class="tool-head">
                <b>{{ TOOL_LABELS[call.tool_name] || call.tool_name }}</b>
                <code>{{ call.tool_name }}</code>
                <span class="muted">{{ AGENT_LABELS[call.agent_type] || call.agent_type }}</span>
                <span class="num muted">{{ formatMs(call.latency_ms) }}</span>
              </div>
              <code class="args">{{ formatArgs(call.input) }}</code>
              <span v-if="call.error" class="error-text">{{ call.error }}</span>
            </li>
          </ul>
        </li>

        <li>
          <h3>车辆状态</h3>
          <ul v-if="data.blocked_actions.length" class="blocked-list">
            <li v-for="(b, i) in data.blocked_actions" :key="i"><AppIcon name="block" /><b>{{ b.rule_id }}</b> {{ b.reason }}</li>
          </ul>
          <ul v-if="data.state_changes.length" class="changes">
            <li v-for="c in data.state_changes" :key="c.field">
              <span>{{ fieldLabel(c.field) }}</span>
              <span class="muted">{{ formatValue(c.field, c.old) }}</span>
              <span class="arrow">→</span>
              <b>{{ formatValue(c.field, c.new) }}</b>
            </li>
          </ul>
          <p v-if="!data.state_changes.length && !data.blocked_actions.length" class="muted">没有变化</p>
        </li>
      </ol>
      <p v-if="data" class="total muted">编排器耗时 <span class="num">{{ formatMs(data.latency_ms) }}</span></p>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import AppIcon from './AppIcon.vue'
import {
  AGENT_LABELS, CONFIDENCE_LABELS, DECIDED_BY_LABELS, FAST_RULE_LABELS, INTENT_LABELS, LLM_STATUS_LABELS, TOOL_LABELS,
  fieldLabel, formatMs, formatValue
} from '../lib/labels'
import { session } from '../lib/session'

defineEmits(['collapse'])

const message = computed(() => session.messages.find(m => m.id === session.selectedId) || null)
const data = computed(() => message.value?.data || null)
const src = computed(() => data.value?.intent_sources || {})
const toolCalls = computed(() => message.value?.toolCalls ?? null)
const blockedTools = computed(() => new Set((data.value?.blocked_actions || []).map(b => b.tool_name)))

function formatArgs(input) {
  if (!input || typeof input !== 'object') return String(input ?? '')
  return Object.entries(input).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join('  ')
}
</script>

<style scoped>
.steps { list-style: none; margin: 0; padding: 0; counter-reset: step; }
.steps > li { position: relative; padding: 0 0 14px 30px; counter-increment: step; }
.steps > li::before {
  content: counter(step); position: absolute; left: 0; top: 1px; width: 20px; height: 20px; border-radius: 50%;
  background: var(--raised); border: 1px solid var(--line); color: var(--ink-2); font: 12px/18px var(--font-num); text-align: center;
}
.steps > li::after { content: ""; position: absolute; left: 10px; top: 24px; bottom: 2px; width: 1px; background: var(--line); }
.steps > li:last-child::after { display: none; }
.steps > li.fast::before { border-color: var(--amber); color: var(--amber); }
.steps > li.slow::before { border-color: var(--ice); color: var(--ice); }

h3 { margin: 0 0 4px; font-size: 13px; font-weight: 600; color: var(--ink-2); }
p { margin: 0 0 4px; }

.kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; margin: 4px 0 0; font-size: 13px; }
.kv dt { color: var(--ink-3); }
.kv dd { margin: 0; }

.tasks, .tools, .changes, .blocked-list { list-style: none; margin: 4px 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.tag { display: inline-block; margin-right: 6px; padding: 0 6px; border-radius: 6px; background: var(--ice-soft); color: var(--ice); font-size: 12px; }

.tools li { background: var(--raised); border-radius: var(--r-tile); padding: 7px 10px; border-left: 2px solid var(--ice); }
.tools li.blocked { border-left-color: var(--red); }
.tool-head { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; font-size: 13px; }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; color: var(--ink-3); }
.args { display: block; color: var(--ink-2); word-break: break-all; margin-top: 2px; }

.changes li { display: grid; grid-template-columns: minmax(0, 1fr) auto auto auto; gap: 6px; align-items: baseline; font-size: 13px; }
.changes b { color: var(--amber); }
.arrow { color: var(--ink-3); }
.blocked-list li { color: var(--red); font-size: 13px; display: flex; gap: 6px; align-items: flex-start; }
.blocked-list .app-icon { width: 15px; height: 15px; margin-top: 3px; }
.total { margin-top: 4px; font-size: 12px; }
</style>
