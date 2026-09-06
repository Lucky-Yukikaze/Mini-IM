<template>
  <footer class="composer">
    <div v-if="attachOpen" class="attach-panel">
      <div class="attach-grid">
        <label>
          <span>发送文件路径</span>
          <input v-model="filePath" :disabled="disabled" placeholder="C:\\tmp\\demo.bin" />
        </label>
        <button :disabled="disabled || !filePath.trim()" @click="submitFile">发送文件</button>
      </div>
      <div class="attach-grid">
        <label>
          <span>下载 file_id</span>
          <input v-model="downloadFileId" :disabled="disabled" placeholder="源文件 ID" />
        </label>
        <label>
          <span>保存路径</span>
          <input v-model="downloadSavePath" :disabled="disabled" placeholder="C:\\tmp\\download.bin" />
        </label>
        <button
          :disabled="disabled || !downloadFileId.trim() || !downloadSavePath.trim()"
          @click="submitDownload"
        >
          下载
        </button>
      </div>
    </div>

    <form class="composer-main" @submit.prevent="submitMessage">
      <textarea
        v-model="draft"
        :disabled="disabled"
        rows="1"
        placeholder="输入消息"
        @keydown.enter.exact.prevent="submitMessage"
      ></textarea>
      <div class="composer-actions">
        <button type="button" class="icon-button" :class="{ active: attachOpen }" @click="attachOpen = !attachOpen">
          文件
        </button>
        <label class="burn-toggle" :class="{ active: burnEnabled }">
          <input v-model="burnEnabled" type="checkbox" />
          <span>阅后即焚</span>
        </label>
        <input
          v-model.number="burnTtlSec"
          class="ttl-input"
          :disabled="!burnEnabled"
          type="number"
          min="5"
          max="604800"
        />
        <button type="submit" :disabled="disabled || !draft.trim()">发送</button>
      </div>
    </form>
  </footer>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';

const props = defineProps<{
  disabled: boolean;
  downloadFileIdPreset: string;
}>();

const emit = defineEmits<{
  send: [text: string, burnMode: number, burnTtlSec: number];
  sendFile: [filePath: string];
  downloadFile: [fileId: string, savePath: string];
}>();

const draft = ref('');
const burnEnabled = ref(false);
const burnTtlSec = ref(30);
const attachOpen = ref(false);
const filePath = ref('');
const downloadFileId = ref('');
const downloadSavePath = ref('');

watch(
  () => props.downloadFileIdPreset,
  (fileId) => {
    if (fileId) {
      downloadFileId.value = fileId;
      attachOpen.value = true;
    }
  }
);

function submitMessage(): void {
  const text = draft.value.trim();
  if (!text) {
    return;
  }
  const ttl = burnEnabled.value ? Math.max(5, Math.min(604800, Number(burnTtlSec.value) || 0)) : 0;
  emit('send', text, burnEnabled.value ? 1 : 0, ttl);
  draft.value = '';
}

function submitFile(): void {
  const path = filePath.value.trim();
  if (!path) {
    return;
  }
  emit('sendFile', path);
  filePath.value = '';
}

function submitDownload(): void {
  const fileId = downloadFileId.value.trim();
  const savePath = downloadSavePath.value.trim();
  if (!fileId || !savePath) {
    return;
  }
  emit('downloadFile', fileId, savePath);
  downloadFileId.value = '';
}
</script>
