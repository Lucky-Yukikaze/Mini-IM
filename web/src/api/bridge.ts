import type { ConversationItem, MessageItem } from '../types';
import { bridgeEvents } from '../events/bridge-event-store';

type SignalHandler = (payload: unknown) => void;

interface QtSignal {
  connect(handler: SignalHandler): void;
}

interface QtImBridge {
  connectToServer(endpoint: string, token: string, deviceId: string, done: (accepted: boolean) => void): void;
  disconnectFromServer(): void;
  sendMessage(
    conversationId: string,
    clientMsgId: string,
    text: string,
    burnMode: number,
    burnTtlSec: number,
    done: (accepted: boolean) => void
  ): void;
  retryMessage(conversationId: string, clientMsgId: string, done: (accepted: boolean) => void): void;
  createConversation(clientConvId: string, title: string, memberIds: string[], done: (accepted: boolean) => void): void;
  createDirectConversation?(clientConvId: string, peerUserId: string, done: (accepted: boolean) => void): void;
  addMembers?(conversationId: string, memberIds: string[], done: (accepted: boolean) => void): void;
  removeMembers?(conversationId: string, memberIds: string[], done: (accepted: boolean) => void): void;
  leaveConversation?(conversationId: string, done: (accepted: boolean) => void): void;
  joinConversation?(conversationId: string, done: (accepted: boolean) => void): void;
  renameConversation?(conversationId: string, title: string, done: (accepted: boolean) => void): void;
  sendReceipt(conversationId: string, lastReadSeq: number, done: (accepted: boolean) => void): void;
  recallMessage(conversationId: string, messageId: string, done: (accepted: boolean) => void): void;
  sendFile(conversationId: string, filePath: string, priority: number, done: (accepted: boolean) => void): void;
  downloadFile(conversationId: string, sourceFileId: string, savePath: string, priority: number, done: (accepted: boolean) => void): void;
  retryFile(clientFileId: string, done: (accepted: boolean) => void): void;
  cancelFile(clientFileId: string, done: (accepted: boolean) => void): void;
  connectionChanged?: QtSignal;
  initialStateLoaded?: QtSignal;
  messagePushed?: QtSignal;
  messageUpdated?: QtSignal;
  conversationUpdated?: QtSignal;
  fileProgress?: QtSignal;
  syncProgress?: QtSignal;
  messageSendsChanged?: QtSignal;
  fileTasksChanged?: QtSignal;
  controlWritesChanged?: QtSignal;
  errorRaised?: QtSignal;
}

interface QtBridgeContainer {
  webChannelTransport?: unknown;
}

interface QtWindow {
  qt?: QtBridgeContainer;
  QWebChannel?: new (
    transport: unknown,
    callback: (channel: { objects: { imBridge?: QtImBridge } }) => void
  ) => unknown;
  imBridge?: QtImBridge;
}

const runtimeWindow = window as Window & QtWindow;
let m_bound = false;
let m_debugUserId = 'u-demo';

function resolveDevUserId(token: string): string {
  const prefix = 'dev-token:';
  if (!token.startsWith(prefix)) {
    return 'u-demo';
  }
  const userId = token.slice(prefix.length).trim();
  return /^[A-Za-z0-9_.-]{1,64}$/.test(userId) ? userId : 'u-demo';
}

function bindSignals(bridge: QtImBridge): void {
  if (m_bound) {
    return;
  }
  bridge.connectionChanged?.connect((payload) => {
    bridgeEvents.emit('connectionChanged', payload);
  });
  bridge.initialStateLoaded?.connect((payload) => {
    bridgeEvents.emit('initialStateLoaded', payload);
  });
  bridge.messagePushed?.connect((payload) => {
    bridgeEvents.emit('messagePushed', payload);
  });
  bridge.messageUpdated?.connect((payload) => {
    bridgeEvents.emit('messageUpdated', payload);
  });
  bridge.conversationUpdated?.connect((payload) => {
    bridgeEvents.emit('conversationUpdated', payload);
  });
  bridge.fileProgress?.connect((payload) => {
    bridgeEvents.emit('fileProgress', payload);
  });
  bridge.controlWritesChanged?.connect((payload) => {
    bridgeEvents.emit('controlWritesChanged', payload);
  });
  bridge.fileTasksChanged?.connect((payload) => {
    bridgeEvents.emit('fileTasksChanged', payload);
  });
  bridge.messageSendsChanged?.connect((payload) => {
    bridgeEvents.emit('messageSendsChanged', payload);
  });
  bridge.syncProgress?.connect((payload) => {
    bridgeEvents.emit('syncProgress', payload);
  });
  bridge.errorRaised?.connect((payload) => {
    bridgeEvents.emit('errorRaised', payload);
  });
  m_bound = true;
}

