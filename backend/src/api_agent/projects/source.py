"""Read-only project snapshots and source-backed route discovery for API documentation.

Every eligible text file is retained for the AI pass. Static discovery supplies evidence,
but runtime registrations, generated code and dynamic prefixes still require review.
"""

import ast
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from api_agent.infrastructure.logging import (
    BEARER_CREDENTIAL,
    REDACTED,
    URL_CREDENTIAL,
    is_sensitive_key,
)
from api_agent.infrastructure.privacy import EMAIL, KEY, PHONE

SOURCE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".java",
    ".kt",
    ".kts",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".cs",
    ".php",
    ".rb",
    ".scala",
    ".swift",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".vue",
    ".svelte",
    ".graphql",
    ".gql",
    ".proto",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".md",
    ".toml",
    ".ini",
    ".conf",
    ".properties",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    ".next",
    ".nuxt",
    "coverage",
    ".gradle",
    ".python",
    ".playwright-mcp",
    ".zcode",
    ".codex",
    ".agents",
    ".aws",
    ".ssh",
    ".kube",
}
EXCLUDED_FILES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "composer.lock",
    "cargo.lock",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
}
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE")
STATIC_WARNING = (
    "文件扫描覆盖情况见统计与遗漏清单。静态接口清单仍需核验动态注册、运行时前缀、"
    "网关重写及生成代码；AI 文档必须注明无法从源码确认的内容。"
)
STRING_LITERAL = r""""(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*' """.strip()
ASSIGNED_LITERAL = re.compile(
    r"(?<![\w])(?P<key>[A-Za-z_][\w.-]*)(?P<closing>[\"']?)"
    r"(?P<assignment>\s*(?:(?::\s*[A-Za-z_][\w.\[\], |?<>]*)?\s*=|:)\s*)"
    r"(?P<literal>" + STRING_LITERAL + r")"
)
CONFIG_SCALAR = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w.-]*)"
    r"(?P<assignment>[ \t]*[:=][ \t]*)(?P<value>[^\r\n]*)$"
)


def _literal_credentials(text: str) -> set[str]:
    """Learn only quoted credential values, never type names or expression references."""
    return {
        match["literal"][1:-1]
        for match in ASSIGNED_LITERAL.finditer(text)
        if is_sensitive_key(match["key"]) and len(match["literal"]) > 2
    }


def sanitize_source(
    text: str,
    secrets=(),
    *,
    path: str | None = None,
    literal_secrets=(),
) -> str:
    """Redact literal credentials while preserving declarations, expressions and line numbers.

    ``secrets`` contains already-known secrets such as the configured model key. Values
    discovered in source are replaced only as quoted literals, so ``password = "str"``
    cannot erase unrelated ``str`` type annotations across the project.
    """
    literals = _literal_credentials(text) | set(literal_secrets)

    def redact_assignment(match):
        """Keep the original credential key, assignment syntax and quoting style."""
        if not is_sensitive_key(match["key"]):
            return match[0]
        quote = match["literal"][0]
        return match["key"] + match["closing"] + match["assignment"] + quote + REDACTED + quote

    text = ASSIGNED_LITERAL.sub(redact_assignment, text)
    text = re.sub(
        STRING_LITERAL,
        lambda match: (
            match[0][0] + REDACTED + match[0][-1] if match[0][1:-1] in literals else match[0]
        ),
        text,
    )
    if path and PurePosixPath(path).suffix.lower() in {
        ".yaml",
        ".yml",
        ".ini",
        ".conf",
        ".properties",
        ".toml",
    }:

        def redact_scalar(match):
            """Mask configuration scalar credentials, preserving environment references."""
            value = match["value"].strip()
            if (
                not is_sensitive_key(match["key"])
                or not value
                or REDACTED in value
                or value.startswith(("${", "{{", "|", ">"))
            ):
                return match[0]
            return match["indent"] + match["key"] + match["assignment"] + REDACTED

        text = CONFIG_SCALAR.sub(redact_scalar, text)
        lines = text.splitlines(keepends=True)
        secret_indent = None
        for index, line in enumerate(lines):
            indentation = len(line) - len(line.lstrip(" \t"))
            if secret_indent is not None and line.strip():
                if indentation > secret_indent:
                    lines[index] = re.sub(r"[^\r\n]+", REDACTED, line[indentation:])
                    lines[index] = line[:indentation] + lines[index]
                    continue
                secret_indent = None
            match = CONFIG_SCALAR.match(line.rstrip("\r\n"))
            if (
                match
                and is_sensitive_key(match["key"])
                and match["value"].strip().startswith(("|", ">"))
            ):
                secret_indent = indentation
        text = "".join(lines)
    text = re.sub(
        r"<(?P<key>[A-Za-z_][\w.-]*)>(?P<value>[^<>]+)</(?P=key)>",
        lambda match: (
            (
                f"<{match['key']}>"
                + re.sub(r"[^\r\n]+", REDACTED, match["value"])
                + f"</{match['key']}>"
            )
            if is_sensitive_key(match["key"])
            else match[0]
        ),
        text,
    )
    # PEM bodies may contain newlines. Replacing each non-newline span preserves evidence lines.
    text = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        lambda match: re.sub(r"[^\r\n]+", REDACTED, match[0]),
        text,
        flags=re.S,
    )
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = URL_CREDENTIAL.sub(lambda match: f"{match[1]}{REDACTED}@", text)
    text = BEARER_CREDENTIAL.sub(f"Bearer {REDACTED}", text)
    text = KEY.sub(REDACTED, text)
    text = PHONE.sub("[PHONE]", text)
    return EMAIL.sub("[EMAIL]", text)


