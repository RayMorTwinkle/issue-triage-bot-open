const TriageScenarios = {
  empty: {
    name: '空标题回退',
    issue: { title: '清空草稿标题后，列表出现空白条目', body: '创建一个草稿，然后清空标题。保存后列表里只剩日期，希望显示「未命名草稿」。' },
    audit: 'accept', build: 'pass', reviews: { correctness: 'pass', minimal: 'pass', consistency: 'pass' }, final: 'pass'
  },
  scope: {
    name: '审查意见分歧',
    issue: { title: '修复日期排序时一并重写了列表样式', body: '工单只要求按日期排序。修复提案还替换了列表样式，最小改动审查要求拆开提交。' },
    audit: 'accept', build: 'pass', reviews: { correctness: 'pass', minimal: 'fail', consistency: 'pass' }, final: 'pass'
  },
  waiting: {
    name: '审查尚未齐备',
    issue: { title: '导出文件新增了字段，文档仍待核对', body: '示例文件增加了导出时间。边界与最小改动审查通过，文档审查尚未返回。' },
    audit: 'accept', build: 'pass', reviews: { correctness: 'pass', minimal: 'pass', consistency: 'pending' }, final: 'pass'
  }
};