function loadQtWebChannelScript(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (runtimeWindow.QWebChannel) {
      resolve();
      return;
    }
    const script = document.createElement('script');
    script.src = 'qrc:///qtwebchannel/qwebchannel.js';
    script.onload = () => resolve();
    script.onerror = () => reject(new Error('failed to load qwebchannel.js'));
    document.head.appendChild(script);
  });
}

export async function initBridge(): Promise<void> {
  if (runtimeWindow.imBridge) {
    bindSignals(runtimeWindow.imBridge);
    return;
  }

  const transport = runtimeWindow.qt?.webChannelTransport;
  if (!transport) {
    return;
  }

  await loadQtWebChannelScript();
  if (!runtimeWindow.QWebChannel) {
    throw new Error('QWebChannel unavailable');
  }

  await new Promise<void>((resolve) => {
    new runtimeWindow.QWebChannel!(transport, (channel) => {
      runtimeWindow.imBridge = channel.objects.imBridge;
      if (runtimeWindow.imBridge) {
        bindSignals(runtimeWindow.imBridge);
      }
      resolve();
    });
  });
}

export async function connect(endpoint: string, token: string, deviceId: string): Promise<boolean> {
  try {
    await initBridge();
  } catch {
    // Fallback to web-debug mode when Qt bridge is unavailable.
  }
  if (!runtimeWindow.imBridge) {
    m_debugUserId = resolveDevUserId(token);
    bridgeEvents.emit('connectionChanged', { state: 'connected', sessionId: 'web-debug' });
    bridgeEvents.emit('initialStateLoaded', {
      currentUser: { userId: m_debugUserId },
      conversations: [],
      recentMessages: [],
      unreadTotal: 0,
      globalCursor: 0
    });
    return true;
  }
  return new Promise<boolean>((resolve) => {
    runtimeWindow.imBridge!.connectToServer(endpoint, token, deviceId, resolve);
  });
}

export function disconnect(): void {
  if (runtimeWindow.imBridge) {
    runtimeWindow.imBridge.disconnectFromServer();
    return;
  }
  bridgeEvents.emit('connectionChanged', { state: 'disconnected', sessionId: '' });
}

