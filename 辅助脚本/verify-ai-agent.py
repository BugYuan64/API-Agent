"""Opt-in live AI smoke test against a disposable, stateless loopback target.

Run with backend/.venv/Scripts/python.exe 辅助脚本/verify-ai-agent.py --live.
Reads the existing local model settings; never embeds or prints credentials.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from api_agent.agent.runtime import AgentRuntime  # noqa: E402
from api_agent.agent.schemas import AgentRunRequest  # noqa: E402
from api_agent.agent.service import AgentService  # noqa: E402
from api_agent.agent.store import AgentStore  # noqa: E402
from api_agent.infrastructure.config import load_settings  # noqa: E402
from api_agent.infrastructure.database import Database  # noqa: E402
from api_agent.llm.service import LLMSettingService  # noqa: E402


async def main():
    app = FastAPI()

    @app.post("/register")
    async def register(request: Request):
        body = await request.json()
        phone, password = body.get("phone"), body.get("password")
        # Intentional contract defect: the 12-digit branch produces 500, not 400.
        if isinstance(phone, str) and len(phone) == 12:
            return JSONResponse({"error": "uncaught validation branch"}, status_code=500)
        if (
            not isinstance(phone, str)
            or len(phone) != 11
            or not phone.isdigit()
            or not isinstance(password, str)
            or len(password) < 8
        ):
            return JSONResponse({"error": "invalid input"}, status_code=400)
        return {"ok": True}

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    database = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        settings = load_settings(project_root=ROOT, target_allowed_origins=[origin])
        database = Database(settings)
        async with database.session() as session:
            client = await LLMSettingService(session).get_client()
        if not client.is_configured:
            raise RuntimeError("model_not_configured")
        agent = AgentService(settings, client)
        store = AgentStore(settings.artifact_dir / "agent")
        runtime = AgentRuntime(agent, store)
        request = AgentRunRequest(
            contract={
                "openapi": "3.0.3",
                "info": {"title": "Stateless validation demo", "version": "1"},
                "paths": {
                    "/register": {
                        "post": {
                            "description": "公开注册校验接口，无需 token。phone 必须是 11 位数字；"
                            "password 至少 8 个字符。合法返回 200，所有参数非法返回 400。"
                            "请覆盖正向、12 位/10 位手机号、必填缺失、空密码。",
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["phone", "password"],
                                            "properties": {
                                                "phone": {
                                                    "type": "string",
                                                    "pattern": "^[0-9]{11}$",
                                                },
                                                "password": {"type": "string", "minLength": 8},
                                            },
                                        }
                                    }
                                },
                            },
                            "responses": {
                                "200": {"description": "输入合法"},
                                "400": {"description": "参数非法"},
                            },
                        }
                    }
                },
            },
            scope={
                "origin": origin,
                "allowed_methods": ["POST"],
                "path_templates": ["/register"],
                "delay_ms": 0,
            },
            max_cases=5,
            limits={
                "target_requests": 8,
                "model_calls": 8,
                "max_rounds": 2,
                "wall_time_seconds": 240.0,
            },
            # This helper owns a stateless throwaway endpoint; no actual users are created.
            allow_mutating_followups=True,
        )
        record = runtime.create(request)
        await runtime.run(record, request)
        report_path = settings.artifact_dir / "agent" / f"{record['id']}.md"
        report_path.write_text(store.report(record["id"]), encoding="utf-8")
        print(
            json.dumps(
                {
                    "run_id": record["id"],
                    "status": record["status"],
                    "stop_reason": record["stop_reason"],
                    "actual_llm_status": record["actual_llm_status"],
                    "rounds": len(record["rounds"]),
                    "budget": record["budget"],
                    "model_stages": [
                        {"stage": c["stage"], "status": c["status"], "error_kind": c["error_kind"]}
                        for c in record["model_calls"]
                    ],
                    "http_statuses": [
                        [c.get("http_status") for c in r.get("result", {}).get("executions", [])]
                        for r in record["rounds"]
                    ],
                    "report_path": str(report_path),
                },
                ensure_ascii=True,
            )
        )
        if record["status"] != "completed" or record["actual_llm_status"] != "used":
            return 1
        return 0
    finally:
        server.should_exit = True
        await task
        if database is not None:
            await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow up to 8 real model calls")
    if not parser.parse_args().live:
        parser.error("--live is required; this test uses the saved model service and its quota")
    raise SystemExit(asyncio.run(main()))
