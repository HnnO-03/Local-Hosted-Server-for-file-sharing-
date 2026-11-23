from __future__ import annotations

import datetime
import json
import mimetypes
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import api

# ==============================================================================
# Network & Protocol Configuration
# ==============================================================================
HOST = "0.0.0.0"
PORT = int(os.environ.get("LAB_PORT", "8080"))

MAX_HEADER_SIZE = 65536           # 64 KB max HTTP header block
MAX_JSON_BODY_SIZE = 1024 * 1024  # 1 MB max JSON payload
SOCKET_TIMEOUT = 60.0             # Seconds before idle client times out

STATUS_PHRASES = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    411: "Length Required",
    413: "Payload Too Large",
    500: "Internal Server Error",
}

log_lock = threading.Lock()


def log(message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    thread_name = threading.current_thread().name
    with log_lock:
        print(f"[{timestamp}] [{thread_name}] {message}", flush=True)


def get_local_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ==============================================================================
# Raw HTTP/1.1 Protocol Helper (Passed to api.py)
# ==============================================================================
class HTTPResponder:
    """Encapsulates raw HTTP framing and chunked socket streaming."""

    @staticmethod
    def build_headers(
        status_code: int,
        content_type: str | None = None,
        content_length: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        phrase = STATUS_PHRASES.get(status_code, "OK")
        lines = [
            f"HTTP/1.1 {status_code} {phrase}",
            f"Server: {api.SERVER_HEADER}",
            f"X-Served-By-Thread: {threading.current_thread().name}",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers: Content-Type, Authorization, X-File-Overwrite",
            "Access-Control-Expose-Headers: Content-Disposition, Content-Length, X-Served-By-Thread",
            "Connection: close",
        ]
        if content_type is not None:
            lines.append(f"Content-Type: {content_type}")
        if content_length is not None:
            lines.append(f"Content-Length: {content_length}")
        if extra_headers:
            for key, value in extra_headers.items():
                lines.append(f"{key}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

    def send_json(self, conn: socket.socket, status_code: int, payload: dict) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self.build_headers(
            status_code,
            content_type="application/json; charset=utf-8",
            content_length=len(body),
        )
        conn.sendall(headers + body)
        return status_code

    def send_error(self, conn: socket.socket, status_code: int, message: str) -> int:
        return self.send_json(conn, status_code, {"status": "error", "error": message})

    def send_empty(self, conn: socket.socket, status_code: int = 204) -> int:
        conn.sendall(self.build_headers(status_code, content_length=0))
        return status_code

    def send_html(self, conn: socket.socket, status_code: int, html_text: str) -> int:
        body = html_text.encode("utf-8")
        headers = self.build_headers(
            status_code,
            content_type="text/html; charset=utf-8",
            content_length=len(body),
        )
        conn.sendall(headers + body)
        return status_code

    def stream_file(self, conn: socket.socket, file_path: Path, as_attachment: bool = False) -> int:
        if not file_path.exists() or not file_path.is_file():
            return self.send_error(conn, 404, "Requested file was not found.")

        file_size = file_path.stat().st_size
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "application/octet-stream"

        extra_headers: dict[str, str] = {}
        if as_attachment:
            ascii_fallback = (
                file_path.name.encode("ascii", "ignore").decode("ascii").replace('"', "_") or "download"
            )
            utf8_name = quote(file_path.name, safe="")
            extra_headers["Content-Disposition"] = (
                f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{utf8_name}'
            )
            mime_type = "application/octet-stream"

        headers = self.build_headers(
            200,
            content_type=mime_type,
            content_length=file_size,
            extra_headers=extra_headers,
        )
        conn.sendall(headers)

        with open(file_path, "rb") as file_obj:
            while True:
                chunk = file_obj.read(api.CHUNK_SIZE)
                if not chunk:
                    break
                conn.sendall(chunk)
        return 200

    @staticmethod
    def read_json_body(conn: socket.socket, headers: dict[str, str], initial_body: bytes) -> dict:
        raw_len = headers.get("content-length")
        if raw_len is None:
            return {}
        try:
            content_length = int(raw_len)
            if content_length < 0:
                raise ValueError
        except ValueError:
            raise ValueError("Invalid Content-Length header.")

        if content_length > MAX_JSON_BODY_SIZE:
            raise OverflowError("JSON body is too large.")

        body = bytearray(initial_body[:content_length])
        while len(body) < content_length:
            remaining = content_length - len(body)
            chunk = conn.recv(min(8192, remaining))
            if not chunk:
                raise EOFError("Client disconnected before finishing request body.")
            body.extend(chunk)

        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid UTF-8 JSON.") from exc

        if not isinstance(parsed, dict):
            raise ValueError("JSON payload must be an object.")
        return parsed


http_responder = HTTPResponder()


# ==============================================================================
# Raw HTTP Request Parser
# ==============================================================================
def parse_http_request(conn: socket.socket) -> dict | None:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(8192)
        if not chunk:
            return None
        buffer.extend(chunk)
        if len(buffer) > MAX_HEADER_SIZE:
            raise OverflowError("HTTP headers exceed maximum allowed size.")

    sep_index = buffer.index(b"\r\n\r\n")
    header_raw = bytes(buffer[:sep_index]).decode("iso-8859-1")
    initial_body = bytes(buffer[sep_index + 4 :])

    lines = header_raw.split("\r\n")
    request_line = lines[0].split(" ")
    if len(request_line) != 3:
        raise ValueError("Malformed HTTP request line.")

    method, raw_target, version = request_line
    method = method.upper()

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()

    if headers.get("expect", "").lower() == "100-continue":
        conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

    parsed_url = urlsplit(raw_target)
    return {
        "method": method,
        "route": unquote(parsed_url.path),
        "query": parse_qs(parsed_url.query, keep_blank_values=True),
        "version": version,
        "headers": headers,
        "initial_body": initial_body,
    }


# ==============================================================================
# Per-Client Worker Thread & Main TCP Accept Loop
# ==============================================================================
def handle_client(conn: socket.socket, addr: tuple[str, int]) -> None:
    start_time = time.perf_counter()
    client_ip, client_port = addr
    method, route, status_code = "?", "?", 500

    try:
        conn.settimeout(SOCKET_TIMEOUT)
        req = parse_http_request(conn)
        if req is None:
            return
        method = req["method"]
        route = req["route"]
        status_code = api.dispatch_request(conn, req, http_responder)
    except socket.timeout:
        status_code = 408
        log(f"{client_ip}:{client_port} -> Request timed out")
    except OverflowError as exc:
        try:
            status_code = http_responder.send_error(conn, 413, str(exc))
        except OSError:
            pass
    except ValueError as exc:
        try:
            status_code = http_responder.send_error(conn, 400, str(exc))
        except OSError:
            pass
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        log(f"{client_ip}:{client_port} -> Client disconnected mid-transfer ({method} {route})")
        return
    except Exception as exc:
        log(f"{client_ip}:{client_port} -> Unhandled error: {exc}")
        try:
            status_code = http_responder.send_error(conn, 500, "Internal server error.")
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if method != "?":
            log(f"{client_ip}:{client_port} -> {method} {route} -> {status_code} ({elapsed_ms:.1f} ms)")


def run_server() -> None:
    api.ensure_directories()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(64)
    server_sock.settimeout(1.0)  # Allows instant Ctrl+C exit on Windows

    lan_ip = get_local_lan_ip()
    print("=" * 66)
    print(f"  {api.SERVER_TITLE} — Pure Python Socket Server")
    print("=" * 66)
    print(f"  Local URL      : http://127.0.0.1:{PORT}")
    print(f"  LAN URL        : http://{lan_ip}:{PORT}")
    print(f"  Storage Folder : {api.STORAGE_DIR}")
    print(f"  Assets Folder  : {api.ASSETS_DIR}")
    print(f"  Admin Password : {api.ADMIN_PASSWORD}")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 66)

    client_counter = 0
    try:
        while True:
            try:
                conn, addr = server_sock.accept()
            except socket.timeout:
                continue

            client_counter += 1
            worker = threading.Thread(
                target=handle_client,
                args=(conn, addr),
                name=f"Client-{client_counter}",
                daemon=True,
            )
            worker.start()
    except KeyboardInterrupt:
        print("\nShutting down Projects Laboratory server...")
    finally:
        server_sock.close()


if __name__ == "__main__":
    run_server()