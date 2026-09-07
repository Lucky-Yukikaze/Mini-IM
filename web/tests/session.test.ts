import assert from 'node:assert/strict';
import test from 'node:test';
import { createPinia } from 'pinia';
import { useSessionStore } from '../src/store/session';

function createSession() {
  const session = useSessionStore(createPinia());
  session.applyInitialState({ currentUser: { userId: 'alice' }, globalCursor: 7 });
  session.applyConversationUpdated({
    conversationId: 'private', title: 'test', type: 'direct',
    ownerId: 'alice', memberIds: ['alice', 'carol'], updatedAtMs: 1
  });
  session.pushMessage({
    id: 'm1', conversationId: 'private', senderId: 'carol',
    seq: 1, text: 'private content', createdAtMs: 1, unreadCount: 1
  });
  return session;
}

test('changing users clears every account-owned display state', () => {
  const session = createSession();
  session.applyFileProgress({
    eventId: 'f1', fileId: 'file', conversationId: 'private',
    transferredBytes: 1, completed: false, version: 1, updatedAtMs: 1
  });
  session.applyMessageUpdated({
    type: 'burn', eventId: 'late', conversationId: 'private',
    messageId: 'not-loaded', operatorId: 'system-burn', tsMs: 2
  });
  session.setConnection({ state: 'connected', sessionId: 'bob-session' });
  session.applyInitialState({ currentUser: { userId: 'bob' }, globalCursor: 0 });
  assert.equal(session.currentUserId, 'bob');
  assert.deepEqual(session.conversations, []);
  assert.deepEqual(session.messagesByConversation, {});
  assert.deepEqual(session.fileProgressByConversation, {});
  assert.deepEqual(session.pendingMessageStateByConversation, {});
  assert.deepEqual(session.readProgressByConversation, {});
  assert.equal(session.unreadTotal, 0);
  assert.equal(session.activeConversationId, '');
  assert.equal(session.globalCursor, 0);
  assert.equal(session.connection.sessionId, 'bob-session');
});

test('same-user welcome preserves messages until a snapshot is supplied', () => {
  const session = createSession();
  session.applyInitialState({ currentUser: { userId: 'alice' }, globalCursor: 8 });
  assert.equal(session.currentMessages.length, 1);
  assert.equal(session.globalCursor, 8);
});

for (const kind of ['recall', 'burn'] as const) {
  for (const stateFirst of [false, true]) {
    test(kind + ' survives repeated old messages, stateFirst=' + stateFirst, () => {
      const session = createSession();
      const original = { ...session.currentMessages[0] };
      if (stateFirst) session.messagesByConversation = {};
      session.applyMessageUpdated({
        type: kind, eventId: 'terminal', conversationId: 'private', messageId: original.id,
        operatorId: kind === 'burn' ? 'system-burn' : 'carol', tsMs: 2
      });
      session.pushMessages([original, original]);
      const item = session.currentMessages[0];
      assert.equal(session.currentMessages.length, 1);
      assert.equal(item.recalled, true);
      assert.equal(item.burned, kind === 'burn');
      assert.equal(item.text, '');
    });
  }
}

test('native snapshot restores read counts without subtracting them again on replay', () => {
  const session = createSession();
  const raw = { ...session.currentMessages[0], unreadCount: 2 };
  session.applyInitialState({
    currentUser: { userId: 'alice' }, globalCursor: 12, unreadTotal: 0,
    conversations: [...session.conversations],
    recentMessages: [{ ...raw, unreadCount: 0 }],
    readProgressByConversation: { private: { alice: 1, bob: 1 } }, files: []
  });
  assert.equal(session.currentMessages[0].unreadCount, 0);
  assert.equal(session.unreadTotal, 0);
  session.pushMessages([raw, raw]);
  session.applyMessageUpdated({
    type: 'receipt', eventId: 'duplicate-read', conversationId: 'private',
    readerId: 'bob', lastReadSeq: 1, readAtMs: 3
  });
  assert.equal(session.currentMessages.length, 1);
  assert.equal(session.currentMessages[0].unreadCount, 0);
  assert.equal(session.unreadTotal, 0);
});

test('native snapshot replaces file display and preserves completed versions', () => {
  const session = createSession();
  session.applyFileProgress({
    eventId: 'old-file', fileId: 'old-file', conversationId: 'private',
    transferredBytes: 1, completed: false, version: 1, updatedAtMs: 1
  });
  const complete = {
    eventId: 'complete', fileId: 'restored-file', conversationId: 'private',
    transferredBytes: 128, completed: true, version: 3, updatedAtMs: 3
  };
  session.applyInitialState({
    currentUser: { userId: 'alice' }, globalCursor: 10, files: [complete]
  });
  session.applyFileProgress({ ...complete, eventId: 'late', transferredBytes: 64, completed: false, version: 2 });
  assert.equal(session.currentFileProgress.length, 1);
  assert.equal(session.currentFileProgress[0].fileId, 'restored-file');
  assert.equal(session.currentFileProgress[0].completed, true);
  assert.equal(session.currentFileProgress[0].transferredBytes, 128);
});

test('pending and failed native sends restore per account and clear only when native state changes', () => {
  const session = createSession();
  const pending = {
    requestId: 'request-1', conversationId: 'private', clientMsgId: 'intent-1',
    text: 'saved text', burnMode: 0, burnTtlSec: 0, status: 'pending' as const,
    code: 0, error: '', createdAtMs: 1, attempts: 1
  };
  session.applyInitialState({ currentUser: { userId: 'alice' }, messageSends: [pending] });
  assert.equal(session.currentMessageSends[0].text, 'saved text');
  session.applyInitialState({ currentUser: { userId: 'alice' }, globalCursor: 15 });
  assert.equal(session.currentMessageSends.length, 1);
  session.applyMessageSends([{ ...pending, status: 'failed', code: 403, error: 'denied' }]);
  assert.equal(session.currentMessageSends[0].error, 'denied');
  session.applyInitialState({ currentUser: { userId: 'bob' }, messageSends: [] });
  assert.equal(session.messageSends.length, 0);
  session.applyInitialState({ currentUser: { userId: 'alice' }, messageSends: [pending] });
  assert.equal(session.messageSends[0].requestId, 'request-1');
  session.applyMessageSends([]);
  assert.equal(session.messageSends.length, 0);
});

test('native file tasks restore their intent and clear on account switch or native completion', () => {
  const session = createSession();
  const task = { clientFileId: 'same-intent', conversationId: 'private', fileId: 'server-file',
    fileName: 'source.bin', path: '/private/source.bin', direction: 1, status: 'failed' as const, error: 'changed' };
  session.applyInitialState({ currentUser: { userId: 'alice' }, fileTasks: [task] });
  assert.equal(session.currentFileTasks[0].clientFileId, 'same-intent');
  session.applyInitialState({ currentUser: { userId: 'alice' }, globalCursor: 9 });
  assert.equal(session.currentFileTasks[0].error, 'changed');
  session.applyInitialState({ currentUser: { userId: 'bob' } });
  assert.deepEqual(session.fileTasks, []);
  session.applyInitialState({ currentUser: { userId: 'alice' }, fileTasks: [task] });
  session.applyFileTasks([]);
  assert.deepEqual(session.fileTasks, []);
});
