import { useState } from 'react';
import { Alert, Button, Card, Collapse, Descriptions, Input, Space, Tag, Typography } from 'antd';
import type { WebApproval } from '../types';

interface Props {
  review: WebApproval;
  busy: boolean;
  disabled?: boolean;
  onReview: (approved: boolean, reason?: string) => Promise<void>;
}

export default function ApprovalReview({ review, busy, disabled, onReview }: Props) {
  const [reason, setReason] = useState('');
  const pending = ['pending', 'waiting', 'waiting_approval', 'needs_confirmation'].includes(review.status.toLowerCase());
  const executionLabels = { not_started: '尚未执行', executing: '执行中', succeeded: '命令执行成功', failed: '命令执行失败' };
  const labels: Record<string, string> = {
    pending: '待审查', waiting: '待审查', waiting_approval: '待审查', needs_confirmation: '待审查',
    approved: '已批准', rejected: '已拒绝，流程已终止', completed: '执行已完成',
    failed: '执行失败', executing: '执行中', expired: '审批已失效',
  };
  const content = (
    <>
      <Descriptions column={1} size="small">
        <Descriptions.Item label="审批状态"><Tag>{labels[review.status.toLowerCase()] || review.status}</Tag></Descriptions.Item>
        {review.execution_status && <Descriptions.Item label="执行状态">{executionLabels[review.execution_status]}</Descriptions.Item>}
        <Descriptions.Item label="审批 ID">{review.approval_id}</Descriptions.Item>
        <Descriptions.Item label="目标主机">{review.target_host || '未提供'}</Descriptions.Item>
        <Descriptions.Item label="SSH 账号">{review.ssh_user || '未提供'}</Descriptions.Item>
        <Descriptions.Item label="操作">{review.operation}</Descriptions.Item>
        <Descriptions.Item label="风险">{review.risk}</Descriptions.Item>
        <Descriptions.Item label="影响">{review.impact}</Descriptions.Item>
      </Descriptions>
      <Typography.Text strong>{pending ? '实际待执行命令' : '审批命令记录'}</Typography.Text>
      {review.commands.map((command, index) => (
        <pre key={index} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', background: '#f5f5f5', padding: 12 }}>{command}</pre>
      ))}
      <Alert type="warning" showIcon message="批准后将执行以上命令；批准或执行完成均不代表业务已恢复。" style={{ marginBottom: 16 }} />
      {pending && review.channel === 'web' && (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.TextArea value={reason} onChange={event => setReason(event.target.value)} placeholder="拒绝理由（可选）" disabled={busy || disabled} />
          <Space>
            <Button type="primary" loading={busy} disabled={disabled || busy || !review.commands.length} onClick={() => void onReview(true)}>批准执行</Button>
            <Button danger disabled={busy || disabled} onClick={() => void onReview(false, reason.trim() || undefined)}>拒绝并终止</Button>
          </Space>
        </Space>
      )}
      {review.execution_result != null && (
        <Collapse style={{ marginTop: 16 }} items={[{
          key: 'execution', label: '查看原始执行结果', children:
            <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 360, overflow: 'auto' }}>{JSON.stringify(review.execution_result, null, 2)}</pre>,
        }]} />
      )}
    </>
  );
  if (pending || busy || review.execution_status === 'executing') {
    return <Card title="命令人工审查" style={{ marginBottom: 16 }}>{content}</Card>;
  }
  return (
    <Collapse style={{ marginBottom: 16 }} items={[{
      key: review.approval_id,
      label: ['操作审批记录', labels[review.status.toLowerCase()] || review.status,
        review.execution_status && executionLabels[review.execution_status]].filter(Boolean).join(' · '),
      children: content,
    }]} />
  );
}
