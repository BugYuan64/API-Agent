"""从已保存的接口证据生成 Markdown 与 OpenAPI，完整列出覆盖范围和未确认内容。"""

import html
import json
import re
from copy import deepcopy

from api_agent.projects.document_schema import DocumentEndpoint

TYPE_NAMES = {"string", "integer", "number", "boolean", "object", "array", "null"}
STATUS_LABELS = {
    "queued": "排队中",
    "running": "生成中",
    "completed": "全部源码批次处理完成",
    "partial": "部分完成",
    "failed": "生成失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def markdown_text(value) -> str:
    """转义 Markdown 与 HTML 控制符，保证项目和模型文本不会插入可执行内容。"""
    text = html.escape(str(value or ""), quote=False)
    text = re.sub(r"([\\`*_{}\[\]()#+!|>])", r"\\\1", text)
    return text.replace("\r", "").replace("\n", " ")


def schema_block(value: dict) -> str:
    """用长于正文中反引号序列的围栏展示完整 JSON Schema，避免提前闭合代码块。"""
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    longest = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}json\n{text}\n{fence}"


def coverage(record: dict) -> dict:
    """提供两种导出共用的事实进度，不把源码处理完成解释为运行时路由验证。"""
    return {
        "status": record.get("status", "unknown"),
        "source_id": record.get("source_id", ""),
        "source_sha256": record.get("source_sha256", ""),
        "processed_files": record.get("processed_files", 0),
        "total_files": record.get("total_files", 0),
        "processed_batches": record.get("processed_batches", 0),
        "total_batches": record.get("total_batches", 0),
        "scan_complete": record.get("scan_complete", False),
        "runtime_routes_verified": False,
        "warnings": list(record.get("warnings", [])),
        "issues": list(record.get("issues", [])),
        "error": record.get("error"),
    }


