"""Facts are rendered by code; model prose is a visibly separate commentary."""

import html
import json


def cell(value) -> str:
    return html.escape(str(value)).replace("|", "\\|").replace("\n", " ")


def _json_type(value) -> str:
    """与执行层一致的 JSON 类型名，用于在说明列标出类型差异。"""
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in {int, float}:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _explain(fact: dict) -> str:
    """为一条证据生成最有诊断价值的说明。

    优先级：执行错误 > 类型不符 > 实际值 > 引擎消息 > 缺失断言。
    类型不符单独点名，是因为"期望 400 得到 \"400\""最容易被误读为业务缺陷，
    而它通常只是模型侧的期望类型推断错误。
    """
    if fact.get("error_kind"):
        return str(fact["error_kind"])
    if fact.get("passed") is False and fact.get("actual_present"):
        expected_type = _json_type(fact.get("expected"))
        actual_type = fact.get("actual_type") or _json_type(fact.get("actual"))
        if actual_type and expected_type != actual_type:
            return f"类型不符：期望 {expected_type}，实际 {actual_type}（可能为期望类型推断错误）"
        if fact.get("actual_truncated"):
            return "实际值过长已截断"
        return f"实际 {json.dumps(fact.get('actual'), ensure_ascii=False)}"
    if fact.get("message"):
        return str(fact["message"])
    if fact.get("actual_present") and fact.get("actual_truncated"):
        return "实际值过长已截断"
    return ""


def json_block(value) -> list[str]:
    # A string value cannot close the Markdown fence and inject an extra report section.
    encoded = json.dumps(value, ensure_ascii=False, indent=2).replace("`", "\\u0060")
    return ["```json", encoded, "```", ""]


