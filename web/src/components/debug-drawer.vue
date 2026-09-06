<template>
  <aside class="debug-drawer" :class="{ open }">
    <header class="drawer-header">
      <div>
        <p class="section-kicker">Debug</p>
        <h2>联调</h2>
      </div>
      <button class="icon-button" @click="$emit('close')">关闭</button>
    </header>

    <section class="drawer-section">
      <label>
        <span>Endpoint</span>
        <input v-model="endpoint" />
      </label>
      <label>
        <span>用户名</span>
        <input v-model="userName" placeholder="u-alice" />
      </label>
      <label>
        <span>Device</span>
        <input v-model="deviceId" />
      </label>
      <div class="button-pair">
        <button @click="$emit('connect', endpoint, devToken, deviceId)">连接</button>
        <button class="secondary-button" @click="$emit('disconnect')">断开</button>
      </div>
    </section>

    <section class="drawer-section compact-stats">
      <div><span>状态</span><strong>{{ state }}</strong></div>
      <div><span>用户</span><strong>{{ currentUserId || '-' }}</strong></div>
      <div><span>游标</span><strong>{{ globalCursor }}</strong></div>
      <div><span>未读</span><strong>{{ unreadTotal }}</strong></div>
      <div><span>Session</span><strong>{{ sessionId || '-' }}</strong></div>
    </section>

    <section class="drawer-section">
      <div class="drawer-section-title">文件传输</div>
      <div v-if="fileProgress.length === 0" class="drawer-empty">暂无传输</div>
      <div v-for="item in fileProgress" :key="item.fileId" class="transfer-item">
        <div>
          <div class="transfer-title">{{ item.fileId }}</div>
          <div class="transfer-meta">v{{ item.version }} · {{ item.transferredBytes }} bytes</div>
        </div>
        <span class="status-dot" :class="{ online: item.completed }"></span>
      </div>
    </section>
  </aside>
  <div v-if="open" class="drawer-mask" @click="$emit('close')"></div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import type { ConnectionState, FileProgressItem } from '../types';

defineProps<{
  open: boolean;
  state: ConnectionState['state'];
  sessionId: string;
  currentUserId: string;
  globalCursor: number;
  unreadTotal: number;
  fileProgress: FileProgressItem[];
}>();

defineEmits<{
  close: [];
  connect: [endpoint: string, token: string, deviceId: string];
  disconnect: [];
}>();

const endpoint = ref('quic://127.0.0.1:4433');
const userName = ref('u-alice');
const deviceId = ref('dev-device');
const devToken = computed(() => `dev-token:${userName.value.trim() || 'u-demo'}`);
</script>
