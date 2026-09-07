import { defineStore } from 'pinia';
import type {
  ConnectionState,
  ConversationItem,
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
  activeConversationId: string;
  activeConversationLabel: string;
  messagesByConversation: Record<string, MessageItem[]>;
  fileProgressByConversation: Record<string, FileProgressItem[]>;
  readProgressByConversation: Record<string, Record<string, number>>;
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
    burnMode: item.burnMode ?? 0,
    burnTtlSec: item.burnTtlSec ?? 0
  };
}

function compareMessages(left: MessageItem, right: MessageItem): number {
  return left.seq - right.seq || left.createdAtMs - right.createdAtMs;
}

function upsertSortedMessage(list: MessageItem[], item: MessageItem): void {
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
}

function applyReadProgressToMessage(
  item: MessageItem,
  conversationProgress: Record<string, number> | undefined
): MessageItem {
  if (!conversationProgress) {
    return item;
  }
  let unreadCount = item.unreadCount;
  for (const [readerId, lastReadSeq] of Object.entries(conversationProgress)) {
    if (readerId !== item.senderId && item.seq > 0 && item.seq <= lastReadSeq) {
      unreadCount = Math.max(0, unreadCount - 1);
    }
  }
  return { ...item, unreadCount };
}

function isReadByUser(item: MessageItem, readerId: string, conversationProgress: Record<string, number> | undefined): boolean {
  return Boolean(readerId) && item.seq > 0 && item.seq <= (conversationProgress?.[readerId] ?? 0);
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
    activeConversationId: '',
    activeConversationLabel: '-',
    messagesByConversation: {},
    fileProgressByConversation: {},
    readProgressByConversation: {},
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
      return state.messagesByConversation[state.activeConversationId] ?? [];
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
      if (payload.fileTasks !== undefined) {
        this.applyFileTasks(payload.fileTasks);
      }
      if (payload.messageSends !== undefined) {
        this.applyMessageSends(payload.messageSends);
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
        const normalizedItem = applyReadProgressToMessage(
          item,
          this.readProgressByConversation[item.conversationId]
        );
        const list = this.messagesByConversation[item.conversationId] ?? [];
        const exists = list.some((existing) => existing.id === normalizedItem.id);
        if (
          !exists &&
          normalizedItem.senderId !== this.currentUserId &&
          !isReadByUser(normalizedItem, this.currentUserId, this.readProgressByConversation[item.conversationId])
        ) {
          this.unreadTotal += 1;
        }
        upsertSortedMessage(list, normalizedItem);
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
      if (update.type === 'recall' || update.type === 'burn') {
        const list = this.messagesByConversation[update.conversationId] ?? [];
        const item = list.find((message) => message.id === update.messageId);
        if (item) {
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
        if (item.seq > oldLastReadSeq && item.seq <= update.lastReadSeq && item.senderId !== update.readerId) {
          item.unreadCount = Math.max(0, item.unreadCount - 1);
        }
        if (
          update.readerId === this.currentUserId &&
          item.senderId !== this.currentUserId &&
          item.seq > oldLastReadSeq &&
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