class ScanLimits(BaseModel):
    """Bound memory and traversal without silently dropping eligible source files."""

    max_files: int = Field(default=5000, ge=1)
    max_file_bytes: int = Field(default=512 * 1024, ge=1)
    max_total_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_entries: int = Field(default=100000, ge=1)


class SourceInput(BaseModel):
    """Portable source input with a project-relative path and UTF-8 text."""

    path: str
    content: str


class SourceFile(SourceInput):
    """Sanitized complete text and its fingerprint, suitable for persisted snapshots."""

    sha256: str
    size_bytes: int


class SourceEndpoint(BaseModel):
    """A declaration tied to a file and one-based line; warnings qualify uncertain paths."""

    method: str
    path: str
    file: str
    line: int
    framework: str
    warnings: list[str] = Field(default_factory=list)


class SourceIssue(BaseModel):
    """An excluded entry or unreadable source with a stable, non-secret reason code."""

    path: str
    reason: str


class SourceSnapshot(BaseModel):
    """Immutable input representation; complete describes file coverage, not runtime APIs."""

    files: list[SourceFile] = Field(default_factory=list)
    endpoints: list[SourceEndpoint] = Field(default_factory=list)
    issues: list[SourceIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=lambda: [STATIC_WARNING])
    total_files: int = 0
    scanned_files: int = 0
    excluded_files: int = 0
    total_bytes: int = 0
    complete: bool = True
    sha256: str = ""


class SourceScanError(ValueError):
    """Safe scan failure whose message never includes an absolute local path."""


def normalize_path(value: str) -> str:
    """Reject absolute paths, traversal, drive names and control characters in uploads."""
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise SourceScanError("invalid_source_path")
    return path.as_posix()


def exclusion_reason(value: str) -> str | None:
    """Apply the same source, dependency and credential exclusions to every import mode."""
    path = PurePosixPath(value)
    if any(
        part.lower() in EXCLUDED_DIRECTORIES or part.lower().startswith((".pytest-", ".pt-"))
        for part in path.parts[:-1]
    ):
        return "excluded_directory"
    name = path.name.lower()
    if (
        name in EXCLUDED_FILES
        or name.startswith(".env")
        or (
            re.search(r"(?:^|[._-])(?:credentials?|secrets?)(?:[._-]|$)", name)
            and path.suffix.lower() in {"", ".json", ".yaml", ".yml", ".ini", ".conf", ".toml"}
        )
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}
    ):
        return "credential_or_lock_file"
    if path.suffix.lower() not in SOURCE_EXTENSIONS:
        return "unsupported_file_type"
    return None


