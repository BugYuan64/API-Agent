"""WebSocket handshake policy and bounded transport exceptions."""

from websockets.asyncio.client import connect


class NoRedirectConnect(connect):
    """Preserve the original handshake response without following Location."""

    def process_redirect(self, exc: Exception) -> Exception:
        """Return the handshake failure without connecting to another target."""
        return exc


class StreamLimitExceeded(Exception):
    """Incoming transport bytes exhausted the session budget."""
