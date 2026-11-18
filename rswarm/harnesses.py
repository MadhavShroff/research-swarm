"""Pluggable Coding Harnesses for rswarm (`claude-mux`, `claude-mux-lite`, `custom`)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import validate_model_choice
from .tmux_pool import TmuxPool


@dataclasses.dataclass
class HarnessResult:
    exit_code: int
    elapsed_sec: float
    timed_out: bool
    harness_used: str
    model_used: str
    tmux_window: str
    final_text: str
    log_path: str


def _load_mux_proxy_credentials() -> tuple[str, str]:
    """Load (base_url, proxy_token) from Sparky's llm-provider-proxy.env without logging secrets."""
    candidates = [
        Path.home() / ".secrets/llm-provider-proxy.env",
        Path.home() / ".secrets/glm-proxy.env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        kv: dict[str, str] = {}
        for line in env_path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            kv[k.strip()] = v.strip().strip('"').strip("'")
        bind = kv.get("LLM_PROXY_BIND") or kv.get("GLM_PROXY_BIND")
        port = kv.get("LLM_PROXY_PORT") or kv.get("GLM_PROXY_PORT")
        tok = kv.get("LLM_PROXY_TOKEN") or kv.get("GLM_PROXY_TOKEN")
        if bind and port and tok:
            return f"http://{bind}:{port}", tok
    raise FileNotFoundError(
        "Could not find ~/.secrets/llm-provider-proxy.env (or glm-proxy.env) for claude-mux."
    )


MUX_LITE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "Execute a bash command inside your isolated working directory. "
            "PATH includes `swarm-eval`, `swarm-propose`, `swarm-submit`, and `swarm-hosted-submit`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Exact bash command string to execute."},
                "timeout_sec": {"type": "integer", "description": "Optional timeout in seconds (default 90)."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read text from a file inside the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative or absolute path."},
                "start_line": {"type": "integer", "description": "1-based start line (optional)."},
                "end_line": {"type": "integer", "description": "1-based end line (optional)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in your working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to your working directory."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact target text in an existing file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to your working directory."},
                "old_text": {"type": "string", "description": "Exact substring to replace."},
                "new_text": {"type": "string", "description": "Replacement substring."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]


class HarnessRunner:
    """Callable coding-harness abstraction supporting claude-mux CLI, claude-mux-lite, and custom."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        tmux_pool: TmuxPool,
        custom_harness_cmd: str = "",
        allow_model_override: bool = False,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.tmux_pool = tmux_pool
        self.custom_harness_cmd = custom_harness_cmd
        self.allow_model_override = allow_model_override

    def _build_agent_env(self, node_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for drop in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(drop, None)
        bin_dir = str(self.workspace_dir / "bin")
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
        env["RSWARM_WORKSPACE"] = str(self.workspace_dir)
        env["RSWARM_NODE_ID"] = node_id
        env["RSWARM_SOCK"] = str(self.workspace_dir / "state" / "rswarm.sock")
        env["OMP_NUM_THREADS"] = "1"
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["MKL_NUM_THREADS"] = "1"
        env["MALLOC_ARENA_MAX"] = "2"
        if extra:
            env.update(extra)
        return env

    async def run_agent(
        self,
        *,
        harness_mode: str,
        model_id: str,
        effort: str,
        node_id: str,
        window_name: str,
        workdir: Path,
        system_prompt: str,
        task_prompt: str,
        timeout_sec: int,
        allow_eval_write: bool = False,
    ) -> HarnessResult:
        validate_model_choice(model_id, self.allow_model_override)
        workdir.mkdir(parents=True, exist_ok=True)
        log_path = self.workspace_dir / "logs" / f"{node_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if harness_mode == "claude-mux":
            return await self._run_claude_mux_cli(
                model_id=model_id,
                effort=effort,
                node_id=node_id,
                window_name=window_name,
                workdir=workdir,
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                log_path=log_path,
                timeout_sec=timeout_sec,
            )
        if harness_mode == "claude-mux-lite":
            return await self._run_claude_mux_lite(
                model_id=model_id,
                effort=effort,
                node_id=node_id,
                window_name=window_name,
                workdir=workdir,
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                log_path=log_path,
                timeout_sec=timeout_sec,
                allow_eval_write=allow_eval_write,
            )
        if harness_mode == "custom":
            return await self._run_custom_cmd(
                model_id=model_id,
                node_id=node_id,
                window_name=window_name,
                workdir=workdir,
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                log_path=log_path,
                timeout_sec=timeout_sec,
            )
        raise ValueError(f"Unsupported concrete harness_mode: {harness_mode}")

    async def _run_claude_mux_cli(
        self,
        *,
        model_id: str,
        effort: str,
        node_id: str,
        window_name: str,
        workdir: Path,
        system_prompt: str,
        task_prompt: str,
        log_path: Path,
        timeout_sec: int,
    ) -> HarnessResult:
        prompt_file = workdir / ".rswarm_prompt.md"
        sys_file = workdir / ".rswarm_system.md"
        script_file = workdir / ".rswarm_exec.sh"
        exit_file = workdir / ".rswarm_exit"

        prompt_file.write_text(task_prompt)
        sys_file.write_text(system_prompt)

        # Source llm-provider-proxy.env inside bash so proxy token never appears in ps/tmux argv!
        script_body = f"""#!/usr/bin/env bash
set -uo pipefail
if [ -r "$HOME/.secrets/llm-provider-proxy.env" ]; then
  . "$HOME/.secrets/llm-provider-proxy.env"
  BIND="${{LLM_PROXY_BIND:-${{GLM_PROXY_BIND}}}}"
  PORT="${{LLM_PROXY_PORT:-${{GLM_PROXY_PORT}}}}"
  TOKEN="${{LLM_PROXY_TOKEN:-${{GLM_PROXY_TOKEN}}}}"
elif [ -r "$HOME/.secrets/glm-proxy.env" ]; then
  . "$HOME/.secrets/glm-proxy.env"
  BIND="${{GLM_PROXY_BIND}}"
  PORT="${{GLM_PROXY_PORT}}"
  TOKEN="${{GLM_PROXY_TOKEN}}"
else
  echo "ERROR: missing ~/.secrets/llm-provider-proxy.env" >&2
  exit 2
fi
unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
export ANTHROPIC_BASE_URL="http://${{BIND}}:${{PORT}}"
export ANTHROPIC_CUSTOM_HEADERS="X-Glm-Proxy-Token: ${{TOKEN}}"
export CLAUDE_MUX_ROUTING_MARKER="dgx-claude-mux-routing-v1"
export DISABLE_AUTOUPDATER=1
export DISABLE_UPDATES=1
export API_TIMEOUT_MS=1800000
export NODE_OPTIONS="--max-old-space-size=192"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MALLOC_ARENA_MAX=2
export PATH={shlex.quote(str(self.workspace_dir / 'bin'))}:"$PATH"
export RSWARM_WORKSPACE={shlex.quote(str(self.workspace_dir))}
export RSWARM_NODE_ID={shlex.quote(node_id)}
export RSWARM_SOCK={shlex.quote(str(self.workspace_dir / 'state' / 'rswarm.sock'))}

cd {shlex.quote(str(workdir))}
printf '=== [claude-mux] node=%s window=%s model=%s ===\\n' {shlex.quote(node_id)} {shlex.quote(window_name)} {shlex.quote(model_id)}
exec nice -n 15 claude \\
  --model {shlex.quote(model_id)} \\
  --effort {shlex.quote(effort)} \\
  --dangerously-skip-permissions \\
  --no-session-persistence \\
  --setting-sources "" \\
  --strict-mcp-config \\
  --mcp-config '{{"mcpServers":{{}}}}' \\
  --append-system-prompt-file {shlex.quote(str(sys_file))} \\
  -p "$(cat {shlex.quote(str(prompt_file))})"
"""
        script_file.write_text(script_body)
        script_file.chmod(0o700)

        rc, elapsed, timed_out = await self.tmux_pool.run_script_in_window(
            window_name=window_name,
            node_id=node_id,
            workdir=workdir,
            script_path=script_file,
            log_path=log_path,
            exit_file=exit_file,
            timeout_sec=timeout_sec,
        )
        tail_txt = ""
        if log_path.exists():
            tail_txt = log_path.read_text(errors="replace")[-6000:]
        return HarnessResult(
            exit_code=rc,
            elapsed_sec=elapsed,
            timed_out=timed_out,
            harness_used="claude-mux",
            model_used=model_id,
            tmux_window=window_name,
            final_text=tail_txt,
            log_path=str(log_path),
        )

    async def _run_custom_cmd(
        self,
        *,
        model_id: str,
        node_id: str,
        window_name: str,
        workdir: Path,
        system_prompt: str,
        task_prompt: str,
        log_path: Path,
        timeout_sec: int,
    ) -> HarnessResult:
        if not self.custom_harness_cmd:
            raise ValueError("custom_harness_cmd is empty for custom harness mode.")
        prompt_file = workdir / ".rswarm_prompt.md"
        script_file = workdir / ".rswarm_custom.sh"
        exit_file = workdir / ".rswarm_exit"
        prompt_file.write_text(f"{system_prompt}\n\n{task_prompt}")

        rendered = self.custom_harness_cmd.format(
            prompt_file=shlex.quote(str(prompt_file)),
            model=shlex.quote(model_id),
            workdir=shlex.quote(str(workdir)),
        )
        script_body = f"""#!/usr/bin/env bash
set -uo pipefail
export PATH={shlex.quote(str(self.workspace_dir / 'bin'))}:"$PATH"
export RSWARM_WORKSPACE={shlex.quote(str(self.workspace_dir))}
export RSWARM_NODE_ID={shlex.quote(node_id)}
export RSWARM_SOCK={shlex.quote(str(self.workspace_dir / 'state' / 'rswarm.sock'))}
export OMP_NUM_THREADS=1
cd {shlex.quote(str(workdir))}
exec nice -n 15 {rendered}
"""
        script_file.write_text(script_body)
        script_file.chmod(0o700)
        rc, elapsed, timed_out = await self.tmux_pool.run_script_in_window(
            window_name=window_name,
            node_id=node_id,
            workdir=workdir,
            script_path=script_file,
            log_path=log_path,
            exit_file=exit_file,
            timeout_sec=timeout_sec,
        )
        tail_txt = log_path.read_text(errors="replace")[-6000:] if log_path.exists() else ""
        return HarnessResult(
            exit_code=rc,
            elapsed_sec=elapsed,
            timed_out=timed_out,
            harness_used="custom",
            model_used=model_id,
            tmux_window=window_name,
            final_text=tail_txt,
            log_path=str(log_path),
        )

    def _call_mux_http_sync(
        self,
        base_url: str,
        token: str,
        payload: dict[str, Any],
        http_timeout: float,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "X-Glm-Proxy-Token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _resolve_safe_path(self, workdir: Path, rel_or_abs: str, allow_eval_write: bool, write: bool) -> Path:
        p = Path(rel_or_abs)
        target = (p if p.is_absolute() else (workdir / p)).resolve()
        ws_root = self.workspace_dir.resolve()
        eval_dir = (ws_root / "eval").resolve()
        if write:
            if allow_eval_write:
                if not str(target).startswith(str(ws_root)):
                    raise PermissionError(f"Write outside workspace refused: {target}")
            else:
                if str(target).startswith(str(eval_dir)):
                    raise PermissionError("Write to immutable eval/ directory is forbidden!")
                if not str(target).startswith(str(workdir.resolve())):
                    raise PermissionError(f"Agents may only write inside {workdir}: got {target}")
        return target

    async def _exec_lite_tool(
        self,
        name: str,
        inp: dict[str, Any],
        workdir: Path,
        node_id: str,
        allow_eval_write: bool,
    ) -> str:
        try:
            if name == "read_file":
                target = self._resolve_safe_path(workdir, str(inp.get("path", "")), allow_eval_write, write=False)
                if not target.is_file():
                    return f"ERROR: file not found: {target}"
                lines = target.read_text(errors="replace").splitlines()
                s = max(1, int(inp.get("start_line") or 1))
                e = min(len(lines), int(inp.get("end_line") or min(len(lines), s + 399)))
                numbered = [f"{idx}: {lines[idx - 1]}" for idx in range(s, e + 1)]
                return "\n".join(numbered)[:24000] or "(empty file)"

            if name == "write_file":
                target = self._resolve_safe_path(workdir, str(inp.get("path", "")), allow_eval_write, write=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                content = str(inp.get("content", ""))
                target.write_text(content)
                return f"OK: wrote {len(content)} bytes to {target}"

            if name == "edit_file":
                target = self._resolve_safe_path(workdir, str(inp.get("path", "")), allow_eval_write, write=True)
                if not target.is_file():
                    return f"ERROR: file not found: {target}"
                src = target.read_text()
                old_t = str(inp.get("old_text", ""))
                new_t = str(inp.get("new_text", ""))
                if old_t not in src:
                    return "ERROR: old_text not found in file."
                target.write_text(src.replace(old_t, new_t, 1))
                return f"OK: updated {target}"

            if name == "bash":
                cmd = str(inp.get("command", "")).strip()
                if not cmd:
                    return "ERROR: empty command"
                t_sec = max(5, min(180, int(inp.get("timeout_sec") or 90)))
                env = self._build_agent_env(node_id)
                proc = await asyncio.create_subprocess_shell(
                    f"nice -n 15 bash -c {shlex.quote(cmd)}",
                    cwd=str(workdir),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=t_sec)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, 15)
                    except OSError:
                        pass
                    return f"ERROR: command timed out after {t_sec}s"
                out_str = out_b.decode("utf-8", errors="replace")
                if len(out_str) > 16000:
                    out_str = out_str[:8000] + "\n...[truncated]...\n" + out_str[-8000:]
                return f"[exit_code={proc.returncode}]\n{out_str}"

            return f"ERROR: unknown tool '{name}'"
        except Exception as exc:
            return f"ERROR executing {name}: {exc}"

    async def _run_claude_mux_lite(
        self,
        *,
        model_id: str,
        effort: str,
        node_id: str,
        window_name: str,
        workdir: Path,
        system_prompt: str,
        task_prompt: str,
        log_path: Path,
        timeout_sec: int,
        allow_eval_write: bool = False,
    ) -> HarnessResult:
        """Ultra-low-RAM (~2 MiB) coding agent speaking Anthropic /v1/messages via llm-provider-proxy."""
        base_url, token = _load_mux_proxy_credentials()
        t0 = time.monotonic()

        def _append_log(text: str) -> None:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        _append_log(f"=== [claude-mux-lite] node={node_id} window={window_name} model={model_id} ===")
        wid = await self.tmux_pool.open_stream_window(window_name, node_id, log_path)

        messages: list[dict[str, Any]] = [{"role": "user", "content": task_prompt}]
        final_text = ""
        timed_out = False
        exit_code = 0

        try:
            for turn in range(1, 20):
                rem = timeout_sec - (time.monotonic() - t0)
                if rem <= 2.0:
                    timed_out = True
                    exit_code = 124
                    _append_log(f"[TIMEOUT] Agent exceeded {timeout_sec}s wall-clock budget.")
                    break

                payload: dict[str, Any] = {
                    "model": model_id,
                    "max_tokens": 24576,
                    "system": system_prompt,
                    "messages": messages,
                    "tools": MUX_LITE_TOOLS,
                    "output_config": {"effort": effort},
                }

                try:
                    resp = await asyncio.to_thread(
                        self._call_mux_http_sync,
                        base_url,
                        token,
                        payload,
                        min(rem, 360.0),
                    )
                except urllib.error.HTTPError as he:
                    err_body = he.read().decode("utf-8", errors="replace")[:500]
                    _append_log(f"[HTTP {he.code}] {err_body}")
                    if he.code in (429, 502, 503, 504) and rem > 15:
                        await asyncio.sleep(4.0)
                        continue
                    exit_code = 1
                    break
                except Exception as exc:
                    _append_log(f"[MUX ERROR] {exc}")
                    exit_code = 1
                    break

                blocks = resp.get("content") or []
                assistant_keep: list[dict[str, Any]] = []
                tool_uses: list[dict[str, Any]] = []

                for b in blocks:
                    btype = b.get("type")
                    if btype == "thinking":
                        th = (b.get("thinking") or "").strip()
                        if th:
                            _append_log(f"\n[Turn {turn} · DeepSeek Thinking]\n{th[:2000]}")
                    elif btype == "text":
                        tx = (b.get("text") or "").strip()
                        if tx:
                            final_text = tx
                            _append_log(f"\n[Turn {turn} · Agent]\n{tx}")
                        assistant_keep.append({"type": "text", "text": b.get("text") or ""})
                    elif btype == "tool_use":
                        tool_uses.append(b)
                        assistant_keep.append(b)
                        _append_log(
                            f"\n[Turn {turn} · Tool -> {b.get('name')}] {json.dumps(b.get('input') or {})[:500]}"
                        )

                stop_reason = resp.get("stop_reason")
                if not tool_uses:
                    if stop_reason == "max_tokens" and turn < 19:
                        _append_log(f"\n[Turn {turn} · Max-tokens reached mid-thought; nudging tool execution...]")
                        if assistant_keep:
                            messages.append({"role": "assistant", "content": assistant_keep})
                        messages.append(
                            {
                                "role": "user",
                                "content": "Your previous turn hit max_tokens before emitting a tool call. Do not repeat your reasoning—immediately call `write_file` or `bash` to complete the task.",
                            }
                        )
                        continue
                    _append_log(f"\n=== [DONE] turns={turn} elapsed={time.monotonic()-t0:.1f}s ===")
                    break

                messages.append({"role": "assistant", "content": assistant_keep})
                tool_results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    res_str = await self._exec_lite_tool(
                        str(tu.get("name", "")),
                        tu.get("input") or {},
                        workdir,
                        node_id,
                        allow_eval_write,
                    )
                    _append_log(f"[Tool Result <- {tu.get('name')}]\n{res_str[:1500]}")
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": res_str,
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
        finally:
            if wid:
                await self.tmux_pool.close_window(wid)

        return HarnessResult(
            exit_code=exit_code,
            elapsed_sec=round(time.monotonic() - t0, 2),
            timed_out=timed_out,
            harness_used="claude-mux-lite",
            model_used=model_id,
            tmux_window=window_name,
            final_text=final_text,
            log_path=str(log_path),
        )
