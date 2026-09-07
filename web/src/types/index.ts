export interface MessageItem {
  id: string;
  conversationId: string;
  senderId: string;
  clientMsgId?: string;
  seq: number;
  text: string;
  createdAtMs: number;
  recalled: boolean;
  burned: boolean;
  unreadCount: number;
  burnMode: number;
  burnTtlSec: number;
}

export interface ConversationItem {
  conversationId: string;
  title: string;
  type: 'direct' | 'group';
  ownerId: string;
  memberIds: string[];
  updatedAtMs: number;
}

export interface ConnectionState {
  state: 'idle' | 'connecting' | 'reconnecting' | 'connected' | 'disconnected' | 'error';
  sessionId: string;
}

export interface InitialStatePayload {
  currentUser: {
    userId: string;
  };
  conversations?: ConversationItem[];
  recentMessages?: MessageItem[];
  unreadTotal?: number;
  globalCursor?: number;
  readProgressByConversation?: Record<string, Record<string, number>>;
  files?: FileProgressItem[];
  messageSends?: MessageSendItem[];
}

export interface ReceiptUpdate {
  type: 'receipt';
  eventId: string;
  conversationId: string;
  lastReadSeq: number;
  readAtMs: number;
  readerId: string;
}

export interface RecallUpdate {
  type: 'recall';
  eventId: string;
  conversationId: string;
  messageId: string;
  tsMs: number;
  operatorId: string;
}

export interface BurnUpdate {
  type: 'burn';
  eventId: string;
  conversationId: string;
  messageId: string;
  tsMs: number;
  operatorId: string;
}

export type MessageUpdate = ReceiptUpdate | RecallUpdate | BurnUpdate;

export interface FileProgressItem {
  eventId: string;
  fileId: string;
  conversationId: string;
  transferredBytes: number;
  completed: boolean;
  version: number;
  updatedAtMs: number;
}

export interface MessageSendItem {
  requestId: string;
  conversationId: string;
  clientMsgId: string;
  text: string;
  burnMode: number;
  burnTtlSec: number;
  status: 'pending' | 'failed';
  code: number;
  error: string;
  createdAtMs: number;
  attempts: number;
}
