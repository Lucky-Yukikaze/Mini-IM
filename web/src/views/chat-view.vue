<template>
  <div class="workspace">
    <ConversationList
      @create="dialogMode = 'create'"
      @join="dialogMode = 'join'"
      @direct="dialogMode = 'direct'"
    />

    <main class="chat-pane">
      <header class="chat-header">
        <div class="chat-title">
          <h2>{{ session.activeConversationLabel }}</h2>
          <p>{{ activeSubtitle }}</p>
        </div>
        <div class="chat-header-actions">
          <span class="connection-pill">
            <span class="status-dot" :class="{ online: session.connection.state === 'connected' }"></span>
            {{ session.connection.state }}
          </span>
          <button class="secondary-button" @click="debugOpen = true">联调</button>
        </div>
      </header>

      <MessageList
        :conversation-id="session.activeConversationId"
        :conversation-type="session.currentConversation?.type ?? ''"
        :current-user-id="session.currentUserId"
        :messages="session.currentMessages"
        @recall="onRecall"
        @fill-download="onFillDownload"
      />

      <MessageComposer
        :disabled="!canSendToActiveConversation"
        :download-file-id-preset="downloadFileIdPreset"
        @send="onSend"
        @send-file="onSendFile"
        @download-file="onDownloadFile"
      />
    </main>

    <aside class="details-pane">
      <section class="details-section">
        <p class="section-kicker">Conversation</p>
        <h3>{{ session.activeConversationLabel }}</h3>
        <div class="details-line">{{ session.activeConversationId || '-' }}</div>
        <div v-if="isGroupConversation" class="group-actions">
          <input v-model="renameTitle" :disabled="!isOwner" placeholder="群名称" />
          <button class="secondary-button" :disabled="!isOwner || !renameTitle.trim()" @click="onRenameConversation">
            改名
          </button>
        </div>
      </section>
      <section class="details-section">
        <div class="details-title">成员</div>
        <div class="member-list">
          <span v-for="member in activeMembers" :key="member" class="member-chip">
            {{ member }}
            <button
              v-if="isGroupConversation && isOwner && member !== session.currentUserId"
              class="member-remove"
              @click="onRemoveMember(member)"
            >
              移除
            </button>
          </span>
          <span v-if="activeMembers.length === 0" class="muted-text">暂无成员</span>
        </div>
        <div v-if="isGroupConversation" class="group-actions">
          <input v-model="memberDraft" :disabled="!isOwner" placeholder="成员 ID，逗号分隔" />
          <button class="secondary-button" :disabled="!isOwner || !memberDraft.trim()" @click="onAddMembers">
            邀请
          </button>
        </div>
        <button
          v-if="isGroupConversation"
          class="danger-button"
          :disabled="!session.activeConversationId || !isActiveMember"
          @click="onLeaveConversation"
        >
          退出群聊
        </button>
      </section>
      <section class="details-section">
        <div class="details-title">文件</div>
        <div class="file-progress-scroll">
          <div v-if="currentFileProgress.length === 0" class="muted-text">暂无传输</div>
          <div v-for="item in currentFileProgress" :key="item.fileId" class="file-progress-card">
            <div>
              <div class="file-name">传输任务 ID: {{ item.fileId }}</div>
              <div class="file-meta">{{ item.transferredBytes }} bytes · v{{ item.version }}</div>
            </div>
            <span class="status-dot" :class="{ online: item.completed }"></span>
          </div>
        </div>
      </section>
    </aside>

    <ConversationDialog
      v-if="dialogMode"
      :mode="dialogMode"
      @close="dialogMode = null"
      @create="onCreateConversation"
      @join="onJoinConversation"
      @direct="onCreateDirectConversation"
    />

    <DebugDrawer
      :open="debugOpen"
      :state="session.connection.state"
      :session-id="session.connection.sessionId"
      :current-user-id="session.currentUserId"
      :global-cursor="session.globalCursor"
      :unread-total="session.unreadTotal"
      :file-progress="currentFileProgress"
      @close="debugOpen = false"
      @connect="onConnect"
      @disconnect="onDisconnect"
    />

    <div v-if="notice" class="toast">{{ notice }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  addMembers,
  connectWithResume,
  createConversation,
  createDirectConversation,
  disconnect,
  downloadFile,
  initBridge,
  joinConversation,
  leaveConversation,
  recallMessage,
  removeMembers,
  renameConversation,
  sendFile,
  sendMessage,
  sendReceipt
} from '../api/bridge';
import ConversationDialog from '../components/conversation-dialog.vue';
import ConversationList from '../components/conversation-list.vue';
import DebugDrawer from '../components/debug-drawer.vue';
import MessageComposer from '../components/message-composer.vue';
import MessageList from '../components/message-list.vue';
import { bridgeEvents } from '../events/bridge-event-store';
import { useSessionStore } from '../store/session';
import type {
  ConnectionState,
  ConversationItem,
  FileProgressItem,
  InitialStatePayload,
  MessageItem,
  MessageUpdate
} from '../types';

