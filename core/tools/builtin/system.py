"""Built-in read-only tools: filesystem reads, system status."""

from __future__ import annotations

import fnmatch
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..registry import ToolError

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 2_000_000
MAX_STDOUT_BYTES = 20_000
MAX_STDERR_BYTES = 8_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_SEARCH_FILES = 5_000
MAX_SEARCH_DIRECTORIES = 2_000
MAX_SEARCH_ENTRIES = 10_000
MAX_SEARCH_MATCHES = 100
MAX_MATCHES_PER_FILE = 5


def make_read_file(registry):
    def read_file(args: dict) -> dict:
        path = registry.check_path(str(args.get("path", "")))
        if not path.is_file():
            raise ValueError(f"Not a file: {path}")
        size = path.stat().st_size
        with path.open("rb") as stream:
            raw = stream.read(MAX_READ_BYTES + 1)
        truncated = len(raw) > MAX_READ_BYTES or size > MAX_READ_BYTES
        raw = raw[:MAX_READ_BYTES]
        text = raw.decode("utf-8", errors="replace")
        return {
            "path": str(path),
            "bytes": size,
            "returned_bytes": len(raw),
            "truncated": truncated,
            "content": text,
        }
    return read_file


def make_list_directory(registry):
    def list_directory(args: dict) -> dict:
        path = registry.check_path(str(args.get("path", "")))
        if not path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        entries = []
        filtered = 0
        for p in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if registry.is_sensitive_path(p):
                filtered += 1
                continue
            try:
                entries.append({
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "bytes": p.stat().st_size if p.is_file() else None,
                })
            except OSError:
                continue
            if len(entries) >= 500:
                break
        return {
            "path": str(path), "entries": entries,
            "filtered_protected_entries": filtered,
            "truncated": len(entries) >= 500,
        }
    return list_directory