export async function sendMessage(
  conversationId: string,
  text: string,
  burnMode = 0,
  burnTtlSec = 0
): Promise<boolean> {
  if (runtimeWindow.imBridge) {
    const clientMsgId = `cm-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
    return new Promise<boolean>((resolve) => {
      runtimeWindow.imBridge!.sendMessage(conversationId, clientMsgId, text, burnMode, burnTtlSec, resolve);
    });
  }

  const item: MessageItem = {
    id: `local-${Date.now()}`,
    conversationId,
    senderId: m_debugUserId,
    clientMsgId: '',
    seq: Date.now(),
    text,
    createdAtMs: Date.now(),
    recalled: false,
    burned: false,
    unreadCount: 0,
    burnMode,
    burnTtlSec
  };
  bridgeEvents.emit('messagePushed', item);
  return true;
}

export async function retryMessage(conversationId: string, clientMsgId: string): Promise<boolean> {
  if (!runtimeWindow.imBridge) return false;
  return new Promise<boolean>((resolve) => {
    runtimeWindow.imBridge!.retryMessage(conversationId, clientMsgId, resolve);
  });
}

export async function createConversation(title: string, memberIds: string[]): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    const clientConvId = crypto.randomUUID();
    if (typeof runtimeWindow.imBridge.createConversation !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.createConversation!(clientConvId, title, memberIds, resolve));
  }

  const item: ConversationItem = {
    conversationId: `local-conv-${Date.now()}`,
    title,
    type: 'group',
    ownerId: m_debugUserId,
    memberIds: [m_debugUserId, ...memberIds],
    updatedAtMs: Date.now()
  };
  bridgeEvents.emit('conversationUpdated', item);
  return true;
}

export async function createDirectConversation(peerUserId: string): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    const clientConvId = crypto.randomUUID();
    if (typeof runtimeWindow.imBridge.createDirectConversation !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.createDirectConversation!(clientConvId, peerUserId, resolve));
  }

  const item: ConversationItem = {
    conversationId: `local-direct-${Date.now()}`,
    title: peerUserId,
    type: 'direct',
    ownerId: m_debugUserId,
    memberIds: [m_debugUserId, peerUserId],
    updatedAtMs: Date.now()
  };
  bridgeEvents.emit('conversationUpdated', item);
  return true;
}

export async function addMembers(conversationId: string, memberIds: string[]): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.addMembers !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.addMembers!(conversationId, memberIds, resolve));
  }
  bridgeEvents.emit('conversationUpdated', {
    conversationId,
    title: conversationId,
    type: 'group',
    ownerId: m_debugUserId,
    memberIds: [m_debugUserId, ...memberIds],
    updatedAtMs: Date.now()
  });
  return true;
}

export async function removeMembers(conversationId: string, memberIds: string[]): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.removeMembers !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.removeMembers!(conversationId, memberIds, resolve));
  }
  bridgeEvents.emit('messageUpdated', {
    type: 'receipt',
    eventId: `noop-${Date.now()}`,
    conversationId,
    lastReadSeq: 0,
    readAtMs: Date.now(),
    readerId: memberIds[0] ?? 'u-demo'
  });
  return true;
}

export async function leaveConversation(conversationId: string): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.leaveConversation !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.leaveConversation!(conversationId, resolve));
  }
  bridgeEvents.emit('messageUpdated', {
    type: 'receipt',
    eventId: `leave-${Date.now()}`,
    conversationId,
    lastReadSeq: 0,
    readAtMs: Date.now(),
    readerId: m_debugUserId
  });
  return true;
}

export async function joinConversation(conversationId: string): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.joinConversation !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.joinConversation!(conversationId, resolve));
  }
  bridgeEvents.emit('conversationUpdated', {
    conversationId,
    title: conversationId,
    type: 'group',
    ownerId: '',
    memberIds: [m_debugUserId],
    updatedAtMs: Date.now()
  });
  return true;
}

export async function renameConversation(conversationId: string, title: string): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.renameConversation !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.renameConversation!(conversationId, title, resolve));
  }
  bridgeEvents.emit('conversationUpdated', {
    conversationId,
    title,
    type: 'group',
    ownerId: m_debugUserId,
    memberIds: [m_debugUserId],
    updatedAtMs: Date.now()
  });
  return true;
}

export async function sendReceipt(conversationId: string, lastReadSeq: number): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.sendReceipt !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.sendReceipt!(conversationId, lastReadSeq, resolve));
  }
  bridgeEvents.emit('messageUpdated', {
    type: 'receipt',
    eventId: `receipt-${Date.now()}`,
    conversationId,
    lastReadSeq,
    readAtMs: Date.now(),
    readerId: m_debugUserId
  });
  return true;
}

export async function recallMessage(conversationId: string, messageId: string): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.recallMessage !== 'function') return false;
    return new Promise<boolean>((resolve) => runtimeWindow.imBridge!.recallMessage!(conversationId, messageId, resolve));
  }
  bridgeEvents.emit('messageUpdated', {
    type: 'recall',
    eventId: `recall-${Date.now()}`,
    conversationId,
    messageId,
    tsMs: Date.now(),
    operatorId: 'u-demo'
  });
  return true;
}

export async function sendFile(conversationId: string, filePath: string, priority: number): Promise<boolean> {
  if (runtimeWindow.qt?.webChannelTransport && !runtimeWindow.imBridge) return false;
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.sendFile !== 'function') return false;
    return new Promise((resolve) => runtimeWindow.imBridge!.sendFile(conversationId, filePath, priority, resolve));
  }
  bridgeEvents.emit('fileProgress', {
    eventId: `file-${Date.now()}`,
    fileId: `local-file-${Date.now()}`,
    conversationId,
    transferredBytes: 0,
    completed: false,
    version: 1,
    updatedAtMs: Date.now()
  });
  return true;
}

export async function downloadFile(
  conversationId: string,
  sourceFileId: string,
  savePath: string,
  priority: number
): Promise<boolean> {
  if (runtimeWindow.imBridge) {
    if (typeof runtimeWindow.imBridge.downloadFile !== 'function') return false;
    return new Promise((resolve) => runtimeWindow.imBridge!.downloadFile(conversationId, sourceFileId, savePath, priority, resolve));
  }
  bridgeEvents.emit('errorRaised', { message: 'download is unavailable in web-debug mode' });
  return false;
}

export async function retryFile(clientFileId: string): Promise<boolean> {
  if (typeof runtimeWindow.imBridge?.retryFile !== 'function') return false;
  return new Promise((resolve) => runtimeWindow.imBridge!.retryFile(clientFileId, resolve));
}

export async function cancelFile(clientFileId: string): Promise<boolean> {
  if (typeof runtimeWindow.imBridge?.cancelFile !== 'function') return false;
  return new Promise((resolve) => runtimeWindow.imBridge!.cancelFile(clientFileId, resolve));
}
