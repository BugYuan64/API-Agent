"""Generate one OpenAPI file from an explicitly selected local project, without snapshots."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request

from api_agent.infrastructure.api import ApiError
from api_agent.infrastructure.orm import utc_now
from api_agent.projects.document_export import render_openapi
from api_agent.projects.source import SourceScanError, scan_directory


def require_local_browser(request: Request):
    """Do not expose a desktop dialog to remote clients or foreign browser origins."""
    local = {"localhost", "127.0.0.1", "::1"}
    try:
        host = urlsplit(f"//{request.headers.get('host', '')}")
        origin = request.headers.get("origin")
        allowed = (
            request.client
            and request.client.host in local
            and host.hostname in local
            and (not origin or urlsplit(origin).netloc == host.netloc)
            and request.headers.get("sec-fetch-site") in (None, "same-origin")
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise ApiError(403, "local_browser_required", "请从本机浏览器打开 API-Agent 后选择项目")


def choose_directory():
    """Run the interactive dialog in a separate process with a bounded lifetime."""
    if sys.platform == "win32":
        script = (
            "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Windows.Forms; "
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            "$dialog = [Windows.Forms.FolderBrowserDialog]::new(); "
            "$dialog.Description = 'Select a project for OpenAPI generation'; "
            "try { if ($dialog.ShowDialog() -eq 'OK') { [Console]::Write($dialog.SelectedPath) } } "
            "finally { $dialog.Dispose() }"
        )
        command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
    else:
        script = (
            "import tkinter as tk; from tkinter.filedialog import askdirectory; "
            "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
            "print(askdirectory(parent=root, mustexist=True, title='Select project')); "
            "root.destroy()"
        )
        command = [sys.executable, "-c", script]
    selected = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    ).stdout.rstrip("\r\n")
    return Path(selected) if selected else None


def output_directory(project: Path):
    """Reject linked roots or output destinations outside the selected project."""
    root = project.resolve(strict=True)
    output = project / "API"
    if (
        root != project
        or not root.is_dir()
        or output.is_symlink()
        or output.resolve() != root / "API"
    ):
        raise ValueError("invalid_project_directory")
    return output


def save_openapi(project: Path, text: str):
    """Write a new timestamped document without replacing existing files."""
    output = output_directory(project)
    output.mkdir(exist_ok=True)
    path = output_directory(project) / utc_now().strftime("openapi%Y%m%d-%H%M%S-%f.json")
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
    return path.name


async def pick_local_project_dir() -> Path | None:
    """Open the native desktop folder picker on a worker thread and return the selected path."""
    try:
        return await asyncio.to_thread(choose_directory)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ApiError(
            503, "project_picker_unavailable", "无法打开本机目录选择器，请确认后端运行在桌面会话中"
        ) from None


async def generate_openapi_for_directory(project: Path, settings, documents, agent) -> dict:
    """Scan a local project directory, analyze its routes with agent, and write OpenAPI JSON."""
    try:
        output = output_directory(project)
        snapshot = await asyncio.to_thread(
            scan_directory,
            project,
            exclude_paths=[output, settings.artifact_dir],
        )
    except (OSError, SourceScanError, ValueError):
        raise ApiError(
            400, "source_unreadable", "无法读取所选项目目录或目录中的 API 文件夹"
        ) from None
    try:
        async with asyncio.timeout(300):
            record = await documents.analyze(project.name, snapshot.model_dump(mode="json"), agent)
    except TimeoutError:
        raise ApiError(504, "project_document_timeout", "项目文档生成超时，未写入文件") from None
    if record["status"] == "failed" or not record["endpoints"]:
        raise ApiError(
            422, "project_document_failed", record["error"] or "未识别到接口，未写入文件"
        )
    document = render_openapi(record)
    document["info"]["version"] = utc_now().strftime("%Y%m%d-%H%M%S")
    text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
    try:
        filename = await asyncio.to_thread(save_openapi, project, text)
    except (OSError, ValueError):
        raise ApiError(
            400, "project_document_not_saved", "无法在所选项目的 API 目录保存文档"
        ) from None
    return {
        "filename": filename,
        "text": text if len(text.encode()) <= 524288 else None,
        "partial": record["status"] == "partial",
    }


async def create_project_document(request, agent):
    """Use only the native picker's selection, and release source data after the request."""
    project = await pick_local_project_dir()
    if project is None:
        return None
    return await generate_openapi_for_directory(
        project,
        request.app.state.settings,
        request.app.state.documents,
        agent,
    )
