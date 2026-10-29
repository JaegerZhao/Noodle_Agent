"""Read-only summary for a single generic PDF validation, including failed runs."""

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()


def read(name, default=None):
    path = ROOT / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


info = read("run-input.json", {})
execution = read("run-result.json", {})
plan = read("planning-result.json", {})
parse = read("parse-result.json", {})
wall = read("pipeline-wall.json", {})
post = read("post-probe-state.json", {})
grades = read("case-assessment.json", {}).get("cases", {})
expected = read("acceptance-cases.json")["cases"]
math = read("math-render-check.json")
exports = read("export-check.json")
freeze = read("final-freeze-check.json", {})
events = [json.loads(line) for line in (ROOT / "timeline.jsonl").read_text(encoding="utf-8").splitlines()] if (ROOT / "timeline.jsonl").exists() else []
connection = sqlite3.connect((ROOT / "library/noodle.db").as_uri() + "?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
usage = [dict(r) for r in connection.execute("SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms,sum(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage FROM provider_calls GROUP BY kind,operation,model,status")]
jobs = [dict(r) for r in connection.execute("SELECT id,kind,status,attempts,error_json FROM jobs ORDER BY rowid")]
integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
foreign = [list(r) for r in connection.execute("PRAGMA foreign_key_check")]
publication_diagnostics = []
for row in connection.execute("SELECT storage_ref FROM build_artifacts WHERE storage_ref LIKE 'artifacts/builds/%.json'"):
    payload = json.loads((ROOT / "library" / row[0]).read_text(encoding="utf-8"))
    report = payload.get("review", {}).get("report", {})
    publication_diagnostics.append({"artifact_ref":row[0],"title":payload.get("candidate",{}).get("title"),
        "normalizations":report.get("normalizations",[]),"warnings":report.get("warnings",[]),
        "content_review_mode":report.get("content_review_mode"),"post_repair_semantic_review":report.get("post_repair_semantic_review")})
connection.close()
totals = {}
for kind in ("LLM_CALL", "VISION_CALL", "EMBEDDING_CALL"):
    rows = [r for r in usage if r["kind"] == kind]
    totals[kind] = {field:sum(r[field] or 0 for r in rows) for field in ("calls", "input_tokens", "output_tokens", "total_tokens", "elapsed_ms", "unknown_usage")}
probe_totals = defaultdict(lambda: {"calls":0,"input_tokens":0,"output_tokens":0,"total_tokens":0,"unknown_usage":0})
probe_path = ROOT / "probe-provider-calls.jsonl"
for line in probe_path.read_text(encoding="utf-8").splitlines() if probe_path.exists() else []:
    receipt = json.loads(line)
    value = probe_totals[receipt["kind"]]
    value["calls"] += 1
    tokens = receipt.get("usage") or receipt
    for field, alternative in (("input_tokens","prompt_tokens"),("output_tokens","completion_tokens"),("total_tokens","total_tokens")):
        value[field] += tokens.get(field, tokens.get(alternative)) or 0
    value["unknown_usage"] += tokens.get("total_tokens") is None
generation = (execution.get("generation", {}).get("terminal") or {}).get("status") == "SUCCEEDED"
converged = execution.get("convergence", {}).get("passed", False)
lineage = (execution.get("lineage") or {}).get("passed", False)
business = generation and converged and lineage and integrity == "ok" and not foreign
qa = set(grades) == {c["id"] for c in expected} and all(g["status"] in {"PASS", "REFUSAL_PASS"} for g in grades.values())
frozen = all(freeze.get(k,False) for k in ("passed","cases_unchanged","env_unchanged")) and execution.get("freeze",{}).get("passed",False)
outputs = math is not None and not math["errors"] and exports is not None and not exports["errors"]
post_ok = post.get("convergence",{}).get("passed",False) and post.get("lineage",{}).get("passed",False)
timing = execution.get("timing", {})
atomic = timing.get("atomic_index_ready_s")
parse_contribution = parse.get("historical_elapsed_seconds") or parse.get("elapsed_seconds",0)
plan_contribution = plan.get("historical_elapsed_seconds") or plan.get("elapsed_seconds",0)
atomic_after_parse = plan_contribution + atomic if atomic is not None else None
atomic_from_start = parse_contribution + atomic_after_parse if atomic_after_parse is not None else None
full_seconds = wall.get("wall_seconds")
reconstructed = parse_contribution + plan_contribution + execution.get("wall_s",0)
passed = business and qa and frozen and outputs and post_ok
unsettled = [j for j in jobs if j["status"] not in {"SUCCEEDED","SUPERSEDED","CANCELLED"}]
summary = {"status":"PASSED" if passed else "NOT_PASSED", "business_pass":business,"qa_pass":qa,
    "output_pass":outputs,"freeze_pass":frozen,"post_probe_pass":post_ok,
    "pages":read("pipeline-start.json")["pages"],"planning":plan,
    "eligible_counts":execution.get("convergence",{}).get("eligible_counts",{}),
    "qa_grade_counts":dict(Counter(g["status"] for g in grades.values())),
    "timing":{"parse_seconds":parse.get("elapsed_seconds"),"planning_seconds":plan.get("elapsed_seconds"),
        "execution_seconds":execution.get("wall_s"),"current_run_wall_seconds":full_seconds,
        "parse_mode":parse.get("mode"),"planning_mode":plan.get("mode","FRESH_PLANNING"),
        "historical_parse_contribution_seconds":parse_contribution,"planning_contribution_seconds":plan_contribution,
        "reconstructed_pipeline_seconds":reconstructed,
        "atomic_after_parse_seconds":atomic_after_parse,"atomic_from_start_sum_seconds":atomic_from_start,
        "execution_relative_milestones":timing},
    "time_targets_observed":{"full_le_900s":reconstructed<=900,
        "atomic_after_parse_le_600s":atomic_after_parse is not None and atomic_after_parse<=600},
    "usage":totals,"qa_usage":dict(probe_totals),"mineru_tokens":None,
    "preparation_failure":read("preparation-failure.json"),"unsettled_jobs":unsettled,
    "manual_candidate_edits":0,"visual_cache_hits":sum(e["kind"]=="vision_cache_hit" for e in events),
    "optional_relation_suggestions":execution.get("convergence",{}).get("optional_relation_suggestions",[])}
summary["publication_diagnostics"] = publication_diagnostics
summary["review_calls"] = {r["operation"]:sum(v["calls"] for v in usage if v["operation"]==r["operation"]) for r in usage if "review" in r["operation"]}
summary["visual_reuse"] = {"hits":summary["visual_cache_hits"],"new_network_calls":totals["VISION_CALL"]["calls"],
    "historical_tokens_not_billed_this_run":sum(e["data"].get("historical_tokens",0) for e in events if e["kind"]=="vision_cache_hit")}
(ROOT / "validation-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
(ROOT / "provider-usage-all-stages.json").write_text(json.dumps(usage,ensure_ascii=False,indent=2),encoding="utf-8")
rows = [f"# {info.get('benchmark_label','PDF')} 全流程冻结验证", "", f"结果：**{summary['status']}**。自动运行结束，未在中途改源码、修改生成结果或人工重试REVIEW。", "",
    f"输入：{summary['pages']}页；原PDF哈希 `{info.get('source_sha256')}`。独立空库，原主库和教材暂停状态不动。", "",
    "## 实际规划", "", "| # | 主题 | 页码 |", "|---|---|---|"]
for i,p in enumerate(plan.get("topics",[]),1):
    rows.append(f"| {i} | {p['title']} | {p['pages'][0]}–{p['pages'][-1]} |")
rows += ["", "## 阶段与用时", "", "| 项目 | 秒 |", "|---|---:|"]
for label,value in summary["timing"].items():
    if label != "execution_relative_milestones": rows.append(f"| {label} | {value} |")
rows += ["", f"执行相对里程碑：{timing}。", "当前墙钟只包含本轮实际发生的步骤；复用解析/规划时单列原阶段历史耗时，并与本轮执行组成可比较总时长，不冒充首次冷启动。QA/导出另计；并发请求耗时不能累加当墙钟。", "",
    "## 实际服务用量（含规划，QA另计）", "", "| 类别 | 次数 | Input | Output | Total | 请求合计ms | usage未知 |", "|---|---:|---:|---:|---:|---:|---:|"]
for kind,v in totals.items(): rows.append(f"| {kind} | {v['calls']} | {v['input_tokens']} | {v['output_tokens']} | {v['total_tokens']} | {v['elapsed_ms']} | {v['unknown_usage']} |")
rows += ["", f"QA另计：{dict(probe_totals)}。MinerU未提供Token/金额，记未知，不记零。", "",
    "## 正确性与遗留问题", "", f"- 生成/业务收敛/当前版本：{generation}/{converged}/{lineage}；有效笔记{summary['eligible_counts']}。",
    f"- 自动固定问答：{summary['qa_grade_counts']}；预期依据原课件事先冻结，未传入生成，不要求来源外补全。",
    f"- 公式渲染：{None if math is None else {'checked':math['checked'],'errors':len(math['errors'])}}；导出错误：{None if exports is None else exports['errors']}。",
    f"- 冻结检查：{freeze}；SQLite：{integrity}；外键：{foreign}；问答后状态：{post_ok}。",
    f"- 真实视觉缓存命中：{summary['visual_cache_hits']}；仅复用兼容回执，未更改旧视觉输出。",
    f"- 视觉复用与新增调用：{summary['visual_reuse']}。",
    f"- 内容审查操作调用：{summary['review_calls']}；重试/分组细节见原始回执，不把多个独立分组误称同一正文复审。",
    "- 每篇发布的规范化、警告、修复后不复审标记见validation-summary.json的publication_diagnostics；不修改原始模型响应。",
    f"- 未决任务：{len(unsettled)}；完整保留在jobs-final.json或validation-summary.json。",
    f"- 时间目标观测：{summary['time_targets_observed']}。只有完整验收通过才可称有效达标。",
    "- 单份新课件通过不能证明所有资料都已解决；Token优化及多来源增量合并仍需分别评估。"]
if summary["preparation_failure"]: rows.append(f"- 前置阶段失败：{summary['preparation_failure']}。")
for j in unsettled: rows.append(f"- {j['kind']} / {j['status']}：{j['error_json']}。")
for case_id,grade in grades.items():
    if grade["status"] not in {"PASS","REFUSAL_PASS"}: rows.append(f"- {case_id} / {grade['status']}：{grade['reason']}。")
rows += ["", "完整调用/Token按provider-usage-all-stages.json分操作记录，QA见probe-provider-calls.jsonl；全部原始回执、时间线、冻结源码、提示与验收问题保留。未提交或推送。", ""]
(ROOT / "VALIDATION-REPORT.md").write_text("\n".join(rows),encoding="utf-8")
print(json.dumps({k:summary[k] for k in ("status","eligible_counts","qa_grade_counts","timing","usage","qa_usage")},ensure_ascii=True))
