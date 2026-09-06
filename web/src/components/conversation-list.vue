<template>
  <aside class="conversation-pane">
    <header class="pane-header">
      <div>
        <p class="section-kicker">Mini-IM</p>
        <h1>消息</h1>
      </div>
      <div class="pane-actions">
        <button class="icon-button" @click="$emit('create')">建群</button>
        <button class="icon-button" @click="$emit('join')">加群</button>
        <button class="icon-button" @click="$emit('direct')">私聊</button>
      </div>
    </header>

    <div class="search-box">
      <input v-model="keyword" placeholder="搜索会话" />
    </div>

    <div class="conversation-scroll">
      <button
        v-for="item in filteredConversations"
        :key="item.conversationId"
        class="conversation-item"
        :class="{ active: item.conversationId === session.activeConversationId }"
        @click="session.setCurrentConversation(item.conversationId)"
      >
        <span class="conversation-avatar">{{ avatarText(conversationTitle(item)) }}</span>
        <span class="conversation-copy">
          <span class="conversation-line">
            <strong>{{ conversationTitle(item) }}</strong>
            <time>{{ formatTime(item.updatedAtMs) }}</time>
          </span>
          <span class="conversation-preview">
            {{ item.type === 'group' ? `${item.memberIds.length} 人群聊` : '单聊' }}
          </span>
        </span>
      </button>
      <div v-if="filteredConversations.length === 0" class="empty-list">暂无会话</div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useSessionStore } from '../store/session';
import type { ConversationItem } from '../types';

defineEmits<{
  create: [];
  join: [];
  direct: [];
}>();

const session = useSessionStore();
const keyword = ref('');

const filteredConversations = computed(() => {
  const value = keyword.value.trim().toLowerCase();
  if (!value) {
    return session.conversations;
  }
  return session.conversations.filter((item) => {
    const title = `${item.title} ${item.conversationId}`.toLowerCase();
    return title.includes(value);
  });
});

function avatarText(text: string): string {
  return (text.trim()[0] ?? '#').toUpperCase();
}

function conversationTitle(item: ConversationItem): string {
  if (item.title) {
    return item.title;
  }
  if (item.type === 'direct') {
    return item.memberIds.find((memberId) => memberId !== session.currentUserId) ?? item.conversationId;
  }
  return item.conversationId;
}

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
</script>