const session = useSessionStore();
const debugOpen = ref(false);
const dialogMode = ref<'create' | 'join' | 'direct' | null>(null);
const notice = ref('');
const memberDraft = ref('');
const renameTitle = ref('');
const downloadFileIdPreset = ref('');
const pendingMessages: MessageItem[] = [];
let messageFlushFrame = 0;

const currentFileProgress = computed(() => session.currentFileProgress);
const activeMembers = computed(() => session.currentConversation?.memberIds ?? []);
const isOwner = computed(() => session.currentConversation?.ownerId === session.currentUserId);
const isActiveMember = computed(() => activeMembers.value.includes(session.currentUserId));
const isGroupConversation = computed(() => session.currentConversation?.type === 'group');
const canSendToActiveConversation = computed(
  () => Boolean(session.activeConversationId) && isActiveMember.value
);
const activeSubtitle = computed(() => {
  const conversation = session.currentConversation;
  if (!conversation) {
    return '未选择会话';
  }
  return conversation.type === 'group'
    ? `${conversation.memberIds.length} 人群聊`
    : `单聊 · ${conversation.ownerId}`;
});

function showNotice(message: string): void {
  notice.value = message;
  window.setTimeout(() => {
    if (notice.value === message) {
      notice.value = '';
    }
  }, 2200);
}

function syncReceiptForCurrentConversation(): void {
  const lastMessage = [...session.currentMessages]
    .filter((item) => item.senderId !== session.currentUserId)
    .at(-1);
  if (!lastMessage || !session.activeConversationId) {
    return;
  }
  sendReceipt(session.activeConversationId, lastMessage.seq);
}

function queueMessage(item: MessageItem): void {
  pendingMessages.push(item);
  if (messageFlushFrame > 0) {
    return;
  }
  messageFlushFrame = window.requestAnimationFrame(() => {
    messageFlushFrame = 0;
    flushPendingMessages();
  });
}

function flushPendingMessages(): void {
  if (messageFlushFrame > 0) {
    window.cancelAnimationFrame(messageFlushFrame);
    messageFlushFrame = 0;
  }
  const items = pendingMessages.splice(0, pendingMessages.length);
  if (items.length === 0) {
    return;
  }
  session.pushMessages(items);
  if (items.some((entry) => entry.conversationId === session.activeConversationId)) {
    syncReceiptForCurrentConversation();
  }
}

bridgeEvents.on('connectionChanged', (payload) => {
  const connection = payload as Partial<ConnectionState>;
  session.setConnection({
    state: (connection.state ?? 'error') as ConnectionState['state'],
    sessionId: connection.sessionId ?? ''
  });
});

bridgeEvents.on('initialStateLoaded', (payload) => {
  session.applyInitialState(payload as InitialStatePayload);
});

