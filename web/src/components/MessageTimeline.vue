<script setup lang="ts">
/**
 * 渲染分组消息、工具活动和当前运行摘要。
 *
 * 对话组装位于 chat/conversation。此组件负责展示和滚动跟随：
 * 阅读较早消息时暂停自动滚动，直到视口回到底部附近。
 * 不规范化模型生成的空白字符。
 */

import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'

import type { PublicMessage, StreamTimeline } from '../domain/types'
import {
  buildConversationEntries,
  userBlocks,
  visibleConversationEvents,
  type AgentPart,
} from '../chat/conversation'
import MediaMessageBlock from './MediaMessageBlock.vue'
import MarkdownReply from './MarkdownReply.vue'

const props = defineProps<{
  messages: PublicMessage[]
  timeline: StreamTimeline
  isBusy: boolean
  readAsset?: (assetId: string, signal?: AbortSignal) => Promise<Blob>
}>()

const scrollArea = ref<HTMLElement | null>(null)
const scrollContent = ref<HTMLElement | null>(null)
let resizeObserver: ResizeObserver | undefined
const shouldFollowOutput = ref(true)
const BOTTOM_FOLLOW_THRESHOLD = 80
const visibleEvents = computed(() =>
  visibleConversationEvents(props.messages, props.timeline),
)
const conversationEntries = computed(() =>
  buildConversationEntries(props.messages, props.timeline),
)
/** 较新的输入消息仍在处理时隐藏旧的终态摘要。 */
const shouldShowTerminal = computed(() => {
  const terminal = props.timeline.terminal
  if (!terminal) return false
  const lastMessage = props.messages.at(-1)
  if (!lastMessage || (lastMessage.role !== 'user' && lastMessage.role !== 'system')) {
    return true
  }
  const endedAt = terminal.timing?.ended_at
  if (!endedAt) return !props.isBusy
  const terminalTime = Date.parse(endedAt)
  const messageTime = Date.parse(lastMessage.created_at)
  if (Number.isNaN(terminalTime) || Number.isNaN(messageTime)) return !props.isBusy
  return terminalTime >= messageTime
})

watch(
  () => [props.messages.length, props.timeline.items.length, props.timeline.cursor],
  () => void nextTick(scrollToBottom),
)
onMounted(() => {
  scrollToBottom(true)
  // 异步 Markdown 节点和语法高亮可能在消息更新后改变高度。
  if (typeof ResizeObserver !== 'undefined' && scrollContent.value) {
    resizeObserver = new ResizeObserver(() => scrollToBottom())
    resizeObserver.observe(scrollContent.value)
  }
})
onBeforeUnmount(() => resizeObserver?.disconnect())

/**
 * 仅在读者仍靠近底部时跟随输出。
 *
 * @param force - 忽略跟随偏好，用于首次挂载。
 */
function scrollToBottom(force = false): void {
  if (!scrollArea.value || (!force && !shouldFollowOutput.value)) return
  scrollArea.value.scrollTop = scrollArea.value.scrollHeight
}

/**
 * 视口距底部超过 80 像素时暂停自动跟随。
 */
function updateScrollFollow(): void {
  const element = scrollArea.value
  if (!element) return
  const distanceFromBottom =
    element.scrollHeight - element.scrollTop - element.clientHeight
  shouldFollowOutput.value = distanceFromBottom <= BOTTOM_FOLLOW_THRESHOLD
}

/**
 * 使用中文区域设置将消息时间戳格式化为本地时分。
 */
function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

/**
 * 不足一秒的耗时显示为四舍五入的毫秒，否则显示一位小数的秒数。
 */
