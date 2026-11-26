(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const scopes = { global: '全局', project: '项目', issue: '工单' };
  const descriptions = {
    dry: '草稿归档，远端动作保留为计划。',
    semi: '模拟发布评论与 PR，由维护者合并。',
    full: '模拟发布评论与 PR，由维护者合并。当前动作与 SEMI 相同。',
    yolo: '所有门禁放行后，模拟自动合并。',
    paused: '在环节边界停止，保留输入供下一次运行使用。'
  };
  const words = {
    pass: '通过', fail: '返工', pending: '待返回', accept: '接收', reject: '退回',
    simulated: '模拟完成', simulate: '模拟执行', manual: '交给维护者', draft: '仅存草稿',
    skipped: '未进入', paused: '暂停'
  };
  const policy = { global: 'dry', project: 'inherit', issue: 'inherit' };
  const resolved = settings => {
    const source = ['issue', 'project', 'global'].find(key => settings[key] !== 'inherit');
    return { mode: settings[source], source };
  };
  let lastRun;
  let pendingPolicy;
  let pendingSelect;
  for (const [key, example] of Object.entries(TriageScenarios)) $('scenario').add(new Option(example.name, key));
  $('scenario').add(new Option('自定义工单', 'custom'));
  function makeSelect(id, title, choices) {
    const label = document.createElement('label');
    label.textContent = title;
    const select = document.createElement('select');
    select.id = id;
    for (const [value, text] of choices) select.add(new Option(text, value));
    label.append(select);
    return label;
  }
  function showPolicy() {
    for (const key of Object.keys(scopes)) $('p-' + key).value = policy[key];
    const { mode, source } = resolved(policy);
    $('effective').textContent = mode.toUpperCase() + ' · ' + scopes[source];
    $('mode-hint').textContent = descriptions[mode];
  }
  for (const [key, label] of Object.entries(scopes)) {
    const choices = TriageLab.modes.map(mode => [mode, mode.toUpperCase()]);
    if (key !== 'global') choices.unshift(['inherit', '继承上层']);
    const control = makeSelect('p-' + key, label, choices);
    $('policies').append(control);
    control.lastChild.addEventListener('change', event => {
      const value = event.target.value;
      const next = { ...policy, [key]: value };
      if (value === 'yolo' || (resolved(next).mode === 'yolo' && resolved(policy).mode !== 'yolo')) {
        pendingPolicy = next;
        pendingSelect = event.target;
        showPolicy();
        $('confirm-yolo').returnValue = 'cancel';
        $('confirm-yolo').showModal();
      } else {
        Object.assign(policy, next);
        showPolicy();
      }
      markDraft();
    });
  }
  $('confirm-yolo').addEventListener('close', () => {
    if ($('confirm-yolo').returnValue === 'confirm' && pendingPolicy) Object.assign(policy, pendingPolicy);
    showPolicy();
    pendingPolicy = undefined;
    pendingSelect?.focus();
    pendingSelect = undefined;
    markDraft();
  });
  for (const [key, name] of Object.entries(TriageLab.lanes)) {
    $('reviews').insertBefore(makeSelect(key, name, [['pass', '通过'], ['fail', '返工'], ['pending', '待返回']]), $('final').parentNode);
  }
  function loadScenario() {
    const fixture = TriageScenarios[$('scenario').value];
    if (!fixture) { $('issue-title').focus(); return; }
    $('issue-title').value = fixture.issue.title;
    $('issue-body').value = fixture.issue.body;
    for (const key of ['audit', 'build', 'final']) $(key).value = fixture[key];
    for (const key of Object.keys(TriageLab.lanes)) $(key).value = fixture.reviews[key];
    markDraft();
  }
  function readInputs() {
    return {
      issue: { title: $('issue-title').value.trim(), body: $('issue-body').value.trim() },
      policy: { ...policy }, audit: $('audit').value, build: $('build').value,
      reviews: Object.fromEntries(Object.keys(TriageLab.lanes).map(key => [key, $(key).value])),
      final: $('final').value
    };
  }
  function markDraft() {
    const dirty = !lastRun || JSON.stringify(readInputs()) !== JSON.stringify(lastRun.input);
    $('dirty').textContent = dirty ? '输入已更改，运行后更新记录。' : '记录已与当前输入同步。';
  }
  $('scenario').addEventListener('change', loadScenario);
  $('lab-form').addEventListener('input', event => {
    if (!['scenario'].includes(event.target.id) && !event.target.id.startsWith('p-')) $('scenario').value = 'custom';
    markDraft();
  });
  function item(title, status) {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = title;
    const badge = document.createElement('small');
    badge.textContent = words[status] || status.toUpperCase();
    badge.dataset.status = status;
    li.append(label, badge);
    return li;
  }
  function run() {
    lastRun = TriageLab.evaluate(readInputs());
    const { effective, input, trace, plan, outcome } = lastRun;
    $('outcome').textContent = $('scope-outcome').textContent = TriageLab.labels[outcome];
    $('source').textContent = effective.mode.toUpperCase() + ' / ' + scopes[effective.source] + '策略';
    $('record-title').textContent = input.issue.title;
    $('trace').replaceChildren(...trace.map(row => item(row.stage, row.result)));
    $('actions').replaceChildren(...Object.entries(plan).map(([key, status]) => item(TriageLab.actions[key], status)));
    $('export').disabled = false;
    markDraft();
    draw(performance.now());
  }
  $('lab-form').addEventListener('submit', event => {
    event.preventDefault();
    try {
      run();
      $('record').scrollIntoView({ block: 'start' });
    } catch (error) {
      $('dirty').textContent = error.message;
    }
  });
  $('export').addEventListener('click', () => {
    if (!lastRun) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(lastRun, null, 2) + '\n'], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'triage-decision.json';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  });

  const canvas = $('scope');
  const ctx = canvas.getContext('2d');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  function draw(time) {
    if (!ctx || !lastRun) return;
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(width * dpr), h = Math.round(height * dpr);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#152d24'; ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = '#354c3a';
    for (let y = 10; y < height; y += 14) for (let x = 10; x < width; x += 14) ctx.fillRect(x,y,1,1);
    const cx = width / 2, cy = height / 2, gap = height * .245;
    const box = Math.max(88, Math.min(146, width * .32)), left = cx - box / 2, right = cx + box / 2;
    const start = width * .09, end = width - start;
    ctx.lineWidth = 1.3;
    for (const [i, [key, name]] of Object.entries(TriageLab.lanes).entries()) {
      const lineY = cy + (i - 1) * gap;
      const stage = lastRun.trace.find(row => row.stage === name);
      const status = stage?.result;
      const color = status === 'pass' ? '#c6e880' : status === 'fail' ? '#ffab7d' : '#829d8a';
      ctx.strokeStyle = color;
      ctx.globalAlpha = .69;
      ctx.beginPath();ctx.moveTo(start,cy);
      ctx.bezierCurveTo(start+(left-start)*.56,cy,left-(left-start)*.60,lineY,left,lineY);
      ctx.moveTo(right,lineY);ctx.bezierCurveTo(right+(end-right)*.60,lineY,end-(end-right)*.56,cy,end,cy);ctx.stroke();
      ctx.globalAlpha = 1;
      const bob = reduced.matches ? .56 : ((time/3200 + i/3) % 1);
      const inv = 1 - bob;
      const x = start + (left-start) * (3*inv*inv*bob*.56 + 3*inv*bob*bob*.40 + bob*bob*bob);
      const y = cy + (lineY-cy) * (bob*bob*(3-2*bob));
      if (status && status !== 'pending') {
        ctx.fillStyle = color;ctx.beginPath();ctx.arc(x,y,2.4,0,Math.PI*2);ctx.fill();
      }
      const rh = Math.min(32, gap * .7);
      ctx.fillStyle = '#1d382b';ctx.fillRect(left,lineY-rh/2,box,rh);
      ctx.strokeStyle = '#56714c';ctx.strokeRect(left,lineY-rh/2,box,rh);
      ctx.fillStyle = color;ctx.fillRect(left + 8,lineY - 3,3,6);
      ctx.fillStyle = '#e1ead4';ctx.textAlign = 'center';ctx.textBaseline = 'middle';
      ctx.font = '500 '+(width < 420 ? 11 : 13)+'px "PingFang SC", sans-serif';ctx.fillText(TriageLab.lanes[key], cx+3,lineY);
    }
    for (const [x,label] of [[start,'修复提案'],[end,'终审']]) {
      ctx.fillStyle = '#142a21';ctx.beginPath();ctx.arc(x,cy,7,0,Math.PI*2);ctx.fill();
      ctx.strokeStyle = '#c8ddab';ctx.stroke();ctx.fillStyle = '#e0efc9';ctx.beginPath();ctx.arc(x,cy,2.3,0,Math.PI*2);ctx.fill();
      ctx.font = '11px "PingFang SC", sans-serif';ctx.fillStyle = '#c7d8bd';ctx.fillText(label,x,cy+21);
    }
  }
  let frame = 0, visible = false;
  function animate(time) {
    frame = 0;
    draw(time);
    if (visible && !document.hidden && !reduced.matches) frame = requestAnimationFrame(animate);
  }
  function refreshAnimation() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    if (visible && !document.hidden) animate(performance.now());
  }
  new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; refreshAnimation(); }).observe(canvas);
  reduced.addEventListener('change', refreshAnimation);
  document.addEventListener('visibilitychange', refreshAnimation);
  new ResizeObserver(() => draw(performance.now())).observe(canvas);
  showPolicy();loadScenario();run();
})();
