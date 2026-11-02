"""Compile fixed test templates; input text is never treated as JavaScript."""

import json
from urllib.parse import urlencode

from api_agent.execution.schemas import ExecutionPlan


def compile_collection(plan: ExecutionPlan, secrets: dict[str, str]) -> dict:
    """把受控执行计划编译成 Postman Collection v2.1 结构。

    作用：这是“数据 → 可执行集合”的唯一入口。集合内容是本项目固定模板拼出来的，
    用户输入只作为数据填进去，绝不会被当成 JS 代码执行。
    实现：
    ① 逐用例组装请求——规范化头名（Accept/Content-Type）、只在有 body 时补
       Content-Type、有 ``auth_ref`` 时从 ``secrets`` 取值注入对应头；
    ② URL 由 ``origin + path`` 加 ``urlencode(query)`` 拼成，避免手工拼接注入；
    ③ body 用 ``json.dumps`` 序列化成字符串放入 raw 模式（只传数据）；
    ④ 断言通过一段预置的 ``atPointer`` 函数按 JSON Pointer 取值，
       每种断言各自生成对应的 ``pm.expect`` 表达式，
       期望值经 ``json.dumps`` 双重编码后交给 ``JSON.parse``，杜绝字符串拼接注入；
    ⑤ 每条用例挂一个 test 脚本，最后包成 v2.1 collection。

    注意：``secrets`` 里的值会进入生成的集合文件，但该文件位于临时目录且用后即删，
    且 CLI 输出被丢弃，所以凭据不会经由 stdout 或报告外泄。
    """
    items = []
    for case in plan.requests:
        canonical = {"accept": "Accept", "content-type": "Content-Type"}
        headers = {"Accept": "application/json"}
        headers.update({canonical[name.lower()]: value for name, value in case.headers.items()})
        if case.body is not None:
            headers.setdefault("Content-Type", "application/json")
        if plan.auth_ref:
            value = secrets[plan.auth_ref.env_var]
            name = plan.auth_ref.header_name or "Authorization"
            headers[name] = f"Bearer {value}" if plan.auth_ref.kind == "bearer" else value
        request = {
            "method": case.method,
            "url": plan.origin + case.path + ("?" + urlencode(case.query) if case.query else ""),
            "header": [{"key": name, "value": value} for name, value in headers.items()],
        }
        if case.body is not None:
            request["body"] = {"mode": "raw", "raw": json.dumps(case.body, ensure_ascii=True)}
        # 预置的 Pointer 取值函数：对数组下标只接受规范十进制，且必须落在长度内；
        # 对对象用 hasOwnProperty 判断，避免把原型链上的属性当成存在。
        script = [
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
        for index, assertion in enumerate(case.assertions):
            encoded = json.dumps(assertion.expected, ensure_ascii=True)
            expected = f"JSON.parse({json.dumps(encoded, ensure_ascii=True)})"
            pointer = json.dumps(assertion.pointer, ensure_ascii=True)
            checks = {
                "status": f"pm.expect(pm.response.code).to.eql({expected});",
                "json_equals": (
                    f"const v=atPointer({pointer}); pm.expect(v.found).to.eql(true); "
                    f"pm.expect(v.value).to.deep.eql({expected});"
                ),
                "json_exists": f"pm.expect(atPointer({pointer}).found).to.eql(true);",
                "content_type": (
                    "const t=(pm.response.headers.get('Content-Type') || ''); "
                    "pm.expect(t.split(';')[0].trim().toLowerCase()).to.eql('application/json');"
                ),
                "response_time": f"pm.expect(pm.response.responseTime).to.be.at.most({expected});",
            }
            # 断言名固定为 a{序号}_{类型}，报告解析时靠它把结果与原断言对齐。
            script.append(
                f'pm.test("a{index}_{assertion.kind}", function() {{ {checks[assertion.kind]} }});'
            )
        items.append(
            {
                "id": case.id,
                "name": case.id,
                "request": request,
                "event": [
                    {"listen": "test", "script": {"type": "text/javascript", "exec": script}}
                ],
            }
        )
    return {
        "info": {
            "name": "api-agent-local",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }
