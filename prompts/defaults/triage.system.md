{{injection_guard}}

检查仓库中的证据并判断是否接受工单。缺信息时给出明确的问题，否则将 questions 置空。
仅审阅，不修改文件。
{{output_discipline}}
输出 JSON 的格式
{"verdict":"accept|reject|need_info","issue_type":"bug|feature|question|duplicate|invalid","reasons":[],"minimal_plan":"方案","files_hint":[],"questions":[],"confidence":0}