def render_markdown(record: dict) -> str:
    """逐项导出已保存接口、参数、正文、响应、鉴权和来源，部分任务也可导出。"""
    progress = coverage(record)
    endpoints = [DocumentEndpoint.model_validate(item) for item in record.get("endpoints", [])]
    status_label = STATUS_LABELS.get(progress["status"], progress["status"])
    lines = [
        f"# {markdown_text(record.get('project_name', '项目'))} 接口文档",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        f"| 生成状态 | {markdown_text(status_label)} |",
        f"| 源码文件进度 | {progress['processed_files']} / {progress['total_files']} |",
        f"| 分批分析进度 | {progress['processed_batches']} / {progress['total_batches']} |",
        f"| 接口条目数 | {len(endpoints)} |",
        f"| 源码快照 | {markdown_text(progress['source_id'])} |",
        f"| 源码摘要 SHA-256 | {markdown_text(progress['source_sha256'])} |",
        f"| 创建时间 | {markdown_text(record.get('created_at', ''))} |",
        "",
        "文档来自源码静态扫描与 AI 分析。动态注册、运行时配置和外部组件提供的路由仍需核验。",
        "",
        "## 覆盖情况",
        "",
        "扫描范围内的文件全部读取完成。"
        if progress["scan_complete"]
        else "扫描存在未完整读取的文件，请结合下列说明核验覆盖范围。",
    ]
    if progress["error"]:
        lines.extend(["", f"任务错误为 {markdown_text(progress['error'])}。"])
    for warning in progress["warnings"]:
        lines.extend(["", f"- {markdown_text(warning)}"])
    if progress["issues"]:
        lines.extend(["", "| 文件或目录 | 扫描说明 |", "| --- | --- |"])
        lines.extend(
            f"| {markdown_text(item.get('path'))} | {markdown_text(item.get('reason'))} |"
            for item in progress["issues"]
        )
    lines.extend(["", "## 接口目录", "", "| 方法 | 路径 | 说明 |", "| --- | --- | --- |"])
    lines.extend(
        f"| {item.method} | {markdown_text(item.path)} | {markdown_text(item.summary)} |"
        for item in endpoints
    )
    for item in endpoints:
        lines.extend(["", f"## {item.method} {markdown_text(item.path)}", ""])
        if item.summary:
            lines.extend([markdown_text(item.summary), ""])
        if item.description:
            lines.extend([markdown_text(item.description), ""])
        lines.extend(["### 鉴权", "", markdown_text(item.authentication) or "待确认", ""])
        lines.extend(["### 请求参数", ""])
        if item.parameters:
            lines.extend(
                [
                    "| 名称 | 位置 | 必填 | 类型 | 说明 |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for parameter in item.parameters:
                lines.append(
                    f"| {markdown_text(parameter.name)} | {parameter.location} | "
                    f"{'是' if parameter.required else '否'} | "
                    f"{markdown_text(parameter.type) or '待确认'} | "
                    f"{markdown_text(parameter.description)} |"
                )
            for parameter in item.parameters:
                if parameter.schema_definition is not None:
                    lines.extend(
                        [
                            "",
                            f"参数 {markdown_text(parameter.name)} 的结构",
                            "",
                            schema_block(parameter.schema_definition),
                            "",
                        ]
                    )
        else:
            lines.append("源码分析未提供参数定义。")
        lines.extend(["", "### 请求正文", "", markdown_text(item.request_body) or "待确认"])
        if item.request_content_type:
            lines.extend(["", f"媒体类型为 {markdown_text(item.request_content_type)}。"])
        if item.request_schema is not None:
            lines.extend(["", schema_block(item.request_schema)])
        lines.extend(["", "### 响应", ""])
        if item.responses:
            for response in item.responses:
                lines.extend(
                    [
                        f"#### {response.status}",
                        "",
                        markdown_text(response.description) or "响应含义待确认",
                        "",
                    ]
                )
                if response.content_type:
                    lines.extend([f"媒体类型为 {markdown_text(response.content_type)}。", ""])
                if response.body_schema is not None:
                    lines.extend([schema_block(response.body_schema), ""])
        else:
            lines.append("响应状态和正文结构待确认。")
        lines.extend(["", "### 源码依据", ""])
        lines.extend(f"- {markdown_text(path)}" for path in item.source_files)
        if item.uncertainties:
            lines.extend(["", "### 待确认事项", ""])
            lines.extend(f"- {markdown_text(value)}" for value in item.uncertainties)
    return "\n".join(lines).rstrip() + "\n"


def operation_for(endpoint: DocumentEndpoint) -> dict:
    """转换单个已验证接口，缺少契约时用未知说明与扩展字段保留证据。"""
    operation = {
        "summary": endpoint.summary,
        "description": endpoint.description,
        "responses": {},
        "x-source-files": endpoint.source_files,
        "x-uncertainties": endpoint.uncertainties,
        "x-authentication-notes": endpoint.authentication or "待确认",
    }
    parameters = []
    for item in endpoint.parameters:
        definition = deepcopy(item.schema_definition)
        if definition is None:
            definition = {"type": item.type} if item.type in TYPE_NAMES else {}
        parameter = {
            "name": item.name,
            "in": item.location,
            "required": True if item.location == "path" else item.required,
            "description": item.description,
            "schema": definition,
        }
        if item.type and item.type not in TYPE_NAMES:
            parameter["x-source-type"] = item.type
        parameters.append(parameter)
    existing = {(item["name"], item["in"]) for item in parameters}
    for name in re.findall(r"\{([^{}]+)\}", endpoint.path):
        if (name, "path") not in existing:
            parameters.append(
                {
                    "name": name,
                    "in": "path",
                    "required": True,
                    "schema": {},
                    "description": "路径模板声明的参数，类型和约束待确认",
                    "x-inferred-from-path": True,
                }
            )
            existing.add((name, "path"))
    if parameters:
        operation["parameters"] = parameters
    if endpoint.request_content_type and endpoint.request_schema is not None:
        operation["requestBody"] = {
            "description": endpoint.request_body,
            "content": {endpoint.request_content_type: {"schema": endpoint.request_schema}},
        }
    else:
        operation["x-request-body-description"] = endpoint.request_body or "待确认"
        if endpoint.request_schema is not None:
            operation["x-request-schema"] = endpoint.request_schema
    for response in endpoint.responses:
        output = {"description": response.description or "响应含义待确认"}
        if response.body_schema is not None:
            if response.content_type:
                output["content"] = {response.content_type: {"schema": response.body_schema}}
            else:
                output["x-body-schema"] = response.body_schema
        if response.status in operation["responses"]:
            operation["responses"][response.status].setdefault("x-variants", []).append(output)
        else:
            operation["responses"][response.status] = output
    if not operation["responses"]:
        operation["responses"]["default"] = {"description": "响应状态和正文结构待确认"}
    return operation


def relocate_schema_references(document: dict) -> None:
    """调整嵌入 Schema 的本地引用，无法解析的引用转为显式待确认扩展。"""

    def segment(value: str) -> str:
        """按 JSON Pointer 规则转义对象键。"""
        return value.replace("~", "~0").replace("/", "~1")

    def rewrite(schema: dict, pointer: str) -> None:
        """把相对独立 Schema 的引用定位到导出文档中的真实位置。"""
        source = deepcopy(schema)
        pending = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                if "$id" in node:
                    node["x-source-id"] = node.pop("$id")
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    reference = node.get(key)
                    if reference is None:
                        continue
                    target = source
                    resolved = reference == "#" or reference.startswith("#/")
                    try:
                        for part in reference.removeprefix("#/").split("/"):
                            if reference == "#":
                                break
                            part = part.replace("~1", "/").replace("~0", "~")
                            target = target[int(part)] if isinstance(target, list) else target[part]
                    except (KeyError, IndexError, TypeError, ValueError):
                        resolved = False
                    if resolved and key == "$ref":
                        node[key] = pointer + reference[1:]
                    else:
                        node[f"x-unresolved-{key[1:]}"] = node.pop(key)
                        node["x-reference-note"] = "引用缺少可独立解析的定义，具体约束待确认"
                pending.extend(node.values())

    def visit(node, pointer: str) -> None:
        """遍历接口结构，仅在完整 Schema 根处改写引用，保留其他元信息。"""
        if isinstance(node, dict):
            for key, value in node.items():
                child_pointer = f"{pointer}/{segment(key)}"
                if key in {"schema", "x-request-schema", "x-body-schema"} and isinstance(
                    value, dict
                ):
                    rewrite(value, child_pointer)
                else:
                    visit(value, child_pointer)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{pointer}/{index}")

    visit(document, "#")


def render_openapi(record: dict) -> dict:
    """导出 OpenAPI 3.1 与覆盖扩展，相同路由的额外定义和 WebSocket 均完整保留。"""
    document = {
        "openapi": "3.1.0",
        "info": {
            "title": f"{record.get('project_name', '项目')} 接口文档",
            "version": record.get("source_id", "") or "source-snapshot",
            "description": "根据源码静态扫描与 AI 分析生成，运行时路由和未确认字段需要核验。",
        },
        "paths": {},
        "x-generation": coverage(record),
    }
    for raw in record.get("endpoints", []):
        endpoint = DocumentEndpoint.model_validate(raw)
        operation = operation_for(endpoint)
        if endpoint.method == "WEBSOCKET":
            document.setdefault("x-websocket-operations", []).append(
                {
                    "path": endpoint.path,
                    **operation,
                }
            )
            continue
        path_item = document["paths"].setdefault(endpoint.path, {})
        method = endpoint.method.lower()
        if method in path_item:
            path_item[method].setdefault("x-definition-variants", []).append(operation)
        else:
            path_item[method] = operation
    relocate_schema_references(document)
    return document
