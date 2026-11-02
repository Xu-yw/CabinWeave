<!-- 悬停提示：基于 Reka UI 的 Tooltip，默认插槽里的元素作为触发点（as-child），text 为空时只渲染插槽。 -->
<template>
  <TooltipRoot v-if="text">
    <TooltipTrigger as-child><slot /></TooltipTrigger>
    <TooltipPortal>
      <TooltipContent class="tip" :side="side" :side-offset="6" :collision-padding="8">
        {{ text }}
        <TooltipArrow class="tip-arrow" :width="10" :height="5" />
      </TooltipContent>
    </TooltipPortal>
  </TooltipRoot>
  <slot v-else />
</template>

<script setup>
import { TooltipArrow, TooltipContent, TooltipPortal, TooltipRoot, TooltipTrigger } from 'reka-ui'

defineProps({
  text: { type: String, default: '' },
  side: { type: String, default: 'top' }
})
</script>
