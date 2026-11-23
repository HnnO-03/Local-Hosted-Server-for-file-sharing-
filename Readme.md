# Projects Laboratory

**Projects Laboratory** is a lightweight, self-hosted local web repository designed for university computer labs. Built **exclusively with Python's built-in `socket` and `threading` libraries** (zero web frameworks), it allows students to browse and download courses, software installers, tools, and large archives over a local network while giving administrators password-protected control over the repository.

---

## Table of Contents

- [Features](#features)
- [How It Works (Architecture)](#how-it-works-architecture)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Quick Start Guide](#quick-start-guide)
- [Changing the Admin Password & Port](#changing-the-admin-password--port)
- [API Reference](#api-reference)
- [Wireshark Packet Capture & Concurrency Testing](#wireshark-packet-capture--concurrency-testing)

---

## Features

- **Role-Based Access Gate:** Visitors choose between **Guest** (instant read/download access) and **Admin** (password-authenticated session).
- **Large File Streaming (64 KB Chunks):** Uploads and downloads stream directly to and from disk in 64 KB chunks—capable of transferring multi-gigabyte ISOs, installers, and archives with minimal RAM usage.
- **Atomic Safe Uploads:** Incoming files write to temporary `.part` files first and only replace the target file once 100% of bytes arrive, preventing corrupted files if a connection drops.
- **Full Admin File Management:**
  - Single and multi-file queued uploads (with optional overwrite/update mode)
  - Nested folder creation
  - Single-item move/rename and multi-select bulk move (with folder autocomplete)
  - Single-item and bulk **permanent deletion** with an explicit confirmation dialog
- **Cross-Platform & Concurrency Safe:** Runs identically on **Windows** and **Linux (Lubuntu / Ubuntu)** with thread-safe download/write locking to prevent file-in-use crashes.
- **Pure-Socket CLI & Stress Tester (`client.py`):** Includes a terminal client for Guest/Admin operations and a multi-threaded concurrency test mode for Wireshark analysis.

---

## How It Works (Architecture)

Because high-level frameworks (`Flask`, `Django`, `FastAPI`, etc.) are intentionally excluded, **Projects Laboratory** implements its own HTTP/1.1 server from scratch on top of raw TCP sockets:

```text
+-----------------------------------+          HTTP/1.1 + JSON / Binary          +-----------------------------------+
|        Frontend (Client)          |  --------------------------------------->  |         Backend (Server)          |
|                                   |                                            |                                   |
|  • index.html (HTML / CSS / JS)   |  <---------------------------------------  |  • server.py (Pure socket + TCP)  |
|  • client.py  (Pure socket CLI)   |       Streams, JSON, Custom Headers        |  • api.py    (Routes, Auth, Locks) |
+-----------------------------------+                                            +-----------------------------------+
```

1. **TCP Listener & Thread-Per-Client (`server.py`):**
   - Binds a TCP socket to `0.0.0.0:8080` so both `localhost` and any PC on the local lab network can connect.
   - Spawns a dedicated daemon thread (`Client-1`, `Client-2`, ...) for every incoming connection so multiple students can download large files simultaneously without blocking each other.
   - Manually parses raw HTTP request lines, headers, query strings, `Expect: 100-continue` handshakes, and CORS preflight (`OPTIONS`) requests, attaching an `X-Served-By-Thread` header to every response.
2. **Application Logic, Auth & Locking (`api.py`):**
   - **Token Authentication (`SessionManager`):** Verifies the admin password using constant-time comparison (`secrets.compare_digest`) and issues a 256-bit Bearer token stored in thread-safe memory.
   - **Concurrent Transfer Tracking (`TransferTracker`):** Tracks active file reads and writes across all threads. Unlimited guests can download the same file concurrently, while admin moves, overwrites, or deletions on an actively downloading file cleanly return `409 Conflict` instead of crashing on Windows or corrupting streams.
   - **Path Jail (`resolve_storage_path`):** Normalizes Windows/Linux separators and blocks directory traversal (`../`) or reserved OS filenames so all operations stay strictly inside `lab_files/`.
3. **Web Interface (`index.html`):**
   - Zero external CDNs or third-party scripts—renders completely offline on isolated lab networks.
   - Communicates with the backend via `fetch()` for JSON control operations and `XMLHttpRequest` for real-time upload progress bars.

---

## Project Structure

```text
ProjectsLaboratory/
├── server.py          # Pure TCP socket server, multi-client threading, and raw HTTP/1.1 engine
├── api.py             # REST/JSON API endpoints, admin token auth, path security, and transfer locks
├── client.py          # Pure-socket terminal client (Guest/Admin CLI + Wireshark concurrency test)
├── index.html         # Single-file web frontend (HTML/CSS/JS)
├── start_lab.bat      # One-click Windows launcher (starts server, browser, and CLI client)
├── start_lab.sh       # Linux / Lubuntu shell launcher
├── assets/            # UI branding assets (auto-created on first run)
│   ├── logo.png       # Place your server logo here (served at GET /assets/logo.png)
│   └── background.jpg # Place your background wallpaper here (served at GET /assets/background.jpg)
├── lab_files/         # Root storage directory for hosted files (auto-created with starter folders)
│   ├── Courses/
│   ├── Programs/
│   └── Tools/
└── downloads/         # Auto-created by client.py when downloading files via terminal
```

---

## Requirements

- **Python:** Python **3.9 or higher** (Python 3.10+ recommended)
- **External Libraries:** **None** — uses 100% Python standard library modules (`socket`, `threading`, `json`, `pathlib`, `secrets`, `shutil`, `urllib.parse`, `mimetypes`)
- **Operating System:** Windows 10/11 or Linux (tested for Lubuntu 26.04.1 LTS)
- **Browser:** Any modern web browser (Chrome, Edge, Firefox, Brave)
- **Optional:** [Wireshark](https://www.wireshark.org/) (with Npcap Loopback capture enabled on Windows) if you want to inspect raw HTTP headers and multi-client TCP packets

---

## Quick Start Guide

### 1. Download the Repository
Clone the repository with Git or click **Code → Download ZIP** on GitHub and extract it:

```bash
git clone [https://github.com/](https://github.com/)<your-username>/Projects-Laboratory.git
cd Projects-Laboratory
```

### 2. Add Logo & Background Images (Optional)
Create an `assets` folder (or run the server once to auto-create it) and place your UI images inside:
- `assets/logo.png` — displayed in the role selection gate, top navigation bar, and browser tab icon
- `assets/background.jpg` — full-screen background wallpaper

*(The server and web UI still work normally even if these two image files are omitted.)*

### 3. Start the Server

#### On Windows:
Double-click **`start_lab.bat`**, or run from Command Prompt / PowerShell:
```bat
python server.py
```

#### On Linux / Lubuntu:
```bash
chmod +x start_lab.sh
./start_lab.sh
```
Or run directly with Python:
```bash
python3 server.py
```

### 4. Connect as Guest or Admin
- **On the host machine:** Open `http://127.0.0.1:8080` in your browser.
- **From other PCs on the same LAN / Lab Wi-Fi:** Check the terminal startup banner for the `LAN URL` (for example, `http://192.168.1.50:8080`) and open it on any student machine.
  - **Guest Mode:** Click **Enter as Guest** to browse folders and download files immediately.
  - **Admin Mode:** Enter the admin password (default: `admin123`) to upload files, create folders, move/rename items, or permanently delete files.

> **Linux Firewall Note:** If UFW is enabled on the lab Linux machine, allow incoming traffic on port `8080`:
> ```bash
> sudo ufw allow 8080/tcp
> ```

---

## Changing the Admin Password & Port

The default admin password is **`admin123`** and the default port is **`8080`**. You can change them in any of three ways:

### Option A: Edit the Launcher Script (Recommended)
- **Windows (`start_lab.bat`):** Open in any text editor and modify lines 10–11:
  ```bat
  set "LAB_PORT=8080"
  set "LAB_ADMIN_PASSWORD=YourNewStrongPassword"
  ```
- **Linux (`start_lab.sh`):** Open and modify lines 4–5:
  ```bash
  export LAB_PORT=8080
  export LAB_ADMIN_PASSWORD="YourNewStrongPassword"
  ```

### Option B: Set Environment Variables Before Running
- **Windows PowerShell:**
  ```powershell
  $env:LAB_ADMIN_PASSWORD="YourNewStrongPassword"
  $env:LAB_PORT="8080"
  python server.py
  ```
- **Linux / macOS Bash:**
  ```bash
  LAB_ADMIN_PASSWORD="YourNewStrongPassword" LAB_PORT=8080 python3 server.py
  ```

### Option C: Edit the Source Code Defaults Directly
- Change `ADMIN_PASSWORD` in **`api.py`** (line 16):
  ```python
  ADMIN_PASSWORD = os.environ.get("LAB_ADMIN_PASSWORD", "YourNewStrongPassword")
  ```
- Change `PORT` in **`server.py`** (line 18):
  ```python
  PORT = int(os.environ.get("LAB_PORT", "8080"))
  ```

---

## API Reference

All control routes exchange `application/json` payloads, while file transfers stream raw binary (`application/octet-stream`). Admin endpoints require an `Authorization: Bearer <token>` header.

| Method | Endpoint | Auth | Request Payload | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/api/info` | Public | — | Returns server status, active role, chunk size, and worker thread name. |
| `POST` | `/api/auth/login` | Public | `{"password": "..."}` | Validates admin password and returns a session Bearer token. |
| `POST` | `/api/auth/logout` | Admin | — | Revokes the active admin session token. |
| `GET` | `/api/files?path=...` | Public | Query param `path` | Lists folders and files (name, path, type, byte size, modified timestamp). |
| `GET` | `/api/folders` | Public | — | Lists all directory paths inside `lab_files/` for move autocomplete. |
| `GET` | `/api/download?path=...` | Public | Query param `path` | Streams the requested file in 64 KB chunks as an attachment. |
| `POST` | `/api/upload?path=...&overwrite=...` | Admin | Raw binary + `Content-Length` | Streams an uploaded file to disk (creates or overwrites). |
| `POST` | `/api/folder` | Admin | `{"path": "Courses/Net"}` | Creates a new directory (supports nested paths). |
| `PUT` | `/api/move` | Admin | `{"source": "...", "destination": "..."}` or `{"sources": [...], "destination_dir": "..."}` | Moves/renames a single item or batch-moves multiple selected items. |
| `DELETE` | `/api/delete` | Admin | Query `?path=...` or `{"paths": [...]}` | Permanently deletes a single item or batch of selected items. |

---

## Wireshark Packet Capture & Concurrency Testing

Because the server communicates over unencrypted HTTP/1.1 on TCP, all API headers, JSON bodies, and interleaved multi-client streams are directly visible in Wireshark:

1. Start **Wireshark** and select your **Adapter for loopback traffic capture** (for local testing) or your **Wi-Fi / Ethernet** interface (for LAN testing).
2. Apply one of the following display filters:
   - `tcp.port == 8080` — shows TCP handshakes, chunked segments, and interleaved packets across multiple client ports.
   - `http` — isolates clean HTTP request/response headers and JSON payloads.
3. Run the built-in multi-client stress tester in a second terminal:
   ```bash
   python client.py
   ```
   Choose option **`3` (Multi-Client Concurrency Test)** (and optionally enter the path of a large file on the server) to launch simultaneous threads against `server.py` and capture concurrent packet interleaving.