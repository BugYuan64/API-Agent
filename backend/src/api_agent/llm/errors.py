"""Stable supplier failures shared by HTTP transport and response parsing."""


class LLMError(Exception):
    """Base failure exposed to the application layer."""


class LLMConfigError(LLMError):
    """Required local configuration is missing."""


class LLMAuthenticationError(LLMError):
    """The supplier rejected authentication."""


class LLMConnectionError(LLMError):
    """The supplier transport failed."""


class LLMTimeoutError(LLMError):
    """The supplier did not respond before the deadline."""


class LLMResponseError(LLMError):
    """The supplier returned an unsupported response."""
