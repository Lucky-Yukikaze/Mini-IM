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
  createConversation(clientConvId: string, title: string, memberIds: string[]): boolean;
  createDirectConversation?(clientConvId: string, peerUserId: string): boolean;
  addMembers?(conversationId: string, memberIds: string[]): boolean;
  removeMembers?(conversationId: string, memberIds: string[]): boolean;
  leaveConversation?(conversationId: string): boolean;
  joinConversation?(conversationId: string): boolean;
  renameConversation?(conversationId: string, title: string): boolean;
  sendReceipt(conversationId: string, lastReadSeq: number): boolean;
  recallMessage(conversationId: string, messageId: string): boolean;
  sendFile(conversationId: string, filePath: string, priority: number): boolean;
  downloadFile(conversationId: string, sourceFileId: string, savePath: string, priority: number): boolean;
  connectionChanged?: QtSignal;
  initialStateLoaded?: QtSignal;
  messagePushed?: QtSignal;
  messageUpdated?: QtSignal;
  conversationUpdated?: QtSignal;
  fileProgress?: QtSignal;
  syncProgress?: QtSignal;
  messageSendsChanged?: QtSignal;
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

export function createConversation(title: string, memberIds: string[]): boolean {
  if (runtimeWindow.imBridge) {
    const clientConvId = `cc-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
    return runtimeWindow.imBridge.createConversation(clientConvId, title, memberIds);
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

export function createDirectConversation(peerUserId: string): boolean {
  if (runtimeWindow.imBridge?.createDirectConversation) {
    const clientConvId = `dc-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
    return runtimeWindow.imBridge.createDirectConversation(clientConvId, peerUserId);
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

export function addMembers(conversationId: string, memberIds: string[]): boolean {
  if (runtimeWindow.imBridge?.addMembers) {
    return runtimeWindow.imBridge.addMembers(conversationId, memberIds);
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

export function removeMembers(conversationId: string, memberIds: string[]): boolean {
  if (runtimeWindow.imBridge?.removeMembers) {
    return runtimeWindow.imBridge.removeMembers(conversationId, memberIds);
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

export function leaveConversation(conversationId: string): boolean {
  if (runtimeWindow.imBridge?.leaveConversation) {
    return runtimeWindow.imBridge.leaveConversation(conversationId);
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

export function joinConversation(conversationId: string): boolean {
  if (runtimeWindow.imBridge?.joinConversation) {
    return runtimeWindow.imBridge.joinConversation(conversationId);
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

export function renameConversation(conversationId: string, title: string): boolean {
  if (runtimeWindow.imBridge?.renameConversation) {
    return runtimeWindow.imBridge.renameConversation(conversationId, title);
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

export function sendReceipt(conversationId: string, lastReadSeq: number): boolean {
  if (runtimeWindow.imBridge) {
    return runtimeWindow.imBridge.sendReceipt(conversationId, lastReadSeq);
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

export function recallMessage(conversationId: string, messageId: string): boolean {
  if (runtimeWindow.imBridge) {
    return runtimeWindow.imBridge.recallMessage(conversationId, messageId);
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

export function sendFile(conversationId: string, filePath: string, priority: number): boolean {
  if (runtimeWindow.imBridge) {
    return runtimeWindow.imBridge.sendFile(conversationId, filePath, priority);
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

export function downloadFile(
  conversationId: string,
  sourceFileId: string,
  savePath: string,
  priority: number
): boolean {
  if (runtimeWindow.imBridge) {
    return runtimeWindow.imBridge.downloadFile(conversationId, sourceFileId, savePath, priority);
  }
  bridgeEvents.emit('errorRaised', { message: 'download is unavailable in web-debug mode' });
  return false;
}
