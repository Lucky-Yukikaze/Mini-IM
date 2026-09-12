// Executed by Playwright CLI run-code against the actual Qt WebEngine page.
async page => {
  const data = __CASE__;
  page.setDefaultTimeout(10000);
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const modal = page.locator('.modal');
  const status = page.getByRole('region', { name: '操作状态' });
  const button = name => page.getByRole('button', { name, exact: true });
  const select = async title => {
    await page.locator('.conversation-item').filter({ hasText: title }).click();
    await page.getByRole('heading', { name: title, exact: true, level: 2 }).waitFor();
  };
  if (data.phase === 'login') {
    await page.waitForFunction(() => !!window.qt?.webChannelTransport && !!window.imBridge);
    await button('联调').click();
    if (data.switch) {
      await button('断开').click();
      await page.waitForFunction(() => document.querySelector('.connection-pill').textContent.trim() !== 'connected');
    }
    await page.getByLabel('Endpoint', { exact: true }).fill(data.endpoint);
    await page.getByLabel('用户名', { exact: true }).fill(data.user);
    await page.getByLabel('Device', { exact: true }).fill(data.device);
    await button('连接').click();
    await page.waitForFunction(user => document.querySelector('.connection-pill').textContent.trim() === 'connected'
      && [...document.querySelectorAll('.compact-stats strong')].some(item => item.textContent === user), data.user);
    await page.locator('.debug-drawer').getByRole('button', { name: '关闭', exact: true }).click();
    await select(data.title);
    if (data.failure !== undefined) {
      await page.waitForFunction(expected => document.querySelector('[aria-label="操作状态"]').textContent.includes('邀请成员 · 未成功') === expected, data.failure);
    }
  } else if (data.phase === 'history-visible') {
    await page.locator('.message-row .bubble').filter({ hasText: data.text }).waitFor();
  } else if (data.phase === 'history-directions') {
    const result = await page.evaluate(async conversation => {
      const read = (cursor, direction) => new Promise(resolve => window.imBridge.loadHistoryPage(conversation, cursor, direction, resolve));
      const collect = (page, found) => {
        if (!page.ok || page.messages.length > 50) throw new Error('invalid directional page');
        for (const item of page.messages) {
          if (found.has(item.id)) throw new Error('duplicate directional message');
          found.add(item.id);
        }
      };
      let current = await read('', 'latest');
      const backwards = new Set();
      while (true) {
        collect(current, backwards);
        if (!current.hasOlder) break;
        current = await read(current.beforeCursor, 'older');
      }
      const forwards = new Set();
      while (true) {
        collect(current, forwards);
        if (!current.hasNewer) break;
        current = await read(current.afterCursor, 'newer');
      }
      const empty = await read(current.afterCursor, 'newer');
      const invalid = await read('', 'newer');
      return { backwards: [...backwards].sort(), forwards: [...forwards].sort(),
        empty: empty.ok && empty.messages.length === 0 && !empty.hasNewer, rejected: !invalid.ok };
    }, data.group);
    check(result.backwards.length === 125 && JSON.stringify(result.backwards) === JSON.stringify(result.forwards), 'directional history lost messages');
    check(result.empty && result.rejected, 'directional boundary handling failed');
  } else if (data.phase === 'history-top') {
    await page.locator('.message-viewport').evaluate(element => { element.scrollTop = 0; });
    await page.locator('.message-row .bubble').filter({ hasText: data.text }).waitFor();
    check(await button('加载更早消息').count() === (data.more ? 1 : 0), 'incorrect history availability');
    if (data.more) check(await button('加载更早消息').isEnabled(), 'history button remained disabled');
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'history-load') {
    await page.evaluate(() => {
      const original = window.imBridge.loadHistory;
      window.historyQa = { original, calls: 0 };
      window.imBridge.loadHistory = (...args) => {
        window.historyQa.calls++;
        const callback = args.pop();
        original(...args, result => {
          window.historyQa.result = result;
          window.historyQa.release = () => callback(result);
        });
      };
    });
    try {
      await button('加载更早消息').click();
      await page.waitForFunction(() => typeof window.historyQa.release === 'function');
      check(await button('正在加载历史…').isDisabled(), 'history enabled before completion callback');
      const result = await page.evaluate(() => window.historyQa.result);
      check(result.ok && result.messages.length === data.count, 'incorrect native history page');
      check(await page.evaluate(() => window.historyQa.calls) === 1, 'duplicate history request');
      await page.evaluate(() => window.historyQa.release());
      await button('正在加载历史…').waitFor({ state: 'hidden' });
    } finally {
      await page.evaluate(() => { window.imBridge.loadHistory = window.historyQa.original; delete window.historyQa; });
    }
  } else if (data.phase === 'callback') {
    await page.evaluate(() => {
      const original = window.imBridge.createConversation;
      window.desktopQa = { original, calls: 0 };
      window.imBridge.createConversation = (...args) => {
        window.desktopQa.calls++;
        const callback = args.pop();
        original(...args, accepted => { window.desktopQa.release = () => callback(accepted); });
      };
    });
    try {
      await button('建群').click();
      await modal.getByLabel('群名称', { exact: true }).fill('Callback group');
      await modal.getByLabel('成员 ID', { exact: true }).fill('bob');
      await modal.getByRole('button', { name: '创建', exact: true }).click();
      await page.waitForFunction(() => typeof window.desktopQa.release === 'function');
      check(await modal.isVisible(), 'form closed before native callback');
      check(await modal.getByLabel('群名称', { exact: true }).isDisabled(), 'input enabled during save');
      check(await modal.getByRole('button', { name: '关闭', exact: true }).isDisabled(), 'close enabled during save');
      await modal.locator('form').dispatchEvent('submit');
      check(await page.evaluate(() => window.desktopQa.calls) === 1, 'duplicate submission reached Qt');
      await page.evaluate(() => window.desktopQa.release());
      await modal.waitFor({ state: 'hidden' });
      await select('Callback group');
    } finally {
      await page.evaluate(() => { window.imBridge.createConversation = window.desktopQa.original; delete window.desktopQa; });
    }
  } else if (data.phase === 'form-failure') {
    await button(data.mode).click();
    for (const [label, value] of data.fields) await modal.getByLabel(label, { exact: true }).fill(value);
    await modal.getByRole('button', { name: data.submit, exact: true }).click();
    await modal.getByRole('alert').waitFor();
    for (const [label, value] of data.fields) check(await modal.getByLabel(label, { exact: true }).inputValue() === value, 'draft was lost');
    check(await modal.getByRole('button', { name: data.submit, exact: true }).isEnabled(), 'retry remains disabled');
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'form-retry') {
    await modal.getByRole('button', { name: data.submit, exact: true }).click();
    await modal.waitFor({ state: 'hidden' });
    await select(data.title);
  } else if (data.phase === 'member-failure') {
    await select(data.title);
    await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).fill('cindy');
    await button('邀请').click();
    await page.getByText('操作未保存，请检查连接和输入', { exact: true }).waitFor();
    check(await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).inputValue() === 'cindy', 'member draft lost');
    await page.getByPlaceholder('群名称', { exact: true }).fill('Desktop renamed');
    await button('改名').click();
    check(await page.getByPlaceholder('群名称', { exact: true }).inputValue() === 'Desktop renamed', 'rename draft lost');
  } else if (data.phase === 'member-retry') {
    await button('邀请').click();
    await page.locator('.member-chip').filter({ hasText: 'cindy' }).waitFor();
    check(await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).inputValue() === '', 'saved member draft retained');
    await page.locator('.member-chip').filter({ hasText: 'cindy' }).getByRole('button', { name: '移除' }).click();
    await page.locator('.member-chip').filter({ hasText: 'cindy' }).waitFor({ state: 'hidden' });
  } else if (data.phase === 'rename') {
    await button('改名').click();
    await status.getByText('修改群名 · 等待确认', { exact: true }).waitFor();
    await status.getByText('等待自动重试', { exact: true }).waitFor();
  } else if (data.phase === 'renamed') {
    await page.getByRole('heading', { name: 'Desktop renamed', exact: true, level: 2 }).waitFor();
    await status.getByText('修改群名 · 等待确认', { exact: true }).waitFor({ state: 'hidden' });
  } else if (data.phase === 'rejected') {
    await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).fill('missing-user');
    await button('邀请').click();
    await status.getByText('邀请成员 · 未成功', { exact: true }).waitFor();
    check(await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).inputValue() === '', 'saved rejected draft retained');
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'delivery-send') {
    await page.getByPlaceholder('输入消息', { exact: true }).fill('Desktop delivery proof');
    await button('发送').click();
    const row = page.locator('.message-row').filter({ hasText: 'Desktop delivery proof' });
    await row.getByText('等待送达', { exact: true }).waitFor();
  } else if (data.phase === 'delivery-status') {
    const row = page.locator('.message-row').filter({ hasText: 'Desktop delivery proof' });
    await row.waitFor();
    if (data.read) {
      await row.getByText('1 人已送达', { exact: true }).waitFor({ state: 'hidden' });
      await row.getByText('1 人未读', { exact: true }).waitFor({ state: 'hidden' });
      await row.getByText('等待送达', { exact: true }).waitFor({ state: 'hidden' });
    } else {
      await row.getByText('1 人已送达', { exact: true }).waitFor();
      await row.getByText('1 人未读', { exact: true }).waitFor();
    }
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'late-member') {
    if (!(await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).count())) await button('成员').click();
    await page.getByPlaceholder('成员 ID，逗号分隔', { exact: true }).fill('cindy');
    await button('邀请').click();
    await page.locator('.member-chip').filter({ hasText: 'cindy' }).waitFor();
  } else if (data.phase === 'late-member-count') {
    const row = page.locator('.message-row').filter({ hasText: 'Desktop delivery' });
    await row.getByText('1 人未读', { exact: true }).waitFor();
    await row.getByText('等待送达', { exact: true }).waitFor();
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'recall') {
    await page.getByPlaceholder('输入消息', { exact: true }).fill('Desktop recall');
    await button('发送').click();
    const row = page.locator('.message-row').filter({ hasText: 'Desktop recall' });
    await row.getByRole('button', { name: '撤回', exact: true }).click();
    await page.getByText('消息已撤回', { exact: true }).waitFor();
  } else if (data.phase === 'incoming') {
    await page.locator('.message-row').filter({ hasText: data.text }).waitFor();
    if (data.failed) await page.getByText('已读操作未保存，可点击“标为已读”重试', { exact: true }).waitFor();
    if (data.pending) await status.getByText('标记已读 · 等待确认', { exact: true }).waitFor();
  } else if (data.phase === 'read') {
    await button('标为已读').click();
  } else if (data.phase === 'settled') {
    await status.getByText('标记已读 · 等待确认', { exact: true }).waitFor({ state: 'hidden' });
  } else if (data.phase === 'file-upload') {
    if (!(await page.getByLabel('发送文件路径', { exact: true }).count())) await button('文件').click();
    const input = page.getByLabel('发送文件路径', { exact: true });
    await input.fill(data.transferSource);
    await button('发送文件').click();
    if (data.failed) {
      await page.getByText('文件任务未保存，请检查路径', { exact: true }).waitFor();
      check(await input.inputValue() === data.transferSource, 'upload path cleared before save');
      check(await page.locator('.file-task-card').count() === 0, 'failed save created visible file task');
    } else {
      await page.locator('.file-task-card').filter({ hasText: '上传 · ' + (data.fileName || 'source.bin') }).waitFor();
      check(await input.inputValue() === '', 'saved upload path was not cleared');
    }
  } else if (data.phase === 'file-pending') {
    await page.locator('.file-task-card').filter({ hasText: data.direction + ' · ' + (data.direction === '上传' ? 'source.bin' : 'download.bin') }).waitFor();
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'file-uploaded') {
    await page.locator('.file-task-card').waitFor({ state: 'hidden' });
    const row = page.locator('.message-row').filter({ hasText: 'source.bin' });
    await row.getByRole('button', { name: '填入下载', exact: true }).waitFor();
    check(await row.count() === 1, 'upload published duplicate messages');
  } else if (data.phase === 'file-fill') {
    await page.locator('.message-row').filter({ hasText: data.fileName || 'source.bin' })
      .getByRole('button', { name: '填入下载', exact: true }).click();
    const input = page.getByLabel('下载 file_id', { exact: true });
    await input.waitFor();
    check(await input.inputValue() === data.fileId, 'repeated fill did not restore file id');
    await page.getByLabel('保存路径', { exact: true }).fill(data.transferTarget);
  } else if (data.phase === 'file-download') {
    await button('下载').click();
    if (data.failed) {
      await page.getByText('下载任务未保存，请检查输入', { exact: true }).waitFor();
      check(await page.getByLabel('下载 file_id', { exact: true }).inputValue() === data.fileId,
        'download source cleared before save');
      check(await page.getByLabel('保存路径', { exact: true }).inputValue() === data.transferTarget,
        'download path cleared before save');
    } else {
      await page.waitForFunction(() => document.querySelector('[placeholder="源文件 ID"]')?.value === '');
      if (data.pending) await page.locator('.file-task-card').filter({ hasText: '下载 · ' + (data.targetName || 'download.bin') }).waitFor();
    }
  } else if (data.phase === 'file-failed') {
    const task = page.locator('.file-task-card').filter({ hasText: '下载 · download.bin' });
    await task.getByText('传输失败', { exact: true }).waitFor();
    if (data.error) await task.getByText(data.error, { exact: true }).waitFor();
    await task.getByRole('button', { name: '重试', exact: true }).waitFor();
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'file-retry') {
    await page.locator('.file-task-card').getByRole('button', { name: '重试', exact: true }).click();
  } else if (data.phase === 'file-cancel-active') {
    const task = data.fileName ? page.locator('.file-task-card').filter({ hasText: data.fileName }) : page.locator('.file-task-card');
    await task.getByRole('button', { name: '取消', exact: true }).click();
    await task.waitFor({ state: 'hidden' });
  } else if (data.phase === 'file-cleanup-preview') {
    await button('清理已取消下载').click();
    const dialog = page.getByRole('dialog', { name: '清理已取消下载', exact: true });
    await dialog.waitFor();
    if (data.empty) {
      await dialog.getByText('没有可清理的下载片段', { exact: true }).waitFor();
      check(await dialog.getByRole('button', { name: '确认清理', exact: true }).isDisabled(), 'empty cleanup was enabled');
    } else {
      await dialog.getByText(data.path, { exact: true }).waitFor();
      await dialog.getByRole('button', { name: '确认清理', exact: true }).waitFor();
    }
    if (data.image) await page.screenshot({ path: data.image });
    if (data.close) await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  } else if (data.phase === 'file-cleanup-apply') {
    const dialog = page.getByRole('dialog', { name: '清理已取消下载', exact: true });
    await dialog.getByRole('button', { name: '确认清理', exact: true }).click();
    await dialog.getByText('已清理 1 个临时文件', { exact: true }).waitFor();
    if (data.image) await page.screenshot({ path: data.image });
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  } else if (data.phase === 'file-concurrent') {
    await page.waitForFunction(count => document.querySelectorAll('.file-task-card').length === count, data.count);
    for (const name of data.names) await page.locator('.file-task-card').filter({ hasText: name }).waitFor();
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'send-during-files') {
    await page.getByPlaceholder('输入消息', { exact: true }).fill(data.text);
    await button('发送').click();
    await page.locator('.message-row').filter({ hasText: data.text }).waitFor();
    check(await page.getByPlaceholder('输入消息', { exact: true }).inputValue() === '', 'saved message draft retained');
  } else if (data.phase === 'crash-submit-message') {
    await page.getByPlaceholder('输入消息', { exact: true }).fill(data.text);
    await button('发送').click();
    await page.waitForFunction(() => document.querySelector('.composer textarea').value === '');
  } else if (data.phase === 'crash-submit-recall') {
    await page.locator('.message-row').filter({ hasText: data.text })
      .getByRole('button', { name: '撤回', exact: true }).click();
    await status.getByText('撤回消息 · 等待确认', { exact: true }).waitFor();
  } else if (data.phase === 'crash-submit-cancel') {
    await page.locator('.file-task-card').getByRole('button', { name: '取消', exact: true }).click();
    await page.locator('.file-task-card').getByText('取消待确认', { exact: true }).waitFor();
  } else if (data.phase === 'crash-submit-rename') {
    if (!(await page.getByPlaceholder('群名称', { exact: true }).count())) await button('成员').click();
    await page.getByPlaceholder('群名称', { exact: true }).fill(data.title);
    await button('改名').click();
    await status.getByText('修改群名 · 等待确认', { exact: true }).waitFor();
  } else if (data.phase === 'crash-recovered') {
    await page.getByRole('heading', { name: data.title, exact: true, level: 2 }).waitFor();
    if (data.recalled) {
      await page.getByText('消息已撤回', { exact: true }).waitFor();
      check(await page.locator('.message-row').filter({ hasText: data.recalled }).count() === 0,
        'recalled body returned after recovery');
    }
    if (data.text) {
      const row = page.locator('.message-row').filter({ hasText: data.text });
      await row.waitFor();
      check(await row.count() === 1, 'recovery duplicated the message');
    }
    if (data.fileName) {
      const row = page.locator('.message-row').filter({ hasText: data.fileName });
      await row.getByRole('button', { name: '填入下载', exact: true }).waitFor();
      check(await row.count() === 1, 'recovery duplicated the file message');
    }
    check(await page.locator('.file-task-card').count() === 0, 'file recovery remains pending');
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'file-complete') {
    await page.locator('.file-task-card').waitFor({ state: 'hidden' });
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'file-isolated') {
    check(await page.locator('.file-task-card').count() === 0, 'another user saw pending file task');
  } else if (data.phase === 'upload') {
    if (!(await page.getByLabel('发送文件路径', { exact: true }).count())) await button('文件').click();
    await page.getByLabel('发送文件路径', { exact: true }).fill(data.upload);
    await button('发送文件').click();
    await page.locator('.file-task-card').getByText('上传 · desktop-upload.bin', { exact: true }).waitFor();
  } else if (data.phase === 'cancel-file') {
    const task = page.locator('.file-task-card');
    await task.getByRole('button', { name: '取消', exact: true }).click();
    await task.getByText(data.failed ? '取消未成功' : '取消待确认', { exact: true }).waitFor();
    if (!data.failed) check(await task.getByRole('button', { name: '取消', exact: true }).isDisabled(), 'duplicate cancellation enabled');
    if (data.image) await page.screenshot({ path: data.image });
  } else if (data.phase === 'cancel-file-retry') {
    await page.locator('.file-task-card').getByRole('button', { name: '重试取消', exact: true }).click();
  } else if (data.phase === 'file-cancelled') {
    await page.locator('.file-task-card').waitFor({ state: 'hidden' });
  } else if (data.phase === 'join') {
    await button('加群').click();
    await modal.getByLabel('会话 ID', { exact: true }).fill(data.group);
    await modal.getByRole('button', { name: '进入', exact: true }).click();
    await modal.waitFor({ state: 'hidden' });
    await select(data.title);
    check(await page.getByPlaceholder('输入消息', { exact: true }).isEnabled(), 'joined conversation unavailable');
  } else if (data.phase === 'leave') {
    await button('退出群聊').click();
    await page.waitForFunction(() => document.querySelector('.composer textarea').disabled);
  } else {
    throw new Error('unknown desktop UI phase');
  }
  return { ok: true, phase: data.phase };
}
