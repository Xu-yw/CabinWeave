<!-- 场景下拉：基于 Reka UI 的 Select，每个预设带图标和一句说明（由后端返回的 overrides 生成）。 -->
<template>
  <SelectRoot :model-value="session.scenario || undefined" :disabled="!session.scenarios.length" @update:model-value="switchScenario">
    <SelectTrigger class="select-trigger" aria-label="车辆场景">
      <AppIcon :name="session.scenario || 'car'" />
      <SelectValue placeholder="选择场景" />
      <AppIcon name="chevron" class="select-chevron" />
    </SelectTrigger>
    <SelectPortal>
      <SelectContent class="select-content" position="popper" align="end" :side-offset="6">
        <SelectViewport>
          <SelectItem v-for="item in session.scenarios" :key="item.name" :value="item.name" class="select-item">
            <AppIcon :name="item.name" />
            <span class="select-text">
              <SelectItemText>{{ item.label }}</SelectItemText>
              <small>{{ describeScenario(item.overrides) }}</small>
            </span>
            <SelectItemIndicator class="select-check"><AppIcon name="check" /></SelectItemIndicator>
          </SelectItem>
        </SelectViewport>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>

<script setup>
import {
  SelectContent, SelectItem, SelectItemIndicator, SelectItemText, SelectPortal, SelectRoot, SelectTrigger, SelectValue,
  SelectViewport
} from 'reka-ui'
import AppIcon from './AppIcon.vue'
import { describeScenario } from '../lib/labels'
import { session, switchScenario } from '../lib/session'
</script>
