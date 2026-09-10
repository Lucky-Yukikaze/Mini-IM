import assert from 'node:assert/strict';
import test from 'node:test';

const runtime = {} as { imBridge?: Record<string, unknown>; qt?: { webChannelTransport: unknown } };
Object.assign(globalThis, { window: runtime });
let bridge: typeof import('../src/api/bridge');
test.before(async () => { bridge = await import('../src/api/bridge'); });
const operations: [string, string, unknown[]][] = [
  ['createConversation', 'createConversation', ['group', ['bob']]],
  ['createDirectConversation', 'createDirectConversation', ['bob']],
  ['addMembers', 'addMembers', ['group', ['bob']]],
  ['removeMembers', 'removeMembers', ['group', ['bob']]],
  ['leaveConversation', 'leaveConversation', ['group']],
  ['joinConversation', 'joinConversation', ['group']],
  ['renameConversation', 'renameConversation', ['group', 'new title']],
  ['sendReceipt', 'sendReceipt', ['group', 3]],
  ['recallMessage', 'recallMessage', ['group', 'message']],
  ['sendFile', 'sendFile', ['group', 'source.bin', 0]],
  ['downloadFile', 'downloadFile', ['group', 'source-id', 'target.bin', 0]],
  ['retryFile', 'retryFile', ['intent']],
  ['cancelFile', 'cancelFile', ['intent']]
];

for (const [api, native, args] of operations) {
  test(api + ' waits for native persistence and propagates rejection', async () => {
    let done: ((accepted: boolean) => void) | undefined;
    runtime.imBridge = { [native]: (...values: unknown[]) => { done = values.at(-1) as typeof done; } };
    const result = (bridge as Record<string, Function>)[api](...args);
    assert.ok(result instanceof Promise, 'native call must return a completion promise');
    let settled = false;
    result.then(() => { settled = true; });
    await Promise.resolve();
    assert.equal(settled, false);
    assert.equal(typeof done, 'function');
    done!(false);
    assert.equal(await result, false);
    const accepted = (bridge as Record<string, Function>)[api](...args);
    done!(true);
    assert.equal(await accepted, true);
  });
}

test('a missing method on an existing native bridge cannot fabricate success', async () => {
  runtime.imBridge = {};
  assert.equal(await bridge.createDirectConversation('bob'), false);
  assert.equal(await bridge.addMembers('group', ['bob']), false);
  for (const [api, , args] of operations.slice(-4)) assert.equal(await (bridge as Record<string, Function>)[api](...args), false);
});

test('native initialization failure cannot fall through to local demo events', async () => {
  runtime.imBridge = undefined;
  runtime.qt = { webChannelTransport: {} };
  try {
    for (const [api, , args] of operations) assert.equal(await (bridge as Record<string, Function>)[api](...args), false);
  } finally {
    delete runtime.qt;
  }
});
