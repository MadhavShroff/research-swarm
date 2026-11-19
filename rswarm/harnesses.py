"""Pluggable Coding Harnesses for rswarm (`claude-mux`, `claude-mux-lite`, `custom`)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import socket
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .models import (
    is_gemini_agy_model,
    is_rate_limit_or_transient_text,
    resolve_agy_model_id,
    validate_model_choice,
)
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
    rate_limited: bool = False


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
        cuda_bin = "/usr/local/cuda/bin"
        extra_path = f"{bin_dir}:{cuda_bin}" if Path(cuda_bin).is_dir() else bin_dir
        env["PATH"] = f"{extra_path}:{env.get('PATH', '/usr/bin:/bin')}"
        env["RSWARM_WORKSPACE"] = str(self.workspace_dir)
        env["RSWARM_NODE_ID"] = node_id
        env["RSWARM_SOCK"] = str(self.workspace_dir / "state" / "rswarm.sock")
        env["OMP_NUM_THREADS"] = "2"
        env["OPENBLAS_NUM_THREADS"] = "2"
        env["MKL_NUM_THREADS"] = "2"
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
        http_timeout_sec: int = 1200,
        allow_eval_write: bool = False,
    ) -> HarnessResult:
        validate_model_choice(model_id, self.allow_model_override)
        workdir.mkdir(parents=True, exist_ok=True)
        log_path = self.workspace_dir / "logs" / f"{node_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if harness_mode == "agy" or is_gemini_agy_model(model_id):
            return await self._run_agy_cli(
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
                http_timeout_sec=http_timeout_sec,
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
                http_timeout_sec=http_timeout_sec,
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
        http_timeout_sec: int = 1200,
    ) -> HarnessResult:
        prompt_file = workdir / ".rswarm_prompt.md"
        resume_file = workdir / ".rswarm_resume_prompt.md"
        sys_file = workdir / ".rswarm_system.md"
        script_file = workdir / ".rswarm_exec.sh"
        exit_file = workdir / ".rswarm_exit"
        cli_effort = "low" if effort == "minimal" else effort
        api_ms = max(1800000, int(http_timeout_sec) * 1000)
        session_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"rswarm:{self.workspace_dir}:{node_id}"))

        prompt_file.write_text(task_prompt)
        resume_file.write_text(
            "Session resumed after timeout or rate-limit wait. "
            "All files in your working directory and your prior steps are intact. "
            "Continue immediately from where you left off, run `swarm-eval`, and `swarm-submit` your candidate."
        )
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
export API_TIMEOUT_MS={api_ms}
export NODE_OPTIONS="--max-old-space-size=192"
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MALLOC_ARENA_MAX=2
export PATH={shlex.quote(str(self.workspace_dir / 'bin'))}:/usr/local/cuda/bin:"$PATH"
export RSWARM_WORKSPACE={shlex.quote(str(self.workspace_dir))}
export RSWARM_NODE_ID={shlex.quote(node_id)}
export RSWARM_SOCK={shlex.quote(str(self.workspace_dir / 'state' / 'rswarm.sock'))}

cd {shlex.quote(str(workdir))}
exec 9>".rswarm.lock"
if ! flock -n 9; then
  echo "[LOCK BUSY] lock held by another worker on {node_id}; yielding cleanly." >&2
  exit 75
fi
SESSION_ID={shlex.quote(session_uuid)}
RC=1
LAST_RL=0
for ATTEMPT in $(seq 1 20); do
  OUT_TMP=".rswarm_cli_try.log"
  if compgen -G "$HOME/.claude/projects/*/${{SESSION_ID}}.jsonl" >/dev/null 2>&1; then
    printf '=== [claude-mux RESUME try=%s] node=%s window=%s model=%s session=%s ===\\n' "$ATTEMPT" {shlex.quote(node_id)} {shlex.quote(window_name)} {shlex.quote(model_id)} "$SESSION_ID"
    nice -n 15 claude \\
      --resume "$SESSION_ID" \\
      --model {shlex.quote(model_id)} \\
      --effort {shlex.quote(cli_effort)} \\
      --dangerously-skip-permissions \\
      --setting-sources "" \\
      --strict-mcp-config \\
      --mcp-config '{{"mcpServers":{{}}}}' \\
      --append-system-prompt-file {shlex.quote(str(sys_file))} \\
      -p "$(cat {shlex.quote(str(resume_file))})" 2>&1 | tee "$OUT_TMP"
    RC=${{PIPESTATUS[0]}}
  else
    printf '=== [claude-mux try=%s] node=%s window=%s model=%s session=%s ===\\n' "$ATTEMPT" {shlex.quote(node_id)} {shlex.quote(window_name)} {shlex.quote(model_id)} "$SESSION_ID"
    nice -n 15 claude \\
      --session-id "$SESSION_ID" \\
      --model {shlex.quote(model_id)} \\
      --effort {shlex.quote(cli_effort)} \\
      --dangerously-skip-permissions \\
      --setting-sources "" \\
      --strict-mcp-config \\
      --mcp-config '{{"mcpServers":{{}}}}' \\
      --append-system-prompt-file {shlex.quote(str(sys_file))} \\
      -p "$(cat {shlex.quote(str(prompt_file))})" 2>&1 | tee "$OUT_TMP"
    RC=${{PIPESTATUS[0]}}
  fi
  if [ "$RC" -eq 0 ]; then
    rm -f "$OUT_TMP"
    exit 0
  fi
  if grep -qiE "temporarily limiting requests|exceed your account's rate limit|rate.limit|too many requests|overloaded|429|529|503|502|504|bad gateway|service unavailable|timed out" "$OUT_TMP" 2>/dev/null; then
    LAST_RL=1
    WAIT_S=$(( 15 + ATTEMPT * 12 + (RANDOM % 9) ))
    if [ "$WAIT_S" -gt 90 ]; then WAIT_S=90; fi
    printf '[CLI RETRY attempt=%s] Rate-limited or busy; sleeping %ss and continuing...\\n' "$ATTEMPT" "$WAIT_S"
    sleep "$WAIT_S"
    continue
  fi
  rm -f "$OUT_TMP"
  exit "$RC"
done
if [ "$LAST_RL" -eq 1 ]; then
  exit 75
fi
exit "$RC"
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
        was_rl = (rc == 75) or (rc != 0 and is_rate_limit_or_transient_text(tail_txt[-1600:]))
        return HarnessResult(
            exit_code=rc,
            elapsed_sec=elapsed,
            timed_out=timed_out,
            harness_used="claude-mux",
            model_used=model_id,
            tmux_window=window_name,
            final_text=tail_txt,
            log_path=str(log_path),
            rate_limited=was_rl,
        )

    async def _run_agy_cli(
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
        """Run Google Gemini 4 (Carbon / Argon / Neon / Flash) via Sparky's `agy` CLI (`--output-format stream-json`)."""
        agy_model = resolve_agy_model_id(model_id, effort)
        prompt_file = workdir / ".rswarm_agy_prompt.md"
        resume_file = workdir / ".rswarm_agy_resume.md"
        conv_file = workdir / ".rswarm_agy_conv_id"
        parser_file = workdir / ".rswarm_agy_parse.py"
        script_file = workdir / ".rswarm_agy_exec.sh"
        exit_file = workdir / ".rswarm_exit"

        prompt_file.write_text(f"{system_prompt}\n\n{task_prompt}")
        resume_file.write_text(
            "Session resumed after timeout or rate-limit wait. "
            "All files in your working directory and your prior steps are intact. "
            "Continue immediately from where you left off, run `swarm-eval`, and `swarm-submit` your candidate."
        )
        parser_file.write_text(
            """import json, os, sys, time
conv_path = sys.argv[1]
status_path = sys.argv[2]
status = "UNKNOWN"
err_msg = ""
usage = None
for raw in sys.stdin:
    s = raw.strip()
    if not s:
        continue
    if not s.startswith("{"):
        print(s, flush=True)
        continue
    try:
        ev = json.loads(s)
    except ValueError:
        print(s, flush=True)
        continue
    et = ev.get("event")
    if et == "step_update":
        su = ev.get("step_update") or {}
        cid = su.get("conversation_id")
        if cid and not os.path.exists(conv_path):
            open(conv_path, "w").write(str(cid))
        st_type = su.get("step_type") or ""
        td = su.get("text_delta") or ""
        if td:
            sys.stdout.write(td)
            sys.stdout.flush()
        elif su.get("state") == "DONE" and st_type not in ("user_input", "system_message", "agent_response"):
            print(f"\\n[agy step={su.get('step_index')} {st_type} ({su.get('duration_seconds', 0):.1f}s)]", flush=True)
    elif et == "result":
        res = ev.get("result") or {}
        cid = res.get("conversation_id")
        if cid:
            open(conv_path, "w").write(str(cid))
        status = str(res.get("status") or "ERROR")
        err_msg = str(res.get("error") or "")
        usage = res.get("usage")
        if err_msg:
            print(f"\\n[agy ERROR] {err_msg}", flush=True)
        else:
            print(f"\\n=== [agy {status}] turns={res.get('num_turns')} dur={res.get('duration_seconds', 0):.1f}s ===", flush=True)

if isinstance(usage, dict) and (int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)) > 0:
    row = {
        "ts": time.time(),
        "model": "gemini",
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    store = os.path.expanduser("~/.cache/llm-provider-proxy/gemini-usage.jsonl")
    os.makedirs(os.path.dirname(store), exist_ok=True)
    with open(store, "a") as sink:
        sink.write(json.dumps(row) + "\\n")

open(status_path, "w").write(f"{status}\\n{err_msg}\\n")
"""
        )

        agy_bin = str(Path.home() / ".local" / "bin" / "agy")
        if not Path(agy_bin).is_file():
            agy_bin = "agy"

        script_body = f"""#!/usr/bin/env bash
set -uo pipefail
export PATH={shlex.quote(str(self.workspace_dir / 'bin'))}:{shlex.quote(str(Path.home() / '.local' / 'bin'))}:/usr/local/cuda/bin:"$PATH"
export RSWARM_WORKSPACE={shlex.quote(str(self.workspace_dir))}
export RSWARM_NODE_ID={shlex.quote(node_id)}
export RSWARM_SOCK={shlex.quote(str(self.workspace_dir / 'state' / 'rswarm.sock'))}
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MALLOC_ARENA_MAX=2

cd {shlex.quote(str(workdir))}
exec 9>".rswarm.lock"
if ! flock -n 9; then
  echo "[LOCK BUSY] lock held by another worker on {node_id}; yielding cleanly." >&2
  exit 75
fi

LAST_RL=0
for ATTEMPT in $(seq 1 15); do
  rm -f ".rswarm_agy_status"
  if [ -s {shlex.quote(str(conv_file))} ]; then
    CONV_ID="$(cat {shlex.quote(str(conv_file))})"
    printf '=== [agy RESUME try=%s] node=%s window=%s model=%s conv=%s ===\\n' "$ATTEMPT" {shlex.quote(node_id)} {shlex.quote(window_name)} {shlex.quote(agy_model)} "$CONV_ID"
    nice -n 15 {shlex.quote(agy_bin)} \\
      --conversation "$CONV_ID" \\
      -p "$(cat {shlex.quote(str(resume_file))})" \\
      --model {shlex.quote(agy_model)} \\
      --dangerously-skip-permissions \\
      --disable-slash-commands \\
      --output-format stream-json 2>&1 | python3 {shlex.quote(str(parser_file))} {shlex.quote(str(conv_file))} ".rswarm_agy_status"
    RC=${{PIPESTATUS[0]}}
  else
    printf '=== [agy try=%s] node=%s window=%s model=%s ===\\n' "$ATTEMPT" {shlex.quote(node_id)} {shlex.quote(window_name)} {shlex.quote(agy_model)}
    nice -n 15 {shlex.quote(agy_bin)} \\
      -p "$(cat {shlex.quote(str(prompt_file))})" \\
      --model {shlex.quote(agy_model)} \\
      --dangerously-skip-permissions \\
      --disable-slash-commands \\
      --output-format stream-json 2>&1 | python3 {shlex.quote(str(parser_file))} {shlex.quote(str(conv_file))} ".rswarm_agy_status"
    RC=${{PIPESTATUS[0]}}
  fi
  ST_HEAD="$(head -n 1 .rswarm_agy_status 2>/dev/null || echo UNKNOWN)"
  if [ "$RC" -eq 0 ] && [ "$ST_HEAD" = "SUCCESS" ]; then
    exit 0
  fi
  if grep -qiE "temporarily limiting|rate.limit|resource_exhausted|quota|too many requests|overloaded|429|529|503|502|504|unavailable|timed out" ".rswarm_agy_status" 2>/dev/null; then
    LAST_RL=1
    WAIT_S=$(( 15 + ATTEMPT * 12 + (RANDOM % 9) ))
    if [ "$WAIT_S" -gt 90 ]; then WAIT_S=90; fi
    printf '[AGY RETRY attempt=%s] Rate-limited or busy; sleeping %ss and continuing...\\n' "$ATTEMPT" "$WAIT_S"
    sleep "$WAIT_S"
    continue
  fi
  exit 1
done
if [ "$LAST_RL" -eq 1 ]; then
  exit 75
fi
exit 1
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
        was_rl = (rc == 75) or (rc != 0 and is_rate_limit_or_transient_text(tail_txt[-1600:]))
        return HarnessResult(
            exit_code=rc,
            elapsed_sec=elapsed,
            timed_out=timed_out,
            harness_used="agy",
            model_used=agy_model,
            tmux_window=window_name,
            final_text=tail_txt,
            log_path=str(log_path),
            rate_limited=was_rl,
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
        http_timeout_sec: int = 1200,
        allow_eval_write: bool = False,
    ) -> HarnessResult:
        """Ultra-low-RAM (~2 MiB) coding agent speaking Anthropic /v1/messages via llm-provider-proxy."""
        base_url, token = _load_mux_proxy_credentials()
        t0 = time.monotonic()
        ckpt_path = workdir / ".rswarm_checkpoint.json"

        def _append_log(text: str) -> None:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        def _save_checkpoint(msgs: list[dict[str, Any]], completed_turns: int, ftext: str) -> None:
            doc = {
                "messages": msgs,
                "completed_turns": completed_turns,
                "final_text": ftext,
                "updated_at": time.time(),
            }
            fd, tmp = tempfile.mkstemp(dir=str(workdir), prefix=".ckpt.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as cf:
                    json.dump(doc, cf)
                os.replace(tmp, ckpt_path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        messages: list[dict[str, Any]] = [{"role": "user", "content": task_prompt}]
        final_text = ""
        start_turn = 1
        if ckpt_path.is_file():
            try:
                ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
                loaded_msgs = ckpt.get("messages")
                if isinstance(loaded_msgs, list) and loaded_msgs:
                    messages = loaded_msgs
                    start_turn = int(ckpt.get("completed_turns") or 0) + 1
                    final_text = str(ckpt.get("final_text") or "")
                    if messages[-1].get("role") != "user":
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Session resumed after timeout or rate-limit wait. "
                                    "All files in your working directory are intact; continue from where you left off."
                                ),
                            }
                        )
                    _append_log(
                        f"=== [claude-mux-lite RESUME] node={node_id} from turn={start_turn} model={model_id} ==="
                    )
            except Exception as exc:
                _append_log(f"[WARN] Could not parse checkpoint ({exc}); starting fresh.")

        if start_turn == 1:
            _append_log(f"=== [claude-mux-lite] node={node_id} window={window_name} model={model_id} ===")

        wid = await self.tmux_pool.open_stream_window(window_name, node_id, log_path)
        timed_out = False
        rate_limited = False
        exit_code = 0
        max_turns = max(48, start_turn + 24)

        try:
            for turn in range(start_turn, max_turns + 1):
                rem = timeout_sec - (time.monotonic() - t0)
                if rem <= 2.0:
                    timed_out = True
                    exit_code = 124
                    _save_checkpoint(messages, turn - 1, final_text)
                    _append_log(f"[TIMEOUT] Agent exceeded {timeout_sec}s budget; checkpoint saved at turn {turn - 1}.")
                    break

                payload: dict[str, Any] = {
                    "model": model_id,
                    "max_tokens": 24576,
                    "system": system_prompt,
                    "messages": messages,
                    "tools": MUX_LITE_TOOLS,
                }
                if effort == "minimal":
                    # llm-provider-proxy maps budget_tokens <= 512 to Morph OpenAI-wire reasoning_effort="minimal"
                    payload["thinking"] = {"type": "enabled", "budget_tokens": 512}
                else:
                    payload["output_config"] = {"effort": effort}

                resp: dict[str, Any] | None = None
                attempt = 0
                while True:
                    attempt += 1
                    rem = timeout_sec - (time.monotonic() - t0)
                    if rem <= 2.0:
                        timed_out = True
                        exit_code = 124
                        _save_checkpoint(messages, turn - 1, final_text)
                        _append_log(
                            f"[TIMEOUT] Agent reached {timeout_sec}s window during rate-limit/API wait; checkpoint saved."
                        )
                        break
                    call_timeout = min(rem, float(http_timeout_sec))
                    try:
                        resp = await asyncio.to_thread(
                            self._call_mux_http_sync,
                            base_url,
                            token,
                            payload,
                            call_timeout,
                        )
                        break
                    except urllib.error.HTTPError as he:
                        err_body = he.read().decode("utf-8", errors="replace")[:500]
                        _append_log(f"[HTTP {he.code} turn={turn} attempt={attempt}] {err_body}")
                        rem_after = timeout_sec - (time.monotonic() - t0)
                        if he.code in (408, 409, 425, 429, 500, 502, 503, 504, 529) and rem_after > 2.5:
                            retry_hdr = he.headers.get("Retry-After") if he.headers else None
                            try:
                                wait_s = float(retry_hdr) if retry_hdr is not None else min(30.0, 4.0 * (1.6 ** min(attempt - 1, 5)))
                            except ValueError:
                                wait_s = min(30.0, 4.0 * (1.6 ** min(attempt - 1, 5)))
                            if wait_s + 1.2 < rem_after:
                                wait_s = max(1.0, min(wait_s, 45.0))
                                _append_log(f"[RETRY] Rate-limited/busy (HTTP {he.code}); sleeping {wait_s:.1f}s and continuing turn {turn}...")
                                await asyncio.sleep(wait_s)
                                continue
                        if he.code in (408, 409, 425, 429, 500, 502, 503, 504, 529) or is_rate_limit_or_transient_text(err_body):
                            rate_limited = True
                            exit_code = 75
                            _save_checkpoint(messages, turn - 1, final_text)
                        else:
                            exit_code = 1
                        break
                    except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError, OSError) as exc:
                        _append_log(f"[TRANSIENT MUX WAIT turn={turn} attempt={attempt}] {exc}")
                        rem_after = timeout_sec - (time.monotonic() - t0)
                        if rem_after > 10.0:
                            wait_s = min(20.0, rem_after - 4.0, 4.0 * attempt)
                            _append_log(f"[RETRY] Retrying turn {turn} after {wait_s:.1f}s ({rem_after:.0f}s left in 20m window)...")
                            await asyncio.sleep(wait_s)
                            continue
                        timed_out = True
                        exit_code = 124
                        _save_checkpoint(messages, turn - 1, final_text)
                        break
                    except Exception as exc:
                        _append_log(f"[MUX ERROR] {exc}")
                        exit_code = 1
                        break

                if resp is None:
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
                    if stop_reason == "max_tokens" and turn < max_turns:
                        _append_log(f"\n[Turn {turn} · Max-tokens reached mid-thought; nudging tool execution...]")
                        if assistant_keep:
                            messages.append({"role": "assistant", "content": assistant_keep})
                        messages.append(
                            {
                                "role": "user",
                                "content": "Your previous turn hit max_tokens before emitting a tool call. Do not repeat your reasoning—immediately call `write_file` or `bash` to complete the task.",
                            }
                        )
                        _save_checkpoint(messages, turn, final_text)
                        continue
                    _save_checkpoint(messages, turn, final_text)
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
                rem_post = timeout_sec - (time.monotonic() - t0)
                if (turn >= max_turns - 2 or rem_post < 120.0) and turn < max_turns:
                    tool_results.append(
                        {
                            "type": "text",
                            "text": (
                                "[SYSTEM FINAL-TURN FLUSH NOTICE] Fewer than 2 turns / 120s remain in this window. "
                                "Stop exploratory tuning—immediately write your best verified results into `./candidate/`, "
                                "run `swarm-eval`, and run `swarm-submit` NOW so zero progress is lost!"
                            ),
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
                _save_checkpoint(messages, turn, final_text)
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
            rate_limited=rate_limited,
        )
