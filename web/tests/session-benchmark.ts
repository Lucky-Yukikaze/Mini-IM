import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { createPinia } from 'pinia';
import { useSessionStore } from '../src/store/session';
import type { MessageItem } from '../src/types';

const count = Number(process.argv[2]);
assert(Number.isInteger(count) && count >= 0 && count <= 10000);
const session = useSessionStore(createPinia());
session.applyInitialState({ currentUser: { userId: 'alice' }, unreadAuthoritative: true, unreadTotal: 0,
  conversations: [{ conversationId: 'measure', title: 'measure', type: 'direct', ownerId: 'alice',
    memberIds: ['alice', 'bob'], updatedAtMs: 0 }] });
global.gc!();
const startHeap = process.memoryUsage().heapUsed;
let peakRss = process.memoryUsage().rss;
const batches: { messages: number; mergeMs: number; updatesMs: number }[] = [];
for (let offset = 0; offset < count; offset += 100) {
  const items: MessageItem[] = [];
  for (let index = offset; index < Math.min(count, offset + 100); index++) {
    items.push({ id: `message-${index}`, conversationId: 'measure', senderId: 'bob', clientMsgId: `intent-${index}`,
      seq: index + 1, text: String(index).padEnd(128, 'x'), createdAtMs: index, recalled: false, burned: false,
      unreadCount: 1, readCountKnown: true, burnMode: 0, burnTtlSec: 0 });
  }
  const updateStart = performance.now();
  for (const item of items) {
    session.applyMessageUpdated({ type: 'readCount', eventId: `count-${item.id}`, messageId: item.id,
      conversationId: 'measure', globalSeq: item.seq * 3 - 1, unreadCount: 1 });
    session.applyMessageUpdated({ type: 'delivery', eventId: `delivery-${item.id}`, messageId: item.id,
      conversationId: 'measure', globalSeq: item.seq * 3, userId: 'alice', status: 'delivered',
      sentAtMs: item.seq, deliveredAtMs: item.seq, readAtMs: 0, failedAtMs: 0, failureReason: '' });
  }
  const mergeStart = performance.now();
  session.pushMessages(items);
  const mergeEnd = performance.now();
  session.applySyncProgress({ globalCursor: (offset + items.length) * 3, unreadTotal: offset + items.length });
  batches.push({ messages: offset + items.length, mergeMs: mergeEnd - mergeStart, updatesMs: mergeStart - updateStart });
  peakRss = Math.max(peakRss, process.memoryUsage().rss);
}
const viewStart = performance.now();
const displayed = session.currentMessages;
const viewMs = performance.now() - viewStart;
assert.equal(displayed.length, count);
assert.equal(new Set(displayed.map(item => item.id)).size, count);
for (let index = 0; index < count; index++) {
  assert.equal(displayed[index].seq, index + 1);
  assert.equal(displayed[index].text.length, 128);
  assert.equal(displayed[index].unreadCount, 1);
  assert.equal(displayed[index].deliveries?.length, 1);
}
assert.equal(Object.keys(session.readCountsByConversation.measure ?? {}).length, count);
assert.equal(Object.keys(session.deliveriesByConversation.measure ?? {}).length, count);
assert.equal(session.unreadTotal, count);
assert.equal(session.globalCursor, count * 3);
global.gc!();
const memory = process.memoryUsage();
console.log(JSON.stringify({ count, batchSize: 100, batches, viewMs,
  mergeMs: batches.reduce((sum, row) => sum + row.mergeMs, 0),
  updatesMs: batches.reduce((sum, row) => sum + row.updatesMs, 0),
  retainedHeapBytes: memory.heapUsed - startHeap, sampledPeakRssBytes: Math.max(peakRss, memory.rss),
  messages: displayed.length, readCounts: Object.keys(session.readCountsByConversation.measure ?? {}).length,
  deliveries: Object.keys(session.deliveriesByConversation.measure ?? {}).length, unreadTotal: session.unreadTotal }));
