"""Base transport interface for MCP protocol."""

from abc import ABC, abstractmethod
from typing import Any


class BaseTransport(ABC):
    """Abstract base class for MCP transports.

    Concrete transports take construction config in their own ``__init__``;
    the ABC itself has a no-arg constructor so instances are interchangeable
    as ``BaseTransport`` after construction (connect / send_request / close).
    """

    def __init__(self) -> None:
        self._connected = False

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connection to the server."""

    @abstractmethod
    async def send_request(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Send MCP JSON-RPC request and return the result dict."""

    @abstractmethod
    async def close(self) -> None:
        """Close connection and cleanup resources."""

    @property
    def is_connected(self) -> bool:
        return self._connected
