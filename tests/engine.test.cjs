'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { evaluate, modes, lanes } = require('../docs/lab/engine.js');
const example = require('../examples/split-review.json');
const { spawnSync } = require('node:child_process');
const { mkdtempSync, writeFileSync, rmSync } = require('node:fs');
const { join } = require('node:path');
const { tmpdir } = require('node:os');

const passing = () => ({ ...structuredClone(example), reviews: { correctness: 'pass', minimal: 'pass', consistency: 'pass' } });

test('三层覆盖按工单、项目、全局顺序求解', () => {
  for (const global of modes) for (const project of [...modes, 'inherit']) for (const issue of [...modes, 'inherit']) {
    const input = passing();
    input.policy = { global, project, issue };
    const source = issue !== 'inherit' ? 'issue' : project !== 'inherit' ? 'project' : 'global';
    assert.deepEqual(evaluate(input).effective, { source, mode: input.policy[source] });
  }
});

test('全通过时各档的动作计划与暂停边界', () => {
  const matrix = {
    dry: ['archived', 'draft', 'draft', 'draft', 'manual'],
    semi: ['awaiting_merge', 'simulate', 'simulate', 'simulate', 'manual'],
    full: ['awaiting_merge', 'simulate', 'simulate', 'simulate', 'manual'],
    yolo: ['merged', 'simulate', 'simulate', 'simulate', 'simulate'],
    paused: ['paused', 'skipped', 'skipped', 'skipped', 'skipped']
  };
  for (const [mode, expected] of Object.entries(matrix)) {
    const input = passing();
    input.policy.issue = mode;
    const result = evaluate(input);
    assert.deepEqual([result.outcome,...Object.values(result.plan)], expected);
    if (mode === 'paused') assert.equal(result.trace.length, 2);
  }
});

test('任一独立审查缺席或拒绝都会挡住发布与自动合并', () => {
  for (const name of Object.keys(lanes)) for (const state of ['pending', 'fail']) {
    const input = passing();
    input.policy.issue = 'yolo';
    input.reviews[name] = state;
    const result = evaluate(input);
    assert.equal(result.outcome, state === 'pending' ? 'incomplete' : 'rework');
    assert.equal(result.plan.publish, 'skipped');
    assert.equal(result.plan.merge, 'skipped');
    assert.equal(result.trace.some(row => row.stage === '终审'), false);
  }
});
test('前置、构建、终审拒绝不会计划合并', () => {
  for (const [key, value, outcome] of [['audit','reject','rejected'],['build','fail','build_failed'],['final','fail','final_rework']]) {
    const input = passing();
    input.policy.issue = 'yolo';
    input[key] = value;
    const result = evaluate(input);
    assert.equal(result.outcome, outcome);
    assert.equal(result.plan.merge, 'skipped');
    assert.equal(result.plan.publish, 'skipped');
  }
});
test('未知权限或不完整输入产生显式错误', () => {
  for (const corrupt of [i => delete i.policy.global, i => { i.policy.issue = 'owner'; }, i => delete i.reviews.consistency,
    i => { i.issue.title = ' '; }, i => { i.issue.body = 'a'.repeat(4001); }, i => delete i.final]) {
    const input = passing();
    corrupt(input);
    assert.throws(() => evaluate(input));
  }
});
test('快照独立并支持确定性复算', () => {
  const input = passing();
  const record = evaluate(input);
  input.issue.body = '另一版正文';
  assert.notEqual(input.issue.body, record.input.issue.body);
  assert.deepEqual(evaluate(record.input), record);
});
test('CLI 能重放记录并发现记录与输入不一致', t => {
  const dir = mkdtempSync(join(tmpdir(), 'triage-replay-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'receipt.json');
  const record = evaluate(example);
  writeFileSync(file, JSON.stringify(record));
  const cli = join(__dirname, '..', 'replay.cjs');
  const run = () => spawnSync(process.execPath, [cli, file], { encoding: 'utf8' });
  assert.deepEqual(JSON.parse(run().stdout), record);
  record.plan.merge = 'simulate';
  writeFileSync(file, JSON.stringify(record));
  const mismatch = run();
  assert.equal(mismatch.status, 1);
  assert.match(mismatch.stderr, /不一致/);
});
