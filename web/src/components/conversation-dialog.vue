<template>
  <div class="modal-layer" @click.self="$emit('close')">
    <section class="modal">
      <header class="modal-header">
        <div>
          <p class="section-kicker">{{ modeLabel }}</p>
          <h2>{{ titleLabel }}</h2>
        </div>
        <button class="icon-button" @click="$emit('close')">关闭</button>
      </header>

      <form v-if="mode === 'create'" class="modal-form" @submit.prevent="submitCreate">
        <label>
          <span>群名称</span>
          <input v-model="title" placeholder="项目讨论组" />
        </label>
        <label>
          <span>成员 ID</span>
          <input v-model="memberIds" placeholder="u-bob,u-cindy" />
        </label>
        <button type="submit">创建</button>
      </form>

      <form v-else-if="mode === 'join'" class="modal-form" @submit.prevent="submitJoin">
        <label>
          <span>会话 ID</span>
          <input v-model="conversationId" placeholder="conversation_id" />
        </label>
        <button type="submit">进入</button>
      </form>

      <form v-else class="modal-form" @submit.prevent="submitDirect">
        <label>
          <span>对方用户名</span>
          <input v-model="peerUserId" placeholder="u-bob" />
        </label>
        <button type="submit">开始私聊</button>
      </form>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{
  mode: 'create' | 'join' | 'direct';
}>();

const emit = defineEmits<{
  close: [];
  create: [title: string, memberIds: string[]];
  join: [conversationId: string];
  direct: [peerUserId: string];
}>();

const title = ref('demo-group');
const memberIds = ref('u-bob,u-cindy');
const conversationId = ref('');
const peerUserId = ref('u-bob');
const modeLabel = computed(() => {
  if (props.mode === 'create') {
    return 'Create';
  }
  return props.mode === 'join' ? 'Join' : 'Direct';
});
const titleLabel = computed(() => {
  if (props.mode === 'create') {
    return '建群';
  }
  return props.mode === 'join' ? '加群' : '私聊';
});

function submitCreate(): void {
  const members = memberIds.value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  if (members.length === 0) {
    return;
  }
  emit('create', title.value.trim() || 'untitled-group', members);
  emit('close');
}

function submitJoin(): void {
  const id = conversationId.value.trim();
  if (!id) {
    return;
  }
  emit('join', id);
  emit('close');
}

function submitDirect(): void {
  const id = peerUserId.value.trim();
  if (!id) {
    return;
  }
  emit('direct', id);
  emit('close');
}

watch(
  () => props.mode,
  () => {
    conversationId.value = '';
    peerUserId.value = 'u-bob';
  }
);
</script>
