<template>
  <div class="modal-layer" @click.self="close">
    <section class="modal">
      <header class="modal-header">
        <div>
          <p class="section-kicker">{{ modeLabel }}</p>
          <h2>{{ titleLabel }}</h2>
        </div>
        <button class="icon-button" :disabled="submitting" @click="close">关闭</button>
      </header>

      <form v-if="mode === 'create'" class="modal-form" @submit.prevent="submitCreate">
        <label>
          <span>群名称</span>
          <input :disabled="submitting" v-model="title" placeholder="项目讨论组" />
        </label>
        <label>
          <span>成员 ID</span>
          <input :disabled="submitting" v-model="memberIds" placeholder="u-bob,u-cindy" />
        </label>
        <button type="submit" :disabled="disabled || submitting">创建</button>
      </form>

      <form v-else-if="mode === 'join'" class="modal-form" @submit.prevent="submitJoin">
        <label>
          <span>会话 ID</span>
          <input :disabled="submitting" v-model="conversationId" placeholder="conversation_id" />
        </label>
        <button type="submit" :disabled="disabled || submitting">进入</button>
      </form>

      <form v-else class="modal-form" @submit.prevent="submitDirect">
        <label>
          <span>对方用户名</span>
          <input :disabled="submitting" v-model="peerUserId" placeholder="u-bob" />
        </label>
        <button type="submit" :disabled="disabled || submitting">开始私聊</button>
      </form>
      <p v-if="submitting" role="status">正在提交…</p>
      <p v-if="error" role="alert">{{ error }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{
  mode: 'create' | 'join' | 'direct';
  disabled: boolean;
  create: (title: string, members: string[]) => Promise<boolean>;
  join: (conversationId: string) => Promise<boolean>;
  direct: (peerUserId: string) => Promise<boolean>;
}>();

const emit = defineEmits<{
  close: [];
}>();

const submitting = ref(false);
const error = ref('');
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
  void submit(() => props.create(title.value.trim() || 'untitled-group', members));
}

function submitJoin(): void {
  const id = conversationId.value.trim();
  if (!id) {
    return;
  }
  void submit(() => props.join(id));
}

function submitDirect(): void {
  const id = peerUserId.value.trim();
  if (!id) {
    return;
  }
  void submit(() => props.direct(id));
}

function close(): void {
  if (!submitting.value) emit('close');
}

async function submit(action: () => Promise<boolean>): Promise<void> {
  if (submitting.value || props.disabled) return;
  submitting.value = true;
  error.value = '';
  try {
    if (await action()) emit('close');
    else error.value = '操作未保存，请检查连接和输入后重试';
  } catch {
    error.value = '提交失败，请重试';
  } finally {
    submitting.value = false;
  }
}

watch(
  () => props.mode,
  () => {
    conversationId.value = '';
    peerUserId.value = 'u-bob';
  }
);
</script>
