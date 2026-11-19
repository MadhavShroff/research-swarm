"""Lifecycle manager for descriptive named tmux windows owned by rswarm."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path


@dataclasses.dataclass
class ActiveWindow:
    window_id: str
    window_name: str
    pane_pid: int
    node_id: str
    started_at: float
    log_path: str
    is_stream_tail: bool = False


class TmuxPool:
    """Creates, monitors, and cleans up named tmux windows in session `rswarm-<slug>`."""

    def __init__(
        self,
        session_name: str,
        workspace_dir: str | Path,
        max_windows: int = 128,
        keep_window_sec: float = 1.5,
    ) -> None:
        self.session_name = session_name
        self.workspace_dir = Path(workspace_dir)
        self.max_windows = max_windows
        self.keep_window_sec = keep_window_sec
        self.active_windows: dict[str, ActiveWindow] = {}
        self._lock = asyncio.Lock()

    def _tmux_sync(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            close_fds=True,
            check=check,
        )

    def ensure_session(self) -> None:
        res = self._tmux_sync("has-session", "-t", self.session_name)
        if res.returncode != 0:
            self._tmux_sync(
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-n",
                "00-hub",
                "-c",
                str(self.workspace_dir),
                f"bash -lc 'printf \"=== rswarm session: {self.session_name} ===\\nWorkspace: {self.workspace_dir}\\nActive agent windows appear along the status bar.\\n\"; exec sleep infinity'",
            )
            self._tmux_sync("set-option", "-t", self.session_name, "remain-on-exit", "off")
            self._tmux_sync("set-option", "-t", self.session_name, "history-limit", "5000")

    def list_live_window_names(self) -> list[str]:
        res = self._tmux_sync(
            "list-windows", "-t", self.session_name, "-F", "#{window_name}"
        )
        if res.returncode != 0:
            return []
        return [
            ln.strip()
            for ln in res.stdout.splitlines()
            if ln.strip() and ln.strip() not in ("00-hub", "00-tui")
        ]

    def reap_orphan_windows_sync(self) -> int:
        """Kill leftover worker windows from prior coordinator instances while keeping 00-tui/00-hub intact."""
        res = self._tmux_sync(
            "list-windows", "-t", self.session_name, "-F", "#{window_id} #{window_name} #{pane_pid}"
        )
        if res.returncode != 0:
            return 0
        reaped = 0
        tracked_ids = set(self.active_windows.keys())
        for ln in res.stdout.splitlines():
            parts = ln.strip().split()
            if len(parts) < 2:
                continue
            wid, wname = parts[0], parts[1]
            ppid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            if wname in ("00-hub", "00-tui") or wid in tracked_ids:
                continue
            if ppid > 1:
                self._kill_pid_tree_safe(ppid)
            self._tmux_sync("kill-window", "-t", wid)
            reaped += 1
        return reaped

    def _kill_pid_tree_safe(self, pane_pid: int) -> None:
        """Terminate only the specific pane process group/children spawned by rswarm."""
        if pane_pid <= 1:
            return
        try:
            pgid = os.getpgid(pane_pid)
            if pgid > 1 and pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        try:
            os.kill(pane_pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            pass

    async def close_window(self, window_id: str, delay_sec: float | None = None) -> None:
        wait_s = self.keep_window_sec if delay_sec is None else delay_sec
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        async with self._lock:
            win = self.active_windows.pop(window_id, None)
        if win and win.pane_pid > 1:
            self._kill_pid_tree_safe(win.pane_pid)
        self._tmux_sync("kill-window", "-t", window_id)

    async def open_stream_window(
        self,
        window_name: str,
        node_id: str,
        log_path: Path,
    ) -> str | None:
        """Open a lightweight (~1.5 MiB) named tmux window tailing an async worker's live log."""
        self.ensure_session()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)

        async with self._lock:
            if len(self.active_windows) >= self.max_windows:
                return None
            cmd = f"tail -n +1 -f {shlex.quote(str(log_path))}"
            res = self._tmux_sync(
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id} #{pane_pid}",
                "-t",
                self.session_name,
                "-n",
                window_name,
                "-c",
                str(self.workspace_dir),
                cmd,
            )
            if res.returncode != 0:
                return None
            parts = res.stdout.strip().split()
            wid = parts[0]
            ppid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            self.active_windows[wid] = ActiveWindow(
                window_id=wid,
                window_name=window_name,
                pane_pid=ppid,
                node_id=node_id,
                started_at=time.time(),
                log_path=str(log_path),
                is_stream_tail=True,
            )
            return wid

    async def run_script_in_window(
        self,
        *,
        window_name: str,
        node_id: str,
        workdir: Path,
        script_path: Path,
        log_path: Path,
        exit_file: Path,
        timeout_sec: int,
    ) -> tuple[int, float, bool]:
        """Execute `script_path` inside a dedicated named tmux window and wait for completion.

        Returns: (exit_code, elapsed_sec, timed_out)
        """
        self.ensure_session()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if exit_file.exists():
            exit_file.unlink()

        wrapper_cmd = (
            f"bash {shlex.quote(str(script_path))} 2>&1 | tee -a {shlex.quote(str(log_path))}; "
            f"rc=${{PIPESTATUS[0]}}; printf '%s\\n' \"$rc\" > {shlex.quote(str(exit_file))}"
        )

        t0 = time.monotonic()
        async with self._lock:
            res = self._tmux_sync(
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id} #{pane_pid}",
                "-t",
                self.session_name,
                "-n",
                window_name,
                "-c",
                str(workdir),
                f"bash -c {shlex.quote(wrapper_cmd)}",
            )
            if res.returncode != 0:
                raise RuntimeError(f"Failed to spawn tmux window '{window_name}': {res.stderr.strip()}")
            parts = res.stdout.strip().split()
            wid = parts[0]
            ppid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            self.active_windows[wid] = ActiveWindow(
                window_id=wid,
                window_name=window_name,
                pane_pid=ppid,
                node_id=node_id,
                started_at=time.time(),
                log_path=str(log_path),
                is_stream_tail=False,
            )

        timed_out = False
        exit_code = -1
        try:
            while True:
                if exit_file.exists():
                    raw = exit_file.read_text().strip()
                    if raw:
                        exit_code = int(raw.splitlines()[0])
                        break
                # Check if window exited unexpectedly before writing exit_file.
                chk = self._tmux_sync("list-panes", "-t", wid, "-F", "#{pane_dead}")
                if chk.returncode != 0:
                    if exit_file.exists():
                        raw = exit_file.read_text().strip()
                        exit_code = int(raw.splitlines()[0]) if raw else 1
                    break
                if (time.monotonic() - t0) > timeout_sec:
                    timed_out = True
                    exit_code = 124
                    break
                await asyncio.sleep(0.35)
        finally:
            await self.close_window(wid, delay_sec=0.2 if timed_out else self.keep_window_sec)

        return exit_code, round(time.monotonic() - t0, 2), timed_out

    def cleanup_all_sync(self, kill_session: bool = False) -> None:
        """Cleanly terminate all active agent windows spawned by this coordinator."""
        for wid, win in list(self.active_windows.items()):
            if win.pane_pid > 1:
                self._kill_pid_tree_safe(win.pane_pid)
            self._tmux_sync("kill-window", "-t", wid)
        self.active_windows.clear()
        if kill_session:
            self._tmux_sync("kill-session", "-t", self.session_name)
