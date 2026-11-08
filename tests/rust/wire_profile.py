"""Linux loopback-only kernel timestamps for an isolated synthetic diagnosis."""
from __future__ import annotations

import ctypes
import multiprocessing
import re
import socket
import struct
import threading


def tcp_packet(packet, ports):
    if len(packet) < 54 or packet[12:14] != b"\x08\x00" or packet[23] != 6:
        return None
    ip_size = (packet[14] & 15) * 4
    tcp = 14 + ip_size
    source, target = struct.unpack_from("!HH", packet, tcp)
    if source not in ports and target not in ports:
        return None
    if packet[26:30] != b"\x7f\x00\x00\x01" or packet[30:34] != b"\x7f\x00\x00\x01":
        return None
    flags = packet[tcp + 13]
    offset = tcp + (packet[tcp + 12] >> 4) * 4
    if ip_size != 20 or offset < tcp + 20 or offset > len(packet):
        return None
    sequence = struct.unpack_from("!I", packet, tcp + 4)[0]
    total = struct.unpack_from("!H", packet, 16)[0]
    length = max(len(packet) - offset, total - (offset - 14))
    return source, target, flags, sequence, packet[offset:], length


def parse_packet(packet, timestamp_us, ports):
    parsed = tcp_packet(packet, ports)
    if parsed is None:
        return None
    source, target, flags, _, payload, _ = parsed
    kind = "syn-ack" if flags & 2 and flags & 16 else "syn" if flags & 2 else (
        "request" if payload.startswith(b"GET ") else
        "response" if payload.startswith(b"HTTP/") else None
    )
    if kind is None:
        return None
    trace = re.search(rb"\r\nx-t4-relay-client-trace: ([A-Za-z0-9_.:-]{1,96})\r\n",
                      payload, re.IGNORECASE)
    return {
        "source_port": source, "target_port": target, "kind": kind,
        "timestamp_us": timestamp_us,
        "trace": trace[1].decode("ascii") if trace else None,
        "body_with_headers": kind == "response" and b"\r\n\r\n" in payload
        and len(payload.split(b"\r\n\r\n", 1)[1]) > 0,
    }


def packet_filter(body_ports=(), completed_targets=()):
    instructions = []
    labels = {}

    def mark(name):
        labels[name] = len(instructions)

    def emit(code, jt, jf, value):
        instructions.append((code, jt, jf, value))

    emit(0x28, 0, 0, 12)
    emit(0x15, 0, "reject", 0x800)
    emit(0x30, 0, 0, 23)
    emit(0x15, 0, "reject", 6)
    if body_ports:
        emit(0x28, 0, 0, 34)
        for port in sorted(body_ports):
            emit(0x15, "body", 0, port)
        emit(0x05, 0, 0, "regular")
        mark("body")
        emit(0x28, 0, 0, 36)
        for port in sorted(completed_targets):
            emit(0x15, "reject", 0, port)
        emit(0x06, 0, 0, 512)
        mark("regular")
    emit(0x30, 0, 0, 47)
    emit(0x45, "accept", 0, 2)
    for code, value in (
        (0x30, 46), (0x54, 0xF0), (0x74, 2), (0x04, 34), (0x07, 0), (0x40, 0),
    ):
        emit(code, 0, 0, value)
    emit(0x15, "accept", 0, 0x47455420)
    emit(0x15, 0, "reject", 0x48545450)
    mark("accept")
    emit(0x06, 0, 0, 512)
    mark("reject")
    emit(0x06, 0, 0, 0)
    return [
        (code, labels[jt] - index - 1 if isinstance(jt, str) else jt,
         labels[jf] - index - 1 if isinstance(jf, str) else jf,
         labels[value] - index - 1 if isinstance(value, str) else value)
        for index, (code, jt, jf, value) in enumerate(instructions)
    ]


class WireProfile:
    """No packet payloads are retained; BPF discards bulk body packets in-kernel."""

    def __init__(self, ports):
        self.ports = set(ports)
        self.events = []
        self.error = None
        self.dropped = None
        self.stop = threading.Event()
        self.sock = None
        self.thread = None

    def __enter__(self):
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        self.sock = sock
        try:
            sock.bind(("lo", 0))
            # A snaplen cap does not make a burst of large TCP frames free to queue.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 ** 2)
            # IPv4 loopback has no IP options here. Keep SYN and GET/HTTP prefix only.
            self._attach_filter(self.initial_filter())
            sock.setsockopt(socket.SOL_SOCKET, 35, 1)  # SO_TIMESTAMPNS, native timespec.
            sock.settimeout(.1)
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            sock.close()
            raise

    def initial_filter(self):
        return packet_filter()

    def _attach_filter(self, instructions):
        class Filter(ctypes.Structure):
            _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                        ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]
        class Program(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]
        filters = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
        program = Program(len(filters), filters)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.setsockopt(self.sock.fileno(), socket.SOL_SOCKET, 26,
                           ctypes.byref(program), ctypes.sizeof(program)):
            raise OSError(ctypes.get_errno(), "could not attach diagnostic packet filter")

    def record_packet(self, packet, timestamp_us):
        event = parse_packet(packet, timestamp_us, self.ports)
        if event is not None:
            if len(self.events) >= 10000:
                raise RuntimeError("diagnostic event bound exceeded")
            self.events.append(event)

    def _read(self):
        try:
            while True:
                try:
                    packet, ancillary, _, address = self.sock.recvmsg(512, 128)
                except socket.timeout:
                    if self.stop.is_set():
                        break
                    continue
                if address[2] != 4:  # Loopback presents two copies; keep outgoing only.
                    continue
                timestamp = next(
                    (struct.unpack("@ll", data) for level, kind, data in ancillary
                     if level == socket.SOL_SOCKET and kind == 35), None,
                )
                if timestamp is None:
                    raise RuntimeError("kernel packet timestamp missing")
                self.record_packet(packet, timestamp[0] * 1_000_000 + timestamp[1] // 1000)
        except BaseException as error:
            self.error = f"{type(error).__name__}: {error}"

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=2)
        self.dropped = struct.unpack("II", self.sock.getsockopt(263, 6, 8))[1]
        self.sock.close()
        if self.thread.is_alive() or self.error or self.dropped:
            reason = self.error or (f"packet drops ({self.dropped})" if self.dropped else "join timeout")
            raise RuntimeError(f"kernel timing capture failed: {reason}; dropped={self.dropped}")