bridgeEvents.on('conversationUpdated', (payload) => {
  flushPendingMessages();
  session.applyConversationUpdated(payload as ConversationItem);
});

bridgeEvents.on('messagePushed', (payload) => {
  queueMessage(payload as MessageItem);
});

bridgeEvents.on('messageUpdated', (payload) => {
  flushPendingMessages();
  session.applyMessageUpdated(payload as MessageUpdate);
});

bridgeEvents.on('fileProgress', (payload) => {
  flushPendingMessages();
  session.applyFileProgress(payload as FileProgressItem);
});

bridgeEvents.on('errorRaised', (payload) => {
  if (typeof payload === 'string') {
    showNotice(payload);
    return;
  }
  const message = (payload as { message?: string })?.message;
  showNotice(message || '请求被拒绝');
});

watch(
  () => session.activeConversationId,
  () => {
    renameTitle.value = session.currentConversation?.title ?? '';
    syncReceiptForCurrentConversation();
  }
);

watch(
  () => session.currentConversation?.title,
  (title) => {
    renameTitle.value = title ?? '';
  }
);

onMounted(async () => {
  try {
    await initBridge();
  } catch {
    showNotice('未检测到 Qt Bridge，已进入 Web 调试模式');
  }
});

async function onConnect(endpoint: string, token: string, deviceId: string): Promise<void> {
  session.setConnection({ state: 'connecting', sessionId: '' });
  const ok = await connectWithResume(
    endpoint,
    token,
    deviceId,
    session.connection.sessionId,
    session.globalCursor,
    ''
  );
  if (ok) {
    if (session.connection.state === 'connecting') {
      session.setConnection({ state: 'connected', sessionId: session.connection.sessionId });
    }
  } else {
    session.setConnection({ state: 'error', sessionId: '' });
  }
}

function onDisconnect(): void {
  disconnect();
}

function onCreateConversation(title: string, memberIds: string[]): void {
  createConversation(title, memberIds);
}

function onCreateDirectConversation(peerUserId: string): void {
  createDirectConversation(peerUserId);
}

function parseMemberDraft(): string[] {
  return memberDraft.value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function onAddMembers(): void {
  const members = parseMemberDraft();
  if (!session.activeConversationId || members.length === 0) {
    return;
  }
  if (addMembers(session.activeConversationId, members)) {
    memberDraft.value = '';
  }
}

function onRemoveMember(memberId: string): void {
  if (!session.activeConversationId || !memberId) {
    return;
  }
  removeMembers(session.activeConversationId, [memberId]);
}

function onLeaveConversation(): void {
  if (!session.activeConversationId) {
    return;
  }
  leaveConversation(session.activeConversationId);
}

function onRenameConversation(): void {
  const title = renameTitle.value.trim();
  if (!session.activeConversationId || !title) {
    return;
  }
  renameConversation(session.activeConversationId, title);
}

function onJoinConversation(conversationId: string): void {
  if (!conversationId) {
    return;
  }
  if (joinConversation(conversationId)) {
    showNotice('已发送加群请求');
  }
}

function onSend(text: string, burnMode: number, burnTtlSec: number): void {
  if (!session.activeConversationId) {
    return;
  }
  sendMessage(session.activeConversationId, text, burnMode, burnTtlSec);
}

function onRecall(conversationId: string, messageId: string): void {
  recallMessage(conversationId, messageId);
}

function onFillDownload(fileId: string): void {
  downloadFileIdPreset.value = fileId;
}

function onSendFile(filePath: string): void {
  if (!session.activeConversationId) {
    return;
  }
  sendFile(session.activeConversationId, filePath, 0);
}

function onDownloadFile(fileId: string, savePath: string): void {
  if (!session.activeConversationId) {
    return;
  }
  downloadFile(session.activeConversationId, fileId, savePath, 0);
}
</script>
