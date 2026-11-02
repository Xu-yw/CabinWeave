// 语音交互：用浏览器自带的 Web Speech API 做中文识别和播报，不经过后端。
// 识别时另开一路麦克风读音量，驱动输入区的语音按钮；拿不到音量时按钮只做固定的呼吸动画。
// 浏览器不支持识别时 supported 为 false，界面隐藏麦克风按钮；"播报回复"开关存在 localStorage。
import { reactive } from 'vue'

const PREF_KEY = 'cabinweave.frontend.speak'

const Recognition = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null
const synth = typeof window !== 'undefined' ? window.speechSynthesis : null

const ERRORS = {
  'not-allowed': '麦克风权限被拒绝，请在浏览器地址栏里允许使用麦克风',
  'service-not-allowed': '浏览器不允许使用语音识别服务',
  'no-speech': '没有听到声音，再说一次试试',
  'audio-capture': '没有找到可用的麦克风',
  network: '连不上语音识别服务（Chrome 的识别需要访问 Google 服务）',
  'language-not-supported': '浏览器不支持中文语音识别'
}

function readPref() {
  try {
    return localStorage.getItem(PREF_KEY) === 'on'
  } catch {
    return false
  }
}

export const voice = reactive({
  supported: Boolean(Recognition),
  canSpeak: Boolean(synth),
  listening: false,
  speaking: false,
  transcript: '',          // 识别中的文字，含还没定稿的部分
  level: 0,                // 麦克风音量，0–1
  error: '',
  speakReplies: readPref() // 打字发出的指令是否也播报回复；语音发出的指令总是播报
})

let recognition = null
let finalText = ''
let meter = null
let meterToken = 0   // 每次开关麦克风加一，丢弃过期的 getUserMedia 结果

/** 开始听；说完（或调用 stopListening）后把识别出的整句交给 onFinal */
export function startListening(onFinal) {
  if (!Recognition || voice.listening) return
  stopSpeaking()
  finalText = ''
  voice.transcript = ''
  voice.error = ''

  const rec = new Recognition()
  rec.lang = 'zh-CN'
  rec.interimResults = true
  rec.continuous = false
  rec.onresult = event => {
    let interim = ''
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i]
      if (result.isFinal) finalText += result[0].transcript
      else interim += result[0].transcript
    }
    voice.transcript = finalText + interim
  }
  rec.onerror = event => {
    if (event.error !== 'aborted') voice.error = ERRORS[event.error] || `语音识别出错（${event.error}）`
  }
  rec.onend = () => {
    const text = (finalText || voice.transcript).trim()
    recognition = null
    voice.listening = false
    voice.transcript = ''
    stopMeter()
    if (text && !voice.error) onFinal(text)
  }

  try {
    rec.start()
  } catch {
    voice.error = '无法启动语音识别'
    return
  }
  recognition = rec
  voice.listening = true
  startMeter()
}

export function stopListening() {
  recognition?.stop()
}

async function startMeter() {
  if (!navigator.mediaDevices?.getUserMedia || typeof AudioContext === 'undefined') return
  const token = ++meterToken
  let stream
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch {
    return // 拿不到音量不影响识别本身
  }
  if (!voice.listening || token !== meterToken) {
    stream.getTracks().forEach(track => track.stop())
    return
  }
  const ctx = new AudioContext()
  const analyser = ctx.createAnalyser()
  analyser.fftSize = 512
  ctx.createMediaStreamSource(stream).connect(analyser)
  const samples = new Uint8Array(analyser.fftSize)
  const tick = () => {
    analyser.getByteTimeDomainData(samples)
    let sum = 0
    for (const v of samples) sum += ((v - 128) / 128) ** 2
    const target = Math.min(1, Math.sqrt(sum / samples.length) * 5)
    voice.level = voice.level * 0.6 + target * 0.4
    meter.raf = requestAnimationFrame(tick)
  }
  meter = { stream, ctx, raf: requestAnimationFrame(tick) }
}

function stopMeter() {
  meterToken++
  if (meter) {
    cancelAnimationFrame(meter.raf)
    meter.stream.getTracks().forEach(track => track.stop())
    meter.ctx.close()
    meter = null
  }
  voice.level = 0
}

let zhVoice = null

function pickVoice() {
  const all = synth.getVoices().filter(v => /^zh[-_]CN/i.test(v.lang))
  // 优先用 Edge 的在线自然语音（如 Xiaoxiao），其次是系统自带的中文语音
  zhVoice = all.find(v => /Xiaoxiao|Natural/i.test(v.name)) || all[0] || null
}

if (synth) {
  pickVoice()
  synth.addEventListener?.('voiceschanged', pickVoice)
}

export function speak(text) {
  if (!synth || !text) return
  synth.cancel()
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = 'zh-CN'
  if (zhVoice) utterance.voice = zhVoice
  utterance.onstart = () => { voice.speaking = true }
  utterance.onend = () => { voice.speaking = false }
  utterance.onerror = () => { voice.speaking = false }
  synth.speak(utterance)
}

export function stopSpeaking() {
  if (synth && (synth.speaking || synth.pending)) synth.cancel()
  voice.speaking = false
}

export function setSpeakReplies(on) {
  voice.speakReplies = on
  if (!on) stopSpeaking()
  try {
    localStorage.setItem(PREF_KEY, on ? 'on' : 'off')
  } catch {
    // 存不了就只在本次页面里生效
  }
}
