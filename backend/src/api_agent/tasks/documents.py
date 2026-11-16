"""Recognize OpenAPI/Swagger JSON/YAML locally and use a schema-constrained LLM fallback."""

import json
import re
from copy import deepcopy

import yaml

from api_agent.tasks.schemas import Catalog, Endpoint

METHODS = {"get", "head", "options", "post", "put", "patch", "delete"}


class DocumentLoader(yaml.SafeLoader):
    """Reject YAML aliases to avoid recursive or exponentially expanded documents."""

    def compose_node(self, parent, index):
        """Keep YAML parsing bounded by source size and forbid alias graphs."""
        if self.check_event(yaml.AliasEvent):
            raise ValueError("yaml_alias_not_supported")
        return super().compose_node(parent, index)


def sample(schema, root, depth=0):
    """Build a bounded example from local schema references; never fetch external references."""
    if not isinstance(schema, dict) or depth > 12:
        return None
    if "$ref" in schema:
        return sample(resolve(schema, root), root, depth + 1)
    for key in ("example", "default", "const"):
        if key in schema:
            return deepcopy(schema[key])
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("type") == "object" or "properties" in schema:
        return {
            key: sample(value, root, depth + 1)
            for key, value in schema.get("properties", {}).items()
        }
    if schema.get("type") == "array":
        return [sample(schema.get("items", {}), root, depth + 1)]
    if schema.get("type") in {"integer", "number"}:
        return schema.get("minimum", 1)
    if schema.get("type") == "boolean":
        return True
    return "example"