def _skip(snapshot: SourceSnapshot, path: str, reason: str, *, incomplete: bool = False):
    """Record omissions explicitly; only unreadable/over-budget source makes coverage partial."""
    snapshot.issues.append(SourceIssue(path=path, reason=reason))
    snapshot.excluded_files += 1
    if incomplete:
        snapshot.complete = False


def _append_source(snapshot: SourceSnapshot, source: SourceInput, limits: ScanLimits):
    """Accept one complete source file, with no truncation or loss hidden by size limits."""
    size = len(source.content.encode("utf-8"))
    reason = exclusion_reason(source.path)
    if reason:
        _skip(snapshot, source.path, reason)
        return
    if size > limits.max_file_bytes:
        _skip(snapshot, source.path, "file_size_limit", incomplete=True)
        return
    if (
        len(snapshot.files) >= limits.max_files
        or snapshot.total_bytes + size > limits.max_total_bytes
    ):
        _skip(snapshot, source.path, "project_size_limit", incomplete=True)
        return
    if "\x00" in source.content:
        _skip(snapshot, source.path, "binary_content", incomplete=True)
        return
    # Parse originals before redaction, whose placeholders can invalidate source syntax.
    snapshot.files.append(
        SourceFile(
            path=source.path,
            content=source.content,
            size_bytes=size,
            sha256="",
        )
    )
    snapshot.total_bytes += size


