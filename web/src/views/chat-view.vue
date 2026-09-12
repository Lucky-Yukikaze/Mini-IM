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
        :recall-disabled="controlDisabled || hasPendingRecall"
        :messages="session.currentMessages"
        :has-more="session.historyByConversation[session.activeConversationId]?.hasMore ?? false"
        :history-busy="historyLoading[session.activeConversationId] || session.connection.state !== 'connected'"
        @load-history="onLoadHistory"
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
        :download-preset="downloadPreset"
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
          <input v-model="renameTitle" :disabled="!isOwner || controlDisabled" placeholder="群名称" />
          <button class="secondary-button" :disabled="!isOwner || controlDisabled || !renameTitle.trim()" @click="onRenameConversation">
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
              :disabled="controlDisabled"
              @click="onRemoveMember(member)"
            >
              移除
            </button>
          </span>
          <span v-if="activeMembers.length === 0" class="muted-text">暂无成员</span>
        </div>
        <div v-if="isGroupConversation" class="group-actions">
          <input v-model="memberDraft" :disabled="!isOwner || controlDisabled" placeholder="成员 ID，逗号分隔" />
          <button class="secondary-button" :disabled="!isOwner || controlDisabled || !memberDraft.trim()" @click="onAddMembers">
            邀请
          </button>
        </div>
        <button
          v-if="isGroupConversation"
          class="danger-button"
          :disabled="controlDisabled || !session.activeConversationId || !isActiveMember"
          @click="onLeaveConversation"
        >
          退出群聊
        </button>
      </section>
      <section class="details-section" aria-label="操作状态">
        <div class="details-title">操作状态</div>
        <button class="secondary-button" :disabled="!canSendToActiveConversation || controlDisabled"
          @click="syncReceiptForCurrentConversation(true)">标为已读</button>
        <div v-if="session.controlWrites.length" class="control-write-list">
          <article v-for="item in session.controlWrites" :key="item.requestId" class="control-write-card">
            <strong>{{ operationLabel(item.operation) }} · {{ item.status === 'failed' ? '未成功' : '等待确认' }}</strong>
            <span>{{ controlTarget(item) }}</span>
            <span v-if="item.status === 'pending'">{{ session.connection.state !== 'connected' ? '等待重新连接' : item.code ? '等待自动重试' : '已保存，正在处理' }}</span>
            <span v-if="item.error" class="control-write-error">{{ item.error }}</span>
            <span v-if="item.status === 'failed'">请检查原因后重新操作</span>
          </article>
        </div>
        <span v-else class="muted-text">没有待确认或失败的操作</span>
      </section>
      <section class="details-section">
        <div class="details-title">文件</div>
        <button class="secondary-button" :disabled="session.connection.state !== 'connected'"
          @click="cleanupOpen = true">清理已取消下载</button>
        <div class="file-progress-scroll">
          <div v-for="task in session.currentFileTasks" :key="task.clientFileId" class="file-task-card">
            <div>{{ task.direction === 1 ? '上传' : '下载' }} · {{ task.fileName }}</div>
            <div class="file-meta">{{ task.status === 'cancelling' ? '取消待确认' : task.status === 'cancel_failed' ? '取消未成功' : task.status === 'failed' ? '传输失败' : task.status === 'finishing' ? '等待完成确认' : '等待或传输中' }}</div>
            <div v-if="task.error" class="file-meta">{{ task.error }}</div>
            <button v-if="task.status === 'failed'" class="secondary-button"
              :disabled="session.connection.state !== 'connected'" @click="onFileAction(task.clientFileId, false)">重试</button>
            <button class="secondary-button" :disabled="task.status === 'cancelling'"
              @click="onFileAction(task.clientFileId, true)">{{ task.status === 'cancel_failed' ? '重试取消' : '取消' }}</button>
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

    <FileCleanupDialog v-if="cleanupOpen && session.connection.state === 'connected'"
      :key="session.currentUserId" @close="cleanupOpen = false" />
    <ConversationDialog
      v-if="dialogMode"
      :key="session.currentUserId + dialogMode"
      :mode="dialogMode"
      :disabled="controlDisabled"
      :create="onCreateConversation"
      :join="onJoinConversation"
      :direct="onCreateDirectConversation"
      @close="dialogMode = null"
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
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
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
  sendReceipt,
  loadHistory,
} from '../api/bridge';
import ConversationDialog from '../components/conversation-dialog.vue';
import FileCleanupDialog from '../components/file-cleanup-dialog.vue';
import ConversationList from '../components/conversation-list.vue';
import DebugDrawer from '../components/debug-drawer.vue';
import MessageComposer from '../components/message-composer.vue';
import MessageList from '../components/message-list.vue';
import { bridgeEvents } from '../events/bridge-event-store';
import { useSessionStore } from '../store/session';
import type {
  ConnectionState,
  ControlWriteItem,
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
const cleanupOpen = ref(false);
watch(() => [session.currentUserId, session.connection.state], () => { cleanupOpen.value = false; });
const dialogMode = ref<'create' | 'join' | 'direct' | null>(null);
const notice = ref('');
const historyLoading = ref<Record<string, boolean>>({});
const memberDraft = ref('');
const renameTitle = ref('');
const downloadPreset = ref<{ fileId: string } | null>(null);
const pendingMessages: MessageItem[] = [];
let messageFlushFrame = 0;
let accountEpoch = 0;
const controlSubmitting = ref(false);
const receiptInFlight = new Map<string, number>();
const receiptSaved = new Map<string, number>();
const subscriptions: (() => void)[] = [];
const controlDisabled = computed(() => controlSubmitting.value || session.connection.state !== 'connected');
const hasPendingRecall = computed(() => session.controlWrites.some(item => item.operation === 'recall'
  && item.status === 'pending' && item.conversationId === session.activeConversationId));
function listen(name: string, handler: (payload: unknown) => void): void {
  subscriptions.push(bridgeEvents.on(name, handler));
}

const currentFileProgress = computed(() => session.currentFileProgress);
const activeMembers = computed(() => session.currentConversation?.memberIds ?? []);
const isOwner = computed(() => session.currentConversation?.ownerId === session.currentUserId);
const isActiveMember = computed(() => activeMembers.value.includes(session.currentUserId));
const isGroupConversation = computed(() => session.currentConversation?.type === 'group');
const canSendToActiveConversation = computed(
  () => session.connection.state === 'connected' && Boolean(session.activeConversationId) && isActiveMember.value
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

async function syncReceiptForCurrentConversation(explicit = false): Promise<void> {
  const conversationId = session.activeConversationId;
  if (!canSendToActiveConversation.value || receiptInFlight.has(conversationId)) return;
  const received = session.currentMessages.filter(item => item.senderId !== session.currentUserId);
  const seq = received[received.length - 1]?.seq ?? 0;
  const confirmed = session.readProgressByConversation[conversationId]?.[session.currentUserId] ?? 0;
  if (seq <= confirmed || (!explicit && seq <= (receiptSaved.get(conversationId) ?? 0))) return;
  if (session.controlWrites.some(item => item.operation === 'receipt' && item.status === 'pending'
    && item.conversationId === conversationId)) return;
  if (!explicit && session.controlWrites.some(item => item.operation === 'receipt' && item.status === 'failed'
    && item.conversationId === conversationId)) return;
  const epoch = accountEpoch;
  receiptInFlight.set(conversationId, seq);
  let accepted = false;
  try {
    accepted = await sendReceipt(conversationId, seq);
    if (epoch !== accountEpoch) return;
    if (accepted) receiptSaved.set(conversationId, seq);
    else showNotice('已读操作未保存，可点击“标为已读”重试');
  } catch {
    if (epoch === accountEpoch) showNotice('已读操作提交失败');
  } finally {
    if (epoch === accountEpoch) receiptInFlight.delete(conversationId);
  }
  if (accepted && epoch === accountEpoch) void syncReceiptForCurrentConversation();
}

function operationLabel(operation: ControlWriteItem['operation']): string {
  return { create_conversation: '建立会话', add_members: '邀请成员', remove_members: '移除成员',
    leave_conversation: '退出群聊', join_conversation: '加入群聊', rename_conversation: '修改群名',
    receipt: '标记已读', recall: '撤回消息' }[operation] ?? '会话操作';
}

function controlTarget(item: ControlWriteItem): string {
  if (!item.conversationId) return '新会话';
  const conversation = session.conversations.find(entry => entry.conversationId === item.conversationId);
  return conversation?.title || item.conversationId;
}

async function performControl(action: () => Promise<boolean>): Promise<boolean> {
  if (controlDisabled.value) return false;
  const epoch = accountEpoch;
  controlSubmitting.value = true;
  try {
    const accepted = await action();
    if (epoch !== accountEpoch) return false;
    if (!accepted) showNotice('操作未保存，请检查连接和输入');
    return accepted;
  } catch {
    if (epoch === accountEpoch) showNotice('提交失败，请重试');
    return false;
  } finally {
    if (epoch === accountEpoch) controlSubmitting.value = false;
  }
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

async function onLoadHistory(): Promise<void> {
  const conversation = session.activeConversationId;
  const history = session.historyByConversation[conversation];
  if (!history?.hasMore || historyLoading.value[conversation] || session.connection.state !== 'connected') return;
  const epoch = accountEpoch;
  const connection = session.connection.sessionId;
  historyLoading.value[conversation] = true;
  try {
    const page = await loadHistory(conversation, history.cursor);
    if (epoch !== accountEpoch || connection !== session.connection.sessionId || session.connection.state !== 'connected') return;
    if (!page.ok) { showNotice(page.error ?? '历史消息加载失败'); return; }
    if (page.conversationId !== conversation) { showNotice('历史消息会话不匹配'); return; }
    flushPendingMessages();
    session.applyHistory(page);
  } catch {
    if (epoch === accountEpoch) showNotice('历史消息加载失败，请重试');
  } finally {
    if (epoch === accountEpoch) delete historyLoading.value[conversation];
  }
}

listen('connectionChanged', (payload) => {
  const connection = payload as Partial<ConnectionState>;
  session.setConnection({
    state: (connection.state ?? 'error') as ConnectionState['state'],
    sessionId: connection.sessionId ?? ''
  });
});

listen('initialStateLoaded', (payload) => {
  accountEpoch++;
  historyLoading.value = {};
  controlSubmitting.value = false;
  receiptSaved.clear();
  receiptInFlight.clear();
  if ((payload as InitialStatePayload).currentUser.userId !== session.currentUserId) {
    dialogMode.value = null;
    memberDraft.value = '';
    notice.value = '';
  }
  if (messageFlushFrame > 0) {
    window.cancelAnimationFrame(messageFlushFrame);
    messageFlushFrame = 0;
  }
  pendingMessages.splice(0);
  session.applyInitialState(payload as InitialStatePayload);
  void syncReceiptForCurrentConversation();
});

listen('controlWritesChanged', (payload) => {
  session.applyControlWrites((payload as { items: ControlWriteItem[] }).items);
  void syncReceiptForCurrentConversation();
});

listen('fileTasksChanged', (payload) => {
  session.applyFileTasks((payload as { items: FileTaskItem[] }).items);
});

listen('messageSendsChanged', (payload) => {
  session.applyMessageSends((payload as { items: MessageSendItem[] }).items);
});

listen('syncProgress', (payload) => {
  session.applySyncProgress(payload as { globalCursor: number; unreadTotal?: number });
});

listen('conversationUpdated', (payload) => {
  flushPendingMessages();
  session.applyConversationUpdated(payload as ConversationItem);
});

listen('messagePushed', (payload) => {
  queueMessage(payload as MessageItem);
});

listen('messageUpdated', (payload) => {
  flushPendingMessages();
  session.applyMessageUpdated(payload as MessageUpdate);
  if ((payload as MessageUpdate).type === 'receipt') void syncReceiptForCurrentConversation();
});

listen('fileProgress', (payload) => {
  flushPendingMessages();
  session.applyFileProgress(payload as FileProgressItem);
});

listen('errorRaised', (payload) => {
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
    memberDraft.value = '';
    renameTitle.value = session.currentConversation?.title ?? '';
    syncReceiptForCurrentConversation();
  }
);

watch(
  () => session.currentConversation?.title,
  (title, previous) => {
    if (renameTitle.value === (previous ?? '')) renameTitle.value = title ?? '';
  }
);

onUnmounted(() => {
  accountEpoch++;
  for (const unsubscribe of subscriptions) unsubscribe();
  if (messageFlushFrame) window.cancelAnimationFrame(messageFlushFrame);
  pendingMessages.splice(0);
});

onMounted(async () => {
  try {
    await initBridge();
  } catch {
    showNotice('Qt 接口初始化失败，请重新打开客户端');
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

function onCreateConversation(title: string, memberIds: string[]): Promise<boolean> {
  return performControl(() => createConversation(title, memberIds));
}

function onCreateDirectConversation(peerUserId: string): Promise<boolean> {
  return performControl(() => createDirectConversation(peerUserId));
}

function parseMemberDraft(): string[] {
  return memberDraft.value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

async function onAddMembers(): Promise<void> {
  const members = parseMemberDraft();
  if (!session.activeConversationId || members.length === 0) {
    return;
  }
  const conversationId = session.activeConversationId;
  const draft = memberDraft.value;
  if (await performControl(() => addMembers(conversationId, members))
    && session.activeConversationId === conversationId && memberDraft.value === draft) {
    memberDraft.value = '';
  }
}

function onRemoveMember(memberId: string): void {
  if (!session.activeConversationId || !memberId) {
    return;
  }
  void performControl(() => removeMembers(session.activeConversationId, [memberId]));
}

function onLeaveConversation(): void {
  if (!session.activeConversationId) {
    return;
  }
  void performControl(() => leaveConversation(session.activeConversationId));
}

function onRenameConversation(): void {
  const title = renameTitle.value.trim();
  if (!session.activeConversationId || !title) {
    return;
  }
  void performControl(() => renameConversation(session.activeConversationId, title));
}

function onJoinConversation(conversationId: string): Promise<boolean> {
  return performControl(() => joinConversation(conversationId));
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
  if (!hasPendingRecall.value) void performControl(() => recallMessage(conversationId, messageId));
}

function onFillDownload(fileId: string): void {
  downloadPreset.value = { fileId };
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
.control-write-list { display: grid; gap: 8px; max-height: 240px; overflow-y: auto; }
.control-write-card { display: grid; gap: 4px; padding: 8px; border: 1px solid #d9e1ea; border-radius: 8px;
  background: white; font-size: 12px; overflow-wrap: anywhere; }
.control-write-error { color: #b42318; }
.file-task-card { padding: 8px 0; overflow-wrap: anywhere; }
.file-task-card button { margin-top: 6px; margin-right: 6px; }
.chat-pane.has-pending-messages { grid-template-rows: auto minmax(0, 1fr) auto auto; }
.pending-message-list { padding: 8px 20px; max-height: 160px; overflow-y: auto; }
.pending-message { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 13px; }
.pending-message-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
