from __future__ import annotations

import datetime
import os
import secrets
import shutil
import socket
import threading
from pathlib import Path

# ==============================================================================
# Application Configuration
# ==============================================================================
SERVER_TITLE = "Projects Laboratory"
SERVER_HEADER = "Projects-Laboratory/1.0 (Pure-Python-Socket)"

ADMIN_PASSWORD = os.environ.get("LAB_ADMIN_PASSWORD", "admin123")

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "lab_files"   # Hosted courses, programs, tools
ASSETS_DIR = BASE_DIR / "assets"       # Logo and background images
FRONTEND_FILE = BASE_DIR / "index.html"

CHUNK_SIZE = 65536                     # 64 KB chunks for large file streaming

INVALID_FILENAME_CHARS = set('<>:"\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ==============================================================================
# Thread-Safe Admin Session Manager
# ==============================================================================
class SessionManager:
    """Stores active admin Bearer tokens in memory across concurrent threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: set[str] = set()

    def create_session(self) -> str:
        token = secrets.token_hex(32)
        with self._lock:
            self._tokens.add(token)
        return token

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return token in self._tokens

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            if token in self._tokens:
                self._tokens.remove(token)
                return True
            return False


# ==============================================================================
# Thread-Safe Concurrent Transfer Tracker (Windows + Linux Safe)
# ==============================================================================
class TransferTracker:
    """
    Allows unlimited concurrent downloads of the same file while preventing
    admin writes/moves/deletes from colliding with active transfers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_reads: dict[Path, int] = {}
        self._active_writes: set[Path] = set()

    @staticmethod
    def _is_related(path_a: Path, path_b: Path) -> bool:
        return path_a == path_b or path_a in path_b.parents or path_b in path_a.parents

    def acquire_read(self, path: Path) -> bool:
        with self._lock:
            for write_path in self._active_writes:
                if self._is_related(path, write_path):
                    return False
            self._active_reads[path] = self._active_reads.get(path, 0) + 1
            return True

    def release_read(self, path: Path) -> None:
        with self._lock:
            count = self._active_reads.get(path, 0)
            if count <= 1:
                self._active_reads.pop(path, None)
            else:
                self._active_reads[path] = count - 1

    def acquire_write(self, *paths: Path) -> bool:
        unique_paths = list(dict.fromkeys(paths))
        with self._lock:
            for target in unique_paths:
                for write_path in self._active_writes:
                    if self._is_related(target, write_path):
                        return False
                for read_path in self._active_reads:
                    if self._is_related(target, read_path):
                        return False
            for target in unique_paths:
                self._active_writes.add(target)
            return True

    def release_write(self, *paths: Path) -> None:
        with self._lock:
            for target in paths:
                self._active_writes.discard(target)


sessions = SessionManager()
transfers = TransferTracker()


# ==============================================================================
# Directory Setup & Path Security
# ==============================================================================
def ensure_directories() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    if not any(STORAGE_DIR.iterdir()):
        for starter in ("Courses", "Programs", "Tools"):
            (STORAGE_DIR / starter).mkdir(exist_ok=True)


def is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_segment(segment: str) -> None:
    if not segment or segment in (".", ".."):
        raise ValueError("Invalid path segment.")
    if any(ch in INVALID_FILENAME_CHARS or ord(ch) < 32 for ch in segment):
        raise ValueError(f"Illegal character in name: {segment}")
    if segment.endswith(".") or segment.endswith(" "):
        raise ValueError(f"Name cannot end with a space or dot: {segment}")
    stem = segment.split(".")[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Reserved filename is not allowed: {segment}")


def resolve_storage_path(raw_path: str, validate_names: bool = False) -> tuple[Path, str]:
    cleaned = raw_path.replace("\\", "/").strip()
    parts = [p for p in cleaned.split("/") if p]

    for part in parts:
        if part in (".", ".."):
            raise ValueError("Directory traversal is not permitted.")
        if validate_names:
            validate_segment(part)

    root = STORAGE_DIR.resolve()
    target = STORAGE_DIR.joinpath(*parts).resolve()
    if not is_subpath(target, root):
        raise ValueError("Access outside storage root is forbidden.")

    rel_posix = "" if target == root else target.relative_to(root).as_posix()
    return target, rel_posix


def build_item_metadata(entry: Path) -> dict:
    stat = entry.stat()
    is_dir = entry.is_dir()
    rel_path = entry.resolve().relative_to(STORAGE_DIR.resolve()).as_posix()
    modified_iso = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    return {
        "name": entry.name,
        "path": rel_path,
        "type": "folder" if is_dir else "file",
        "size": 0 if is_dir else stat.st_size,
        "modified": modified_iso,
    }


def extract_bearer_token(headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def query_first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def require_admin(conn: socket.socket, headers: dict[str, str], http) -> bool:
    token = extract_bearer_token(headers)
    if not sessions.is_valid(token):
        http.send_error(conn, 401, "Admin authentication required.")
        return False
    return True


# ==============================================================================
# API Endpoint Handlers
# ==============================================================================
def handle_info(conn: socket.socket, req: dict, http) -> int:
    token = extract_bearer_token(req["headers"])
    is_admin = sessions.is_valid(token)
    return http.send_json(
        conn,
        200,
        {
            "status": "ok",
            "title": SERVER_TITLE,
            "role": "admin" if is_admin else "guest",
            "chunk_size": CHUNK_SIZE,
            "served_by_thread": threading.current_thread().name,
        },
    )


def handle_login(conn: socket.socket, req: dict, http) -> int:
    try:
        data = http.read_json_body(conn, req["headers"], req["initial_body"])
    except OverflowError as exc:
        return http.send_error(conn, 413, str(exc))
    except (ValueError, EOFError) as exc:
        return http.send_error(conn, 400, str(exc))

    password = str(data.get("password", ""))
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        return http.send_error(conn, 401, "Incorrect admin password.")

    token = sessions.create_session()
    return http.send_json(
        conn,
        200,
        {
            "status": "ok",
            "role": "admin",
            "token": token,
            "message": "Admin login successful.",
        },
    )


def handle_logout(conn: socket.socket, req: dict, http) -> int:
    token = extract_bearer_token(req["headers"])
    sessions.revoke(token)
    return http.send_json(conn, 200, {"status": "ok", "role": "guest", "message": "Logged out."})


def handle_list_files(conn: socket.socket, req: dict, http) -> int:
    raw_path = query_first(req["query"], "path", "")
    try:
        target, rel_path = resolve_storage_path(raw_path)
    except ValueError as exc:
        return http.send_error(conn, 400, str(exc))

    if not target.exists():
        return http.send_error(conn, 404, "Directory not found.")
    if not target.is_dir():
        return http.send_error(conn, 400, "Requested path is a file, not a directory.")

    items = []
    try:
        for entry in target.iterdir():
            if entry.name.endswith(".part"):
                continue
            try:
                items.append(build_item_metadata(entry))
            except OSError:
                continue
    except OSError as exc:
        return http.send_error(conn, 500, f"Could not read directory: {exc}")

    items.sort(key=lambda item: (0 if item["type"] == "folder" else 1, item["name"].lower()))

    parent_path = None
    if rel_path:
        parent_path = "/".join(rel_path.split("/")[:-1])

    return http.send_json(
        conn,
        200,
        {
            "status": "ok",
            "title": SERVER_TITLE,
            "current_path": rel_path,
            "parent_path": parent_path,
            "count": len(items),
            "items": items,
        },
    )


def handle_list_folders(conn: socket.socket, req: dict, http) -> int:
    """Returns all available directory paths inside lab_files for Move autocomplete."""
    root = STORAGE_DIR.resolve()
    folders: list[str] = [""]
    try:
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            dirnames.sort(key=str.lower)
            current = Path(dirpath).resolve()
            if current != root and is_subpath(current, root):
                folders.append(current.relative_to(root).as_posix())
    except OSError as exc:
        return http.send_error(conn, 500, f"Could not scan folders: {exc}")

    return http.send_json(conn, 200, {"status": "ok", "folders": folders})


def handle_download(conn: socket.socket, req: dict, http) -> int:
    raw_path = query_first(req["query"], "path", "")
    if not raw_path:
        return http.send_error(conn, 400, "Query parameter 'path' is required.")

    try:
        target, _ = resolve_storage_path(raw_path)
    except ValueError as exc:
        return http.send_error(conn, 400, str(exc))

    if not target.exists() or not target.is_file():
        return http.send_error(conn, 404, "File not found.")

    if not transfers.acquire_read(target):
        return http.send_error(conn, 409, "File is currently being modified by an admin.")

    try:
        return http.stream_file(conn, target, as_attachment=True)
    finally:
        transfers.release_read(target)


def handle_upload(conn: socket.socket, req: dict, http) -> int:
    if not require_admin(conn, req["headers"], http):
        return 401

    raw_path = query_first(req["query"], "path", "")
    if not raw_path:
        return http.send_error(conn, 400, "Query parameter 'path' is required.")

    overwrite_q = query_first(req["query"], "overwrite", "").lower()
    overwrite_h = req["headers"].get("x-file-overwrite", "").lower()
    allow_overwrite = overwrite_q in ("1", "true", "yes") or overwrite_h in ("1", "true", "yes")

    raw_len = req["headers"].get("content-length")
    if raw_len is None:
        return http.send_error(conn, 411, "Content-Length header is required for uploads.")
    try:
        content_length = int(raw_len)
        if content_length < 0:
            raise ValueError
    except ValueError:
        return http.send_error(conn, 400, "Invalid Content-Length header.")

    try:
        target, _ = resolve_storage_path(raw_path, validate_names=True)
    except ValueError as exc:
        return http.send_error(conn, 400, str(exc))

    if target == STORAGE_DIR.resolve():
        return http.send_error(conn, 400, "Upload path must include a filename.")
    if target.name.endswith(".part"):
        return http.send_error(conn, 400, "Filenames ending with '.part' are reserved.")
    if not target.parent.exists() or not target.parent.is_dir():
        return http.send_error(conn, 404, "Destination folder does not exist.")
    if target.exists() and target.is_dir():
        return http.send_error(conn, 409, "A folder with this name already exists.")

    existed_before = target.exists()
    if existed_before and not allow_overwrite:
        return http.send_error(conn, 409, "File already exists. Enable overwrite to update it.")

    if not transfers.acquire_write(target):
        return http.send_error(conn, 409, "File is currently being downloaded or modified.")

    temp_path = target.parent / f".{target.name}.{secrets.token_hex(6)}.part"
    bytes_written = 0
    try:
        with open(temp_path, "wb") as out_file:
            initial = req["initial_body"]
            if initial:
                to_write = initial[:content_length]
                out_file.write(to_write)
                bytes_written += len(to_write)

            while bytes_written < content_length:
                remaining = content_length - bytes_written
                chunk = conn.recv(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise EOFError("Upload interrupted before all bytes were received.")
                out_file.write(chunk)
                bytes_written += len(chunk)

        os.replace(temp_path, target)
    except EOFError as exc:
        temp_path.unlink(missing_ok=True)
        return http.send_error(conn, 400, str(exc))
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        return http.send_error(conn, 500, f"Failed to save uploaded file: {exc}")
    finally:
        transfers.release_write(target)

    status_code = 200 if existed_before else 201
    return http.send_json(
        conn,
        status_code,
        {
            "status": "ok",
            "message": "File updated successfully." if existed_before else "File uploaded successfully.",
            "item": build_item_metadata(target),
        },
    )


def handle_create_folder(conn: socket.socket, req: dict, http) -> int:
    if not require_admin(conn, req["headers"], http):
        return 401

    try:
        data = http.read_json_body(conn, req["headers"], req["initial_body"])
    except OverflowError as exc:
        return http.send_error(conn, 413, str(exc))
    except (ValueError, EOFError) as exc:
        return http.send_error(conn, 400, str(exc))

    raw_path = str(data.get("path", "")).strip()
    if not raw_path:
        return http.send_error(conn, 400, "Field 'path' is required.")

    try:
        target, _ = resolve_storage_path(raw_path, validate_names=True)
    except ValueError as exc:
        return http.send_error(conn, 400, str(exc))

    if target == STORAGE_DIR.resolve():
        return http.send_error(conn, 400, "Root directory already exists.")
    if target.exists():
        return http.send_error(conn, 409, "A file or folder already exists at that path.")

    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return http.send_error(conn, 500, f"Could not create folder: {exc}")

    return http.send_json(
        conn,
        201,
        {
            "status": "ok",
            "message": "Folder created successfully.",
            "item": build_item_metadata(target),
        },
    )


def _move_single_item(raw_src: str, raw_dst: str) -> tuple[int, dict | None, str | None]:
    """Core single-item move/rename with path security and transfer locks."""
    try:
        src, _ = resolve_storage_path(raw_src)
        dst, _ = resolve_storage_path(raw_dst, validate_names=True)
    except ValueError as exc:
        return 400, None, str(exc)

    root = STORAGE_DIR.resolve()
    if src == root or dst == root:
        return 400, None, "Cannot move the root storage directory."
    if not src.exists():
        return 404, None, "Source item not found."
    if src == dst:
        return 400, None, "Item is already at the destination path."
    if src.is_dir() and is_subpath(dst, src):
        return 400, None, "Cannot move a folder inside itself."
    if not dst.parent.exists() or not dst.parent.is_dir():
        return 404, None, "Destination parent folder does not exist."
    if dst.exists():
        return 409, None, "An item with that name already exists at the destination."

    if not transfers.acquire_write(src, dst):
        return 409, None, "Item is currently in use by another client."

    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return 500, None, f"Move failed: {exc}"
    finally:
        transfers.release_write(src, dst)

    return 200, build_item_metadata(dst), None


def handle_move(conn: socket.socket, req: dict, http) -> int:
    if not require_admin(conn, req["headers"], http):
        return 401

    try:
        data = http.read_json_body(conn, req["headers"], req["initial_body"])
    except OverflowError as exc:
        return http.send_error(conn, 413, str(exc))
    except (ValueError, EOFError) as exc:
        return http.send_error(conn, 400, str(exc))

    # Batch Move: {"sources": ["a/f1", "a/f2"], "destination_dir": "Courses/New"}
    if "sources" in data:
        sources = data.get("sources")
        if not isinstance(sources, list) or not sources:
            return http.send_error(conn, 400, "Field 'sources' must be a non-empty list of paths.")

        raw_dest_dir = str(data.get("destination_dir", "")).strip()
        try:
            dest_dir, dest_rel = resolve_storage_path(raw_dest_dir)
        except ValueError as exc:
            return http.send_error(conn, 400, str(exc))

        if not dest_dir.exists() or not dest_dir.is_dir():
            return http.send_error(conn, 404, "Destination folder does not exist.")

        moved: list[dict] = []
        failed: list[dict] = []

        for raw_src in dict.fromkeys(str(s).strip() for s in sources if str(s).strip()):
            try:
                src_path, src_rel = resolve_storage_path(raw_src)
            except ValueError as exc:
                failed.append({"path": raw_src, "error": str(exc)})
                continue

            target_rel = f"{dest_rel}/{src_path.name}" if dest_rel else src_path.name
            code, item_meta, err = _move_single_item(src_rel, target_rel)
            if code == 200 and item_meta is not None:
                moved.append(item_meta)
            else:
                failed.append({"path": src_rel or raw_src, "error": err or "Move failed."})

        if not moved:
            first_err = failed[0]["error"] if failed else "No items could be moved."
            return http.send_json(
                conn,
                409,
                {
                    "status": "error",
                    "error": first_err,
                    "moved": moved,
                    "failed": failed,
                },
            )

        msg = f"Moved {len(moved)} item(s)."
        if failed:
            msg += f" {len(failed)} item(s) could not be moved."

        return http.send_json(
            conn,
            200,
            {
                "status": "ok" if not failed else "partial",
                "message": msg,
                "moved": moved,
                "failed": failed,
            },
        )

    # Single Move / Rename: {"source": "...", "destination": "..."}
    raw_src = str(data.get("source", "")).strip()
    raw_dst = str(data.get("destination", "")).strip()
    if not raw_src or not raw_dst:
        return http.send_error(conn, 400, "Both 'source' and 'destination' are required.")

    code, item_meta, err = _move_single_item(raw_src, raw_dst)
    if code != 200 or item_meta is None:
        return http.send_error(conn, code, err or "Move failed.")

    return http.send_json(
        conn,
        200,
        {
            "status": "ok",
            "message": "Item moved/renamed successfully.",
            "item": item_meta,
        },
    )


def _delete_single_item(raw_path: str) -> tuple[int, str | None, str | None]:
    """Core single-item permanent deletion with path security and transfer locks."""
    try:
        target, rel_path = resolve_storage_path(raw_path)
    except ValueError as exc:
        return 400, None, str(exc)

    if target == STORAGE_DIR.resolve():
        return 403, None, "Cannot delete the root storage folder."
    if not target.exists():
        return 404, None, "Target file or folder not found."

    if not transfers.acquire_write(target):
        return 409, None, "Cannot delete while a client is downloading or modifying it."

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        return 500, None, f"Permanent delete failed: {exc}"
    finally:
        transfers.release_write(target)

    return 200, rel_path, None


def handle_delete(conn: socket.socket, req: dict, http) -> int:
    if not require_admin(conn, req["headers"], http):
        return 401

    raw_path = query_first(req["query"], "path", "")
    body_data: dict = {}
    if not raw_path:
        try:
            body_data = http.read_json_body(conn, req["headers"], req["initial_body"])
        except OverflowError as exc:
            return http.send_error(conn, 413, str(exc))
        except (ValueError, EOFError) as exc:
            return http.send_error(conn, 400, str(exc))

    # Batch Permanent Delete: {"paths": ["a/f1", "a/f2"]}
    if "paths" in body_data:
        raw_paths = body_data.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            return http.send_error(conn, 400, "Field 'paths' must be a non-empty list.")

        unique_paths = list(dict.fromkeys(str(p).strip() for p in raw_paths if str(p).strip()))
        # Sort deepest paths first so child items delete cleanly before parent folders
        unique_paths.sort(key=lambda p: p.count("/"), reverse=True)

        deleted: list[str] = []
        failed: list[dict] = []

        for item_path in unique_paths:
            code, rel_path, err = _delete_single_item(item_path)
            if code == 200 and rel_path is not None:
                deleted.append(rel_path)
            else:
                failed.append({"path": item_path, "error": err or "Delete failed."})

        if not deleted:
            first_err = failed[0]["error"] if failed else "No items could be deleted."
            return http.send_json(
                conn,
                409,
                {
                    "status": "error",
                    "error": first_err,
                    "deleted": deleted,
                    "failed": failed,
                },
            )

        msg = f"Permanently deleted {len(deleted)} item(s)."
        if failed:
            msg += f" {len(failed)} item(s) could not be deleted."

        return http.send_json(
            conn,
            200,
            {
                "status": "ok" if not failed else "partial",
                "message": msg,
                "deleted": deleted,
                "failed": failed,
            },
        )

    # Single Permanent Delete (?path=... or {"path": "..."})
    if not raw_path:
        raw_path = str(body_data.get("path", "")).strip()
    if not raw_path:
        return http.send_error(conn, 400, "Target 'path' is required for permanent deletion.")

    code, rel_path, err = _delete_single_item(raw_path)
    if code != 200 or rel_path is None:
        return http.send_error(conn, code, err or "Delete failed.")

    return http.send_json(
        conn,
        200,
        {
            "status": "ok",
            "message": f"Permanently deleted '{rel_path}'.",
            "deleted_path": rel_path,
        },
    )


def handle_static_asset(conn: socket.socket, route: str, http) -> int:
    asset_name = route[len("/assets/"):].strip("/")
    if not asset_name or "/" in asset_name or "\\" in asset_name or asset_name in (".", ".."):
        return http.send_error(conn, 400, "Invalid asset path.")

    asset_path = (ASSETS_DIR / asset_name).resolve()
    if not is_subpath(asset_path, ASSETS_DIR.resolve()):
        return http.send_error(conn, 403, "Forbidden asset path.")

    return http.stream_file(conn, asset_path, as_attachment=False)


# ==============================================================================
# Central API Router
# ==============================================================================
def dispatch_request(conn: socket.socket, req: dict, http) -> int:
    method = req["method"]
    route = req["route"]

    # CORS Preflight
    if method == "OPTIONS":
        return http.send_empty(conn, 204)

    # Web Frontend & Static Assets
    if method == "GET" and route in ("/", "/index.html"):
        if FRONTEND_FILE.exists():
            return http.stream_file(conn, FRONTEND_FILE, as_attachment=False)
        return http.send_html(
            conn,
            200,
            f"<html><body style='font-family:sans-serif;padding:2rem;'>"
            f"<h1>{SERVER_TITLE}</h1>"
            f"<p>Pure Python socket server is online. Add <code>index.html</code> next to "
            f"<code>server.py</code> to load the web interface.</p></body></html>",
        )

    if method == "GET" and route.startswith("/assets/"):
        return handle_static_asset(conn, route, http)

    # REST / JSON API Routes
    if route == "/api/info":
        return handle_info(conn, req, http) if method == "GET" else http.send_error(conn, 405, "Use GET.")

    if route == "/api/auth/login":
        return handle_login(conn, req, http) if method == "POST" else http.send_error(conn, 405, "Use POST.")

    if route == "/api/auth/logout":
        return handle_logout(conn, req, http) if method == "POST" else http.send_error(conn, 405, "Use POST.")

    if route == "/api/files":
        return handle_list_files(conn, req, http) if method == "GET" else http.send_error(conn, 405, "Use GET.")

    if route == "/api/folders":
        return handle_list_folders(conn, req, http) if method == "GET" else http.send_error(conn, 405, "Use GET.")

    if route == "/api/download":
        return handle_download(conn, req, http) if method == "GET" else http.send_error(conn, 405, "Use GET.")

    if route == "/api/upload":
        return handle_upload(conn, req, http) if method == "POST" else http.send_error(conn, 405, "Use POST.")

    if route == "/api/folder":
        return handle_create_folder(conn, req, http) if method == "POST" else http.send_error(conn, 405, "Use POST.")

    if route == "/api/move":
        return handle_move(conn, req, http) if method == "PUT" else http.send_error(conn, 405, "Use PUT.")

    if route == "/api/delete":
        return handle_delete(conn, req, http) if method == "DELETE" else http.send_error(conn, 405, "Use DELETE.")

    return http.send_error(conn, 404, f"Unknown endpoint: {route}")