"""Compile fixed test templates; input text is never treated as JavaScript."""

import json
import re

from api_agent.execution.request import build_request_headers, build_request_url
from api_agent.execution.schemas import ExecutionPlan
from api_agent.execution.variables import find_placeholders

_JS_VAR_HELPERS = [
    "function readVar(name) {",
    "  const raw = pm.collectionVariables.get('__aa_' + name);",
    "  if (raw === undefined || raw === null || raw === '') return {found: false};",
    "  try { return {found: true, value: JSON.parse(raw)}; } catch (_) { return {found: false}; }",
    "}",
    "function scalarText(val) {",
    "  if (typeof val === 'boolean') return val ? 'true' : 'false';",
    "  return String(val);",
    "}",
    "function resolveText(str) {",
    "  if (typeof str !== 'string' || str.indexOf('{{') === -1) return {ok: true, value: str};",
    "  let missing = false;",
    "  const out = str.replace(/\\{\\{([A-Za-z_][A-Za-z0-9_]{0,63})\\}\\}/g, function(_, name) {",
    "    const entry = readVar(name);",
    "    if (!entry.found) { missing = true; return ''; }",
    "    return scalarText(entry.value);",
    "  });",
    "  return {ok: !missing, value: out};",
    "}",
    "function resolveJson(node) {",
    "  if (typeof node === 'string') {",
    "    const m = /^\\{\\{([A-Za-z_][A-Za-z0-9_]{0,63})\\}\\}$/.exec(node);",
    "    if (m) {",
    "      const entry = readVar(m[1]);",
    "      return entry.found ? {ok: true, value: entry.value} : {ok: false};",
    "    }",
    "    return resolveText(node);",
    "  }",
    "  if (Array.isArray(node)) {",
    "    const arr = [];",
    "    for (const item of node) {",
    "      const r = resolveJson(item);",
    "      if (!r.ok) return {ok: false};",
    "      arr.push(r.value);",
    "    }",
    "    return {ok: true, value: arr};",
    "  }",
    "  if (node !== null && typeof node === 'object') {",
    "    const out = {};",
    "    for (const k of Object.keys(node)) {",
    "      const r = resolveJson(node[k]);",
    "      if (!r.ok) return {ok: false};",
    "      out[k] = r.value;",
    "    }",
    "    return {ok: true, value: out};",
    "  }",
    "  return {ok: true, value: node};",
    "}",
]

_JS_AT_POINTER = [
    "function atPointer(pointer) {",
    "  let value = pm.response.json();",
    "  if (pointer === '') return {found:true, value:value};",
    "  for (const raw of pointer.slice(1).split('/')) {",
    "    const key = raw.replace(/~1/g, '/').replace(/~0/g, '~');",
    "    if (Array.isArray(value) && (!/^(0|[1-9][0-9]*)$/.test(key) ||",
    "        Number(key) >= value.length)) return {found:false};",
    "    if (value === null || typeof value !== 'object' ||",
    "        !Object.prototype.hasOwnProperty.call(value, key)) return {found:false};",
    "    value = value[key];",
    "  }",
    "  return {found:true, value:value};",
    "}",
]


def _plan_uses_state(plan: ExecutionPlan) -> bool:
    if plan.variables:
        return True
    for case in plan.requests:
        if case.extract or case.depends_on:
            return True
        refs, _ = find_placeholders(case.model_dump(exclude={"extract"}))
        if refs:
            return True
    return False


def _json_js_literal(value) -> str:
    encoded = json.dumps(value, ensure_ascii=True)
    return f"JSON.parse({json.dumps(encoded, ensure_ascii=True)})"


def _path_template_regex_sources(templates: list[str]) -> list[str]:
    """把计划允许的 path_templates 编译成等价 JS RegExp 源码，供 prerequest 复核解析结果。"""
    patterns: list[str] = []
    for template in templates:
        if template == "/":
            patterns.append(r"^/$")
            continue
        parts = template[1:].removesuffix("/").split("/")
        rendered = []
        for part in parts:
            if part.startswith("{") and part.endswith("}"):
                rendered.append(r"[A-Za-z0-9_~-]+")
            else:
                rendered.append(re.escape(part))
        patterns.append(r"^/" + "/".join(rendered) + r"/?$")
    return patterns


