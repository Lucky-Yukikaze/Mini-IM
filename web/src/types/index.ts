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
  deliveries?: DeliveryUpdate[];
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
  deliveries?: DeliveryUpdate[];
  files?: FileProgressItem[];
  messageSends?: MessageSendItem[];
  fileTasks?: FileTaskItem[];
  controlWrites?: ControlWriteItem[];
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

export interface DeliveryUpdate {
  type: 'delivery';
  eventId: string;
  globalSeq: number;
  conversationId: string;
  messageId: string;
  userId: string;
  status: 'sent' | 'delivered' | 'read' | 'failed';
  sentAtMs: number;
  deliveredAtMs: number;
  readAtMs: number;
  failedAtMs: number;
  failureReason: string;
}

export type MessageUpdate = ReceiptUpdate | RecallUpdate | BurnUpdate | DeliveryUpdate;

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

export interface FileTaskItem {
  clientFileId: string;
  conversationId: string;
  fileId: string;
  fileName: string;
  path: string;
  direction: number;
  status: 'pending' | 'transferring' | 'finishing' | 'failed' | 'cancelling' | 'cancel_failed';
  error: string;
}

export interface ControlWriteItem {
  requestId: string;
  operation: 'create_conversation' | 'add_members' | 'remove_members' | 'leave_conversation'
    | 'join_conversation' | 'rename_conversation' | 'receipt' | 'recall';
  conversationId: string;
  clientConvId: string;
  status: 'pending' | 'failed';
  code: number;
  error: string;
  entityId: string;
  createdAtMs: number;
  attempts: number;
}
