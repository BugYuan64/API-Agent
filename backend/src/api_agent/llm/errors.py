"""定义传输层与响应解析共用的大模型异常类型。

不依赖 HTTP 框架；上层按类型映射业务错误码，避免依赖供应商正文内容。
"""


class LLMError(Exception):
    """模型服务失败的应用层基类，由调用方决定 HTTP 或业务错误映射。"""


class LLMConfigError(LLMError):
    """缺少发起请求所需的本地模型配置。"""


class LLMAuthenticationError(LLMError):
    """供应商以鉴权失败状态拒绝请求。"""


class LLMConnectionError(LLMError):
    """连接或其他 HTTP 传输失败，尚未得到可处理的正常响应。"""


class LLMTimeoutError(LLMError):
    """HTTP 传输触发配置的超时限制，不代表已确认供应商停止处理请求。"""


class LLMResponseError(LLMError):
    """供应商返回非预期状态、非法 JSON 或不支持的响应结构。"""
