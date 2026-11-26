/* Independent, deterministic model for synthetic issues. Shared by browser and CLI. */
const TriageLab = (() => {
  'use strict';
  const modes = Object.freeze(['dry', 'semi', 'full', 'yolo', 'paused']);
  const lanes = Object.freeze({ correctness: '边界与兼容', minimal: '最小改动', consistency: '规范与文档' });
  const labels = Object.freeze({
    paused: '已暂停', rejected: '审核退回', build_failed: '构建待修复', rework: '审查要求返工',
    incomplete: '等待审查', final_rework: '终审要求返工', archived: '已归档草稿',
    awaiting_merge: '等待人工合并', merged: '已模拟合并'
  });
  const actions = Object.freeze({
    tag: '标记与分诊回复', publish: '推送分支并创建 PR',
    reply: '发布终审回复', merge: 'Squash 合并 PR'
  });
  function oneOf(value, allowed, field) {
    if (!allowed.includes(value)) throw new Error(field + ' 的取值无效');
    return value;
  }
  function boundedText(value, max, field) {
    if (typeof value !== 'string' || !value.trim() || value.length > max) throw new Error(field + ' 为空或超出长度限制');
    return value.trim();
  }
  function evaluate(raw) {
    if (!raw || typeof raw !== 'object') throw new Error('请输入工单对象');
    const input = {
      issue: {
        title: boundedText(raw.issue?.title, 160, '工单标题'),
        body: boundedText(raw.issue?.body, 4000, '工单正文')
      },
      policy: {},
      audit: oneOf(raw.audit, ['accept', 'reject'], '前置审核'),
      build: oneOf(raw.build, ['pass', 'fail'], '构建'),
      reviews: {},
      final: oneOf(raw.final, ['pass', 'fail'], '终审')
    };
    for (const key of ['global', 'project', 'issue']) {
      input.policy[key] = oneOf(raw.policy?.[key], key === 'global' ? modes : ['inherit', ...modes], key);
    }
    for (const [key, name] of Object.entries(lanes)) {
      input.reviews[key] = oneOf(raw.reviews?.[key], ['pass', 'fail', 'pending'], name);
    }
    const source = ['issue', 'project', 'global'].find(key => input.policy[key] !== 'inherit');
    const effective = { source, mode: input.policy[source] };
    const trace = [];
    const plan = Object.fromEntries(Object.keys(actions).map(key => [key, 'skipped']));
    const step = (stage, result, reason) => trace.push({ stage, result, reason });
    const finish = outcome => ({ schema: 'triage-lab/v1', input, effective, trace, plan, outcome });
    const writable = effective.mode !== 'dry';
    step('权限解析', effective.mode, { global: '全局默认', project: '项目覆盖', issue: '工单覆盖' }[source]);
    if (effective.mode === 'paused') {
      step('环节边界', 'paused', '当前工单暂停，尚未开始审核');
      return finish('paused');
    }
    step('前置审核', input.audit, '采用场景提供的审核意见');
    plan.tag = writable ? 'simulate' : 'draft';
    if (input.audit === 'reject') return finish('rejected');
    step('分诊与修复', 'simulated', '读取模拟修复提案');
    step('构建门禁', input.build, '采用场景提供的构建结果');
    if (input.build === 'fail') return finish('build_failed');
    for (const [key, name] of Object.entries(lanes)) step(name, input.reviews[key], '独立意见');
    const reviews = Object.values(input.reviews);
    if (reviews.includes('fail')) return finish('rework');
    if (reviews.includes('pending')) return finish('incomplete');
    step('终审', input.final, '采用场景提供的终审意见');
    if (input.final === 'fail') return finish('final_rework');
    plan.publish = plan.reply = writable ? 'simulate' : 'draft';
    plan.merge = effective.mode === 'yolo' ? 'simulate' : 'manual';
    if (!writable) return finish('archived');
    return finish(plan.merge === 'manual' ? 'awaiting_merge' : 'merged');
  }
  return Object.freeze({ modes, lanes, actions, labels, evaluate });
})();
if (typeof module !== 'undefined' && module.exports) module.exports = TriageLab;
