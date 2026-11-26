#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const { isDeepStrictEqual } = require('node:util');
const { evaluate } = require('./docs/lab/engine.js');

if (process.argv.length !== 3) {
  console.error('用法: node replay.cjs <场景或审计包.json>');
  process.exitCode = 2;
} else {
  try {
    const file = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const isAudit = Object.hasOwn(file, 'schema');
    if (isAudit && file.schema !== 'triage-lab/v1') throw new Error('未知的审计格式');
    const result = evaluate(isAudit ? file.input : file);
    if (isAudit && !isDeepStrictEqual(result, file)) throw new Error('重放结果与记录不一致');
    console.log(JSON.stringify(result, null, 2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
