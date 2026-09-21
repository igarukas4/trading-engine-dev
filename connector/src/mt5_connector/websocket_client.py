"""Injectable, read-only async WSS session boundary."""
from __future__ import annotations

import asyncio
import json
import random
import ssl
from urllib.parse import urlparse

from .config import ConnectorConfig
from .protocol import ConnectorProtocol, ProtocolError


class TransportError(RuntimeError):
    pass


class WssTransport:
    """Small lazy websockets adapter; the websocket dependency is optional."""

    def __init__(self, url: str, *, local_test: bool = False, connect_factory=None,
                 ssl_context: ssl.SSLContext | None = None):
        parsed = urlparse(url)
        if parsed.scheme.lower() != "wss" and not local_test:
            raise TransportError("WSS transport requires a wss:// URL")
        if ssl_context is not None and (ssl_context.check_hostname is False or
                                        ssl_context.verify_mode == ssl.CERT_NONE):
            raise TransportError("TLS certificate verification cannot be disabled")
        self.url = url
        self._factory = connect_factory
        self._ssl_context = ssl_context
        self._socket = None

    async def connect(self):
        if self._factory is None:
            try:
                import websockets
            except ImportError as exc:
                raise TransportError("websockets is required for live WSS runtime") from exc
            self._socket = await websockets.connect(self.url, ssl=self._ssl_context)
        else:
            self._socket = await self._factory(self.url, ssl=self._ssl_context)
        return self

    async def send(self, message: str):
        if self._socket is None:
            raise TransportError("transport is not connected")
        await self._socket.send(message)

    async def recv(self):
        if self._socket is None:
            raise TransportError("transport is not connected")
        return await self._socket.recv()

    async def close(self):
        if self._socket is not None:
            await self._socket.close()
            self._socket = None


class ConnectorClient:
    """Runs one bounded protocol session; commands are never initiated locally."""

    def __init__(self, config: ConnectorConfig, adapter, *, transport=None,
                 transport_factory=None, random_fn=random.random):
        self.config = config
        self.protocol = ConnectorProtocol(config, adapter, random_fn=random_fn)
        self.transport = transport
        self.transport_factory = transport_factory
        self._transport = None

    @staticmethod
    def _decode(frame):
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        if isinstance(frame, str):
            try:
                return json.loads(frame)
            except json.JSONDecodeError as exc:
                raise ProtocolError("invalid JSON frame") from exc
        if not isinstance(frame, dict):
            raise ProtocolError("invalid frame")
        return frame

    async def _open(self):
        transport = self.transport
        if transport is None:
            transport = (self.transport_factory(self.config.wss_url)
                         if self.transport_factory else
                         WssTransport(self.config.wss_url, local_test=self.config.local_test))
        self._transport = transport
        connect = getattr(transport, "connect", None)
        if connect is not None:
            await connect()
        return transport

    async def connect_once(self, secret, *, max_messages=1):
        if not secret:
            raise ProtocolError("secret is required")
        self.protocol.begin_session()
        transport = await self._open()
        try:
            await transport.send(json.dumps(self.protocol.hello(secret), separators=(",", ":")))
            snapshot = self._decode(await transport.recv())
            self.protocol.accept_snapshot(snapshot)
            processed = 0
            while max_messages is None or processed < max_messages:
                message = self._decode(await transport.recv())
                response = self.protocol.handle(message)
                if message.get("type") == "heartbeat_ack":
                    processed += 1
                    continue
                if response is not None:
                    responses = response if isinstance(response, list) else [response]
                    for frame in responses:
                        await transport.send(json.dumps(frame, separators=(",", ":")))
                processed += 1
            return self.protocol
        finally:
            close = getattr(transport, "close", None)
            if close is not None:
                await close()

    async def run(self, secret_provider, *, max_attempts=1, max_messages=1,
                  sleep=asyncio.sleep):
        """Reconnect with configured bounded jitter; secret is supplied, never discovered."""
        if not callable(secret_provider):
            raise TypeError("secret_provider callback is required")
        for attempt in range(max_attempts):
            try:
                return await self.connect_once(secret_provider(), max_messages=max_messages)
            except (OSError, TransportError, ProtocolError):
                if attempt + 1 >= max_attempts:
                    raise
                await sleep(self.protocol.backoff(attempt))
        raise RuntimeError("unreachable")
