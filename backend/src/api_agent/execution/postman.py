"""Native CLI process boundary and ephemeral, local-only execution."""

import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import anyio

from api_agent.execution.collection import compile_collection
from api_agent.execution.report import normalize_report
from api_agent.execution.schemas import ExecutionPlan, RunResult, empty_result

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def process_environment(workspace: Path, *, trust_env: bool = False) -> dict[str, str]:
    """为子进程构造一个“最小且隔离”的环境变量集合。

    作用：既让 Windows 上的可执行文件能找到系统 DLL（因此保留 SYSTEMROOT/WINDIR/
    COMSPEC/PATH/PATHEXT），又把用户目录、临时目录全部重定向到本次临时工作区，
    并关闭遥测和网络跟踪；默认关闭代理（``NO_PROXY=*``），当 ``trust_env=True`` 时
    显式透传宿主代理环境变量。
    实现：先按白名单从 ``os.environ`` 挑出系统必需变量，再覆盖写入
    HOME/USERPROFILE/APPDATA/LOCALAPPDATA/TEMP/TMP（都指向 workspace）、
    ``DO_NOT_TRACK``/``POSTMAN_DISABLE_TELEMETRY``（关遥测）。

    注意：这里刻意不继承路径里可能存在的凭据类变量，目标鉴权变量由调用方单独注入。
    """
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT")
        if key in os.environ
    }
    environment.update(
        {
            "HOME": str(workspace),
            "USERPROFILE": str(workspace),
            "APPDATA": str(workspace),
            "LOCALAPPDATA": str(workspace),
            "TEMP": str(workspace),
            "TMP": str(workspace),
            "DO_NOT_TRACK": "1",
            "POSTMAN_DISABLE_TELEMETRY": "1",
        }
    )
    if trust_env:
        for key in _PROXY_ENV_KEYS:
            if key in os.environ:
                environment[key] = os.environ[key]
    else:
        environment["NO_PROXY"] = "*"
        environment["no_proxy"] = "*"
    return environment


def process_job(pid: int):
    """在 Windows 上把子进程放进一个“关闭即杀”的 Job Object。

    作用：如果本服务（或 MCP 宿主）异常退出，Job 句柄被关闭时会连带杀掉
    子进程及其后代，避免 Postman CLI 变成孤儿进程继续持有网络连接。
    实现：非 Windows 直接返回 None（交由 POSIX 的进程组/信号处理）；
    Windows 上创建 Job，读取扩展限制信息，把 ``KILL_ON_JOB_CLOSE`` 标志置位后写回，
    再以 PROCESS_TERMINATE|PROCESS_QUERY_INFORMATION 权限打开该 PID 并加入 Job；
    任一步失败都先关闭 Job 句柄再抛错，防止句柄泄漏。
    """
    if os.name != "nt":
        return None
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        job.Close()
        raise


async def stop_process(process: asyncio.subprocess.Process, job) -> None:
    """确保子进程（及其子进程树）被彻底终止并回收。

    作用：无论正常结束、超时还是异常路径都要执行，防止僵尸进程。
    实现：外层套 ``anyio.CancelScope(shield=True)``，
    使清理过程即使在任务被取消时也能跑完（否则协程一取消就断在清理中间）。
    然后：先关 Job 句柄（触发 KILL_ON_JOB_CLOSE）；若进程还活着，
    Windows 用 ``taskkill /T /F`` 杀整棵树，POSIX 则用进程组 ``killpg``；
    兜底再 ``process.kill()``；最后 ``await process.wait()`` 回收退出状态。
    """
    with anyio.CancelScope(shield=True):
        if job is not None:
            job.Close()
        if process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill.exe",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()