def render_report(record: dict) -> str:
    target = record["budget"]["target_http"]
    model = record["budget"]["model_call"]
    lines = [
        "# API 测试诊断报告",
        "",
        f"运行：`{record['id']}`；状态：{cell(record['status'])}；"
        f"报告完整性：{record['report_status']}。",
        f"停止原因：{cell(record.get('stop_reason'))}；"
        f"实际模型状态：{cell(record['actual_llm_status'])}。",
        "",
        "## 冻结快照与复现边界",
        "",
        f"输入快照 SHA-256：`{record['snapshot_sha256']}`。",
        "初始输入冻结；实际计划按轮次追加，后续补测不修改已执行计划。",
        "快照已脱敏，不含凭据与原始个人数据；不能直接重放含脱敏值的请求。",
        "本报告描述断言观察；模型生成的期望仍需核对契约，差异不等于已确认业务缺陷。",
        "",
        "## 已证实的观察事实",
        "",
        "| 证据 ID | 用例状态 | HTTP | 断言/期望 | 结论 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    count = 0
    for round_ in record["rounds"]:
        for fact in round_.get("evidence", []):
            count += 1
            verdict = (
                "通过"
                if fact.get("passed") is True
                else "失败"
                if fact.get("passed") is False
                else "未评估"
            )
            expected = json.dumps(fact.get("expected"), ensure_ascii=False)
            description = f"{fact.get('kind') or '请求'} / {expected}"
            lines.append(
                "| "
                + " | ".join(
                    map(
                        cell,
                        [
                            fact["id"],
                            fact["status"],
                            fact.get("http_status"),
                            description,
                            verdict,
                            _explain(fact),
                        ],
                    )
                )
                + " |"
            )
    if not count:
        lines.append("| — | unknown | — | — | 未评估 | 尚无可判定执行证据 |")
    lines.extend(["", "## 冻结范围与每轮动作", ""])
    lines.extend(
        json_block(
            {
                "engine": record["snapshot"]["engine"],
                "scope": record["snapshot"]["scope"],
                "limits": record["snapshot"]["limits"],
            }
        )
    )
    contract = record["snapshot"]["contract"]
    contract_text = (
        contract if isinstance(contract, str) else json.dumps(contract, ensure_ascii=False)
    )
    lines.extend(
        [
            "契约摘要（最多 3000 字符；完整脱敏输入见同 ID 的 JSON 快照）：",
            "",
            cell(contract_text[:3000]),
            "",
        ]
    )
    if len(contract_text) > 3000:
        lines.extend(["契约摘要已截断。", ""])
    for round_ in record["rounds"]:
        lines.extend([f"### 第 {round_['index']} 轮", ""])
        lines.extend(json_block({"plan": round_["plan"], "intents": round_.get("intents", [])}))
    lines.extend(["", "## 未证实的假设", ""])
    hypotheses = 0
    for round_ in record["rounds"]:
        analysis = (round_.get("diagnosis") or {}).get("analysis", {})
        for hypothesis in analysis.get("hypotheses", []):
            hypotheses += 1
            lines.append(
                f"- 第 {round_['index']} 轮 {cell(hypothesis['id'])}："
                f"{cell(hypothesis['cause'])}；证据："
                f"{cell(', '.join(hypothesis['evidence_ids']))}；"
                f"待检查：{cell(hypothesis['next_check'])}。"
            )
    if not hypotheses:
        lines.append("无已生成的有效假设；模型未评估或不可用时，不能据此认定没有问题。")
    lines.extend(
        [
            "",
            "## 覆盖盲区与限制",
            "",
            "- 未执行/未知结果、未评估断言均不计为通过。",
            "- 未计算完整契约分母，不宣称接口或业务覆盖率。",
            "- 未进行源代码、数据库和根因验证；有/无鉴权须分计划。",
        ]
    )
    if not record["snapshot"].get("allow_mutating_followups"):
        lines.append("- 修改类操作的自动补测未授权，POST/PUT/PATCH/DELETE 不会自动重放。")
    for reason in record.get("degradation_reasons", []):
        lines.append(f"- 退化：{cell(reason)}。")
    for round_ in record["rounds"]:
        for note in round_.get("notes", []):
            lines.append(f"- 第 {round_['index']} 轮说明：{cell(note)}。")
        for intent in round_.get("intents", []):
            if intent["basis"] == "inferred":
                lines.append(
                    f"- {cell(intent['case_id'])} 的期望来自语义推断，"
                    f"需人工确认：{cell(intent['rationale'])}。"
                )
        for gap in (round_.get("diagnosis") or {}).get("analysis", {}).get("coverage_gaps", []):
            lines.append(f"- {cell(gap)}")
        for reason in (round_.get("adaptation") or {}).get("rejected", []):
            lines.append(f"- 补测拒绝：{cell(reason)}。")
    lines.extend(
        [
            "",
            "## 预算消耗",
            "",
            f"- HTTP：上限 {target['limit']}，保守消耗 {target['consumed']}，"
            f"在途预留 {target['reserved']}，释放 {target['released']}；"
            f"实际观察尝试 {target['observed_attempts']}，未知 {target['unknown_requests']}。",
            f"- 模型：上限 {model['limit']}，消耗 {model['consumed']}，"
            f"在途预留 {model['reserved']}；包括格式修复与报告调用。",
            f"- Token：输入 {model.get('prompt_tokens', 0)}，"
            f"输出 {model.get('completion_tokens', 0)}；"
            "未返回 usage 的调用另记，不能解释为零成本。",
            "- 费用未知：未配置可靠模型单价。",
            f"- 总时间上限：{record['snapshot']['limits']['wall_time_seconds']} 秒。",
            "",
            "## 事件时间线",
            "",
            "| 序号 | UTC 时间 | 节点 | 状态 | 原因 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for event in record["events"]:
        lines.append(
            "| "
            + " | ".join(
                map(
                    cell,
                    [
                        event["sequence"],
                        event["timestamp"],
                        event["node"],
                        event["status"],
                        event.get("reason", ""),
                    ],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 模型调用轨迹",
            "",
            "| 阶段 | 模型 | 状态 | 错误 | 输入/输出 token | usage 已返回 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for call in record["model_calls"]:
        lines.append(
            "| "
            + " | ".join(
                map(
                    cell,
                    [
                        call["stage"],
                        call["model"],
                        call["status"],
                        call.get("error_kind") or "",
                        f"{call['prompt_tokens']}/{call['completion_tokens']}",
                        call["usage_reported"],
                    ],
                )
            )
            + " |"
        )
    commentary = record.get("commentary")
    if commentary:
        lines.extend(
            ["", "## AI 报告概述（解释与建议，未经独立证实）", "", cell(commentary["overview"]), ""]
        )
        lines.extend(f"- {cell(item)}" for item in commentary["recommendations"])
    return "\n".join(lines) + "\n"
