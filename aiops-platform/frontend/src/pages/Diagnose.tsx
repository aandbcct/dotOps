import { useState, useEffect, useRef } from 'react';
import { isAxiosError } from 'axios';
import { Alert, Card, Grid, Input, Button, Tag, Steps, Spin, message, Typography, Collapse, Descriptions, Space } from 'antd';
import { SendOutlined } from '@ant-design/icons';

import { agentApi } from '../services/api';
import type { AgentTask, MultiAgentResponse } from '../types';
import ApprovalReview from '../components/ApprovalReview';

const { TextArea } = Input;
const { Text, Paragraph } = Typography;
const { Panel } = Collapse;

const SESSION_KEY = 'dotops-diagnosis-session';

function errorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail ?? error.response?.data?.message;
    if (error.response?.status === 404) return '诊断会话已失效，可能是服务重启导致检查点丢失。请重新发起诊断。';
    return typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : error.message;
  }
  return error instanceof Error ? error.message : '请求失败，请重试';
}

function mapResponse(data: MultiAgentResponse, query = ''): AgentTask {
  const stages = data.stages || {};
  const dynamic = stages.dynamic_execution || {};
  const analysis = stages.observability_analysis || {};
  const knowledge = stages.knowledge_query || {};
  const skills = stages.skill_matching || {};
  const reviewStatus = data.review?.status.toLowerCase();
  const dynamicStatus = dynamic.status || data.status || '';
  let status = 'processing';
  if (reviewStatus === 'rejected' || dynamicStatus === 'rejected') status = 'rejected';
  else if (reviewStatus === 'failed' || data.review?.execution_status === 'failed' || ['failed', 'error'].includes(dynamicStatus)) status = 'failed';
  else if ((reviewStatus && ['pending', 'waiting', 'waiting_approval', 'needs_confirmation'].includes(reviewStatus))
    || data.confirmation_kind || ['needs_confirmation', 'waiting_approval'].includes(dynamicStatus)
    || data.final_decision?.decision === 'NEEDS_CONFIRMATION') status = 'needs_confirmation';
  else if (['completed', 'success', 'terminated'].includes(dynamicStatus) || data.final_decision?.is_final) status = 'completed';
  else if (['incomplete', 'expired', 'cancelled'].includes(dynamicStatus)) status = 'failed';
  return {
    task_id: data.session_id, session_id: data.session_id, review: data.review,
    diagnosis_assessment: data.diagnosis_assessment, diagnosis_result: data.diagnosis_result, execution_history: data.execution_history,
    confirmation_kind: data.confirmation_kind, user_input: data.query || data.user_input || query, status,
    intent_data: stages.intent_parsing || null,
    analysis_report: { analysis_report: analysis.analysis_report || data.raw_response || '', diagnosis_plan: analysis.diagnosis_plan },
    knowledge_context: { knowledge_report: knowledge.knowledge_report || '', service: knowledge.service || '' },
    decision: data.final_decision || null, action_result: data.execution_result || null,
    created_at: data.start_time || '', updated_at: data.end_time || '',
    warning_cleared: data.warning_cleared, server_status_check: stages.server_status_check,
    mode: data.mode, iterations: dynamic.iterations, diagnosis_plan: analysis.diagnosis_plan,
    execution_outputs: dynamic.command_entries || [],
    skill_matching: { matched_skills: skills.matched_skills || [], skill_summary: skills.skill_summary || '', skills_preview: skills.skills_preview || '' },
    email_approval: dynamic.email_approval, raw_response: data.raw_response, saved_to: data.saved_to,
    data_mode: data.data_mode, data_sources: data.data_sources, stage_progress: data.stage_progress,
  };
}

