from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote

CHUNK_SIZE = 65536
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{num_bytes} B"


class RawSocketHTTPClient:
    """Minimal HTTP/1.1 client built purely on Python's socket library."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080, timeout: float = 60.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        return sock

    @staticmethod
    def _read_response_headers(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(8192)
            if not chunk:
                raise ConnectionError("Server closed connection before sending headers.")
            buffer.extend(chunk)

        sep = buffer.index(b"\r\n\r\n")
        raw_head = bytes(buffer[:sep]).decode("iso-8859-1")
        initial_body = bytes(buffer[sep + 4 :])

        lines = raw_head.split("\r\n")
        status_code = int(lines[0].split(" ")[1])

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        return status_code, headers, initial_body

    @staticmethod
    def _read_full_body(sock: socket.socket, headers: dict[str, str], initial_body: bytes) -> bytes:
        content_length = headers.get("content-length")
        body = bytearray(initial_body)
        if content_length is not None:
            expected = int(content_length)
            while len(body) < expected:
                chunk = sock.recv(min(CHUNK_SIZE, expected - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            return bytes(body[:expected])

        while True:
            chunk = sock.recv(CHUNK_SIZE)
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body)

    def request_json(
        self,
        method: str,
        target: str,
        payload: dict | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, str], dict]:
        body_bytes = b""
        req_lines = [
            f"{method.upper()} {target} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "User-Agent: Projects-Laboratory-CLI/1.0",
            "Connection: close",
        ]
        if token:
            req_lines.append(f"Authorization: Bearer {token}")
        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")
            req_lines.append("Content-Type: application/json")
            req_lines.append(f"Content-Length: {len(body_bytes)}")

        raw_request = ("\r\n".join(req_lines) + "\r\n\r\n").encode("iso-8859-1") + body_bytes

        with self._connect() as sock:
            sock.sendall(raw_request)
            status, headers, initial_body = self._read_response_headers(sock)
            raw_body = self._read_full_body(sock, headers, initial_body)

        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            data = {"raw": raw_body.decode("utf-8", errors="replace")}
        return status, headers, data

    def download_file(
        self,
        remote_path: str,
        dest_dir: Path = DOWNLOADS_DIR,
        save_as: str | None = None,
        show_progress: bool = True,
    ) -> tuple[int, dict[str, str], Path | None]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        encoded = quote(remote_path, safe="/")
        target = f"/api/download?path={encoded}"

        req = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"User-Agent: Projects-Laboratory-CLI/1.0\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("iso-8859-1")

        with self._connect() as sock:
            sock.sendall(req)
            status, headers, initial_body = self._read_response_headers(sock)
            if status != 200:
                err_body = self._read_full_body(sock, headers, initial_body)
                print(f"[Error {status}] {err_body.decode('utf-8', errors='replace')}")
                return status, headers, None

            total_size = int(headers.get("content-length", "0"))
            filename = save_as or Path(remote_path).name or "downloaded_file"
            out_path = dest_dir / filename

            received = 0
            with open(out_path, "wb") as out_file:
                if initial_body:
                    out_file.write(initial_body)
                    received += len(initial_body)

                while total_size == 0 or received < total_size:
                    to_read = CHUNK_SIZE if total_size == 0 else min(CHUNK_SIZE, total_size - received)
                    chunk = sock.recv(to_read)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    received += len(chunk)
                    if show_progress and total_size > 0:
                        pct = (received / total_size) * 100
                        print(
                            f"\rDownloading {filename}: {pct:5.1f}% ({format_size(received)} / {format_size(total_size)})",
                            end="",
                            flush=True,
                        )
            if show_progress:
                print(f"\nSaved to: {out_path}")
            return status, headers, out_path

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        token: str,
        overwrite: bool = False,
    ) -> tuple[int, dict[str, str], dict]:
        file_size = local_path.stat().st_size
        encoded = quote(remote_path, safe="/")
        overwrite_flag = "true" if overwrite else "false"
        target = f"/api/upload?path={encoded}&overwrite={overwrite_flag}"

        req_head = (
            f"POST {target} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"User-Agent: Projects-Laboratory-CLI/1.0\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"Content-Length: {file_size}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("iso-8859-1")

        sent = 0
        with self._connect() as sock:
            sock.sendall(req_head)
            with open(local_path, "rb") as in_file:
                while True:
                    chunk = in_file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent += len(chunk)
                    if file_size > 0:
                        pct = (sent / file_size) * 100
                        print(
                            f"\rUploading {local_path.name}: {pct:5.1f}% ({format_size(sent)} / {format_size(file_size)})",
                            end="",
                            flush=True,
                        )
            print()
            status, headers, initial_body = self._read_response_headers(sock)
            raw_body = self._read_full_body(sock, headers, initial_body)

        data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        return status, headers, data


# ==============================================================================
# Multi-Client Concurrency Demo (For Wireshark Packet Interleaving)
# ==============================================================================
def run_wireshark_concurrency_test(client: RawSocketHTTPClient) -> None:
    print("\n--- Multi-Client Concurrency Test (Wireshark Capture Helper) ---")
    print("Tip: Start Wireshark capture on Loopback (or LAN adapter) with filter:")
    print(f"     tcp.port == {client.port}")
    remote_file = input(
        "Optional: Enter a large file path on server to stream (e.g. Courses/big.zip), or press Enter to skip: "
    ).strip()

    results: list[str] = []
    res_lock = threading.Lock()

    def worker_browse(worker_id: int, path: str) -> None:
        t0 = time.perf_counter()
        encoded = quote(path, safe="/")
        status, headers, data = client.request_json("GET", f"/api/files?path={encoded}")
        dt = (time.perf_counter() - t0) * 1000
        srv_thread = headers.get("x-served-by-thread", "?")
        count = data.get("count", "?")
        with res_lock:
            results.append(
                f"Worker-{worker_id} [GET /api/files?path={path}] -> {status} "
                f"| items={count} | server_thread={srv_thread} | {dt:.1f} ms"
            )

    def worker_download(worker_id: int, path: str) -> None:
        t0 = time.perf_counter()
        save_name = f"test_w{worker_id}_{Path(path).name}"
        status, headers, saved = client.download_file(path, save_as=save_name, show_progress=False)
        dt = (time.perf_counter() - t0) * 1000
        srv_thread = headers.get("x-served-by-thread", "?")
        size_str = format_size(saved.stat().st_size) if saved and saved.exists() else "0 B"
        with res_lock:
            results.append(
                f"Worker-{worker_id} [GET /api/download?path={path}] -> {status} "
                f"| size={size_str} | server_thread={srv_thread} | {dt:.1f} ms"
            )

    threads: list[threading.Thread] = [
        threading.Thread(target=worker_browse, args=(1, "")),
        threading.Thread(target=worker_browse, args=(2, "Courses")),
        threading.Thread(target=worker_browse, args=(3, "Tools")),
    ]
    if remote_file:
        threads.insert(0, threading.Thread(target=worker_download, args=(4, remote_file)))
        threads.insert(1, threading.Thread(target=worker_download, args=(5, remote_file)))

    print(f"Launching {len(threads)} simultaneous clients...")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("\nConcurrency Test Results:")
    for line in sorted(results):
        print("  " + line)
    print()


# ==============================================================================
# Interactive Guest / Admin Terminal Client
# ==============================================================================
def join_remote(current_path: str, name: str) -> str:
    name = name.strip("/")
    return f"{current_path}/{name}" if current_path else name


def interactive_session(client: RawSocketHTTPClient, role: str, token: str | None) -> None:
    current_path = ""
    while True:
        encoded = quote(current_path, safe="/")
        status, _, data = client.request_json("GET", f"/api/files?path={encoded}", token=token)
        if status != 200:
            print(f"[Error {status}] {data.get('error', data)}")
            current_path = ""
            continue

        items = data.get("items", [])
        display_path = f"/{current_path}" if current_path else "/"
        print("\n" + "=" * 62)
        print(f"  Projects Laboratory  |  Role: {role.upper()}  |  Path: {display_path}")
        print("=" * 62)
        if not items:
            print("  (Empty folder)")
        else:
            for idx, item in enumerate(items, start=1):
                tag = "[DIR ]" if item["type"] == "folder" else "[FILE]"
                size_col = "" if item["type"] == "folder" else f" ({format_size(item['size'])})"
                print(f"  {idx:2d}. {tag} {item['name']}{size_col}")

        print("-" * 62)
        print("  [O] Open folder   [U] Go up   [D] Download file   [R] Refresh")
        if role == "admin":
            print("  [A] Upload/Update [N] New folder [M] Move/Rename  [X] Permanent Delete")
        print("  [Q] Back to role menu")

        choice = input("Choose action: ").strip().lower()

        if choice == "q":
            if role == "admin" and token:
                client.request_json("POST", "/api/auth/logout", token=token)
                print("Admin logged out.")
            break

        elif choice == "r":
            continue

        elif choice == "u":
            current_path = data.get("parent_path") or ""

        elif choice == "o":
            folder_name = input("Folder name to open: ").strip()
            if folder_name:
                current_path = join_remote(current_path, folder_name)

        elif choice == "d":
            file_name = input("File name to download: ").strip()
            if file_name:
                remote = join_remote(current_path, file_name)
                client.download_file(remote)

        elif role == "admin" and choice == "a":
            local_str = input("Local file path on this PC: ").strip().strip('"')
            local_file = Path(local_str)
            if not local_file.exists() or not local_file.is_file():
                print("Local file not found.")
                continue
            remote_name = input(f"Target filename on server [{local_file.name}]: ").strip() or local_file.name
            overwrite = input("Overwrite if file already exists? (y/N): ").strip().lower() == "y"
            remote = join_remote(current_path, remote_name)
            st, _, res = client.upload_file(local_file, remote, token=token or "", overwrite=overwrite)
            print(f"[{st}] {res.get('message') or res.get('error')}")

        elif role == "admin" and choice == "n":
            folder_name = input("New folder name (or sub/path): ").strip()
            if folder_name:
                remote = join_remote(current_path, folder_name)
                st, _, res = client.request_json("POST", "/api/folder", {"path": remote}, token=token)
                print(f"[{st}] {res.get('message') or res.get('error')}")

        elif role == "admin" and choice == "m":
            src_name = input("Item name to move/rename (in current folder): ").strip()
            dst_full = input("New full path relative to root (e.g. Tools/newname.zip): ").strip()
            if src_name and dst_full:
                src_full = join_remote(current_path, src_name)
                st, _, res = client.request_json(
                    "PUT",
                    "/api/move",
                    {"source": src_full, "destination": dst_full},
                    token=token,
                )
                print(f"[{st}] {res.get('message') or res.get('error')}")

        elif role == "admin" and choice == "x":
            target_name = input("File or folder name to PERMANENTLY delete: ").strip()
            if not target_name:
                continue
            remote = join_remote(current_path, target_name)
            confirm = input(
                f"WARNING: '{remote}' will be PERMANENTLY deleted. Type DELETE to confirm: "
            ).strip()
            if confirm != "DELETE":
                print("Deletion canceled.")
                continue
            encoded_target = quote(remote, safe="/")
            st, _, res = client.request_json("DELETE", f"/api/delete?path={encoded_target}", token=token)
            print(f"[{st}] {res.get('message') or res.get('error')}")

        else:
            print("Invalid option.")


def main() -> None:
    print("=" * 62)
    print("  Projects Laboratory — Pure Python Socket Client")
    print("=" * 62)
    host = input("Server IP [127.0.0.1]: ").strip() or "127.0.0.1"
    port_raw = input("Server Port [8080]: ").strip() or "8080"
    client = RawSocketHTTPClient(host=host, port=int(port_raw))

    while True:
        print("\nConnect as:")
        print("  1. Guest (Browse & Download)")
        print("  2. Admin (Password Required — Full Management)")
        print("  3. Multi-Client Concurrency Test (Wireshark Demo)")
        print("  4. Exit")
        mode = input("Select [1-4]: ").strip()

        if mode == "1":
            interactive_session(client, role="guest", token=None)

        elif mode == "2":
            password = input("Enter admin password: ").strip()
            status, _, data = client.request_json("POST", "/api/auth/login", {"password": password})
            if status == 200 and data.get("token"):
                print("Admin authenticated.")
                interactive_session(client, role="admin", token=data["token"])
            else:
                print(f"[Login Failed {status}] {data.get('error', 'Unknown error')}")

        elif mode == "3":
            run_wireshark_concurrency_test(client)

        elif mode == "4":
            break
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()