"""Workspace readers and bounded, persisted review baselines for the workbench."""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
from typing import Any

from coding_agent.desktop.protocol import RpcError

MAX_PREVIEW_BYTES = 1024 * 1024
MAX_BASELINE_BYTES = 32 * 1024 * 1024
MAX_DIFF_BYTES = 4 * MAX_PREVIEW_BYTES
MAX_FILES = 2000
HIDDEN_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".tmp"}


class WorkspaceWorkbench:
    def __init__(self, workspace: Path, session_id: str, storage: Path) -> None:
        self.workspace = workspace.resolve()
        self.session_id = session_id
        self.storage_root = storage.resolve()
        identity = hashlib.sha256(f"{self.workspace}\0{session_id}".encode()).hexdigest()
        self.storage = storage / identity
        self._capture_lock = asyncio.Lock()

    def path(self, value: Any = "") -> Path:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise RpcError("INVALID_PATH", "文件路径无效")
        candidate = (self.workspace / value).resolve()
        if not candidate.is_relative_to(self.workspace):
            raise RpcError("PATH_OUTSIDE_WORKSPACE", "路径不在当前工作区内")
        return candidate

    def git(self, *args: str) -> bytes:
        environment = {**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        try:
            result = subprocess.run(
                ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
                 "-C", str(self.workspace), *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, env=environment, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RpcError("GIT_UNAVAILABLE", "Git 读取失败或超时") from exc
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
            code = "NOT_GIT_REPOSITORY" if "not a git repository" in detail.lower() else "GIT_ERROR"
            raise RpcError(code, detail or "Git 读取失败")
        if len(result.stdout) > MAX_BASELINE_BYTES:
            raise RpcError("DIFF_TOO_LARGE", "变更数据过大，请按文件查看")
        return result.stdout

    def list_files(self, params: dict[str, Any]) -> dict[str, Any]:
        directory = self.path(params.get("path", ""))
        if not directory.is_dir():
            raise RpcError("NOT_DIRECTORY", "目录不存在")
        show_hidden = bool(params.get("showHidden", False))
        entries = []
        try:
            for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
                if not show_hidden and (child.name.startswith(".") or child.name in HIDDEN_DIRECTORIES):
                    continue
                outside = not child.resolve().is_relative_to(self.workspace)
                stat = child.lstat()
                entries.append({
                    "name": child.name, "path": child.relative_to(self.workspace).as_posix(),
                    "kind": "directory" if child.is_dir() and not outside else "file",
                    "size": stat.st_size, "symlink": child.is_symlink(), "blocked": outside,
                })
                if len(entries) >= 500:
                    break
        except OSError as exc:
            raise RpcError("FILE_READ_ERROR", str(exc)) from exc
        return {"path": directory.relative_to(self.workspace).as_posix(), "entries": entries, "truncated": len(entries) == 500}

    def read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        file = self.path(params.get("path"))
        if not file.is_file():
            raise RpcError("FILE_NOT_FOUND", "文件不存在或不是普通文件")
        try:
            size = file.stat().st_size
            with file.open("rb") as stream:
                data = stream.read(MAX_PREVIEW_BYTES)
        except OSError as exc:
            raise RpcError("FILE_READ_ERROR", str(exc)) from exc
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        image = mime in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml", "image/bmp"}
        text = None
        if not image and b"\x00" not in data:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = None
        return {
            "path": file.relative_to(self.workspace).as_posix(), "size": size, "mime": mime,
            "text": text, "image": f"data:{mime};base64,{base64.b64encode(data).decode()}" if image and size <= MAX_PREVIEW_BYTES else None,
            "binary": text is None and not image, "truncated": size > MAX_PREVIEW_BYTES,
        }

    def _files(self) -> tuple[list[str], bool]:
        try:
            raw = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".")
            names = sorted(set(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part))
        except RpcError as exc:
            if exc.code != "NOT_GIT_REPOSITORY":
                raise
            names = []
            for root, directories, files in os.walk(self.workspace, followlinks=False):
                directories[:] = [name for name in directories if name not in HIDDEN_DIRECTORIES and not name.startswith(".") and not (Path(root) / name).resolve().is_relative_to(self.storage_root)]
                for name in files:
                    names.append((Path(root) / name).relative_to(self.workspace).as_posix())
                if len(names) >= MAX_FILES:
                    break
        names = [name for name in names if not (self.workspace / name).resolve().is_relative_to(self.storage_root)]
        return names[:MAX_FILES], len(names) >= MAX_FILES

    def _snapshot(self) -> dict[str, Any]:
        names, truncated = self._files()
        files: dict[str, Any] = {}
        used = 0
        for name in names:
            try:
                file = self.path(name)
                if not file.is_file():
                    continue
                size = file.stat().st_size
                if size > MAX_PREVIEW_BYTES or used + size > MAX_BASELINE_BYTES:
                    truncated = True
                    files[name] = {"digest": f"unavailable:{size}:{file.stat().st_mtime_ns}", "text": None}
                    continue
                data = file.read_bytes()
                used += len(data)
                text = None
                if b"\0" not in data:
                    try:
                        text = data.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        pass
                files[name] = {"digest": hashlib.sha256(data).hexdigest(), "text": text}
            except (OSError, RpcError):
                truncated = True
        return {"version": 1, "sessionId": self.session_id, "workspace": str(self.workspace), "files": files, "truncated": truncated}

    async def capture(self) -> None:
        async with self._capture_lock:
            await asyncio.to_thread(self._capture)

    def _capture(self) -> None:
        snapshot = self._snapshot()
        self.storage.mkdir(parents=True, exist_ok=True)
        for name in ("session", "turn"):
            target = self.storage / f"{name}.json"
            if name == "session" and target.exists():
                continue
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)

    def _baseline_changes(self, source: str) -> dict[str, Any]:
        try:
            baseline = json.loads((self.storage / f"{source}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RpcError("BASELINE_UNAVAILABLE", "尚未记录该会话的变更基线；发送下一条任务后即可查看") from exc
        if baseline.get("workspace") != str(self.workspace) or baseline.get("sessionId") != self.session_id:
            raise RpcError("BASELINE_INVALID", "变更基线与当前会话不匹配")
        current = self._snapshot()
        files = []
        remaining = MAX_DIFF_BYTES
        truncated = baseline["truncated"] or current["truncated"]
        for name in sorted(baseline["files"].keys() | current["files"].keys()):
            before = baseline["files"].get(name)
            after = current["files"].get(name)
            if before is None and baseline["truncated"]:
                continue  # An omitted baseline file is not evidence of addition.
            if after is None and current["truncated"] and self.path(name).exists():
                continue  # An omitted current file is not evidence of deletion.
            if before and after and before["digest"] == after["digest"]:
                continue
            old_text = before["text"] if before else ""
            new_text = after["text"] if after else ""
            binary = old_text is None or new_text is None
            diff = "" if old_text is None or new_text is None else self._text_diff(old_text, new_text, f"a/{name}" if before else "/dev/null", f"b/{name}" if after else "/dev/null")
            additions, deletions = self._counts(diff)
            diff, clipped, remaining = self._bounded_diff(diff, remaining)
            truncated |= clipped
            files.append({"path": name, "status": "M" if before and after else "A" if after else "D", "additions": additions, "deletions": deletions, "binary": binary, "diff": diff, "truncated": clipped})
        return {"files": files, "truncated": truncated}

    @staticmethod
    def _bounded_diff(diff: str, remaining: int) -> tuple[str, bool, int]:
        encoded = diff.encode("utf-8")
        limit = min(MAX_PREVIEW_BYTES, remaining)
        preview = encoded[:limit].decode("utf-8", errors="ignore")
        return preview, len(encoded) > limit, remaining - len(preview.encode("utf-8"))

    @staticmethod
    def _text_diff(old: str, new: str, before: str, after: str) -> str:
        chunks = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=before, tofile=after)
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in chunks)

    @staticmethod
    def _counts(diff: str) -> tuple[int, int]:
        additions = deletions = 0
        in_hunk = False
        for line in diff.splitlines():
            if line.startswith("diff "):
                in_hunk = False
            elif line.startswith("@@ "):
                in_hunk = True
            elif in_hunk:
                additions += line.startswith("+")
                deletions += line.startswith("-")
        return additions, deletions

    def changes(self, params: dict[str, Any]) -> dict[str, Any]:
        source = params.get("source", "unstaged")
        if source not in {"unstaged", "staged", "branch", "session", "turn"}:
            raise RpcError("INVALID_PARAMS", "未知变更来源")
        try:
            branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
        except RpcError:
            branch = "HEAD"
        try:
            branches = [line for line in self.git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").decode().splitlines() if not line.endswith("/HEAD")][:200]
        except RpcError:
            branches = []
        base = params.get("baseRef")
        if source in {"session", "turn"}:
            result = self._baseline_changes(source)
        else:
            revision = []
            if source == "staged":
                revision = ["--cached"]
            elif source == "branch":
                if base is None:
                    base = next((name for name in ("origin/dev", "origin/main", "origin/master", "dev", "main", "master") if name in branches and name != branch), None)
                if not isinstance(base, str) or base not in branches or base.startswith("-") or any(ord(c) < 33 for c in base):
                    raise RpcError("INVALID_BASE_BRANCH", "请选择可用的比较分支")
                revision = [self.git("merge-base", "HEAD", base).decode().strip()]
            diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--relative", *revision]
            raw_names = self.git(*diff_args, "--name-only", "-z", "--", ".")
            names = [part.decode("utf-8", errors="replace") for part in raw_names.split(b"\0") if part]
            if source == "unstaged":
                names.extend(part.decode("utf-8", errors="replace") for part in self.git("ls-files", "--others", "--exclude-standard", "-z", "--", ".").split(b"\0") if part)
            files = []
            remaining = MAX_DIFF_BYTES
            truncated = len(set(names)) > 500
            for name in sorted(set(names))[:500]:
                self.path(name)
                diff = self.git(*diff_args, "--", name).decode("utf-8", errors="replace")
                status = "M"
                if not diff and source == "unstaged":
                    file = self.path(name)
                    if file.is_file() and file.stat().st_size <= MAX_PREVIEW_BYTES:
                        preview = self.read_file({"path": name})
                        if preview["text"] is not None:
                            diff = self._text_diff("", preview["text"], "/dev/null", f"b/{name}")
                        else:
                            diff = "Binary files differ"
                    else:
                        diff = "File too large to preview"
                    status = "A"
                elif "new file mode" in diff:
                    status = "A"
                elif "deleted file mode" in diff:
                    status = "D"
                additions, deletions = self._counts(diff)
                binary = "Binary files" in diff
                diff, clipped, remaining = self._bounded_diff(diff, remaining)
                truncated |= clipped
                files.append({"path": name, "status": status, "additions": additions, "deletions": deletions, "binary": binary, "diff": diff, "truncated": clipped})
            result = {"files": files, "truncated": truncated}
        return {
            **result, "source": source, "branch": branch, "branches": branches, "baseRef": base,
            "additions": sum(file["additions"] for file in result["files"]),
            "deletions": sum(file["deletions"] for file in result["files"]),
        }
