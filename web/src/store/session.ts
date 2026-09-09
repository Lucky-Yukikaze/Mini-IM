import { defineStore } from 'pinia';
import type {
  ConnectionState,
  ControlWriteItem,
  ConversationItem,
  DeliveryUpdate,
  ReadCountUpdate,
  InitialStatePayload,
  FileProgressItem,
  FileTaskItem,
  MessageItem,
  MessageSendItem,
  MessageUpdate
} from '../types';

interface SessionStoreState {
  connection: ConnectionState;
  currentUserId: string;
  globalCursor: number;
  unreadTotal: number;
  conversations: ConversationItem[];
  messageSends: MessageSendItem[];
  fileTasks: FileTaskItem[];
  controlWrites: ControlWriteItem[];
  activeConversationId: string;
  activeConversationLabel: string;
  messagesByConversation: Record<string, MessageItem[]>;
  fileProgressByConversation: Record<string, FileProgressItem[]>;
  readProgressByConversation: Record<string, Record<string, number>>;
  readCountsByConversation: Record<string, Record<string, ReadCountUpdate>>;
  deliveriesByConversation: Record<string, Record<string, Record<string, DeliveryUpdate>>>;
  pendingMessageStateByConversation: Record<string, Record<string, { recalled: boolean; burned: boolean }>>;
}

function getConversationLabel(
  conversations: ConversationItem[],
  conversationId: string,
  currentUserId: string
): string {
  if (!conversationId) {
    return '-';
  }
  const conversation = conversations.find((item) => item.conversationId === conversationId);
  if (!conversation) {
    return '-';
  }
  if (conversation.title) {
    return conversation.title;
  }
  if (conversation.type === 'direct') {
    return conversation.memberIds.find((item) => item !== currentUserId) ?? conversation.conversationId;
  }
  return conversation.conversationId;
}

function ensureMessageDefaults(item: Partial<MessageItem>): MessageItem {
  return {
    id: item.id ?? '',
    conversationId: item.conversationId ?? '',
    senderId: item.senderId ?? '',
    clientMsgId: item.clientMsgId ?? '',
    seq: item.seq ?? 0,
    text: item.text ?? '',
    createdAtMs: item.createdAtMs ?? Date.now(),
    recalled: item.recalled ?? false,
    burned: item.burned ?? false,
    unreadCount: item.unreadCount ?? 0,
    readCountKnown: item.readCountKnown ?? true,
    burnMode: item.burnMode ?? 0,
    burnTtlSec: item.burnTtlSec ?? 0
  };
}

function compareMessages(left: MessageItem, right: MessageItem): number {
  return left.seq - right.seq || left.createdAtMs - right.createdAtMs;
}

function upsertSortedMessage(list: MessageItem[], item: MessageItem): MessageItem {
  const existingIndex = list.findIndex((existing) => existing.id === item.id);
  const existing = list[existingIndex];
  const nextItem = existing ? {
    ...existing,
    ...item,
    recalled: existing.recalled || item.recalled,
    burned: existing.burned || item.burned
  } : item;
  if (nextItem.recalled || nextItem.burned) {
    nextItem.text = '';
  }
  if (existingIndex >= 0) {
    list.splice(existingIndex, 1);
  }

  let low = 0;
  let high = list.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (compareMessages(list[mid], nextItem) <= 0) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  list.splice(low, 0, nextItem);
  return nextItem;
}

function countsAsUnread(
  item: MessageItem | undefined,
  userId: string,
  conversationProgress: Record<string, number> | undefined
): boolean {
  return Boolean(item && item.senderId !== userId && !item.recalled && !item.burned &&
    item.seq > (conversationProgress?.[userId] ?? 0));
}

