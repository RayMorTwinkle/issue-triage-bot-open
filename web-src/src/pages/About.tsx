import { PageHeader } from "../components/AppShell";

export function About() {
  return <main>
    <PageHeader title="关于分诊工作台" subtitle="Issue Triage Bot" />
    <p>在队列中选择工单，即可查看状态时间线、补丁和各路审查意见。</p>
    <p><a className="underline" href="/replay/index.html">使用本地样例回放完整流程</a></p>
  </main>;
}