def resolve(node, root):
    """Resolve only local JSON pointers with a cycle guard and explicit unsupported-ref errors."""
    visited = set()
    while isinstance(node, dict) and "$ref" in node:
        reference = node["$ref"]
        if reference in visited or not reference.startswith("#/"):
            raise ValueError("unsupported_or_cyclic_reference")
        visited.add(reference)
        node = root
        try:
            for part in reference[2:].split("/"):
                node = node[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            raise ValueError("unresolved_document_reference") from None
    return node


def recognize(text):
    """Return a complete standard catalog, or None for LLM-readable nonstandard text."""
    try:
        root = yaml.load(text.lstrip("\ufeff"), Loader=DocumentLoader)
        json.dumps(root, allow_nan=False)
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        return None
    if not isinstance(root, dict):
        return None
    if "endpoints" in root:
        return Catalog.model_validate(root)
    if not (root.get("openapi") or root.get("swagger")) or not isinstance(root.get("paths"), dict):
        return None
    endpoints = []
    warnings = []
    for path, raw_item in root["paths"].items():
        item = resolve(raw_item, root)
        if not isinstance(item, dict):
            raise ValueError("invalid_path_item")
        for method, raw_operation in item.items():
            if method not in METHODS:
                continue
            operation = resolve(raw_operation, root)
            if not isinstance(operation, dict):
                raise ValueError("invalid_operation")
            request_path = root.get("basePath", "").rstrip("/") + path
            path_template = request_path
            query = {}
            body = None
            headers = {}
            parameters = item.get("parameters", []) + operation.get("parameters", [])
            for raw_parameter in parameters:
                parameter = resolve(raw_parameter, root)
                value = parameter.get("example", sample(parameter.get("schema", parameter), root))
                if parameter.get("in") == "path":
                    from urllib.parse import quote

                    request_path = request_path.replace(
                        "{" + parameter["name"] + "}", quote(str(value), safe="")
                    )
                elif parameter.get("in") == "query" and parameter.get("required"):
                    query[parameter["name"]] = value
                elif parameter.get("in") == "body":
                    body = value
                elif parameter.get("in") == "header":
                    headers[parameter["name"]] = str(value)
            request_path = re.sub(r"\{[^}]+\}", "1", request_path)
            if query:
                from urllib.parse import urlencode

                request_path += "?" + urlencode(query)
            request_body = resolve(operation.get("requestBody", {}), root)
            media = request_body.get("content", {}).get("application/json", {})
            if media:
                body = media.get("example", sample(media.get("schema", {}), root))
            responses = operation.get("responses", {})
            responses = {str(code): response for code, response in responses.items()}
            statuses = [
                int(code) for code in responses if str(code).isdigit() and 200 <= int(code) < 300
            ]
            protocol = operation.get("x-protocol", "http")
            if any(
                "text/event-stream" in resolve(response, root).get("content", {})
                for response in responses.values()
            ) or "text/event-stream" in operation.get("produces", []):
                protocol = "sse"
            original = operation.get("x-test-cases", operation.get("test_cases", []))
            generated = _boundary_cases(
                {**operation, "parameters": parameters},
                request_path,
                method.upper(),
                protocol,
                root,
                query,
                path_template,
            )
            if not statuses:
                warnings.append(f"{method.upper()} {path}: 未声明成功状态，生成用例不推断状态码")
            endpoints.append(
                Endpoint.model_validate(
                    {
                        "name": operation.get("summary", operation.get("operationId", path)),
                        "protocol": protocol,
                        "method": method.upper(),
                        "path": request_path,
                        "body": body if method not in {"get", "head", "options"} else None,
                        "assertions": [{"kind": "status", "expected": min(statuses)}]
                        if statuses
                        else [],
                        "messages": operation.get("x-messages", []),
                        "headers": headers,
                        "cases": [
                            {
                                "path": request_path,
                                "method": method.upper(),
                                "protocol": protocol,
                                "name": case.get("name", "文档原有用例"),
                                **case,
                            }
                            for case in original
                            if isinstance(case, dict)
                        ],
                        "generated_cases": generated,
                    }
                )
            )
    return Catalog(
        format="openapi" if root.get("openapi") else "swagger",
        endpoints=endpoints,
        cases=root.get("x-test-cases", root.get("test_cases", [])),
        warnings=warnings,
    )


class NativeCaseGenerator:
    """Generate one contract/example case per operation and preserve every original case."""

    def generate(self, catalog):
        """Keep supplied cases in order, then add deterministic per-operation coverage."""
        cases = list(catalog.cases)
        for endpoint in catalog.endpoints:
            cases.extend(endpoint.cases)
        for endpoint in catalog.endpoints:
            cases.extend(endpoint.generated_cases)
        for endpoint in catalog.endpoints:
            from api_agent.tasks.schemas import Case

            cases.append(
                Case.model_validate(
                    {
                        **{
                            key: value
                            for key, value in endpoint.model_dump(
                                exclude={"cases", "generated_cases"}
                            ).items()
                            if key in Case.model_fields
                        },
                        "source": "generated",
                    }
                )
            )
        if len(cases) > 200:
            raise ValueError("case_limit_exceeded")
        return cases


def _boundary_cases(operation, path, method, protocol, root, query, path_template):
    """Generate required-field and schema-boundary requests without invented verdicts."""
    from urllib.parse import quote

    from api_agent.tasks.schemas import Case

    base = {
        "method": method,
        "path": path,
        "protocol": protocol,
        "assertions": [],
        "source": "generated",
    }
    generated = []
    path_parameters = {
        parameter["name"]: parameter
        for raw in operation.get("parameters", [])
        if (parameter := resolve(raw, root)).get("in") == "path"
    }
    for name, parameter in path_parameters.items():
        value = parameter.get("example", sample(parameter.get("schema", {}), root))
        base["path"] = base["path"].replace("{" + name + "}", quote(str(value), safe=""))
    for raw in operation.get("parameters", []):
        parameter = resolve(raw, root)
        if parameter.get("required") and parameter.get("in") == "query":
            from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

            parsed = urlsplit(path)
            values = [
                (key, value)
                for key, value in parse_qsl(parsed.query)
                if key != parameter.get("name")
            ]
            without_required = urlunsplit(("", "", parsed.path, urlencode(values), ""))
            generated.append(
                Case.model_validate({**base, "path": without_required, "name": "缺少必填查询参数"})
            )
        schema = resolve(parameter.get("schema", {}), root)
        invalid = None
        if schema.get("type") == "string" and schema.get("maxLength") is not None:
            invalid = "x" * min(schema["maxLength"] + 1, 1024)
        elif schema.get("type") in {"integer", "number"} and schema.get("minimum") is not None:
            invalid = schema["minimum"] - 1
        elif schema.get("type") == "integer" and schema.get("maximum") is not None:
            invalid = schema["maximum"] + 1
        if invalid is not None:
            location = parameter.get("in")
            name = parameter.get("name", "参数")
            case = {**base, "name": f"{name} 边界值"}
            if location == "query":
                from urllib.parse import urlencode

                values = dict(query)
                values[name] = invalid
                case["path"] = path.split("?", 1)[0] + "?" + urlencode(values)
            elif location == "path":
                from urllib.parse import quote

                case_path = path_template
                for path_name, path_parameter in path_parameters.items():
                    value = (
                        invalid
                        if path_name == name
                        else path_parameter.get(
                            "example", sample(path_parameter.get("schema", {}), root)
                        )
                    )
                    case_path = case_path.replace("{" + path_name + "}", quote(str(value), safe=""))
                case["path"] = case_path
            elif location == "header":
                case["headers"] = {name: str(invalid)}
            elif location == "body":
                case["body"] = invalid
            generated.append(Case.model_validate(case))
    schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if schema:
        resolved = resolve(schema, root)
        required = resolved.get("required", [])
        properties = resolved.get("properties", {})
        for name in required[:20]:
            example = {
                field: sample(value, root) for field, value in properties.items() if field != name
            }
            if method not in {"GET", "HEAD", "OPTIONS"}:
                generated.append(
                    Case.model_validate(
                        {**base, "name": f"请求体缺少必填字段 {name}", "body": example}
                    )
                )
        for name, property_schema in properties.items():
            if property_schema.get("maxLength") is not None:
                example = {field: sample(value, root) for field, value in properties.items()}
                example[name] = "x" * min(property_schema["maxLength"] + 1, 1024)
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    generated.append(
                        Case.model_validate(
                            {**base, "name": f"请求体字段 {name} 超出最大长度", "body": example}
                        )
                    )
                break
    return generated