const Diagnose = () => {
  const screens = Grid.useBreakpoint();
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentTask, setCurrentTask] = useState<AgentTask | null>(null);
  const [error, setError] = useState('');
  const [sessionExpired, setSessionExpired] = useState(false);
  const [sshUser, setSshUser] = useState('');
  const inFlight = useRef(false);

  const applyResponse = (data: MultiAgentResponse, query?: string) => {
    if (!data.session_id) throw new Error('响应缺少诊断会话 ID，无法继续此流程');
    sessionStorage.setItem(SESSION_KEY, data.session_id);
    setCurrentTask(mapResponse(data, query));
    setSessionExpired(false);
  };

  useEffect(() => {
    const sessionId = sessionStorage.getItem(SESSION_KEY);
    if (!sessionId) return;
    let active = true;
    setLoading(true);
    inFlight.current = true;
    agentApi.getSession(sessionId).then(data => {
      if (active) { applyResponse(data); setInput(data.query || data.user_input || ''); }
    }).catch(error => {
      if (active) {
        setError(errorMessage(error));
        setSessionExpired(isAxiosError(error) && error.response?.status === 404);
      }
    }).finally(() => {
      if (active) { setLoading(false); inFlight.current = false; }
    });
    return () => { active = false; };
  }, []);

  const runAction = async (action: () => Promise<MultiAgentResponse>, query?: string) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    setError('');
    try { applyResponse(await action(), query); }
    catch (error) {
      setError(errorMessage(error));
      if (isAxiosError(error) && error.response?.status === 404) setSessionExpired(true);
    } finally { setLoading(false); inFlight.current = false; }
  };

  const handleSubmit = async () => {
    if (!input.trim()) { message.warning('请输入故障描述'); return; }
    if (inFlight.current) return;
    const sessionId = crypto.randomUUID();
    sessionStorage.setItem(SESSION_KEY, sessionId);
    setCurrentTask(null);
    setSessionExpired(false);
    await runAction(() => agentApi.process(input.trim(), sessionId), input.trim());
  };

  const handleReview = async (approved: boolean, reason?: string) => {
    if (!currentTask?.session_id || !currentTask.review || sessionExpired) return;
    await runAction(() => agentApi.review(currentTask.session_id!, currentTask.review!.approval_id, approved, reason), currentTask.user_input);
  };

  const confirmation = currentTask?.decision?.confirmation_request;
  const needsSshUser = currentTask?.status === 'needs_confirmation' && !currentTask.review && (
    currentTask.confirmation_kind === 'ssh_user' || (!currentTask.confirmation_kind && (
      confirmation?.requires_ssh_user || confirmation?.type === 'ssh_user' ||
      /ssh.*(用户|账号|user)|username/i.test(`${confirmation?.operation || ''} ${confirmation?.message || ''}`)
    ))
  );
  const getRiskColor = (risk: string) => {
    const colors: Record<string, string> = { HIGH: 'red', MEDIUM: 'orange', LOW: 'green' };
    return colors[risk] || 'default';
  };

  const getIntentColor = (intent: string) => {
    const colors: Record<string, string> = { 
      DIAGNOSE: 'blue', 
      QUERY_STATUS: 'green', 
      EXECUTE_FIX: 'orange', 
      GENERAL_QA: 'purple' 
    };
    return colors[intent] || 'default';
  };

  return (
    <div style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Card title="故障诊断" style={{ marginBottom: 16 }}>
        <Space.Compact style={{ width: '100%' }}>
          <TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="描述故障现象，例如：订单服务最近响应很慢，经常超时"
            autoSize={{ minRows: 2, maxRows: 4 }}
            style={{ flex: 1 }}
          />
          <Button 
            type="primary" 
            icon={<SendOutlined />} 
            onClick={handleSubmit}
            loading={loading}
            style={{ height: 'auto' }}
          >
            诊断
          </Button>
        </Space.Compact>
        {sessionStorage.getItem(SESSION_KEY) && (
          <Button style={{ marginTop: 12 }} disabled={loading || sessionExpired} onClick={() => {
            const sessionId = sessionStorage.getItem(SESSION_KEY);
            if (sessionId) void runAction(() => agentApi.getSession(sessionId), currentTask?.user_input);
          }}>刷新当前诊断状态</Button>
        )}
      </Card>

      {currentTask && (
        <Card title={currentTask.diagnosis_assessment ? '诊断结论' : '诊断状态'} style={{ marginBottom: 16 }}>
          <Space wrap style={{ marginBottom: 16 }}>
            <Tag color={currentTask.diagnosis_assessment?.status === 'confirmed' ? 'blue' : 'orange'}>
              {currentTask.diagnosis_assessment
                ? { confirmed: '已有根因支持证据', suspected: '待验证推测', inconclusive: '证据不足' }[currentTask.diagnosis_assessment.status]
                : currentTask.status === 'needs_confirmation' ? '等待人工确认'
                  : currentTask.status === 'rejected' ? '已拒绝，流程已终止'
                    : currentTask.status === 'failed' ? '诊断或命令执行失败'
                      : currentTask.status === 'completed' ? '流程已结束，尚无诊断评估' : '正在处理中'}
            </Tag>
            {currentTask.diagnosis_assessment && (
              <Tag>{({ needs_confirmation: '等待人工确认', rejected: '已拒绝，流程已终止', failed: '诊断或命令执行失败', completed: '流程已结束', processing: '正在处理中' } as Record<string, string>)[currentTask.status] || currentTask.status}</Tag>
            )}
            <Text type="secondary">{currentTask.data_mode === 'mock' ? '演示数据' : '实际诊断流程'} · 流程结束不等于业务已恢复</Text>
          </Space>
          {currentTask.diagnosis_assessment ? (
            <Descriptions column={1} size="small">
              <Descriptions.Item label="问题">{currentTask.diagnosis_assessment.problem || '未提供'}</Descriptions.Item>
              <Descriptions.Item label="关键证据">
                {currentTask.diagnosis_assessment.evidence.length ? (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    {currentTask.diagnosis_assessment.evidence.map((item, index) => (
                      <div key={`${item.id}-${index}`}>
                        <Text>{item.observation}</Text>
                        <div><Text type="secondary">{[item.id, item.tool, item.source].filter(Boolean).join(' · ')}</Text></div>
                      </div>
                    ))}
                  </Space>
                ) : '未提供可核对的关键证据'}
              </Descriptions.Item>
              <Descriptions.Item label="影响">{currentTask.diagnosis_assessment.impact || '未提供'}</Descriptions.Item>
              <Descriptions.Item label="下一步">{currentTask.diagnosis_assessment.next_steps || '未提供'}</Descriptions.Item>
              {currentTask.diagnosis_assessment.limitations.length > 0 && (
                <Descriptions.Item label="仍待核对">
                  <Space direction="vertical">{currentTask.diagnosis_assessment.limitations.map((item, index) => <Text key={index}>{item}</Text>)}</Space>
                </Descriptions.Item>
              )}
            </Descriptions>
          ) : (
            <Paragraph style={{ marginBottom: 0 }}>
              {currentTask.status === 'needs_confirmation' ? '完成下方人工确认后，将继续同一诊断流程。'
                : currentTask.status === 'rejected' ? '本次执行已被拒绝，未形成最终诊断评估。'
                  : currentTask.status === 'failed' ? '请展开诊断详情核对失败原因。'
                    : currentTask.status === 'completed' ? '后端未返回最终诊断评估，请展开诊断详情核对已有记录。'
                      : '诊断评估尚未形成，请等待流程返回结果。'}
            </Paragraph>
          )}
        </Card>
      )}
      {currentTask?.review && (
        <ApprovalReview key={currentTask.review.approval_id} review={currentTask.review} busy={loading} disabled={sessionExpired} onReview={handleReview} />
      )}
      {needsSshUser && currentTask?.session_id && (
        <Card title="补充 SSH 账号后继续诊断" style={{ marginBottom: 16 }}>
          <Paragraph>{confirmation?.message}</Paragraph>
          <Space.Compact style={{ width: '100%' }}>
            <Input value={sshUser} onChange={event => setSshUser(event.target.value)} placeholder="SSH 用户名" disabled={loading || sessionExpired} />
            <Button type="primary" loading={loading} disabled={!sshUser.trim() || loading || sessionExpired}
              onClick={() => void runAction(() => agentApi.provideSshUser(currentTask.session_id!, sshUser.trim()), currentTask.user_input)}>继续诊断</Button>
          </Space.Compact>
        </Card>
      )}
      {currentTask?.status === 'needs_confirmation' && !currentTask.review && !needsSshUser && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }} message="流程等待确认"
          description={confirmation?.message || currentTask.email_approval?.message || '未返回网页审批记录，当前无法提交审批。'} />
      )}
      {currentTask && (currentTask.status !== 'processing') && (
        <Collapse style={{ marginBottom: 16 }}>
          <Panel header="诊断详情：流程、模型分析与原始执行记录" key="details">
          <Collapse defaultActiveKey={[]} style={{ marginBottom: 16 }}>
            <Panel header="流程节点与数据来源" key="progress">
              <Steps size="small" direction={screens.xl ? 'horizontal' : 'vertical'} responsive
                items={(currentTask.stage_progress || []).map(step => ({
                  title: step.title, status: step.status,
                  description: step.status === 'finish' ? '节点已运行' : '未执行',
                }))} />
              <Space wrap style={{ marginTop: 16 }}>
                {Object.entries(currentTask.data_sources || {}).map(([key, source]) => (
                  <Tag key={key}>{key}: {source === 'model_generated' ? '模型建议' : source === 'mock_data' ? '模拟数据' : source}</Tag>
                ))}
              </Space>
              {currentTask.data_mode === 'mock' && <Alert style={{ marginTop: 16 }} type="warning" message="演示模式：可能包含模拟数据" />}
            </Panel>
            <Panel header="意图识别结果" key="1">
              {currentTask.intent_data && (
                <Descriptions column={2} size="small">
                  <Descriptions.Item label="意图">
                    <Tag color={getIntentColor(currentTask.intent_data.intent)}>
                      {currentTask.intent_data.intent}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="置信度">
                    <Tag color={currentTask.intent_data.confidence === 'HIGH' ? 'green' : 'orange'}>
                      {currentTask.intent_data.confidence}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="服务">{currentTask.intent_data.entities.service}</Descriptions.Item>
                  <Descriptions.Item label="症状">{currentTask.intent_data.entities.symptom}</Descriptions.Item>
                  <Descriptions.Item label="标准化查询">{currentTask.intent_data.normalized_query}</Descriptions.Item>
                </Descriptions>
              )}
            </Panel>

            {currentTask.skill_matching && (
              <Panel header="Skill 匹配结果" key="8">
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="匹配到的技能">
                    {currentTask.skill_matching.matched_skills.map((s: string) => (
                      <Tag key={s} color="blue" style={{ marginBottom: 4 }}>{s}</Tag>
                    ))}
                  </Descriptions.Item>
                  <Descriptions.Item label="匹配摘要">{currentTask.skill_matching.skill_summary}</Descriptions.Item>
                </Descriptions>
                {currentTask.skill_matching.skills_preview && (
                  <Paragraph style={{ marginTop: 12, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 12, backgroundColor: '#f5f5f5', padding: 12, borderRadius: 4, maxHeight: 200, overflow: 'auto' }}>
                    {currentTask.skill_matching.skills_preview}
                  </Paragraph>
                )}
              </Panel>
            )}

            {currentTask.ansible_playbook && (
              <Panel header="生成的 Ansible Playbook" key="6">
                <Descriptions column={2} size="small">
                  <Descriptions.Item label="目标主机">{currentTask.ansible_playbook.target_host}</Descriptions.Item>
                  <Descriptions.Item label="症状">{currentTask.ansible_playbook.symptoms?.join(', ')}</Descriptions.Item>
                  <Descriptions.Item label="检查指标">{currentTask.ansible_playbook.metrics?.join(', ')}</Descriptions.Item>
                </Descriptions>
              </Panel>
            )}

            {currentTask.server_status_check && (
              <Panel header="服务器状态检查结果" key="7">
                <Descriptions column={2} size="small">
                  <Descriptions.Item label="检查状态">
                    <Tag color={currentTask.server_status_check.success ? 'green' : 'red'}>
                      {currentTask.server_status_check.success ? '成功' : '失败'}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="警告状态">
                    <Tag color={currentTask.server_status_check.warning_cleared ? 'green' : 'orange'}>
                      {currentTask.server_status_check.warning_cleared ? '已解除' : '存在异常'}
                    </Tag>
                  </Descriptions.Item>
                  {currentTask.server_status_check.memory_usage !== undefined && (
                    <Descriptions.Item label="内存使用率">
                      <Tag color={currentTask.server_status_check.memory_usage > 80 ? 'red' : currentTask.server_status_check.memory_usage > 60 ? 'orange' : 'green'}>
                        {currentTask.server_status_check.memory_usage}%
                      </Tag>
                    </Descriptions.Item>
                  )}
                  {currentTask.server_status_check.cpu_usage !== undefined && (
                    <Descriptions.Item label="CPU 负载">{currentTask.server_status_check.cpu_usage}</Descriptions.Item>
                  )}
                  {currentTask.server_status_check.disk_usage !== undefined && (
                    <Descriptions.Item label="磁盘使用率">{currentTask.server_status_check.disk_usage}%</Descriptions.Item>
                  )}
                  {currentTask.server_status_check.shm_usage !== undefined && (
                    <Descriptions.Item label="/dev/shm 使用率">
                      <Tag color={currentTask.server_status_check.shm_usage >= 100 ? 'red' : currentTask.server_status_check.shm_usage >= 90 ? 'orange' : 'green'}>
                        {currentTask.server_status_check.shm_usage}%
                      </Tag>
                    </Descriptions.Item>
                  )}
                </Descriptions>
                {currentTask.server_status_check.anomalies && currentTask.server_status_check.anomalies.length > 0 && (
                  <div style={{ marginTop: 16 }}>
                    <Text strong>发现的异常：</Text>
                    <ul>
                      {currentTask.server_status_check.anomalies.map((anomaly, index) => (
                        <li key={index} style={{ color: '#ff4d4f' }}>{anomaly}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </Panel>
            )}

            {currentTask.diagnosis_plan && (
              <Panel header={`诊断计划 (${currentTask.iterations} 次迭代)`} key="10">
                <Descriptions column={2} size="small">
                  <Descriptions.Item label="计划名称">{currentTask.diagnosis_plan.plan_name}</Descriptions.Item>
                  <Descriptions.Item label="检查类型">
                    <Tag color="blue">{currentTask.diagnosis_plan.check_type}</Tag>
                  </Descriptions.Item>
                </Descriptions>
                <div style={{ marginTop: 16 }}>
                  <Text strong>选择原因：</Text>
                  <Paragraph style={{ marginTop: 8, whiteSpace: 'pre-wrap' }}>
                    {currentTask.diagnosis_plan.reasoning}
                  </Paragraph>
                </div>
                <div style={{ marginTop: 16 }}>
                  <Text strong>执行命令：</Text>
                  <ul style={{ marginTop: 8 }}>
                    {currentTask.diagnosis_plan.commands.map((cmd, index) => (
                      <li key={index}>
                        <Tag color="geekblue">{cmd}</Tag>
                      </li>
                    ))}
                  </ul>
                </div>
              </Panel>
            )}

            {currentTask.execution_outputs && currentTask.execution_outputs.length > 0 && (
              <Panel header="诊断命令执行结果" key="9">
                {currentTask.execution_outputs.map((exec, index) => (
                  <Card key={index} size="small" style={{ marginBottom: 16 }} title={
                    <Space>
                      <Tag color={exec.success ? 'green' : 'red'}>
                        {exec.success ? '成功' : '失败'}
                      </Tag>
                      <Text code>{exec.command}</Text>
                    </Space>
                  }>
                    <Paragraph style={{ 
                      whiteSpace: 'pre-wrap', 
                      fontFamily: 'monospace',
                      backgroundColor: '#f5f5f5',
                      padding: 12,
                      borderRadius: 4,
                      maxHeight: 300,
                      overflow: 'auto'
                    }}>
                      {exec.output || '无输出'}
                    </Paragraph>
                  </Card>
                ))}
              </Panel>
            )}

            {currentTask.saved_outputs && currentTask.saved_outputs.length > 0 && (
              <Panel header="保存的中间文件" key="saved">
                <div>
                  {currentTask.saved_outputs.map((item: any, index: number) => (
                    <div key={index} style={{ padding: '8px 0', borderBottom: index < currentTask.saved_outputs!.length - 1 ? '1px solid #f0f0f0' : 'none' }}>
                      <Space>
                        <Tag color="green">已保存</Tag>
                        <Text code>{item.saved_to}</Text>
                        {item.command && <Text type="secondary">({item.command})</Text>}
                      </Space>
                    </div>
                  ))}
                </div>
              </Panel>
            )}

            <Panel header="模型分析与诊断计划（非执行证据）" key="2">
              {currentTask.analysis_report && (
                <div>
                  {currentTask.analysis_report.diagnosis_plan && (
                    <>
                      <Descriptions column={2} size="small" title="诊断计划">
                        <Descriptions.Item label="计划名称">
                          <Tag color="blue">{currentTask.analysis_report.diagnosis_plan.plan_name}</Tag>
                        </Descriptions.Item>
                        <Descriptions.Item label="检查类型">
                          <Tag>{currentTask.analysis_report.diagnosis_plan.check_type}</Tag>
                        </Descriptions.Item>
                      </Descriptions>
                      <Paragraph style={{ marginTop: 12, whiteSpace: 'pre-wrap' }}>
                        <Text strong>诊断逻辑：</Text>
                        {currentTask.analysis_report.diagnosis_plan.reasoning}
                      </Paragraph>
                    </>
                  )}
                  <Paragraph style={{ marginTop: 12, whiteSpace: 'pre-wrap' }}>
                    {currentTask.analysis_report.analysis_report}
                  </Paragraph>
                </div>
              )}
            </Panel>

            <Panel header="知识查询记录" key="3">
              {currentTask.knowledge_context && (
                <Paragraph style={{ whiteSpace: 'pre-wrap' }}>
                  {currentTask.knowledge_context.knowledge_report}
                </Paragraph>
              )}
            </Panel>

            <Panel header="模型原始决策（待核对）" key="4">
              {currentTask.decision && (
                <div>
                  {currentTask.decision.is_final ? (
                    <>
                      <Descriptions column={2} size="small">
                        <Descriptions.Item label="问题类型">
                          <Tag color="blue">{currentTask.decision.problem_type}</Tag>
                        </Descriptions.Item>
                        <Descriptions.Item label="风险等级">
                          <Tag color={getRiskColor(currentTask.decision.risk_level)}>
                            {currentTask.decision.risk_level}
                          </Tag>
                        </Descriptions.Item>
                      </Descriptions>
                      <Paragraph style={{ marginTop: 16 }}>
                        <Text strong>根本原因：</Text>{currentTask.decision.root_cause}
                      </Paragraph>
                      <Paragraph>
                        <Text strong>影响范围：</Text>{currentTask.decision.impact}
                      </Paragraph>
                      <Paragraph>
                        <Text strong>建议操作：</Text>{currentTask.decision.recommendation}
                      </Paragraph>
                    </>
                  ) : (
                    <>
                      <Descriptions column={2} size="small">
                        <Descriptions.Item label="根因">{currentTask.decision.root_cause_summary}</Descriptions.Item>
                        <Descriptions.Item label="决策">
                          <Tag color={currentTask.decision.decision === 'EXECUTE_FIX' ? 'orange' : 'blue'}>
                            {currentTask.decision.decision}
                          </Tag>
                        </Descriptions.Item>
                        <Descriptions.Item label="风险等级">
                          <Tag color={getRiskColor(currentTask.decision.risk_level)}>
                            {currentTask.decision.risk_level}
                          </Tag>
                        </Descriptions.Item>
                      </Descriptions>
                      <Paragraph style={{ marginTop: 16, whiteSpace: 'pre-wrap' }}>
                        <Text strong>执行计划：</Text>{'\n'}{currentTask.decision.action_plan}
                      </Paragraph>
                      <Paragraph style={{ whiteSpace: 'pre-wrap' }}>
                        <Text strong>推理过程：</Text>{'\n'}{currentTask.decision.reasoning}
                      </Paragraph>
                    </>
                  )}
                </div>
              )}
            </Panel>

            {currentTask.action_result && (
            <Panel header="执行信息（不代表已完成修复）" key="5">
                <Descriptions column={2} size="small">
                  <Descriptions.Item label="工具">{currentTask.action_result.tool_name}</Descriptions.Item>
                  <Descriptions.Item label="模板">{currentTask.action_result.template_name}</Descriptions.Item>
                  <Descriptions.Item label="风险评估">
                    <Tag color={getRiskColor(currentTask.action_result.risk_assessment)}>
                      {currentTask.action_result.risk_assessment}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="需要审批">
                    <Tag color={currentTask.action_result.requires_approval ? 'red' : 'green'}>
                      {currentTask.action_result.requires_approval ? '是' : '否'}
                    </Tag>
                  </Descriptions.Item>
                </Descriptions>
                <Paragraph style={{ marginTop: 16 }}>
                  <Text strong>执行说明：</Text>{currentTask.action_result.execution_note}
                </Paragraph>
              </Panel>
            )}
            {(currentTask.diagnosis_result != null || currentTask.execution_history?.length) ? (
              <Panel header="原始诊断与执行数据" key="raw">
                <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 480, overflow: 'auto' }}>
                  {JSON.stringify({ diagnosis_result: currentTask.diagnosis_result, execution_history: currentTask.execution_history }, null, 2)}
                </pre>
              </Panel>
            ) : null}
          </Collapse>
          </Panel>
        </Collapse>
      )}


      {currentTask && currentTask.status === 'processing' && (
        <Card style={{ textAlign: 'center', padding: 40 }}>
          <Spin size="large" />
          <Paragraph style={{ marginTop: 16 }}>正在分析中，请稍候...</Paragraph>
        </Card>
      )}

    </div>
  );
};

export default Diagnose;
