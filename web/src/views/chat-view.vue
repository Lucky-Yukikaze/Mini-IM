<template>
  <div class="workspace">
    <ConversationList
      @create="dialogMode = 'create'"
      @join="dialogMode = 'join'"
      @direct="dialogMode = 'direct'"
    />

    <main class="chat-pane" :class="{ 'has-pending-messages': session.currentMessageSends.length > 0 }">
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

      <section v-if="session.currentMessageSends.length" class="pending-message-list" aria-label="待发送消息">
        <div v-for="item in session.currentMessageSends" :key="item.clientMsgId" class="pending-message">
          <span class="pending-message-text">{{ item.text }}</span>
          <span>{{ item.status === 'failed' ? '发送失败' : item.code ? '等待重试' : '发送中' }}</span>
          <span v-if="item.error" class="muted-text">{{ item.error }}</span>
          <button v-if="item.status === 'failed'" class="secondary-button"
            :disabled="session.connection.state !== 'connected'" @click="onRetryMessage(item.clientMsgId)">重试</button>
        </div>
      </section>

      <MessageComposer
        :disabled="!canSendToActiveConversation"
        :download-file-id-preset="downloadFileIdPreset"
        :send-message="onSend"
        :send-file="onSendFile"
        :download-file="onDownloadFile"
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
          <div v-for="task in session.currentFileTasks" :key="task.clientFileId" class="file-task-card">
            <div>{{ task.direction === 1 ? '上传' : '下载' }} · {{ task.fileName }}</div>
            <div class="file-meta">{{ task.status === 'failed' ? '传输失败' : task.status === 'finishing' ? '等待完成确认' : '等待或传输中' }}</div>
            <div v-if="task.error" class="file-meta">{{ task.error }}</div>
            <button v-if="task.status === 'failed'" class="secondary-button"
              :disabled="session.connection.state !== 'connected'" @click="onFileAction(task.clientFileId, false)">重试</button>
            <button class="secondary-button" @click="onFileAction(task.clientFileId, true)">取消</button>
          </div>
          <div v-if="currentFileProgress.length === 0 && session.currentFileTasks.length === 0" class="muted-text">暂无传输</div>
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
  connect,
  createConversation,
  createDirectConversation,
  disconnect,
  downloadFile,
  initBridge,
  joinConversation,
  leaveConversation,
  recallMessage,
  retryMessage,
  retryFile,
  cancelFile,
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
  MessageSendItem,
  FileTaskItem,
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
  const receivedMessages = session.currentMessages.filter((item) => item.senderId !== session.currentUserId);
  const lastMessage = receivedMessages[receivedMessages.length - 1];
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
  if (messageFlushFrame > 0) {
    window.cancelAnimationFrame(messageFlushFrame);
    messageFlushFrame = 0;
  }
  pendingMessages.splice(0);
  session.applyInitialState(payload as InitialStatePayload);
});

bridgeEvents.on('fileTasksChanged', (payload) => {
  session.applyFileTasks((payload as { items: FileTaskItem[] }).items);
});

bridgeEvents.on('messageSendsChanged', (payload) => {
  session.applyMessageSends((payload as { items: MessageSendItem[] }).items);
});

bridgeEvents.on('syncProgress', (payload) => {
  session.globalCursor = (payload as { globalCursor: number }).globalCursor;
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
  try {
    const accepted = await connect(endpoint, token, deviceId);
    if (!accepted) {
      session.setConnection({ state: 'error', sessionId: '' });
    }
  } catch (error) {
    session.setConnection({ state: 'error', sessionId: '' });
    showNotice(String(error));
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

async function onSend(text: string, burnMode: number, burnTtlSec: number): Promise<boolean> {
  if (!session.activeConversationId) return false;
  try {
    const accepted = await sendMessage(session.activeConversationId, text, burnMode, burnTtlSec);
    if (!accepted) showNotice('消息未能保存，请重试');
    return accepted;
  } catch (error) {
    showNotice(String(error));
    return false;
  }
}

async function onRetryMessage(clientMsgId: string): Promise<void> {
  try {
    if (!await retryMessage(session.activeConversationId, clientMsgId)) showNotice('暂时无法重试此消息');
  } catch (error) {
    showNotice(String(error));
  }
}

function onRecall(conversationId: string, messageId: string): void {
  recallMessage(conversationId, messageId);
}

function onFillDownload(fileId: string): void {
  downloadFileIdPreset.value = fileId;
}

async function onSendFile(filePath: string): Promise<boolean> {
  if (!session.activeConversationId) return false;
  try {
    const accepted = await sendFile(session.activeConversationId, filePath, 0);
    if (!accepted) showNotice('文件任务未保存，请检查路径');
    return accepted;
  } catch {
    showNotice('文件任务调用失败');
    return false;
  }
}

async function onDownloadFile(fileId: string, savePath: string): Promise<boolean> {
  if (!session.activeConversationId) return false;
  try {
    const accepted = await downloadFile(session.activeConversationId, fileId, savePath, 0);
    if (!accepted) showNotice('下载任务未保存，请检查输入');
    return accepted;
  } catch {
    showNotice('下载任务调用失败');
    return false;
  }
}

async function onFileAction(clientFileId: string, cancel: boolean): Promise<void> {
  try {
    const accepted = await (cancel ? cancelFile(clientFileId) : retryFile(clientFileId));
    if (!accepted) showNotice('文件任务操作未被接受');
  } catch {
    showNotice('文件任务操作失败');
  }
}
</script>

<style scoped>
.file-task-card { padding: 8px 0; overflow-wrap: anywhere; }
.file-task-card button { margin-top: 6px; margin-right: 6px; }
.chat-pane.has-pending-messages { grid-template-rows: auto minmax(0, 1fr) auto auto; }
.pending-message-list { padding: 8px 20px; max-height: 160px; overflow-y: auto; }
.pending-message { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 13px; }
.pending-message-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
