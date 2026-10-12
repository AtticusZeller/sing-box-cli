import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_Server"

    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        api = self.server.api
        if api.token and self.headers.get("Authorization") != f"Bearer {api.token}":
            self._send_json(401, {"message": "Unauthorized"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send_json(200, {"hello": "clash"})
        elif path == "/connections":
            self._send_json(200, api.connections)
        elif path in ("/traffic", "/memory", "/logs"):
            self._stream(path)
        else:
            self._send_json(404, {"message": "Not Found"})

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, path: str) -> None:
        api = self.server.api
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        sent = 0
        while not self.server.stopped.is_set():
            if path == "/logs" and sent >= api.log_limit:
                break
            line = (json.dumps(api.stream_payload(path)) + "\n").encode()
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            except OSError:
                return
            sent += 1
            time.sleep(0.02)
        # Return without the terminating chunk, like a crashed sing-box process.


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, api: "FakeSingBox") -> None:
        super().__init__(("127.0.0.1", api.port), _Handler)
        self.api = api
        self.stopped = threading.Event()


class FakeSingBox:
    """Minimal sing-box Clash API that can be stopped and restarted on one port."""

    def __init__(self, token: str | None = None) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port: int = sock.getsockname()[1]
        self.token = token
        self.traffic = {"up": 100, "down": 200}
        self.memory = {"inuse": 1024, "total": 2048}
        self.connections: dict[str, Any] = {
            "uploadTotal": 10,
            "downloadTotal": 20,
            "connections": [],
        }
        self.log_limit = 3
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stream_payload(self, path: str) -> dict[str, Any]:
        if path == "/traffic":
            return self.traffic
        if path == "/memory":
            return self.memory
        return {"type": "info", "payload": "fake log line"}

    def start(self) -> None:
        self._server = _Server(self)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Refuse new connections and drop open streams mid-body."""
        if self._server is None:
            return
        self._server.stopped.set()
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=5)
        self._server = None