def make_search_files(registry):
    """Literal, bounded text search that never opens protected paths."""

    def search_files(args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        if len(query) > 500:
            raise ValueError("query exceeds the 500-character limit")
        raw_path = str(args.get("path") or registry.roots[0])
        start = registry.check_path(raw_path)
        pattern = str(args.get("glob") or "*").strip() or "*"
        needle = query.casefold()
        matches: list[dict] = []
        scanned = 0
        visited_directories = 0
        visited_entries = 0
        skipped_protected = 0
        truncated = False

        def candidate_files():
            nonlocal skipped_protected, truncated, visited_directories, visited_entries
            if start.is_file():
                yield start
                return
            if not start.is_dir():
                raise ValueError(f"Not a file or directory: {start}")
            pending = [start]
            while pending:
                root_path = pending.pop()
                visited_directories += 1
                if visited_directories > MAX_SEARCH_DIRECTORIES:
                    truncated = True
                    return
                try:
                    root_path = registry.check_path(str(root_path))
                    iterator = os.scandir(root_path)
                except (OSError, ValueError, ToolError):
                    continue
                with iterator:
                    for entry in iterator:
                        visited_entries += 1
                        if visited_entries > MAX_SEARCH_ENTRIES:
                            truncated = True
                            return
                        path = Path(entry.path)
                        if registry.is_sensitive_path(path):
                            skipped_protected += 1
                            continue
                        try:
                            # Resolve before queueing or yielding, so junctions
                            # and file symlinks cannot leave a configured root.
                            path = registry.check_path(str(path))
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(path)
                            elif (
                                entry.is_file(follow_symlinks=False) or path.is_file()
                            ) and (
                                fnmatch.fnmatch(entry.name, pattern)
                                or fnmatch.fnmatch(str(path), pattern)
                            ):
                                yield path
                        except (OSError, ValueError, ToolError):
                            continue

        for path in candidate_files():
            if scanned >= MAX_SEARCH_FILES or len(matches) >= MAX_SEARCH_MATCHES:
                truncated = True
                break
            try:
                # Resolving every candidate prevents a file symlink from
                # escaping a configured read root.
                path = registry.check_path(str(path))
                size = path.stat().st_size
                if not path.is_file() or size > MAX_SEARCH_FILE_BYTES:
                    continue
                with path.open("rb") as stream:
                    raw = stream.read(MAX_SEARCH_FILE_BYTES + 1)
            except (OSError, ValueError, ToolError):
                continue
            scanned += 1
            if b"\x00" in raw[:4096]:
                continue
            text = raw[:MAX_SEARCH_FILE_BYTES].decode("utf-8", errors="replace")
            per_file = 0
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                matches.append({
                    "path": str(path),
                    "line": line_number,
                    "text": line.strip()[:500],
                })
                per_file += 1
                if per_file >= MAX_MATCHES_PER_FILE or len(matches) >= MAX_SEARCH_MATCHES:
                    break

        return {
            "query": query,
            "path": str(start),
            "glob": pattern,
            "matches": matches,
            "match_count": len(matches),
            "scanned_files": scanned,
            "visited_directories": visited_directories,
            "visited_entries": visited_entries,
            "skipped_protected_paths": skipped_protected,
            "truncated": truncated,
            "literal_search": True,
        }

    return search_files


def make_write_file(registry):
    def write_file(args: dict) -> dict:
        path = registry.check_path(str(args.get("path", "")), write=True)
        content = str(args.get("content", ""))
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ValueError(f"File content exceeds the {MAX_WRITE_BYTES}-byte tool limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "bytes": len(encoded),
                "overwrote_existing": existed}
    return write_file


def _gpu_snapshot() -> list[dict]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return [{"error": f"nvidia-smi unavailable: {type(exc).__name__}"}]
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append({
            "index": int(parts[0]), "name": parts[1],
            "used_mib": int(parts[2]), "total_mib": int(parts[3]),
            "free_mib": int(parts[4]),
        })
    return gpus


def make_system_status(router):
    async def system_status(_args: dict) -> dict:
        health = await router.health()
        return {
            "active_worker": router.active_name,
            "swapping": router.swapping,
            "external_workload": router.external_workload,
            "active_inferences": router.status().get("active_inferences", 0),
            "supports_vision": router.supports_vision(),
            "supports_audio": router.supports_audio(),
            "last_swap_seconds": router.last_swap_seconds,
            "health": health,
            "gpus": _gpu_snapshot(),
        }
    return system_status


def make_assistant_capabilities(router, registry):
    def assistant_capabilities_read(_args: dict) -> dict:
        tools = []
        for item in registry.model_tools():
            function = item["function"]
            name = function["name"]
            tools.append({
                "name": name,
                "status": "approval_required" if registry.tier(name) == "confirm_required" else "available",
                "description": function["description"],
            })
        workers = []
        for name, cfg in router.configs.items():
            workers.append({
                "name": name,
                "active": name == router.active_name,
                "vision_runtime": bool(cfg.supports_vision),
                "audio_runtime": bool(cfg.supports_audio),
            })
        return {
            "delivery": "existing_chat_stream",
            "catalog_is_execution_proof": False,
            "active_worker": router.active_name,
            "workers": workers,
            "tools": tools,
            "read_roots": [str(path) for path in registry.roots],
            "write_roots": [str(path) for path in registry.write_roots],
            "not_wired": [
                {"name": "image attachments", "reason": "Camera-frame vision is wired, but arbitrary chat image upload is not yet wired."},
                {"name": "autonomous continuous camera analysis", "reason": "The live preview stays in chat; Omni receives only frames the operator explicitly submits."},
                {"name": "finance quotes", "reason": "No verified finance provider is configured."},
            ],
        }
    return assistant_capabilities_read


def make_run_powershell():
    def drain(stream, limit: int, target: dict, key: str) -> None:
        tail = bytearray()
        total = 0
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            total += len(chunk)
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[:-limit]
        target[key] = bytes(tail)
        target[f"{key}_bytes"] = total

    def run_powershell(args: dict) -> dict:
        """Only reachable after explicit operator approval -- the gateway
        enforces that, not this function. Output is captured and returned
        rather than left in a detached window, so the result is auditable."""
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command is required")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        captured: dict[str, Any] = {}
        stdout_thread = threading.Thread(
            target=drain, args=(process.stdout, MAX_STDOUT_BYTES, captured, "stdout"), daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain, args=(process.stderr, MAX_STDERR_BYTES, captured, "stderr"), daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = process.wait(timeout=15)
        stdout_thread.join(timeout=15)
        stderr_thread.join(timeout=15)
        stdout = captured.get("stdout", b"").decode("utf-8", errors="replace")
        stderr = captured.get("stderr", b"").decode("utf-8", errors="replace")
        return {
            "command": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_bytes": captured.get("stdout_bytes", 0),
            "stderr_bytes": captured.get("stderr_bytes", 0),
            "stdout_truncated": captured.get("stdout_bytes", 0) > MAX_STDOUT_BYTES,
            "stderr_truncated": captured.get("stderr_bytes", 0) > MAX_STDERR_BYTES,
        }
    return run_powershell


def make_task_tools(store):
    def scoped(method, *args, user_id: str, **kwargs):
        try:
            return method(*args, user_id=user_id, **kwargs)
        except TypeError:
            return method(*args, **kwargs)

    def list_tasks(args: dict) -> dict:
        user_id = str(args.get("__xomni_user_id") or "local-dev")
        return {"tasks": scoped(store.list_tasks, args.get("status"), user_id=user_id)}

    def add_task(args: dict) -> dict:
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        user_id = str(args.get("__xomni_user_id") or "local-dev")
        task_id = scoped(
            store.add_task, title, due_at=args.get("due_at"), user_id=user_id
        )
        return {"id": task_id, "title": title, "due_at": args.get("due_at"),
                "status": "open"}

    def update_task_status(args: dict) -> dict:
        try:
            task_id = int(args.get("task_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("task_id must be an integer") from exc
        status = str(args.get("status") or "").strip()
        if status not in {"open", "in_progress", "done", "abandoned"}:
            raise ValueError("status must be open, in_progress, done, or abandoned")
        user_id = str(args.get("__xomni_user_id") or "local-dev")
        task = next(
            (item for item in scoped(store.list_tasks, user_id=user_id) if item["id"] == task_id),
            None,
        )
        if task is None:
            raise ValueError(f"Task {task_id} does not exist")
        scoped(store.set_task_status, task_id, status, user_id=user_id)
        return {**task, "status": status}

    return list_tasks, add_task, update_task_status