def _finish(snapshot: SourceSnapshot) -> SourceSnapshot:
    """Extract evidence and redact every retained source without clipping its contents."""
    snapshot.files.sort(key=lambda item: item.path)
    snapshot.endpoints = discover_endpoints(snapshot.files, snapshot.warnings)
    literals = set().union(*(_literal_credentials(source.content) for source in snapshot.files))
    for source in snapshot.files:
        source.content = sanitize_source(
            source.content,
            path=source.path,
            literal_secrets=literals,
        )
        source.sha256 = hashlib.sha256(source.content.encode("utf-8")).hexdigest()
    for endpoint in snapshot.endpoints:
        endpoint.path = sanitize_source(endpoint.path)
    snapshot.scanned_files = len(snapshot.files)
    snapshot.sha256 = hashlib.sha256(
        json.dumps(
            [(item.path, item.sha256) for item in snapshot.files],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if not snapshot.complete:
        snapshot.warnings.append("部分源文件未纳入分析，文档不得标记为完整。请查看扫描遗漏原因。")
    return snapshot


def snapshot_files(files: list[SourceInput], *, limits: ScanLimits | None = None) -> SourceSnapshot:
    """Build a deterministic snapshot from supplied text using the directory import policy."""
    limits = limits or ScanLimits()
    snapshot = SourceSnapshot()
    seen = set()
    for source in files:
        path = normalize_path(source.path)
        if path.casefold() in seen:
            raise SourceScanError("duplicate_source_path")
        seen.add(path.casefold())
        snapshot.total_files += 1
        if snapshot.total_files > limits.max_entries:
            _skip(snapshot, ".", "entry_limit", incomplete=True)
            break
        _append_source(snapshot, SourceInput(path=path, content=source.content), limits)
    return _finish(snapshot)


def _is_link(path: Path, info: os.stat_result) -> bool:
    """Reject POSIX links and Windows junction/reparse points before opening them."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    )


def scan_directory(
    root: str | Path,
    *,
    limits: ScanLimits | None = None,
    exclude_paths: Iterable[Path] = (),
) -> SourceSnapshot:
    """Read a designated directory without execution, following links or writing source files."""
    limits = limits or ScanLimits()
    supplied = Path(root).expanduser()
    try:
        if _is_link(supplied, supplied.lstat()):
            raise SourceScanError("source_root_is_link")
        resolved = supplied.resolve(strict=True)
        if not resolved.is_dir():
            raise SourceScanError("source_root_not_directory")
    except OSError:
        raise SourceScanError("source_root_unavailable") from None
    snapshot = SourceSnapshot()
    excluded = set()
    for path in exclude_paths:
        candidate = Path(path).resolve()
        if candidate.is_relative_to(resolved):
            excluded.add(candidate)
    if resolved in excluded:
        _skip(snapshot, ".", "excluded_runtime_directory")
        return _finish(snapshot)
    pending = [resolved]
    entries = 0
    while pending:
        directory = pending.pop()
        try:
            children = []
            for child in directory.iterdir():
                if entries + len(children) >= limits.max_entries:
                    _skip(snapshot, ".", "entry_limit", incomplete=True)
                    return _finish(snapshot)
                children.append(child)
            children.sort(key=lambda item: item.name.casefold())
        except OSError:
            _skip(
                snapshot,
                directory.relative_to(resolved).as_posix(),
                "unreadable_directory",
                incomplete=True,
            )
            continue
        for child in children:
            entries += 1
            relative = child.relative_to(resolved).as_posix()
            if entries > limits.max_entries:
                _skip(snapshot, ".", "entry_limit", incomplete=True)
                return _finish(snapshot)
            try:
                info = child.lstat()
                if _is_link(child, info):
                    _skip(snapshot, relative, "symbolic_link", incomplete=True)
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if child in excluded:
                        _skip(snapshot, relative, "excluded_runtime_directory")
                    elif (
                        child.name.lower() in EXCLUDED_DIRECTORIES
                        or child.name.lower().startswith((".pytest-", ".pt-"))
                    ):
                        _skip(snapshot, relative, "excluded_directory")
                    else:
                        pending.append(child)
                    continue
                snapshot.total_files += 1
                reason = exclusion_reason(relative)
                if reason:
                    _skip(snapshot, relative, reason)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    _skip(snapshot, relative, "not_regular_file", incomplete=True)
                    continue
                if info.st_size > limits.max_file_bytes:
                    _skip(snapshot, relative, "file_size_limit", incomplete=True)
                    continue
                # Re-check containment and file identity before reading any source bytes.
                if not child.resolve(strict=True).is_relative_to(resolved):
                    _skip(snapshot, relative, "outside_source_root", incomplete=True)
                    continue
                with child.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        _skip(snapshot, relative, "source_changed_during_scan", incomplete=True)
                        continue
                    data = stream.read(limits.max_file_bytes + 1)
                if len(data) > limits.max_file_bytes:
                    _skip(snapshot, relative, "file_size_limit", incomplete=True)
                    continue
                text = data.decode("utf-8-sig")
                _append_source(snapshot, SourceInput(path=relative, content=text), limits)
            except UnicodeDecodeError:
                _skip(snapshot, relative, "unsupported_encoding", incomplete=True)
            except OSError:
                _skip(snapshot, relative, "unreadable_file", incomplete=True)
    return _finish(snapshot)


def _literal(node: ast.AST | None):
    """Evaluate only Python literals; imported code and expressions are never executed."""
    try:
        return ast.literal_eval(node) if node is not None else None
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return None


def _keyword(call: ast.Call, name: str):
    """Find a literal keyword argument while retaining None for dynamic expressions."""
    return _literal(next((item.value for item in call.keywords if item.arg == name), None))


def _join_path(prefix: str, path: str) -> str:
    """Combine static registration prefixes and route paths without changing placeholders."""
    if not path:
        return prefix or "/"
    return (prefix.rstrip("/") + "/" + path.lstrip("/")) if prefix else (path or "/")


def _python_endpoints(files: list[SourceFile], warnings: list[str]) -> list[SourceEndpoint]:
    """Resolve Python router declarations, literal mounts and cross-file imports through AST."""
    trees = {}
    routers = {}
    imports = {}
    mounts = {}
    declarations = []
    for source in files:
        if not source.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(source.content)
        except (SyntaxError, RecursionError):
            warnings.append(f"{source.path} 无法完成 Python 静态语法分析，保留全文供 AI 核验。")
            continue
        trees[source.path] = tree
        imports[source.path] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[source.path][alias.asname or alias.name] = (
                        node.module,
                        alias.name,
                        node.level,
                    )
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(
                node.value, ast.Call
            ):
                continue
            call = node.value
            name = ast.unparse(call.func).split(".")[-1]
            if name not in {"APIRouter", "FastAPI", "Flask", "Blueprint"}:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            prefix_key = "url_prefix" if name == "Blueprint" else "prefix"
            prefix = _keyword(call, prefix_key)
            notes = []
            if prefix is None and any(item.arg == prefix_key for item in call.keywords):
                warnings.append(f"{source.path}:{node.lineno} 路由前缀为动态表达式，路径待核验。")
                notes.append("动态路由前缀需核验")
            for target in targets:
                if isinstance(target, ast.Name):
                    routers[(source.path, target.id)] = (
                        prefix if isinstance(prefix, str) else "",
                        name,
                        notes,
                    )

    def resolve(file: str, name: str):
        """Resolve an imported router by module suffix, refusing ambiguous source matches."""
        local = (file, name)
        if local in routers:
            return local
        imported = imports.get(file, {}).get(name)
        if not imported:
            return None
        module, attribute, level = imported
        suffix = module.replace(".", "/") + ".py"
        if level:
            base = PurePosixPath(file).parent
            for _ in range(level - 1):
                base = base.parent
            suffix = str(base / suffix)
        matches = [
            key
            for key in routers
            if key[1] == attribute and (key[0] == suffix or key[0].endswith("/" + suffix))
        ]
        return matches[0] if len(matches) == 1 else None

    for file, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                call = node
                owner = resolve(file, ast.unparse(call.func.value))
                if call.func.attr in {"include_router", "register_blueprint", "mount"} and owner:
                    child_node = (
                        call.args[1]
                        if call.func.attr == "mount" and len(call.args) > 1
                        else (call.args[0] if call.args else None)
                    )
                    if child_node is None:
                        child_node = next(
                            (
                                item.value
                                for item in call.keywords
                                if item.arg in {"router", "blueprint", "app"}
                            ),
                            None,
                        )
                    child = resolve(file, ast.unparse(child_node)) if child_node else None
                    prefix = (
                        _literal(call.args[0])
                        if call.func.attr == "mount" and call.args
                        else _keyword(
                            call, "prefix" if call.func.attr == "include_router" else "url_prefix"
                        )
                    )
                    if child:
                        mounts.setdefault(child, []).append(
                            (
                                owner,
                                prefix if isinstance(prefix, str) else "",
                                call.func.attr == "register_blueprint" and prefix is not None,
                            )
                        )
                    else:
                        warnings.append(
                            f"{file}:{node.lineno} 无法静态关联子路由，挂载路径待核验。"
                        )
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(
                    decorator.func, ast.Attribute
                ):
                    continue
                kind = decorator.func.attr.lower()
                if kind not in {item.lower() for item in HTTP_METHODS} | {
                    "route",
                    "api_route",
                    "websocket",
                    "websocket_route",
                }:
                    continue
                owner = resolve(file, ast.unparse(decorator.func.value))
                path = (
                    _literal(decorator.args[0]) if decorator.args else _keyword(decorator, "path")
                )
                if not isinstance(path, str):
                    warnings.append(f"{file}:{decorator.lineno} 接口路径为动态表达式，需核验。")
                    continue
                methods = (
                    _keyword(decorator, "methods")
                    if kind in {"route", "api_route"}
                    else ["WEBSOCKET" if "websocket" in kind else kind.upper()]
                )
                if (
                    kind in {"route", "api_route"}
                    and any(item.arg == "methods" for item in decorator.keywords)
                    and not isinstance(methods, (list, tuple))
                ):
                    warnings.append(f"{file}:{decorator.lineno} HTTP 方法为动态表达式，需核验。")
                    continue
                methods = methods if isinstance(methods, (list, tuple)) else ["GET"]
                if owner and routers[owner][1] in {"Flask", "Blueprint"}:
                    path = re.sub(r"<(?:[\w]+:)?([\w]+)>", r"{\1}", path)
                declarations.append((file, decorator.lineno, owner, path, methods))

    def prefixes(owner, visited=frozenset()):
        """Expand every static mount path and terminate cyclic registrations explicitly."""
        if owner is None:
            return [("", ["路由对象或挂载前缀无法静态确认"])]
        if owner in visited:
            return [(routers[owner][0], ["路由挂载存在循环，前缀待核验"])]
        own = routers[owner][0]
        own_notes = routers[owner][2]
        if owner not in mounts:
            return [(own, own_notes)]
        output = []
        for parent, mount_prefix, override in mounts[owner]:
            for base, notes in prefixes(parent, visited | {owner}):
                path = _join_path(base, mount_prefix)
                if not override:
                    path = _join_path(path, own)
                output.append((path.rstrip("/"), notes + own_notes))
        return output

    output = []
    for file, line, owner, path, methods in declarations:
        for prefix, notes in prefixes(owner):
            for method in methods:
                if isinstance(method, str):
                    output.append(
                        SourceEndpoint(
                            method=method.upper(),
                            path=_join_path(prefix, path),
                            file=file,
                            line=line,
                            framework="python",
                            warnings=notes,
                        )
                    )
    return output


def _annotation_arguments(content: str, start: int) -> tuple[str, int]:
    """Read balanced Java annotation arguments, including multiline nested annotations."""
    position = start
    while position < len(content) and content[position].isspace():
        position += 1
    if position >= len(content) or content[position] != "(":
        return "", position
    depth = 0
    quoted = False
    escaped = False
    for end in range(position, len(content)):
        char = content[end]
        if quoted:
            if char == '"' and not escaped:
                quoted = False
            escaped = char == "\\" and not escaped
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return content[position + 1 : end], end + 1
    return "", position


def _spring_paths(arguments: str) -> list[str]:
    """Read mapping value/path arrays while ignoring unrelated produces/consumes strings."""
    selected = re.search(r"\b(?:value|path)\s*=\s*(\{[^}]*\}|\"(?:\\.|[^\"])*\")", arguments, re.S)
    if selected is None and re.search(r"\b(?:value|path)\s*=", arguments):
        return []
    fragment = selected.group(1) if selected else re.split(r",\s*\w+\s*=", arguments, maxsplit=1)[0]
    if selected is None and re.match(r"\s*\w+\s*=", fragment):
        return [""]
    without_literals = re.sub(r'"(?:\\.|[^"\\])*"', "", fragment)
    if "+" in without_literals or (
        fragment.strip() and not fragment.lstrip().startswith(('"', "{"))
    ):
        return []
    values = re.findall(r'"((?:\\.|[^"\\])*)"', fragment)
    return values or [""]


def _spring_endpoints(source: SourceFile, warnings: list[str]) -> list[SourceEndpoint]:
    """Extract Spring mappings with class prefixes, arrays and multiline annotations."""
    # Replace comments with equal-length whitespace to preserve declaration line evidence.
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*',
        lambda match: match[0] if match[0].startswith('"') else re.sub(r"[^\n]", " ", match[0]),
        source.content,
        flags=re.S,
    )
    mappings = []
    for match in re.finditer(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b", text):
        arguments, end = _annotation_arguments(text, match.end())
        mappings.append((match, arguments, end))
    # Annotation array braces are not Java block boundaries around class declarations.
    declaration_text = list(text)
    for annotation in re.finditer(r"@[\w.]+\b", text):
        _, end = _annotation_arguments(text, annotation.end())
        declaration_text[annotation.start() : end] = " " * (end - annotation.start())
    declaration_text = "".join(declaration_text)
    classes = []
    class_mappings = set()
    for declaration in re.finditer(r"\bclass\s+\w+", text):
        start = (
            max(
                declaration_text.rfind("}", 0, declaration.start()),
                declaration_text.rfind(";", 0, declaration.start()),
            )
            + 1
        )
        candidates = [
            (match, args)
            for match, args, end in mappings
            if start <= match.start() < declaration.start()
        ]
        paths = [""]
        for match, arguments in candidates:
            paths = _spring_paths(arguments)
            class_mappings.add(match.start())
            if not paths:
                warnings.append(f"{source.path} 类级路由前缀含动态表达式，需核验。")
        classes.append((declaration.start(), paths))
    output = []
    for match, arguments, _end in mappings:
        if match.start() in class_mappings:
            continue
        ancestors = [item for item in classes if item[0] < match.start()]
        bases = ancestors[-1][1] if ancestors else [""]
        methods = (
            re.findall(r"RequestMethod\.(\w+)", arguments)
            if match[1] == "Request"
            else [match[1].upper()]
        )
        methods = methods or list(HTTP_METHODS)
        paths = _spring_paths(arguments)
        if not paths:
            warnings.append(f"{source.path} 方法路由含动态表达式，需核验。")
        for base in bases:
            for path in paths:
                for method in methods:
                    output.append(
                        SourceEndpoint(
                            method=method,
                            path=_join_path(base, path),
                            file=source.path,
                            line=text.count("\n", 0, match.start()) + 1,
                            framework="spring",
                        )
                    )
    return output


def _express_endpoints(source: SourceFile) -> list[SourceEndpoint]:
    """Extract literal Express calls and route chains; imported/dynamic mounts stay qualified."""
    text = re.sub(
        r"/\*.*?\*/|^\s*//[^\n]*",
        lambda match: re.sub(r"[^\n]", " ", match[0]),
        source.content,
        flags=re.S | re.M,
    )
    mounts = {}
    owners = {"app", "router"}
    owners.update(
        re.findall(
            r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:new\s+)?"
            r"(?:express\s*\(|(?:express\s*\.\s*)?Router\s*\()",
            text,
        )
    )
    for match in re.finditer(r"\b(\w+)\.use\(\s*(['\"])(.*?)\2\s*,\s*(\w+)\s*\)", text, re.S):
        if match[1] in owners:
            mounts.setdefault(match[4], []).append((match[1], match[3]))

    def prefixes(owner: str, visited=frozenset()):
        """Expand literal same-file Express mounts and qualify unknown exported roots."""
        if owner in visited:
            return [("", ["路由挂载存在循环，前缀待核验"])]
        if owner not in mounts:
            notes = [] if owner == "app" else ["导出路由的跨文件挂载前缀需核验"]
            return [("", notes)]
        output = []
        for parent, prefix in mounts[owner]:
            for base, notes in prefixes(parent, visited | {owner}):
                output.append((_join_path(base, prefix), notes))
        return output

    output = []
    pattern = r"\b(\w+)\.(get|post|put|patch|delete|head|options|all|route)\s*\(\s*(['\"])(.*?)\3"
    for match in re.finditer(pattern, text, re.S):
        owner, kind, _, path = match.groups()
        if owner not in owners or not path.startswith("/"):
            continue
        methods = [kind.upper()]
        if kind == "all":
            methods = list(HTTP_METHODS)
        if kind == "route":
            tail = text[
                match.end() : text.find(";", match.end())
                if ";" in text[match.end() :]
                else len(text)
            ]
            methods = [
                method.upper()
                for method in re.findall(
                    r"\.\s*(get|post|put|patch|delete|head|options)\s*\(", tail
                )
            ]
        for prefix, notes in prefixes(owner):
            for method in methods:
                normalized = re.sub(r":([A-Za-z_]\w*)", r"{\1}", _join_path(prefix, path))
                output.append(
                    SourceEndpoint(
                        method=method,
                        path=normalized,
                        file=source.path,
                        line=text.count("\n", 0, match.start()) + 1,
                        framework="express",
                        warnings=notes,
                    )
                )
    return output


def discover_endpoints(
    files: list[SourceFile], warnings: list[str] | None = None
) -> list[SourceEndpoint]:
    """Merge deterministic declarations across supported frameworks without inventing routes."""
    warnings = warnings if warnings is not None else []
    endpoints = _python_endpoints(files, warnings)
    for source in files:
        suffix = PurePosixPath(source.path).suffix.lower()
        if suffix in {".java", ".kt"}:
            endpoints.extend(_spring_endpoints(source, warnings))
        elif suffix in {".js", ".mjs", ".cjs", ".ts"}:
            endpoints.extend(_express_endpoints(source))
    unique = {(item.file, item.line, item.method, item.path): item for item in endpoints}
    for item in unique.values():
        path = PurePosixPath(item.file.lower())
        if (
            set(path.parts) & {"test", "tests", "__tests__", "fixtures", "examples", "samples"}
            or path.name.startswith("test_")
            or ".test." in path.name
            or ".spec." in path.name
        ):
            item.warnings.append("此声明位于测试/示例源码，是否属于实际服务需核对入口注册。")
    return sorted(unique.values(), key=lambda item: (item.path, item.method, item.file, item.line))
