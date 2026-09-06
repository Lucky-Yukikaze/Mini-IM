import { reactive } from 'vue';

const listeners = new Map<string, Set<(payload: unknown) => void>>();

export const bridgeEvents = reactive({
  on(eventName: string, handler: (payload: unknown) => void): () => void {
    const set = listeners.get(eventName) ?? new Set<(payload: unknown) => void>();
    set.add(handler);
    listeners.set(eventName, set);
    return () => set.delete(handler);
  },
  emit(eventName: string, payload: unknown): void {
    const set = listeners.get(eventName);
    if (!set) {
      return;
    }
    set.forEach((handler) => handler(payload));
  }
});
