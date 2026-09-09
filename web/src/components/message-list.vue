<template>
  <section class="message-surface">
    <div v-if="!conversationId" class="chat-empty">
      <div class="chat-empty-title">选择一个会话</div>
      <div class="chat-empty-subtitle">消息会在这里显示</div>
    </div>
    <div
      v-else
      ref="viewportRef"
      class="message-viewport"
      @scroll="onScroll"
    >
      <div :style="{ height: `${topSpacerHeight}px` }"></div>
      <article
        v-for="item in visibleMessages"
        :key="item.id"
        class="message-row"
        :class="{ 'message-row-self': item.senderId === currentUserId }"
      >
        <div class="avatar">{{ senderInitial(item.senderId) }}</div>
        <div class="message-stack">
          <div class="message-meta">
            <span>{{ item.senderId }}</span>
            <time>{{ formatTime(item.createdAtMs) }}</time>
          </div>
          <div class="bubble" :class="{ 'bubble-file': item.filePayload }">
            <template v-if="item.recalled">
              <span>{{ item.burned ? '消息已焚毁' : '消息已撤回' }}</span>
            </template>
            <template v-else-if="item.filePayload">
              <div class="file-bubble">
                <div class="file-icon">FILE</div>
                <div class="file-copy">
                  <div class="file-name">
                    {{ item.filePayload.fileName || item.filePayload.fileId }}
                  </div>
                  <div class="file-meta">
                    <span>{{ formatBytes(item.filePayload.fileSize) }}</span>
                    <span>源文件 ID:</span>
                    {{ item.filePayload.fileId }}
                  </div>
                  <button class="text-button file-download-fill" @click="$emit('fillDownload', item.filePayload.fileId)">
                    填入下载
                  </button>
                </div>
              </div>
            </template>
            <template v-else>
              {{ item.text }}
            </template>
          </div>
          <div class="message-actions">
            <span v-if="deliveryLabel(item, currentUserId, conversationType)" class="pill">{{ deliveryLabel(item, currentUserId, conversationType) }}</span>
            <span v-if="readLabel(item)" class="pill">{{ readLabel(item) }}</span>
            <span v-if="!item.recalled && item.burnMode > 0" class="pill">焚毁 {{ item.burnTtlSec }}s</span>
            <button
              v-if="item.senderId === currentUserId && !item.recalled"
              class="text-button"
              :disabled="recallDisabled"
              @click="$emit('recall', item.conversationId, item.id)"
            >
              撤回
            </button>
          </div>
        </div>
      </article>
      <div :style="{ height: `${bottomSpacerHeight}px` }"></div>
      <div v-if="messages.length === 0" class="chat-empty chat-empty-inline">
        <div class="chat-empty-title">暂无消息</div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { deliveryLabel } from '../model/delivery';
import type { MessageItem } from '../types';

interface FilePayload {
  kind: string;
  fileId: string;
  fileName?: string;
  fileSize?: number;
}

interface RenderMessage extends MessageItem {
  filePayload: FilePayload | null;
}

const ESTIMATED_ROW_HEIGHT = 112;
const OVERSCAN = 8;

const props = defineProps<{
  conversationId: string;
  conversationType: 'direct' | 'group' | '';
  currentUserId: string;
  messages: MessageItem[];
  recallDisabled: boolean;
}>();

defineEmits<{
  recall: [conversationId: string, messageId: string];
  fillDownload: [fileId: string];
}>();

const viewportRef = ref<HTMLElement | null>(null);
const scrollTop = ref(0);
const viewportHeight = ref(560);
let scrollFrame = 0;

const startIndex = computed(() =>
  Math.max(0, Math.floor(scrollTop.value / ESTIMATED_ROW_HEIGHT) - OVERSCAN)
);
const visibleCount = computed(() =>
  Math.ceil(viewportHeight.value / ESTIMATED_ROW_HEIGHT) + OVERSCAN * 2
);
const endIndex = computed(() => Math.min(props.messages.length, startIndex.value + visibleCount.value));
const visibleMessages = computed<RenderMessage[]>(() =>
  props.messages.slice(startIndex.value, endIndex.value).map((item) => ({
    ...item,
    filePayload: parseFilePayload(item.text)
  }))
);
const topSpacerHeight = computed(() => startIndex.value * ESTIMATED_ROW_HEIGHT);
const bottomSpacerHeight = computed(() =>
  Math.max(0, (props.messages.length - endIndex.value) * ESTIMATED_ROW_HEIGHT)
);

function measureViewport(): void {
  const viewport = viewportRef.value;
  if (!viewport) {
    return;
  }
  viewportHeight.value = viewport.clientHeight || 560;
}

function isNearBottom(): boolean {
  const viewport = viewportRef.value;
  if (!viewport) {
    return true;
  }
  return viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 120;
}

function scrollToBottom(): void {
  const viewport = viewportRef.value;
  if (!viewport) {
    return;
  }
  viewport.scrollTop = viewport.scrollHeight;
  scrollTop.value = viewport.scrollTop;
}

function onScroll(): void {
  if (scrollFrame > 0) {
    return;
  }
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = 0;
    scrollTop.value = viewportRef.value?.scrollTop ?? 0;
  });
}

function senderInitial(senderId: string): string {
  return (senderId.trim()[0] ?? '?').toUpperCase();
}

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatBytes(size?: number): string {
  const value = Number(size ?? 0);
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function readLabel(item: MessageItem): string {
  if (item.recalled || item.senderId !== props.currentUserId) {
    return '';
  }
  if (item.readCountKnown === false) return '已读状态同步中';
  if (props.conversationType === 'direct') {
    return item.unreadCount > 0 ? '未读' : '已读';
  }
  if (props.conversationType === 'group' && item.unreadCount > 0) {
    return `${item.unreadCount} 人未读`;
  }
  return '';
}

function parseFilePayload(text: string): FilePayload | null {
  if (!text || text[0] !== '{') {
    return null;
  }
  try {
    const payload = JSON.parse(text) as Partial<FilePayload>;
    if (payload.kind === 'file' && payload.fileId) {
      return {
        kind: 'file',
        fileId: payload.fileId,
        fileName: payload.fileName,
        fileSize: Number(payload.fileSize ?? 0)
      };
    }
  } catch {
    return null;
  }
  return null;
}

watch(
  () => [props.conversationId, props.messages.length] as const,
  async ([conversationId], [oldConversationId]) => {
    const shouldStick = conversationId !== oldConversationId || isNearBottom();
    await nextTick();
    measureViewport();
    if (shouldStick) {
      scrollToBottom();
    }
  },
  { flush: 'post' }
);

onMounted(async () => {
  await nextTick();
  measureViewport();
  scrollToBottom();
  window.addEventListener('resize', measureViewport);
});

onUnmounted(() => {
  window.removeEventListener('resize', measureViewport);
  if (scrollFrame > 0) {
    window.cancelAnimationFrame(scrollFrame);
  }
});
</script>
