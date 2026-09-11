<template>
  <div class="modal-layer" @click.self="!busy && emit('close')">
    <section class="modal" role="dialog" aria-label="清理已取消下载">
      <header class="modal-header">
        <h2>清理已取消下载</h2>
        <button class="icon-button" :disabled="busy" @click="emit('close')">关闭</button>
      </header>
      <p>以下临时文件属于当前账号已确认取消的下载。</p>
      <p>正式下载文件和任务记录会保留。</p>
      <ul v-if="items.length" class="cleanup-items">
        <li v-for="item in items" :key="item.clientFileId">
          <strong>{{ item.fileName }} · {{ item.bytes }} 字节</strong>
          <span>{{ item.path }}</span>
        </li>
      </ul>
      <p v-else-if="!busy && !error && !message">没有可清理的下载片段</p>
      <p v-if="busy" role="status">正在处理…</p>
      <p v-if="message" role="status">{{ message }}</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <div class="cleanup-actions">
        <button class="secondary-button" :disabled="busy" @click="preview">重新预览</button>
        <button :disabled="busy || !token || !items.length" @click="apply">确认清理</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { cleanupCancelledDownloads, previewCancelledDownloads, type FileCleanupItem } from '../api/bridge';
const emit = defineEmits<{ close: [] }>();
const items = ref<FileCleanupItem[]>([]);
const token = ref('');
const error = ref('');
const message = ref('');
const busy = ref(false);
let active = true;
onUnmounted(() => { active = false; });

async function preview(): Promise<void> {
  busy.value = true;
  token.value = '';
  items.value = [];
  error.value = '';
  message.value = '';
  try {
    const result = await previewCancelledDownloads();
    if (!active) return;
    if (!result.ok) error.value = result.error || '预览失败，请重新连接后再试';
    else {
      items.value = result.items || [];
      token.value = result.token || '';
    }
  } catch {
    if (active) error.value = '清理预览调用失败';
  } finally {
    if (active) busy.value = false;
  }
}

async function apply(): Promise<void> {
  if (busy.value || !token.value) return;
  busy.value = true;
  const approved = token.value;
  token.value = '';
  try {
    const result = await cleanupCancelledDownloads(approved);
    if (!active) return;
    message.value = `已清理 ${result.removed || 0} 个临时文件`;
    if (result.ok) items.value = [];
    else error.value = result.error || '部分文件已变化或无法删除，请重新预览';
  } catch {
    if (active) error.value = '清理调用失败，请重新预览';
  } finally {
    if (active) busy.value = false;
  }
}
onMounted(preview);
</script>

<style scoped>
.cleanup-items { max-height: 280px; overflow-y: auto; padding-left: 20px; }
.cleanup-items li { margin-bottom: 12px; overflow-wrap: anywhere; }
.cleanup-items span { display: block; font-size: 12px; margin-top: 4px; }
.cleanup-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
</style>
