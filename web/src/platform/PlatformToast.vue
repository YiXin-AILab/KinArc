<script setup lang="ts">
import { useToasts } from './toast'

const { items } = useToasts()
</script>

<template>
  <div class="platform-toast" aria-live="polite">
    <div
      v-for="item in items"
      :key="item.id"
      class="platform-toast-item"
      :class="{ 'platform-toast-error': item.kind === 'error' }"
    >
      {{ item.message }}
    </div>
  </div>
</template>
