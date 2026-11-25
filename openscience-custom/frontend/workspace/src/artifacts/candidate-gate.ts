export function gateDescription(gate?: string, reason?: string) {
  const labels: Record<string, string> = {
    A: 'Gate A：物理检查通过，且有较高的跨方法构象一致性证据。',
    B1: 'Gate B1：必要证据完整，但存在软物理警告或构象一致性未达到较高标准。',
    B2: 'Gate B2：必要评价证据不完整，不能视为全部门控通过。',
    C: 'Gate C：至少一项硬物理检查失败，应保留失败原因，不作为通过候选。',
  }
  return `${labels[gate ?? ''] ?? '门控未评价：不能据此判断通过或失败。'}${reason ? ` 原因：${reason}` : ''}`
}

export function consensusLabel(value?: string) {
  if (value === 'HIGH') return '较高'
  if (value === 'LOW') return '较低（已评价）'
  return value && !['NA', 'UNKNOWN'].includes(value) ? `原始结论：${value}` : '未评价 / 证据缺失'
}
