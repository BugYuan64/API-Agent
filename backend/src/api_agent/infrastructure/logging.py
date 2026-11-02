"""Structured application logs with credential redaction at the output boundary."""

import json
import logging
import re
from collections.abc import Iterable
from copy import copy
from datetime import UTC, datetime

from api_agent.infrastructure.config import Settings

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = {
    "authorization",
    "proxyauthorization",
    "cookie",
    "setcookie",
    "apikey",
    "xapikey",
    "password",
    "passwd",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "llmapikey",
}
HEADER_CREDENTIAL = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=]\s*[^\r\n]+"
)
ASSIGNED_CREDENTIAL = re.compile(
    r"(?i)\b(api[_-]?key|x-api-key|password|passwd|secret|access[_-]?token|"
    r"refresh[_-]?token|token)[\"']?\s*[:=]\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&]+)"
)
URL_CREDENTIAL = re.compile(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", re.IGNORECASE)
BEARER_CREDENTIAL = re.compile(r"\bBearer\s+[^\s,;\"']+", re.IGNORECASE)


class RedactingJsonFormatter(logging.Formatter):
    """把日志记录格式化成单行 JSON，并在输出边界抹掉凭据。

    作用：日志要可机读（便于排查），但绝不能出现 token、Cookie、连接串密码。
    实现：三层脱敏——①已知明文密钥的表驱动替换（``self.secrets``）；
    ②正则识别 ``Authorization: xxx``、``api_key=xxx``、``https://u:p@host``、``Bearer xxx``；
    ③按键名归一化后命中 ``SENSITIVE_KEYS`` 的字典值整段替换成 ``[REDACTED]``。
    """

    def __init__(self, *, secrets: Iterable[str] = ()):
        """记录需要精确匹配删除的明文密钥。

        实现：过滤掉空值后按长度降序排序——
        先替换长串可避免“短密钥是长密钥子串”时只删掉一半、留下残片。
        """
        super().__init__()
        self.secrets = tuple(sorted((value for value in secrets if value), key=len, reverse=True))

    def redact_text(self, value: str) -> str:
        """对一段文本做脱敏，必要时先按 JSON 结构递归处理。

        实现：如果是 ``{``/``[`` 开头的文本，尝试 ``json.loads`` 解析后走结构化
        ``redact``（能按 key 精确命中敏感字段），再 dump 回字符串；
        解析失败（不是合法 JSON 或递归过深）就退回纯文本正则替换流程。
        """
        if value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(self.redact(structured), ensure_ascii=False)
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        value = HEADER_CREDENTIAL.sub(lambda match: f"{match[1]}: {REDACTED}", value)
        value = ASSIGNED_CREDENTIAL.sub(lambda match: f"{match[1]}={REDACTED}", value)
        value = URL_CREDENTIAL.sub(lambda match: f"{match[1]}{REDACTED}@", value)
        return BEARER_CREDENTIAL.sub(f"Bearer {REDACTED}", value)

    def redact(self, value):
        """对任意嵌套数据结构递归脱敏。

        实现：dict 按“键名去掉非字母数字再小写”归一化后查 ``SENSITIVE_KEYS``，
        命中则整值替换，未命中则递归处理值；list/tuple 逐元素递归；
        标量（None/bool/int/float）原样返回；其他类型转成字符串走文本脱敏。
        """
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = str(key)
                normalized = re.sub(r"[^a-z0-9]", "", name.lower())
                result[self.redact_text(name)] = (
                    REDACTED if normalized in SENSITIVE_KEYS else self.redact(item)
                )
            return result
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.redact_text(str(value))

    def format(self, record: logging.LogRecord) -> str:
        """生成一行 JSON 日志。

        实现：先 ``copy`` 出副本来改参数，避免污染其他 handler 看到的原始 record；
        再分别处理 ``record.args``（tuple/dict）和 ``record.msg``；
        消息格式化失败时输出固定占位文本，因为 logging 自身的 handleError
        会把未经脱敏的原始 args 打到 stderr；
        最后组装 timestamp/level/logger/message，附带可选的 ``context`` 与异常堆栈。
        """
        # Work on a copy: other handlers must not see mutated arguments.
        safe_record = copy(record)
        if isinstance(record.args, tuple):
            safe_record.args = tuple(self.redact(item) for item in record.args)
        elif isinstance(record.args, dict):
            safe_record.args = self.redact(record.args)
        if isinstance(record.msg, dict):
            message = self.redact(record.msg)
        else:
            try:
                message = self.redact_text(safe_record.getMessage())
            except (TypeError, ValueError, KeyError, IndexError):
                # logging.handleError would otherwise print the original args.
                message = "Log message omitted (unsafe formatting)"
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": self.redact_text(record.name),
            "message": message,
        }
        context = getattr(record, "context", None)
        if context is not None:
            payload["context"] = self.redact(context)
        if record.exc_info:
            payload["exception"] = self.redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings) -> logging.Logger:
    """配置 ``api_agent`` 这一个 logger，并挂上脱敏 JSON 格式化器。

    作用：只接管应用自己的日志，避免改动 uvicorn/sqlalchemy 等宿主 logger 的配置。
    实现：收集明文密钥（仅模型 API Key）、新建 StreamHandler 并绑定脱敏格式化器；
    关闭并清空该 logger 已有 handler（重复调用不会叠加输出），设置级别；
    ``propagate=False`` 防止日志再冒泡到 root logger 造成重复打印。
    """
    secrets = [settings.llm_api_key.get_secret_value()] if settings.llm_api_key else []
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter(secrets=secrets))
    logger = logging.getLogger("api_agent")
    for old_handler in logger.handlers:
        old_handler.close()
    logger.handlers[:] = [handler]
    logger.setLevel(settings.log_level)
    logger.propagate = False
    return logger
