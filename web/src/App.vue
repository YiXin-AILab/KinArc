<script setup lang="ts">
import PlatformToast from './platform/PlatformToast.vue'
</script>

<template>
  <div class="app-frame">
    <nav class="platform-nav" aria-label="平台导航">
      <RouterLink class="platform-nav-brand" to="/chat">
        <span class="platform-nav-mark">K</span>
        <b>KinArc</b>
      </RouterLink>
      <div class="platform-nav-tabs">
        <RouterLink class="platform-nav-tab" to="/chat">对话</RouterLink>
        <RouterLink class="platform-nav-tab" to="/platform/agents">
          Agent 管理
        </RouterLink>
        <RouterLink class="platform-nav-tab" to="/platform/skills">
          Skill 仓库
        </RouterLink>
        <RouterLink class="platform-nav-tab" to="/platform/knowledge">
          知识库
        </RouterLink>
        <RouterLink class="platform-nav-tab" to="/platform/evolution">
          自进化
        </RouterLink>
        <RouterLink class="platform-nav-tab" to="/platform/evaluation">
          评测
        </RouterLink>
      </div>
    </nav>
    <div class="app-frame-body">
      <RouterView />
    </div>
    <PlatformToast />
  </div>
</template>
