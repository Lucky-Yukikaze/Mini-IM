import type { MessageItem } from '../types';

export function deliveryLabel(item: MessageItem, currentUserId: string, conversationType: string): string {
  if (item.senderId !== currentUserId || item.recalled || item.burned || item.unreadCount === 0) return '';
  if (item.readCountKnown === false) return '';
  const recipients = (item.deliveries ?? []).filter(delivery => delivery.userId !== item.senderId);
  const received = recipients.filter(delivery => delivery.status === 'delivered' || delivery.status === 'read');
  if (conversationType === 'direct') {
    if (item.unreadCount === 0 || received.some(delivery => delivery.status === 'read')) return '';
    if (received.length) return '已送达';
    if (recipients.some(delivery => delivery.status === 'failed')) return '投递失败';
    return '等待送达';
  }
  return received.length ? `${received.length} 人已送达` : '等待送达';
}
