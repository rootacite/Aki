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
        self._write_lock = asyncio.Lock()
        self.published: list[tuple[str, bytes]] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets or []
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        self._writers.clear()
        self._subscriptions.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        self._subscriptions[writer] = set()
        try:
            while True:
                header, body = await self._read_packet(reader)
                packet_type = header >> 4
                if packet_type == 1:  # CONNECT
                    await self._send(writer, 0x20, b"\x00\x00")
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
                    if qos == 1 and packet_id is not None:
                        await self._send(writer, 0x40, packet_id)
                    await self._broadcast(topic, payload)
                elif packet_type == 12:  # PINGREQ
                    await self._send(writer, 0xD0, b"")
                elif packet_type == 14:  # DISCONNECT
                    return
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            self._subscriptions.pop(writer, None)
            with contextlib.suppress(Exception):
                writer.close()

    async def _broadcast(self, topic: str, payload: bytes) -> None:
        body = len(topic.encode("utf-8")).to_bytes(2, "big") + topic.encode("utf-8") + payload
        for writer in list(self._writers):
            if topic in self._subscriptions.get(writer, set()):
                with contextlib.suppress(Exception):
                    await self._send(writer, 0x30, body)

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