class PostmanRunner:
    """Postman CLI 的进程边界封装：可用性探测与一次性受控执行。

    作用：把“找可执行文件、建临时工作区、起子进程、限时等待、清理”这一整套
    生命周期逻辑收在一个类里，业务层只关心返回的 ``RunResult``。
    实现：构造时解析可执行文件绝对路径与临时根目录；
    每次运行都在临时目录里生成集合与报告，运行结束即删除整个目录。
    """

    name = "postman-cli"

    def __init__(
        self,
        executable: Path,
        temp_root: Path,
        omit_response_payload: bool = False,
        *,
        trust_env: bool = False,
    ):
        """记录可执行文件、临时根目录、响应负载裁剪与系统代理开关。

        实现：``resolve()`` 把可执行文件路径固化成绝对路径，
        避免后续子进程以别的 cwd 启动时相对路径解析到别处；
        ``temp_root`` 保持原样（调用方已给出项目内的绝对路径）；
        ``omit_response_payload`` 决定报告里是否保留响应头与响应体——
        关掉裁剪才能看到目标真实返回的内容。
        """
        self.executable = executable.resolve()
        self.temp_root = temp_root
        self.omit_response_payload = omit_response_payload
        self.trust_env = trust_env

    def available(self) -> bool:
        """判断 CLI 是否可用（足够开始一次运行）。

        作用：在做任何实际动作前先做“廉价”判断，避免无谓地建临时目录、起进程。
        实现：要求路径是普通文件；Windows 上额外要求扩展名为 ``.exe``，
        因为命令行方式无法可靠地执行 .cmd/.ps1 包装脚本。
        """
        return self.executable.is_file() and (os.name != "nt" or self.executable.suffix == ".exe")

    async def status(self) -> dict:
        """探测 CLI 是否可用及其版本号（不登录、不发目标请求）。

        实现：不可用直接返回 ``cli_unavailable``；否则建临时目录后执行 ``--version``，
        用与真实运行一致的最小环境与 Job 隔离；10 秒超时内读取前 1KB 输出，
        用正则提取 ``x.y.z`` 版本号；退出码为 0 且能解析出版本才算可用，
        任何 OSError/超时都归为不可用。``finally`` 里统一走 ``stop_process`` 清理。
        """
        if not self.available():
            return {"available": False, "error_kind": "cli_unavailable"}
        self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            process = None
            job = None
            try:
                process = await asyncio.create_subprocess_exec(
                    str(self.executable),
                    "--version",
                    cwd=directory,
                    env=process_environment(Path(directory)),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                )
                job = process_job(process.pid)
                output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
                version = re.search(rb"\b\d+\.\d+\.\d+\b", output[:1024])
                if process.returncode == 0 and version:
                    return {
                        "available": True,
                        "version": version[0].decode(),
                        "format": "v2.1-newman",
                    }
            except (OSError, TimeoutError):
                pass
            finally:
                if process is not None:
                    await stop_process(process, job)
        return {"available": False, "error_kind": "cli_unavailable"}

    async def _run_collection(
        self,
        collection: dict,
        plan: ExecutionPlan,
        *,
        timeout_ms: int,
        secrets: set[str] | None = None,
    ) -> tuple[RunResult, dict | None]:
        """在独立临时目录中运行一次 Postman CLI 集合，返回归一化结果与原始报告。"""
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            workspace = Path(directory)
            collection_path = workspace / "collection.json"
            report_path = workspace / "report.json"
            collection_path.write_text(
                json.dumps(collection, ensure_ascii=True),
                encoding="utf-8",
            )
            arguments = [
                str(self.executable),
                "collection",
                "run",
                str(collection_path),
                "--reporters",
                "json",
                "--reporter-json-export",
                str(report_path),
                "--reporter-json-structure",
                "newman",
                "--ignore-redirects",
                "--no-insecure-file-read",
                "--no-report-events",
                "--iteration-count",
                "1",
                "--timeout",
                str(max(100, timeout_ms)),
                "--timeout-request",
                str(plan.request_timeout_ms),
                "--timeout-script",
                "1000",
                "--silent",
            ]
            if plan.delay_ms > 0:
                arguments.extend(["--delay-request", str(plan.delay_ms)])
            if self.omit_response_payload:
                arguments.append("--reporter-json-omitAllHeadersAndBody")
            process = None
            job = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    cwd=workspace,
                    env=process_environment(workspace, trust_env=self.trust_env),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                )
                job = process_job(process.pid)
                await asyncio.wait_for(process.wait(), max(0.1, timeout_ms / 1000))
                if not report_path.is_file() or report_path.stat().st_size > 20 * 1024 * 1024:
                    return empty_result(plan, "error", "report_unavailable"), None
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if not isinstance(report, dict):
                    raise ValueError
                return (
                    normalize_report(
                        report,
                        plan,
                        process.returncode,
                        capture=not self.omit_response_payload,
                        secrets=secrets,
                    ),
                    report,
                )
            except TimeoutError:
                return empty_result(plan, "timed_out", "deadline_exceeded"), None
            except (OSError, ValueError, KeyError):
                return empty_result(plan, "error", "cli_execution_error"), None
            finally:
                if process is not None:
                    await stop_process(process, job)

    async def run(self, plan: ExecutionPlan, secrets: dict[str, str]) -> RunResult:
        """在临时工作区里通过单次 Postman CLI 进程执行计划并返回归一化结果。

        实现流程：
        ① 不可用则立刻返回 ``cli_unavailable``（此时不消耗任何预算）；
        ② 统一由 ``compile_collection`` 编译集合：无状态计划保持最小结构，
           有状态依赖链通过 ``prerequest`` 校验 ``depends_on``/``always_run``/变量与
           运行期安全并经 ``pm.execution.skipRequest()`` 无回退跳过不就绪步骤；
        ③ 在隔离临时目录执行单次 CLI 进程（仅 ``delay_ms > 0`` 时传入 ``--delay-request``），
           并由 ``normalize_report`` 统一补齐断言观察值与非敏感变量回显。
        """
        if not self.available():
            return empty_result(plan, "error", "cli_unavailable")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        result, _ = await self._run_collection(
            compile_collection(plan, secrets),
            plan,
            timeout_ms=plan.timeout_ms,
            secrets=set(secrets.values()),
        )
        result.duration_ms = round((time.monotonic() - started) * 1000, 2)
        return result
