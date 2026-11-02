<!-- 对话面板：消息列表（助手回复带通道标签和被拦截的操作，点击切换右侧链路追踪）、分组快捷指令、输入框。 -->
<template>
  <section class="panel chat">
    <div class="panel-head">
      <h2>对话</h2>
      <span v-if="session.conversationId" class="muted">会话 {{ session.conversationId.slice(0, 8) }}</span>
    </div>

    <div ref="listEl" class="messages" aria-live="polite">
      <div v-if="!session.messages.length" class="welcome">
        <p>用一句话控制车辆、导航或查询用车问题。</p>
        <p class="muted">简单指令走快速通道，不调用模型；复杂的话交给多 Agent 完整链路。下面的快捷指令可以直接点。</p>
      </div>
      <template v-for="m in session.messages" :key="m.id">
        <p v-if="m.role === 'notice'" :class="['notice', { 'error-text': m.error }]">{{ m.text }}</p>
        <div v-else-if="m.role === 'user'" class="bubble user">{{ m.text }}</div>
        <button
          v-else
          type="button"
          :class="['bubble', 'assistant', { selected: m.id === session.selectedId, failed: m.error }]"
          :disabled="m.error"
          @click="session.selectedId = m.id"
        >
          <span class="text">{{ m.text }}</span>
          <span v-if="m.data?.blocked_actions?.length" class="blocked">
            <span v-for="(b, i) in m.data.blocked_actions" :key="i"><AppIcon name="block" />{{ b.rule_id }} 已拦截</span>
          </span>
          <span v-if="m.data" class="meta">
            <span :class="['route', m.data.route]">
              <AppIcon :name="m.data.route === 'fast' ? 'bolt' : 'route'" />
              {{ m.data.route === 'fast' ? `快速通道 ${m.data.fast_rule}` : m.data.escalated ? '安全节点' : '完整链路' }}
            </span>
            <span v-if="m.data.route !== 'fast' && !m.data.escalated">{{ agentNames(m.data) }}</span>
            <span class="num">{{ formatMs(m.data.latency_ms) }}</span>
          </span>
        </button>
      </template>
      <p v-if="session.busy" class="thinking">正在处理…</p>
    </div>

    <div class="composer">
      <div class="groups" role="tablist" aria-label="快捷指令分组">
        <button
          v-for="g in GROUPS" :key="g.name" role="tab" :aria-selected="group === g.name"
          :class="{ active: group === g.name }" @click="group = g.name"
        >{{ g.name }}</button>
      </div>
      <p v-if="activeGroup.hint" class="hint">{{ activeGroup.hint }}</p>
      <div class="chips">
        <button v-for="text in activeGroup.items" :key="text" :disabled="session.busy" @click="send(text)">{{ text }}</button>
      </div>
      <form class="input-row" @submit.prevent="send(draft)">
        <input v-model="draft" :disabled="session.busy" placeholder="说点什么，比如：温度调到22度" aria-label="输入指令" />
        <button type="submit" class="send" :disabled="session.busy || !draft.trim()" aria-label="发送"><AppIcon name="send" /></button>
      </form>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { AGENT_LABELS, formatMs } from '../lib/labels'
import { sendMessage, session } from '../lib/session'

const GROUPS = [
  { name: '快速指令', items: ['温度调到22度', '打开主驾座椅加热', '车窗开一半', '打开双闪', '下一首', '音量调到20'] },
  { name: '多意图', items: ['打开空调，然后导航去公司', '搜一下附近的充电站，然后导航过去', '关上天窗，再放点轻音乐'] },
  {
    name: '安全拦截',
    hint: '先在顶栏把场景切到「高速行驶」再试。',
    items: ['打开后备箱', '车窗开一半', '温度调到35度', '关闭车身稳定系统']
  },
  { name: '用车问答', items: ['胎压报警了怎么办', '家用充电桩怎么安装', '还能开多少公里'] },
  { name: '导航', items: ['导航去虹桥火车站', '附近有空闲的充电桩吗', '前面堵不堵'] },
  {
    name: '偏好记忆',
    hint: '先说偏好，过几秒等后台提炼完，再用"回家""按我习惯的温度"验证。',
    items: ['我家在徐汇区漕溪北路100号', '我习惯空调开22度', '回家', '按我习惯的温度开空调']
  }
]

const group = ref(GROUPS[0].name)
const activeGroup = computed(() => GROUPS.find(g => g.name === group.value))
const draft = ref('')
const listEl = ref(null)

function agentNames(data) {
  const agents = data.agent_types?.length ? data.agent_types : [data.agent_type]
  return agents.map(a => AGENT_LABELS[a] || a).join('、')
}

async function send(text) {
  if (!text.trim()) return
  draft.value = ''
  await sendMessage(text)
}

watch(() => [session.messages.length, session.busy], async () => {
  await nextTick()
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
})
</script>

<style scoped>
.chat { min-height: 0; }
.messages { flex: 1; min-height: 0; overflow: auto; padding: 4px 16px 12px; display: flex; flex-direction: column; gap: 10px; }

.welcome { margin: auto 0; max-width: 34em; }
.welcome p { margin: 0 0 6px; }
.welcome p:first-child { font-size: 20px; }

.bubble { max-width: 82%; border-radius: 14px; padding: 9px 13px; line-height: 1.6; }
.user { align-self: flex-end; background: var(--raised); border-bottom-right-radius: 4px; }
.assistant {
  align-self: flex-start; text-align: left; display: flex; flex-direction: column; gap: 6px;
  background: var(--ice-soft); border: 1px solid transparent; border-bottom-left-radius: 4px;
}
.assistant.selected { border-color: rgba(124, 198, 230, 0.6); }
.assistant.failed { background: var(--red-soft); color: var(--ink); opacity: 1; cursor: default; }
.text { white-space: pre-wrap; }

.meta { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--ink-3); align-items: center; }
.route { display: inline-flex; align-items: center; gap: 4px; color: var(--ice); }
.route.fast { color: var(--amber); }
.route .app-icon, .blocked .app-icon { width: 14px; height: 14px; }
.blocked { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--red); }
.blocked span { display: inline-flex; align-items: center; gap: 4px; }

.notice { align-self: center; margin: 0; font-size: 12px; color: var(--ink-3); }
.thinking { margin: 0; color: var(--ink-3); font-size: 13px; }

.composer { border-top: 1px solid var(--line); padding: 10px 16px 14px; display: flex; flex-direction: column; gap: 8px; }
.groups { display: flex; gap: 4px; flex-wrap: wrap; }
.groups button { border-color: transparent; background: transparent; color: var(--ink-3); padding: 3px 10px; font-size: 13px; }
.groups button.active { color: var(--ink); background: var(--raised); }
.hint { margin: 0; font-size: 12px; color: var(--ink-3); }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chips button { font-size: 13px; padding: 4px 12px; }

.input-row { display: flex; gap: 8px; }
.input-row input { flex: 1; border-radius: 999px; padding: 10px 16px; font-size: 15px; }
.send { width: 42px; height: 42px; justify-content: center; padding: 0; background: var(--ice); color: var(--night); border: none; }
.send:disabled { background: var(--raised); color: var(--ink-3); }
</style>