def compile_collection(plan: ExecutionPlan, secrets: dict[str, str]) -> dict:
    """把受控执行计划编译成 Postman Collection v2.1 结构。

    - 无状态静态计划保持原有最小集合形态，100% 兼容既有输出；
    - 有状态变量链计划通过固定预置的 data-only ``prerequest`` + ``test`` 模板在单次
      Postman CLI 进程中完成依赖门控（``depends_on`` / ``always_run``）、变量解析、
      路径与请求头运行期安全复核、``pm.execution.skipRequest()`` 无回退跳过及变量提取。
    """
    stateful = _plan_uses_state(plan)
    template_patterns = _path_template_regex_sources(plan.path_templates) if stateful else []
    items = []
    for case in plan.requests:
        headers = build_request_headers(plan, case, secrets)
        case_refs, _ = find_placeholders(case.model_dump(exclude={"extract"}))
        has_dep_guard = stateful and (not case.always_run) and bool(case.depends_on)
        case_needs_var_resolve = stateful and bool(case_refs)
        case_needs_prerequest = has_dep_guard or case_needs_var_resolve

        if case_needs_var_resolve:
            # 用静态安全占位 URL 满足 Schema，实际路径/头/体在 prerequest 经模板安全替换。
            static_headers = [
                {"key": name, "value": value}
                for name, value in headers.items()
                if "{{" not in value
            ]
            request: dict = {
                "method": case.method,
                "url": f"{plan.origin}/",
                "header": static_headers,
            }
            if case.body is not None:
                request["body"] = {"mode": "raw", "raw": "{}"}
        else:
            request = {
                "method": case.method,
                "url": build_request_url(plan, case),
                "header": [{"key": name, "value": value} for name, value in headers.items()],
            }
            if case.body is not None:
                request["body"] = {"mode": "raw", "raw": json.dumps(case.body, ensure_ascii=True)}

        events = []
        case_status_key = json.dumps(f"__aa_status_{case.id}", ensure_ascii=True)
        if case_needs_prerequest:
            prereq = ["(function() {"]
            if case_needs_var_resolve:
                prereq.extend(_JS_VAR_HELPERS)
            if has_dep_guard:
                deps_expr = _json_js_literal(case.depends_on)
                prereq.extend(
                    [
                        f"const __aaDeps = {deps_expr};",
                        "for (const dep of __aaDeps) {",
                        "  if (pm.collectionVariables.get('__aa_status_' + dep) !== 'passed') {",
                        f"    pm.collectionVariables.set({case_status_key}, 'skipped');",
                        "    pm.test('__aa_skip:dependency_failed', function() {});",
                        "    pm.execution.skipRequest();",
                        "    return;",
                        "  }",
                        "}",
                    ]
                )
            if case_needs_var_resolve:
                prereq.extend(
                    [
                        f"const origin = {json.dumps(plan.origin, ensure_ascii=True)};",
                        f"const rawPath = {json.dumps(case.path, ensure_ascii=True)};",
                        f"const rawQuery = {_json_js_literal(case.query)};",
                        f"const rawHeaders = {_json_js_literal(headers)};",
                        f"const hasBody = {'true' if case.body is not None else 'false'};",
                        f"const rawBody = {_json_js_literal(case.body)};",
                        f"const templateRegexes = {_json_js_literal(template_patterns)};",
                        "const pRes = resolveText(rawPath);",
                        "let missing = !pRes.ok;",
                        "const qPairs = [];",
                        "for (const qk of Object.keys(rawQuery)) {",
                        "  const qr = resolveText(rawQuery[qk]);",
                        "  if (!qr.ok) { missing = true; }",
                        "  else { qPairs.push(",
                        "    encodeURIComponent(qk) + '=' + encodeURIComponent(qr.value)); }",
                        "}",
                        "const resolvedHeaders = [];",
                        "for (const hk of Object.keys(rawHeaders)) {",
                        "  const hr = resolveText(rawHeaders[hk]);",
                        "  if (!hr.ok) { missing = true; }",
                        "  else { resolvedHeaders.push([hk, hr.value]); }",
                        "}",
                        "let bodyRes = {ok: true, value: null};",
                        "if (hasBody) {",
                        "  bodyRes = resolveJson(rawBody);",
                        "  if (!bodyRes.ok) { missing = true; }",
                        "}",
                        "if (missing) {",
                        f"  pm.collectionVariables.set({case_status_key}, 'skipped');",
                        "  pm.test('__aa_skip:missing_variable', function() {});",
                        "  pm.execution.skipRequest();",
                        "  return;",
                        "}",
                        "let unsafe = false;",
                        "const segRe = /^[A-Za-z0-9_~-]+(?:\\.[A-Za-z0-9_~-]+)*$/;",
                        "if (!pRes.value.startsWith('/')) { unsafe = true; }",
                        "if (!unsafe && pRes.value !== '/') {",
                        "  const segs = pRes.value.slice(1).replace(/\\/$/, '').split('/');",
                        "  for (const seg of segs) {",
                        "    if (!segRe.test(seg)) { unsafe = true; break; }",
                        "  }",
                        "}",
                        "if (!unsafe && !templateRegexes.some(function(src) {",
                        "  return new RegExp(src).test(pRes.value);",
                        "})) {",
                        "  unsafe = true;",
                        "}",
                        "if (!unsafe) {",
                        "  for (const pair of resolvedHeaders) {",
                        "    const hv = pair[1];",
                        "    if (hv.length > 4096 || /[\\x00-\\x1f\\x7f]/.test(hv)) {",
                        "      unsafe = true; break;",
                        "    }",
                        "  }",
                        "}",
                        "if (unsafe) {",
                        f"  pm.collectionVariables.set({case_status_key}, 'error');",
                        "  pm.test('__aa_error:unsafe_variable_value', function() {});",
                        "  pm.execution.skipRequest();",
                        "  return;",
                        "}",
                        "const qs = qPairs.length ? '?' + qPairs.join('&') : '';",
                        "pm.request.url.update(origin + pRes.value + qs);",
                        "for (const pair of resolvedHeaders) {",
                        "  pm.request.headers.upsert({key: pair[0], value: pair[1]});",
                        "}",
                        "if (hasBody) {",
                        "  pm.request.body.update(",
                        "    {mode: 'raw', raw: JSON.stringify(bodyRes.value)});",
                        "}",
                    ]
                )
            prereq.append("})();")
            events.append(
                {"listen": "prerequest", "script": {"type": "text/javascript", "exec": prereq}}
            )

        script = list(_JS_AT_POINTER)
        if stateful:
            script.extend(_JS_VAR_HELPERS)
            script.append("let __aaCaseOk = true;")

        for index, assertion in enumerate(case.assertions):
            expected_expr = _json_js_literal(assertion.expected)
            pointer = json.dumps(assertion.pointer, ensure_ascii=True)
            if stateful and assertion.kind == "json_equals":
                json_eq_check = (
                    f"const expRes = resolveJson({expected_expr}); "
                    "pm.expect(expRes.ok).to.eql(true); "
                    f"const v=atPointer({pointer}); pm.expect(v.found).to.eql(true); "
                    "pm.expect(v.value).to.deep.eql(expRes.value);"
                )
            else:
                json_eq_check = (
                    f"const v=atPointer({pointer}); pm.expect(v.found).to.eql(true); "
                    f"pm.expect(v.value).to.deep.eql({expected_expr});"
                )
            checks = {
                "status": f"pm.expect(pm.response.code).to.eql({expected_expr});",
                "json_equals": json_eq_check,
                "json_exists": f"pm.expect(atPointer({pointer}).found).to.eql(true);",
                "content_type": (
                    "const t=(pm.response.headers.get('Content-Type') || ''); "
                    "pm.expect(t.split(';')[0].trim().toLowerCase()).to.eql('application/json');"
                ),
                "response_time": (
                    f"pm.expect(pm.response.responseTime).to.be.at.most({expected_expr});"
                ),
            }
            if stateful:
                script.append(
                    f'pm.test("a{index}_{assertion.kind}", function() {{ '
                    f"try {{ {checks[assertion.kind]} }} "
                    f"catch (err) {{ __aaCaseOk = false; throw err; }} }});"
                )
            else:
                script.append(
                    f'pm.test("a{index}_{assertion.kind}", function() {{ '
                    f"{checks[assertion.kind]} }});"
                )

        for ext_idx, rule in enumerate(case.extract):
            var_key = json.dumps(f"__aa_{rule.name}", ensure_ascii=True)
            if rule.source == "status":
                lookup = "const ext = {found: true, value: pm.response.code};"
            elif rule.source == "header":
                h_lit = json.dumps(rule.header_name, ensure_ascii=True)
                lookup = (
                    f"const hv = pm.response.headers.get({h_lit}); "
                    "const ext = (hv !== undefined && hv !== null) "
                    "? {found: true, value: String(hv)} : {found: false};"
                )
            else:
                p_lit = json.dumps(rule.pointer, ensure_ascii=True)
                lookup = (
                    "let ext = {found: false}; "
                    f"try {{ ext = atPointer({p_lit}); }} catch (_) {{ ext = {{found: false}}; }}"
                )
            expect_stmt = (
                "try { pm.expect(scalarOk).to.eql(true); } "
                "catch (err) { __aaCaseOk = false; throw err; }"
                if (stateful and rule.required)
                else "pm.expect(scalarOk).to.eql(true);"
            )
            script.append(
                f'pm.test("e{ext_idx}_{rule.name}", function() {{ '
                f"{lookup} "
                "const scalarOk = ext.found && ext.value !== null && "
                "typeof ext.value !== 'object' && String(ext.value).length <= 4096; "
                f"if (scalarOk) {{ pm.collectionVariables.set({var_key}, "
                "JSON.stringify(ext.value)); } "
                f"{expect_stmt} "
                "});"
            )

        if stateful:
            script.append(
                f"pm.collectionVariables.set({case_status_key}, __aaCaseOk ? 'passed' : 'failed');"
            )

        events.append({"listen": "test", "script": {"type": "text/javascript", "exec": script}})
        items.append(
            {
                "id": case.id,
                "name": case.id,
                "request": request,
                "event": events,
            }
        )

    collection: dict = {
        "info": {
            "name": "api-agent-local",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }
    if plan.variables:
        collection["variable"] = [
            {"key": f"__aa_{name}", "value": json.dumps(val, ensure_ascii=True)}
            for name, val in plan.variables.items()
        ]
    return collection
