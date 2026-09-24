/** 批次状态机共享口径:所有页面必须使用同一套枚举、标签与允许的转移。 */

export const BATCH_STATUS = {
  ACTIVE: 'active',
  HARVESTED: 'harvested',
  CLOSED: 'closed',
} as const;

export const BATCH_STATUS_LABELS: Record<string, string> = {
  active: '养殖中',
  harvested: '已收获',
  closed: '已关闭(已出塘)',
};

export const BATCH_STATUS_BADGES: Record<string, string> = {
  active: 'badge-success',
  harvested: 'badge-info',
  closed: 'badge-warning',
};

export const ACTION_LABELS: Record<string, string> = {
  create: '创建',
  update: '编辑',
  harvest: '登记收获',
  close: '关闭出塘',
  reopen: '重开批次',
  revert_harvested: '回退为养殖中',
  revert_harvest: '回退为养殖中',
  transfer: '跨塘转移',
  normalize: '系统归一',
};

/** 状态机允许的目标状态(键=当前状态)。 */
export const ALLOWED_TARGET_STATUSES: Record<string, string[]> = {
  active: ['active', 'harvested'],
  harvested: ['harvested', 'active', 'closed'],
  closed: ['closed', 'harvested'],
};

/** 哪些目标状态属于必须填写原因的回退/重开。 */
export function isReversal(from?: string, to?: string): boolean {
  return (from === 'harvested' && to === 'active') ||
         (from === 'closed' && to === 'harvested');
}

export function batchStatusLabel(status: string): string {
  return BATCH_STATUS_LABELS[status] || status;
}

export function batchStatusBadge(status: string): string {
  return BATCH_STATUS_BADGES[status] || 'badge-info';
}

export function isClosed(status: string): boolean {
  return status === BATCH_STATUS.CLOSED;
}
