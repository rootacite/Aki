from __future__ import annotations

import asyncio
import contextlib

from engine.mqtt_client import encode_remaining_length


class FakeMqttBroker:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._subscriptions: dict[asyncio.StreamWriter, set[str]] = {}
        self._client_ids: dict[asyncio.StreamWriter, str] = {}
        self._active_clients: dict[str, asyncio.StreamWriter] = {}
        self._sessions: dict[str, set[str]] = {}
        self._offline: dict[str, list[tuple[str, bytes, int]]] = {}
        self._next_packet_id = 0
        self._write_lock = asyncio.Lock()
        self.published: list[tuple[str, bytes]] = []
        self.published_qos: list[int] = []
        self.connects: list[dict[str, object]] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets or []
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        if server is not None:
            await server.wait_closed()
        self._writers.clear()
        self._subscriptions.clear()
        self._client_ids.clear()
        self._active_clients.clear()
        self._sessions.clear()
        self._offline.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        self._subscriptions[writer] = set()
        try:
            while True:
                header, body = await self._read_packet(reader)
                packet_type = header >> 4
                if packet_type == 1:  # CONNECT
                    client_id, clean_session = self._parse_connect(body)
                    self.connects.append(
                        {"client_id": client_id, "clean_session": clean_session}
                    )
                    self._client_ids[writer] = client_id
                    self._active_clients[client_id] = writer
                    if clean_session:
                        self._sessions.pop(client_id, None)
                        self._offline.pop(client_id, None)
                    subscriptions = set(self._sessions.get(client_id, set()))
                    self._subscriptions[writer] = subscriptions
                    queued = self._offline.pop(client_id, [])
                    flags = 0x01 if subscriptions else 0x00
                    await self._send(writer, 0x20, bytes([flags, 0]))
                    for topic, payload, qos in queued:
                        await self._send_publish(writer, topic, payload, qos)
                elif packet_type == 8:  # SUBSCRIBE
                    if len(body) < 2:
                        return
                    packet_id = body[:2]
                    cursor = 2
                    codes = bytearray()
                    while cursor < len(body):
                        topic_len = int.from_bytes(body[cursor : cursor + 2], "big")
                        cursor += 2
                        topic = body[cursor : cursor + topic_len].decode("utf-8")
                        cursor += topic_len
                        self._subscriptions[writer].add(topic)
                        client_id = self._client_ids.get(writer)
                        if client_id is not None:
                            self._sessions.setdefault(client_id, set()).add(topic)
                        codes.append(0x01)
                        cursor += 1
                    await self._send(writer, 0x90, packet_id + bytes(codes))
                elif packet_type == 3:  # PUBLISH
                    qos = (header >> 1) & 0x03
                    topic_len = int.from_bytes(body[:2], "big")
                    cursor = 2
                    topic = body[cursor : cursor + topic_len].decode("utf-8")
                    cursor += topic_len
                    packet_id = None
                    if qos > 0:
                        packet_id = body[cursor : cursor + 2]
                        cursor += 2
                    payload = body[cursor:]
                    self.published.append((topic, payload))
                    self.published_qos.append(qos)
                    if qos == 1 and packet_id is not None:
                        await self._send(writer, 0x40, packet_id)
                    await self._broadcast(topic, payload, qos)
                elif packet_type == 12:  # PINGREQ
                    await self._send(writer, 0xD0, b"")
                elif packet_type == 14:  # DISCONNECT
                    return
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            self._subscriptions.pop(writer, None)
            client_id = self._client_ids.pop(writer, None)
            if client_id is not None and self._active_clients.get(client_id) is writer:
                self._active_clients.pop(client_id, None)
            with contextlib.suppress(Exception):
                writer.close()

    async def _broadcast(self, topic: str, payload: bytes, qos: int) -> None:
        for writer in list(self._writers):
            if topic in self._subscriptions.get(writer, set()):
                with contextlib.suppress(Exception):
                    await self._send_publish(writer, topic, payload, qos)
        if qos <= 0:
            return
        for client_id, subscriptions in self._sessions.items():
            if topic not in subscriptions or client_id in self._active_clients:
                continue
            self._offline.setdefault(client_id, []).append((topic, payload, qos))

    def _parse_connect(self, body: bytes) -> tuple[str, bool]:
        cursor = 0
        name_len = int.from_bytes(body[cursor : cursor + 2], "big")
        cursor += 2 + name_len
        cursor += 1  # protocol level
        flags = body[cursor]
        cursor += 3  # flags + keepalive
        client_len = int.from_bytes(body[cursor : cursor + 2], "big")
        cursor += 2
        client_id = body[cursor : cursor + client_len].decode("utf-8", "replace")
        return client_id, bool(flags & 0x02)

    async def _send_publish(
        self, writer: asyncio.StreamWriter, topic: str, payload: bytes, qos: int
    ) -> None:
        body = len(topic.encode("utf-8")).to_bytes(2, "big") + topic.encode("utf-8")
        if qos > 0:
            self._next_packet_id = (self._next_packet_id % 65535) + 1
            body += self._next_packet_id.to_bytes(2, "big")
        body += payload
        header = (3 << 4) | ((qos & 0x03) << 1)
        await self._send(writer, header, body)

    async def _send(self, writer: asyncio.StreamWriter, header: int, body: bytes) -> None:
        packet = bytes([header]) + encode_remaining_length(len(body)) + body
        async with self._write_lock:
            writer.write(packet)
            await writer.drain()

    @staticmethod
    async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
        header = (await reader.readexactly(1))[0]
        multiplier = 1
        remaining = 0
        for _ in range(4):
            byte = (await reader.readexactly(1))[0]
            remaining += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        body = await reader.readexactly(remaining) if remaining else b""
        return header, body