function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`
  return `${(milliseconds / 1000).toFixed(1)}s`
}

/**
 * 返回 Token 减少百分比；计数不足时返回 undefined。
 */
function compressionRate(
  part: Extract<AgentPart, { kind: 'compaction' }>,
): string | undefined {
  if (
    part.beforeTokens === undefined ||
    part.afterTokens === undefined ||
    part.beforeTokens <= 0
  ) {
    return undefined
  }
  return `${Math.round(((part.beforeTokens - part.afterTokens) / part.beforeTokens) * 100)}%`
}

/**
 * 将活动生命周期状态映射为模板使用的简短标签。
 */
function statusLabel(status: 'running' | 'completed' | 'failed'): string {
  if (status === 'running') return '运行中'
  return status === 'failed' ? '失败' : '已完成'
}
</script>

<template>
  <main
    ref="scrollArea"
    class="timeline-scroll"
    aria-live="polite"
    @scroll.passive="updateScrollFollow"
  >
    <div ref="scrollContent" class="timeline-content">
      <section
        v-if="
          messages.length === 0 &&
          visibleEvents.length === 0 &&
          !isBusy &&
          !timeline.terminal
        "
        class="empty-state"
      >
        <div class="empty-orbit">
          <span />
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path d="M9 10h14v10H12l-5 4v-4H5V10h4Z" />
            <path d="M11 14h10M11 17h7" />
          </svg>
        </div>
        <div class="empty-heading">
          <p class="eyebrow">新的对话</p>
          <h1>从一个问题开始</h1>
        </div>
      </section>

      <article
        v-for="entry in conversationEntries"
        :key="entry.key"
        class="message"
        :class="[
          `message-${entry.kind}`,
          {
            'message-error': entry.kind === 'agent' && entry.isError,
            'message-live': entry.kind === 'agent' && entry.isLive,
          },
        ]"
      >
        <div v-if="entry.kind === 'agent'" class="assistant-meta">
          <div class="assistant-avatar">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 3 9 9l-6 3 6 3 3 6 3-6 6-3-6-3-3-6Z" />
            </svg>
          </div>
          <span v-if="entry.isLive" class="live-label">生成中</span>
          <time v-else :datetime="entry.createdAt">{{
            formatTime(entry.createdAt)
          }}</time>
        </div>
        <div v-if="entry.kind === 'user'" class="message-body">
          <div class="user-message-blocks">
            <template
              v-for="(block, index) in userBlocks(entry.message)"
              :key="`${entry.message.message_id}-${index}`"
            >
              <p v-if="block.type === 'text'" data-history-block data-user-block="text">
                {{ block.text }}
              </p>
              <MediaMessageBlock
                v-else
                :block="block"
                :read-asset="readAsset"
                :data-user-block="block.type"
              />
            </template>
          </div>
          <time class="user-message-time" :datetime="entry.message.created_at">
            {{ formatTime(entry.message.created_at) }}
          </time>
        </div>
        <div v-else class="message-body agent-response">
          <template v-for="part in entry.parts" :key="part.key">
            <div
              v-if="part.kind === 'text'"
              class="streaming-answer"
              data-history-block
            >
              <MarkdownReply
                :content="part.text"
                :streaming="part.isLive && !part.completed && isBusy"
              />
              <i v-if="part.isLive && !part.completed && isBusy" class="text-caret" />
            </div>
            <div
              v-else-if="part.kind === 'compaction'"
              class="compaction-event"
              :data-status="part.status"
              data-history-block
            >
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M4 5h12M6 10h8M8 15h4" />
              </svg>
              <span>自动压缩上下文</span>
              <i
                class="event-status-dot"
                :class="`status-${part.status}`"
                role="img"
                :aria-label="statusLabel(part.status)"
                :title="statusLabel(part.status)"
              />
              <small v-if="compressionRate(part)" class="event-metric">
                {{ compressionRate(part) }}
              </small>
              <small v-if="part.durationMs !== undefined" class="event-duration">
                {{ formatDuration(part.durationMs) }}
              </small>
            </div>
            <details
              v-else-if="part.kind === 'thinking'"
              class="compact-event thinking-event"
              :data-status="part.isLive && !part.completed ? 'running' : 'completed'"
              :open="part.isLive && !part.completed"
            >
              <summary data-history-block>
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path
                    d="m10 2 1.5 4.5L16 8l-4.5 1.5L10 14 8.5 9.5 4 8l4.5-1.5L10 2Z"
                  />
                  <path
                    d="m15.5 13 .7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8Z"
                  />
                </svg>
                <span>思考过程</span>
                <i
                  class="event-status-dot"
                  :class="
                    part.isLive && !part.completed
                      ? 'status-running'
                      : 'status-completed'
                  "
                  role="img"
                  :aria-label="
                    statusLabel(
                      part.isLive && !part.completed ? 'running' : 'completed',
                    )
                  "
                  :title="
                    statusLabel(
                      part.isLive && !part.completed ? 'running' : 'completed',
                    )
                  "
                />
                <small v-if="part.durationMs !== undefined" class="event-duration">
                  {{ formatDuration(part.durationMs) }}
                </small>
                <i class="event-chevron" />
              </summary>
              <p>{{ part.content || '正在组织思路…' }}</p>
            </details>
            <details
              v-else-if="part.kind === 'tool'"
              class="compact-event tool-event"
              :class="{ 'tool-result-error': part.isError }"
              :data-status="part.status"
            >
              <summary data-history-block>
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path
                    d="m12 3-2 4 3 3 4-2a6 6 0 0 1-8 7l-4 3-3-3 3-4a6 6 0 0 1 7-8Z"
                  />
                </svg>
                <strong>{{ part.name }}</strong>
                <i
                  class="event-status-dot"
                  :class="`status-${part.status}`"
                  role="img"
                  :aria-label="statusLabel(part.status)"
                  :title="statusLabel(part.status)"
                />
                <small v-if="part.durationMs !== undefined" class="event-duration">
                  {{ formatDuration(part.durationMs) }}
                </small>
                <i class="event-chevron" />
              </summary>
              <div class="event-detail">
                <pre v-if="part.arguments">{{
                  JSON.stringify(part.arguments, null, 2)
                }}</pre>
                <p v-if="part.content !== undefined">{{ part.content }}</p>
                <p v-if="part.reasonCode" class="tool-error">{{ part.reasonCode }}</p>
              </div>
            </details>
          </template>
        </div>
      </article>

      <div v-if="isBusy && !shouldShowTerminal" class="working-indicator">
        <span /><span /><span />
        <small>Agent 正在处理</small>
      </div>

      <section
        v-if="timeline.terminal && shouldShowTerminal"
        data-run-summary
        class="run-summary"
        :class="`run-summary-${timeline.terminal.status}`"
        :role="timeline.terminal.status === 'failed' ? 'alert' : undefined"
      >
        <div class="summary-content">
          <div class="summary-heading">
            <span class="summary-check">
              {{ timeline.terminal.status === 'succeeded' ? '✓' : '!' }}
            </span>
            <strong>
              {{
                timeline.terminal.status === 'succeeded'
                  ? '本次运行完成'
                  : timeline.terminal.status === 'failed'
                    ? '本次运行失败'
                    : '本次运行已取消'
              }}
            </strong>
          </div>
          <p v-if="timeline.terminal.status !== 'succeeded'" class="run-error-message">
            {{
              timeline.terminal.error?.message ||
              (timeline.terminal.status === 'failed'
                ? '运行过程中发生错误'
                : '运行已取消')
            }}
            <code v-if="timeline.terminal.error?.code">
              {{ timeline.terminal.error.code }}
            </code>
          </p>
        </div>
        <div class="metric-row">
          <span v-if="timeline.terminal.usage">
            {{ timeline.terminal.usage.total_tokens }} tokens
          </span>
          <span v-if="timeline.terminal.timing">
            总耗时 {{ formatDuration(timeline.terminal.timing.elapsed_ms) }}
          </span>
          <span v-if="timeline.terminal.timing">
            活跃 {{ formatDuration(timeline.terminal.timing.active_ms) }}
          </span>
        </div>
      </section>
    </div>
  </main>
</template>