class FirstBodyWireProfile(WireProfile):
    """Observe first-batch downstream body sequences, not just HTTP header packets."""

    def __init__(self, ports, *, body_port, expected):
        super().__init__(ports)
        assert body_port in self.ports and expected > 0
        self.body_port, self.expected = body_port, expected
        self.flows = {}
        self.body_events = 0

    def initial_filter(self):
        return packet_filter({self.body_port})

    def record_packet(self, packet, timestamp_us):
        super().record_packet(packet, timestamp_us)
        if self.body_events >= self.expected:
            return
        parsed = tcp_packet(packet, self.ports)
        if parsed is None:
            return
        source, target, _, sequence, payload, length = parsed
        if target == self.body_port and payload.startswith(b"GET "):
            request = parse_packet(packet, timestamp_us, self.ports)
            if request["trace"]:
                self.flows[(target, source)] = {"trace": request["trace"]}
            return
        flow = self.flows.get((source, target))
        if source != self.body_port or flow is None or flow.get("complete") or not length:
            return
        if payload.startswith(b"HTTP/"):
            end = payload.find(b"\r\n\r\n")
            if end < 0:
                raise RuntimeError("diagnostic response headers exceed capture prefix")
            header_size = end + 4
            flow["body_sequence"] = (sequence + header_size) & 0xFFFFFFFF
            if length <= header_size:
                return
        body_sequence = flow.get("body_sequence")
        if body_sequence is None:
            raise RuntimeError("body packet without correlated response headers")
        if ((body_sequence - sequence) & 0xFFFFFFFF) >= length:
            raise RuntimeError(
                f"first body sequence missing from capture: expected={body_sequence}, "
                f"sequence={sequence}, captured={len(payload)}, length={length}, flags={parsed[2]}",
            )
        flow["complete"] = True
        self.body_events += 1
        self.events.append({
            "kind": "first-body", "source_port": source, "target_port": target,
            "trace": flow["trace"], "timestamp_us": timestamp_us,
        })
        if self.sock is not None:
            # Do not queue bulk media from streams whose first byte is already known.
            completed = {target for (_, target), flow in self.flows.items() if flow.get("complete")}
            self._attach_filter(
                packet_filter() if self.body_events == self.expected
                else packet_filter({self.body_port}, completed),
            )


def _first_body_process(connection, ports, body_port, expected):
    try:
        with FirstBodyWireProfile(ports, body_port=body_port, expected=expected) as capture:
            connection.send({"ready": True})
            if connection.recv() != "stop":
                raise RuntimeError("invalid diagnostic control message")
        connection.send({"events": capture.events, "dropped": capture.dropped})
    except BaseException as error:
        connection.send({"error": type(error).__name__ + ": " + str(error)})
    finally:
        connection.close()


class ProcessFirstBodyWireProfile:
    """Keep packet parsing out of the original async consumer's interpreter."""

    def __init__(self, ports, *, body_port, expected):
        self.ports, self.body_port, self.expected = ports, body_port, expected
        self.events, self.dropped, self.body_events = [], None, 0
        self.connection = self.process = None

    def _receive(self):
        if not self.connection.poll(10):
            raise RuntimeError("diagnostic collector timed out")
        result = self.connection.recv()
        if result.get("error"):
            raise RuntimeError("diagnostic collector failed: " + result["error"])
        return result

    def _close(self):
        if self.process is not None:
            if self.process.pid is not None:
                self.process.join(timeout=2)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join()
            self.process.close()
        if self.connection is not None:
            self.connection.close()

    def __enter__(self):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(
            target=_first_body_process,
            args=(child, self.ports, self.body_port, self.expected), daemon=True,
        )
        try:
            self.process.start()
            child.close()
            assert self._receive().get("ready"), "diagnostic collector not ready"
            return self
        except BaseException:
            child.close()
            self._close()
            raise

    def __exit__(self, *exc):
        try:
            self.connection.send("stop")
            result = self._receive()
            self.events, self.dropped = result["events"], result["dropped"]
            self.body_events = sum(event["kind"] == "first-body" for event in self.events)
            self.process.join(timeout=2)
            if self.process.exitcode != 0:
                raise RuntimeError("diagnostic collector did not exit cleanly")
        finally:
            self._close()
