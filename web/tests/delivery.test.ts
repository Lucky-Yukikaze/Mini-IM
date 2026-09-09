import assert from 'node:assert/strict';
import test from 'node:test';
import { createPinia } from 'pinia';
import { useSessionStore } from '../src/store/session';
import { deliveryLabel } from '../src/model/delivery';
import type { DeliveryUpdate, MessageItem } from '../src/types';

const message: MessageItem = { id: 'm1', conversationId: 'c1', senderId: 'alice', seq: 1, text: 'body',
  createdAtMs: 1, recalled: false, burned: false, unreadCount: 1, burnMode: 0, burnTtlSec: 0 };
const delivered: DeliveryUpdate = { type: 'delivery', eventId: 'd1', globalSeq: 2, conversationId: 'c1',
  messageId: 'm1', userId: 'bob', status: 'delivered', sentAtMs: 1, deliveredAtMs: 2,
  readAtMs: 0, failedAtMs: 0, failureReason: '' };
function session() {
  const state = useSessionStore(createPinia());
  state.applyInitialState({ currentUser: { userId: 'alice' }, deliveries: [] });
  state.setCurrentConversation('c1');
  return state;
}

for (const deliveryFirst of [true, false]) {
  test('delivery survives message order and duplicate replay: deliveryFirst=' + deliveryFirst, () => {
    const state = session();
    if (deliveryFirst) state.applyMessageUpdated(delivered);
    state.pushMessage(message);
    state.applyMessageUpdated(delivered);
    state.pushMessage(message);
    assert.deepEqual(state.currentMessages[0].deliveries, [delivered]);
    assert.equal(deliveryLabel(state.currentMessages[0], 'alice', 'direct'), '已送达');
    assert.equal(state.currentMessages[0].unreadCount, 1);
    assert.equal(state.unreadTotal, 0);
  });
}

test('late delivery cannot regress read and old positions cannot replace newer delivery', () => {
  const state = session();
  state.pushMessage(message);
  state.applyMessageUpdated({ type: 'receipt', eventId: 'read', conversationId: 'c1',
    lastReadSeq: 1, readAtMs: 3, readerId: 'bob' });
  state.applyMessageUpdated(delivered);
  state.applyMessageUpdated({ ...delivered, globalSeq: 1, status: 'sent', deliveredAtMs: 0 });
  assert.equal(state.currentMessages[0].deliveries?.[0].status, 'read');
  assert.equal(state.currentMessages[0].deliveries?.[0].deliveredAtMs, 2);
  assert.equal(state.currentMessages[0].unreadCount, 0);
  assert.equal(deliveryLabel(state.currentMessages[0], 'alice', 'direct'), '');
});

test('native snapshot restores delivery and account changes isolate it', () => {
  const state = session();
  state.applyInitialState({ currentUser: { userId: 'alice' }, recentMessages: [message], deliveries: [delivered] });
  assert.equal(state.currentMessages[0].deliveries?.[0].userId, 'bob');
  state.applyInitialState({ currentUser: { userId: 'alice' } });
  assert.equal(state.currentMessages[0].deliveries?.length, 1);
  state.applyInitialState({ currentUser: { userId: 'carol' }, recentMessages: [message] });
  state.setCurrentConversation('c1');
  assert.deepEqual(state.currentMessages[0].deliveries, []);
  state.applyInitialState({ currentUser: { userId: 'alice' }, recentMessages: [message], deliveries: [delivered] });
  state.setCurrentConversation('c1');
  assert.equal(state.currentMessages[0].deliveries?.length, 1);
  state.applyInitialState({ currentUser: { userId: 'alice' }, deliveries: [] });
  assert.deepEqual(state.currentMessages[0].deliveries, []);
});

test('delivery labels separate waiting, received, failed and terminal messages', () => {
  assert.equal(deliveryLabel(message, 'alice', 'direct'), '等待送达');
  assert.equal(deliveryLabel({ ...message, deliveries: [delivered] }, 'alice', 'direct'), '已送达');
  assert.equal(deliveryLabel({ ...message, deliveries: [{ ...delivered, status: 'failed' }] }, 'alice', 'direct'), '投递失败');
  assert.equal(deliveryLabel(message, 'bob', 'direct'), '');
  assert.equal(deliveryLabel({ ...message, recalled: true, deliveries: [delivered] }, 'alice', 'direct'), '');
  assert.equal(deliveryLabel({ ...message, burned: true }, 'alice', 'direct'), '');
  assert.equal(deliveryLabel({ ...message, unreadCount: 0 }, 'alice', 'group'), '');
});

test('group delivery counts users and preserves first timestamp across duplicate device confirmations', () => {
  const state = session();
  state.pushMessage({ ...message, unreadCount: 2 });
  state.applyMessageUpdated(delivered);
  state.applyMessageUpdated({ ...delivered, eventId: 'duplicate', deliveredAtMs: 10 });
  state.applyMessageUpdated({ ...delivered, eventId: 'carol', globalSeq: 3, userId: 'carol' });
  assert.equal(state.currentMessages[0].deliveries?.length, 2);
  assert.equal(state.currentMessages[0].deliveries?.find(item => item.userId === 'bob')?.deliveredAtMs, 2);
  assert.equal(deliveryLabel(state.currentMessages[0], 'alice', 'group'), '2 人已送达');
  assert.equal(state.currentMessages[0].unreadCount, 2);
});