export const useSessionStore = defineStore('session', {
  state: (): SessionStoreState => ({
    connection: {
      state: 'idle',
      sessionId: ''
    },
    currentUserId: '',
    globalCursor: 0,
    unreadTotal: 0,
    conversations: [],
    messageSends: [],
    fileTasks: [],
    controlWrites: [],
    activeConversationId: '',
    activeConversationLabel: '-',
    messagesByConversation: {},
    fileProgressByConversation: {},
    readProgressByConversation: {},
    readCountsByConversation: {},
    deliveriesByConversation: {},
    pendingMessageStateByConversation: {}
  }),
  getters: {
    currentFileTasks(state): FileTaskItem[] {
      return state.fileTasks.filter((item) => item.conversationId === state.activeConversationId);
    },
    currentMessageSends(state): MessageSendItem[] {
      return state.messageSends.filter((item) => item.conversationId === state.activeConversationId);
    },
    currentMessages(state): MessageItem[] {
      return (state.messagesByConversation[state.activeConversationId] ?? []).map(item => ({
        ...item,
        deliveries: Object.values(state.deliveriesByConversation[item.conversationId]?.[item.id] ?? {}).map(delivery => {
          const readSeq = state.readProgressByConversation[item.conversationId]?.[delivery.userId] ?? 0;
          return item.seq > 0 && item.seq <= readSeq ? { ...delivery, status: 'read' as const } : delivery;
        })
      }));
    },
    currentConversation(state): ConversationItem | undefined {
      return state.conversations.find((item) => item.conversationId === state.activeConversationId);
    },
    currentFileProgress(state): FileProgressItem[] {
      return state.fileProgressByConversation[state.activeConversationId] ?? [];
    }
  },
  actions: {
    setConnection(connection: ConnectionState): void {
      this.connection = connection;
    },
    setCurrentConversation(conversationId: string): void {
      this.activeConversationId = conversationId;
      this.activeConversationLabel = getConversationLabel(this.conversations, conversationId, this.currentUserId);
    },
    applyControlWrites(items: ControlWriteItem[]): void {
      this.controlWrites = items;
    },
    applyFileTasks(items: FileTaskItem[]): void {
      this.fileTasks = items;
    },
    applyMessageSends(items: MessageSendItem[]): void {
      this.messageSends = items;
    },
    applyInitialState(payload: InitialStatePayload): void {
      const userId = payload.currentUser?.userId ?? '';
      if (userId !== this.currentUserId) {
        const connection = this.connection;
        this.$reset();
        this.connection = connection;
      }
      this.currentUserId = userId;
      if (payload.globalCursor !== undefined) {
        this.globalCursor = payload.globalCursor;
      }
      if (payload.unreadTotal !== undefined) {
        this.unreadTotal = payload.unreadTotal;
      }
      if (payload.conversations !== undefined) {
        this.conversations = payload.conversations;
      }
      if (payload.readProgressByConversation !== undefined) {
        this.readProgressByConversation = payload.readProgressByConversation;
      }
      if (payload.controlWrites !== undefined) {
        this.applyControlWrites(payload.controlWrites);
      }
      if (payload.fileTasks !== undefined) {
        this.applyFileTasks(payload.fileTasks);
      }
      if (payload.messageSends !== undefined) {
        this.applyMessageSends(payload.messageSends);
      }
      if (payload.readCounts !== undefined) {
        this.readCountsByConversation = {};
        for (const count of payload.readCounts) this.applyMessageUpdated(count);
      }
      if (payload.deliveries !== undefined) {
        this.deliveriesByConversation = {};
        for (const delivery of payload.deliveries) this.applyMessageUpdated(delivery);
      }
      if (payload.files !== undefined) {
        this.fileProgressByConversation = {};
        for (const file of payload.files) this.applyFileProgress(file);
      }
      if (payload.recentMessages !== undefined) {
        this.messagesByConversation = {};
        this.pendingMessageStateByConversation = {};
        const grouped: Record<string, MessageItem[]> = {};
        for (const rawItem of payload.recentMessages) {
          const item = ensureMessageDefaults(rawItem);
          const count = this.readCountsByConversation[item.conversationId]?.[item.id];
          if (count) { item.unreadCount = count.unreadCount; item.readCountKnown = true; }
          const list = grouped[item.conversationId] ?? [];
          list.push(item);
          grouped[item.conversationId] = list;
        }
        for (const [conversationId, list] of Object.entries(grouped)) {
          list.sort(compareMessages);
          this.messagesByConversation[conversationId] = list;
        }
      }
      if (!this.activeConversationId && this.conversations.length > 0) {
        this.activeConversationId = this.conversations[0].conversationId;
      }
      this.activeConversationLabel = getConversationLabel(
        this.conversations,
        this.activeConversationId,
        this.currentUserId
      );
    },
    applyConversationUpdated(item: ConversationItem): void {
      const index = this.conversations.findIndex(
        (conversation) => conversation.conversationId === item.conversationId
      );
      if (index >= 0) {
        this.conversations[index] = item;
      } else {
        this.conversations.unshift(item);
      }
      this.conversations = [...this.conversations].sort((left, right) => right.updatedAtMs - left.updatedAtMs);
      if (!this.activeConversationId) {
        this.activeConversationId = item.conversationId;
      }
      this.activeConversationLabel = getConversationLabel(
        this.conversations,
        this.activeConversationId,
        this.currentUserId
      );
    },
    pushMessage(rawItem: Partial<MessageItem>): void {
      this.pushMessages([rawItem]);
    },
    pushMessages(rawItems: Partial<MessageItem>[]): void {
      const touchedConversationIds = new Set<string>();
      for (const rawItem of rawItems) {
        const item = ensureMessageDefaults(rawItem);
        const pending = this.pendingMessageStateByConversation[item.conversationId]?.[item.id];
        if (pending) {
          item.recalled = item.recalled || pending.recalled;
          item.burned = item.burned || pending.burned;
          delete this.pendingMessageStateByConversation[item.conversationId][item.id];
        }
        const count = this.readCountsByConversation[item.conversationId]?.[item.id];
        const normalizedItem = count ? { ...item, unreadCount: count.unreadCount, readCountKnown: true } : item;
        const list = this.messagesByConversation[item.conversationId] ?? [];
        const progress = this.readProgressByConversation[item.conversationId];
        const existing = list.find((entry) => entry.id === normalizedItem.id);
        const wasUnread = countsAsUnread(existing, this.currentUserId, progress);
        const merged = upsertSortedMessage(list, normalizedItem);
        const isUnread = countsAsUnread(merged, this.currentUserId, progress);
        this.unreadTotal = Math.max(0, this.unreadTotal + Number(isUnread) - Number(wasUnread));
        this.messagesByConversation[item.conversationId] = [...list];

        const conversation = this.conversations.find(
          (entry) => entry.conversationId === normalizedItem.conversationId
        );
        if (conversation) {
          conversation.updatedAtMs = Math.max(conversation.updatedAtMs, normalizedItem.createdAtMs);
          touchedConversationIds.add(normalizedItem.conversationId);
        }
      }
      if (touchedConversationIds.size > 0) {
        this.conversations = [...this.conversations].sort((left, right) => right.updatedAtMs - left.updatedAtMs);
      }
    },
    applyMessageUpdated(update: MessageUpdate): void {
      if (update.type === 'readCount') {
        const counts = this.readCountsByConversation[update.conversationId] ?? {};
        const previous = counts[update.messageId];
        if (!previous || update.globalSeq > previous.globalSeq) {
          counts[update.messageId] = update;
          this.readCountsByConversation[update.conversationId] = counts;
          const item = this.messagesByConversation[update.conversationId]?.find(message => message.id === update.messageId);
          if (item) { item.unreadCount = update.unreadCount; item.readCountKnown = true; }
        }
        return;
      }
      if (update.type === 'delivery') {
        const conversation = this.deliveriesByConversation[update.conversationId] ?? {};
        const deliveries = conversation[update.messageId] ?? {};
        const previous = deliveries[update.userId];
        if (!previous || update.globalSeq > previous.globalSeq) {
          deliveries[update.userId] = update;
          conversation[update.messageId] = deliveries;
          this.deliveriesByConversation[update.conversationId] = conversation;
        }
        return;
      }
      if (update.type === 'recall' || update.type === 'burn') {
        const list = this.messagesByConversation[update.conversationId] ?? [];
        const item = list.find((message) => message.id === update.messageId);
        if (item) {
          if (countsAsUnread(item, this.currentUserId, this.readProgressByConversation[update.conversationId])) {
            this.unreadTotal = Math.max(0, this.unreadTotal - 1);
          }
          item.recalled = true;
          item.text = '';
          if (update.type === 'burn' || update.operatorId === 'system-burn') {
            item.burned = true;
          }
          this.messagesByConversation[update.conversationId] = [...list];
        } else {
          const conversationPending = this.pendingMessageStateByConversation[update.conversationId] ?? {};
          const oldState = conversationPending[update.messageId] ?? { recalled: false, burned: false };
          conversationPending[update.messageId] = {
            recalled: true,
            burned: oldState.burned || update.type === 'burn' || update.operatorId === 'system-burn'
          };
          this.pendingMessageStateByConversation[update.conversationId] = conversationPending;
        }
        return;
      }

      const conversationProgress = this.readProgressByConversation[update.conversationId] ?? {};
      const oldLastReadSeq = conversationProgress[update.readerId] ?? 0;
      if (update.lastReadSeq <= oldLastReadSeq) {
        this.readProgressByConversation[update.conversationId] = conversationProgress;
        return;
      }

      const list = this.messagesByConversation[update.conversationId] ?? [];
      let readByCurrentUser = 0;
      for (const item of list) {
        if (
          update.readerId === this.currentUserId &&
          countsAsUnread(item, this.currentUserId, conversationProgress) &&
          item.seq <= update.lastReadSeq
        ) {
          readByCurrentUser += 1;
        }
      }
      conversationProgress[update.readerId] = update.lastReadSeq;
      this.readProgressByConversation[update.conversationId] = conversationProgress;
      this.messagesByConversation[update.conversationId] = [...list];
      this.unreadTotal = Math.max(0, this.unreadTotal - readByCurrentUser);
    },
    applyFileProgress(rawItem: FileProgressItem): void {
      const item = {
        ...rawItem,
        version: rawItem.version ?? 1,
        updatedAtMs: rawItem.updatedAtMs ?? Date.now()
      };
      const list = this.fileProgressByConversation[item.conversationId] ?? [];
      const index = list.findIndex((entry) => entry.fileId === item.fileId);
      if (index >= 0) {
        if ((item.version ?? 0) >= (list[index].version ?? 0)) {
          list[index] = item;
        }
      } else {
        list.unshift(item);
      }
      this.fileProgressByConversation[item.conversationId] = [...list];
    }
  }
});
