"""Command-line entrypoint for `rswarm`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .bootstrap import bootstrap_workspace
from .coordinator import SwarmCoordinator
from .dashboard import DashboardServer
from .db import SwarmDB
from .governor import ResourceGovernor
from .harnesses import _load_mux_proxy_credentials
from .models import (
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_TAILSCALE_BIND,
    DEFAULT_WORKSPACE_ROOT,
    VALID_EFFORTS,
    SwarmConfig,
    slugify,
)
from .research_tools import fetch_paper_or_url, search_literature_and_web
from .rpc import EvalAndTreeService, send_rpc_request
from .tmux_pool import TmuxPool
from .tui import run_with_tui


def _rswarm_bin_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "bin" / "rswarm").resolve()


def _atomic_save_cfg(ws_dir: Path, cfg: SwarmConfig) -> None:
    cfg.validate()
    cfg_path = ws_dir / "swarm.json"
    fd, tmp = tempfile.mkstemp(dir=str(ws_dir), prefix=".swarm.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg.to_dict(), f, indent=2)
        os.replace(tmp, cfg_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_workspace_cfg(ws_dir: Path) -> tuple[SwarmConfig, SwarmDB]:
    cfg_file = ws_dir / "swarm.json"
    if not cfg_file.is_file():
        raise FileNotFoundError(f"Workspace config not found at {cfg_file}. Run `rswarm init` or `rswarm start` first.")
    cfg = SwarmConfig.from_dict(json.loads(cfg_file.read_text()))
    db = SwarmDB(ws_dir / "state" / "swarm.db")
    return cfg, db


def _build_config_from_args(args: argparse.Namespace) -> SwarmConfig:
    brief = (args.brief or "").strip()
    if args.brief_file:
        brief = Path(args.brief_file).read_text().strip()
    if not brief:
        raise ValueError("Provide `--brief \"...\"` or `--brief-file <path>`.")

    slug = slugify(args.name or brief[:32], max_len=32)
    ws_dir = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else (Path(args.workspace_root).expanduser().resolve() / slug)
    )

    cfg = SwarmConfig(
        name=slug,
        workspace_dir=str(ws_dir),
        brief=brief,
        direction=args.direction,
        leaderboard_target=args.target,
        leaderboard_number_one=args.leaderboard_one,
        leaderboard_submit_threshold=args.submit_threshold,
        harness=args.harness,
        custom_harness_cmd=args.custom_cmd or "",
        researcher_model=args.researcher_model,
        researcher_effort=args.researcher_effort,
        implementer_model=args.implementer_model,
        implementer_effort=args.implementer_effort,
        deepen_model=args.deepen_model or args.researcher_model,
        deepen_effort=args.deepen_effort,
        debugger_model=args.debugger_model or args.implementer_model,
        debugger_effort=args.debugger_effort,
        crossover_model=args.crossover_model or args.researcher_model,
        crossover_effort=args.crossover_effort,
        effort=args.implementer_effort,
        concurrency=args.concurrency,
        max_heavy_mux_procs=args.max_heavy_procs,
        eval_workers=args.eval_workers,
        num_islands=args.islands,
        hosted_quota_per_day=args.hosted_quota,
        auto_hosted_on_sota=args.auto_hosted_on_sota,
        agent_timeout_sec=args.agent_timeout,
        http_timeout_sec=args.agent_timeout,
        eval_timeout_sec=args.eval_timeout,
        max_slice_mem_gib=args.max_slice_gib,
        min_avail_mem_gib=args.min_avail_gib,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,
        use_gpu_gate=args.use_gpu_gate,
        allow_model_override=args.allow_model_override,
    )
    cfg.validate()
    return cfg


async def _cmd_init(args: argparse.Namespace) -> int:
    console = Console()
    cfg = _build_config_from_args(args)
    ws = Path(cfg.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    db = SwarmDB(ws / "state" / "swarm.db")

    console.print(f"[bold cyan]Bootstrapping workspace:[/] {ws}")
    res = await bootstrap_workspace(
        cfg,
        db,
        _rswarm_bin_path(),
        local_eval_src=args.local_eval,
        hosted_eval_src=args.hosted_eval,
        merge_pool_src=args.merge_pool,
        baseline_src=args.baseline,
        force_rebuild=args.force_rebuild,
    )
    console.print(f"[bold green]Workspace ready![/] Baseline score = [bold]{res['baseline_score']}[/] (1.0000x)")
    console.print(f"  Local evaluator : {ws / 'eval/local_eval.sh'}")
    console.print(f"  Hosted adapter  : {ws / 'eval/hosted_submit.sh'}")
    console.print(f"  Installed skills: {ws / 'bin'} (swarm-eval, swarm-propose, swarm-submit, swarm-hosted-submit)")
    return 0


async def _cmd_start_or_run(args: argparse.Namespace, do_init: bool) -> int:
    if do_init:
        cfg = _build_config_from_args(args)
        ws = Path(cfg.workspace_dir)
        ws.mkdir(parents=True, exist_ok=True)
        db = SwarmDB(ws / "state" / "swarm.db")
        await bootstrap_workspace(
            cfg,
            db,
            _rswarm_bin_path(),
            local_eval_src=args.local_eval,
            hosted_eval_src=args.hosted_eval,
            baseline_src=args.baseline,
            force_rebuild=args.force_rebuild,
        )
    else:
        ws = Path(args.workspace).expanduser().resolve()
        cfg, db = _load_workspace_cfg(ws)
        if args.concurrency is not None:
            cfg.concurrency = args.concurrency
        if args.harness is not None:
            cfg.harness = args.harness
        if args.researcher_model is not None:
            cfg.researcher_model = args.researcher_model
        if args.implementer_model is not None:
            cfg.implementer_model = args.implementer_model
        if args.dashboard_port is not None:
            cfg.dashboard_port = args.dashboard_port
        cfg.validate()
        (ws / "swarm.json").write_text(json.dumps(cfg.to_dict(), indent=2))

    coord = SwarmCoordinator(cfg, db)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, coord.request_stop)
        except NotImplementedError:
            pass

    stats = await run_with_tui(
        coord,
        max_evaluations=args.max_evals,
        max_duration_sec=args.duration_sec,
        enable_tui=not args.no_tui,
    )
    console = Console()
    console.print(
        f"\n[bold green]Swarm session stopped cleanly.[/] "
        f"Baseline={stats['baseline_score']} -> Best={stats['best_score']} "
        f"([bold]{float(stats['best_speedup'] or 1.0):.4f}x[/]), "
        f"Evals={stats['evaluated_total']}, Breakthroughs={stats['breakthroughs']}"
    )
    console.print(f"Best artifact directory: [cyan]{ws / 'vault' / 'best'}[/]")
    return 0


async def _cmd_dashboard(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).expanduser().resolve()
    cfg, db = _load_workspace_cfg(ws)
    if args.host:
        cfg.dashboard_host = args.host
    if args.port:
        cfg.dashboard_port = args.port

    gov = ResourceGovernor(
        max_slice_mem_gib=cfg.max_slice_mem_gib,
        min_avail_mem_gib=cfg.min_avail_mem_gib,
        max_heavy_mux_procs=cfg.max_heavy_mux_procs,
    )
    tpool = TmuxPool(f"rswarm-{cfg.name[:20]}", ws)
    srv = DashboardServer(cfg=cfg, db=db, governor=gov, tmux_pool=tpool)
    url = await srv.start()
    print(f"Radial Research Diary serving at: {url}", flush=True)
    stop_ev = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except NotImplementedError:
            pass
    try:
        if args.duration_sec:
            await asyncio.wait_for(stop_ev.wait(), timeout=args.duration_sec)
        else:
            await stop_ev.wait()
    except asyncio.TimeoutError:
        pass
    finally:
        await srv.stop()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).expanduser().resolve()
    cfg, db = _load_workspace_cfg(ws)
    st = db.get_summary_stats()
    islands = db.get_islands()
    console = Console()

    console.print(f"[bold bright_cyan]Workspace:[/] {ws}  ([bold]{cfg.name}[/])")
    lb1_str = f"  |  LB #1: [bold bright_magenta]{cfg.leaderboard_number_one}[/]" if cfg.leaderboard_number_one is not None else ""
    gate_str = f"  |  Submit Gate (Top 5): [bold cyan]{cfg.leaderboard_submit_threshold}[/]" if cfg.leaderboard_submit_threshold is not None else ""
    console.print(
        f"  Baseline: [white]{st['baseline_score']}[/] (1.0000x)  |  "
        f"Global Best: [bold green]{st['best_score']}[/] ([bold green]{float(st['best_speedup'] or 1.0):.4f}x[/])  |  "
        f"Goal: [gold1]{st['leaderboard_target']}[/] ({cfg.direction}){lb1_str}{gate_str}"
    )
    avg_s = float(st["avg_eval_ms"] or 0.0) / 1000.0
    console.print(
        f"  Hypotheses -> Created: [bold cyan]{st['hypotheses_created']}[/]  |  "
        f"Tested: [bold green]{st['hypotheses_tested']}[/] ({st['eval_suite_runs']} eval suite runs)  |  "
        f"Testing Now: [bold yellow]{st['hypotheses_testing_now']}[/]  |  "
        f"Queued: {st['hypotheses_queued']}  |  "
        f"Avg Eval Suite: [bold bright_cyan]{avg_s:.2f}s[/] ({st['avg_eval_ms']:.0f}ms)"
    )
    t = Table(title="Research Islands")
    t.add_column("ID")
    t.add_column("Name", style="cyan")
    t.add_column("Best Score", justify="right")
    t.add_column("Speedup", justify="right", style="green")
    t.add_column("Wins / Evals", justify="right")
    t.add_column("Best Node")
    for isl in islands:
        t.add_row(
            str(isl["island_id"]),
            str(isl["name"]),
            str(isl["best_score"]) if isl["best_score"] is not None else "—",
            f"{float(isl['best_speedup'] or 1.0):.4f}x",
            f"{isl['breakthrough_count']} / {isl['eval_count']}",
            str(isl["best_node_id"] or "—"),
        )
    console.print(t)
    return 0


def _cmd_scale(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).expanduser().resolve()
    cfg, db = _load_workspace_cfg(ws)
    old_c = cfg.concurrency
    cfg.concurrency = max(1, int(args.concurrency))
    if getattr(args, "allow_model_override", False):
        cfg.allow_model_override = True
    _atomic_save_cfg(ws, cfg)
    db.set_meta("concurrency", cfg.concurrency)
    db.log_event(
        "INFO",
        "cli-scale",
        f"Non-destructive live concurrency updated: {old_c} -> {cfg.concurrency} (in-flight agents finish uninterrupted)",
    )
    Console().print(
        f"[bold green]Updated live concurrency:[/] [cyan]{old_c}[/] -> [bold bright_green]{cfg.concurrency}[/] "
        f"(active in-flight agents continue uninterrupted; new spawns respect {cfg.concurrency})."
    )
    return 0


def _cmd_set_model(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).expanduser().resolve()
    cfg, db = _load_workspace_cfg(ws)
    if not args.model and not args.effort:
        raise ValueError("Specify at least `--model <id>` or `--effort <level>`.")

    roles = (
        ["researcher", "implementer", "deepen", "debugger", "crossover"]
        if args.role == "all"
        else [args.role]
    )
    for r in roles:
        if args.model:
            setattr(cfg, f"{r}_model", args.model)
        if args.effort:
            setattr(cfg, f"{r}_effort", args.effort)

    if getattr(args, "allow_model_override", False):
        cfg.allow_model_override = True
    _atomic_save_cfg(ws, cfg)
    routing = cfg.role_routing_dict()
    db.set_meta("researcher_model", cfg.researcher_model)
    db.set_meta("implementer_model", cfg.implementer_model)
    db.set_meta("role_routing", routing)
    summary = ", ".join(f"{r}={routing[r]['model']}[{routing[r]['effort']}]" for r in roles)
    db.log_event("INFO", "cli-model", f"Live role routing updated: {summary}")

    console = Console()
    console.print(f"[bold green]Updated live role routing ({ws.name}):[/] {summary}")
    console.print("Every newly spawned agent of those roles will immediately use this configuration.")
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    console = Console()
    try:
        base_url, token = _load_mux_proxy_credentials()
        req = urllib.request.Request(
            f"{base_url}/v1/models",
            headers={"X-Glm-Proxy-Token": token},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            rows = json.loads(resp.read().decode()).get("data") or []
    except Exception as exc:
        rows = []
        console.print(f"[yellow]Could not query live mux gateway /v1/models ({exc}).[/]")

    if rows:
        mt = Table(title="Available claude-mux Gateway Models")
        mt.add_column("Model ID", style="bold cyan")
        mt.add_column("Display Name", style="white")
        for r in rows:
            mid = str(r.get("id", ""))
            if mid.startswith(("claude-", "fable", "astra", "anthropic-gpt-")):
                continue
            mt.add_row(mid, str(r.get("display_name") or mid))
        console.print(mt)

    agy_table = Table(title="Available Google Gemini Models (via Sparky `agy` CLI)")
    agy_table.add_column("Model ID", style="bold bright_cyan")
    agy_table.add_column("Max Thinking Tier / Notes", style="white")
    for mid, desc in (
        ("gemini-4-carbon-high", "Gemini 4 Carbon (max thinking; `--model gemini-4-carbon --effort max` auto-maps here)"),
        ("gemini-4-argon-high", "Gemini 4 Argon (max thinking; `--model gemini-4-argon --effort max` auto-maps here)"),
        ("gemini-4-neon-xhigh", "Gemini 4 Neon (xhigh max thinking)"),
        ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (xhigh max thinking)"),
        ("gemini-pro-orchestrator", "Gemini Pro Orchestrator (Carbon)"),
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (fast high-thinking builder)"),
    ):
        agy_table.add_row(mid, desc)
    console.print(agy_table)

    if args.workspace:
        ws = Path(args.workspace).expanduser().resolve()
        cfg, _ = _load_workspace_cfg(ws)
        rt = Table(title=f"Active Per-Role Routing ({cfg.name} · concurrency={cfg.concurrency})")
        rt.add_column("Agent Role", style="bold magenta")
        rt.add_column("Tree Node Types", style="dim")
        rt.add_column("Mux Model ID", style="bold green")
        rt.add_column("Reasoning Effort", style="bold gold1")
        role_nodes = {
            "researcher": "EXPLORE (Hypothesis Creator)",
            "implementer": "IMPLEMENT (Builder / Executor)",
            "deepen": "DEEPEN (Breakthrough Exploiter / Scaler)",
            "debugger": "DEBUG (Tester / Repairer)",
            "crossover": "CROSSOVER (Cross-Island Synthesizer)",
        }
        for rname, spec in cfg.role_routing_dict().items():
            rt.add_row(rname, role_nodes[rname], spec["model"], spec["effort"])
        console.print(rt)
    return 0


async def _cmd_agent_rpc(args: argparse.Namespace) -> int:
    ws = Path(args.workspace or os.environ.get("RSWARM_WORKSPACE", "")).expanduser().resolve()
    node_id = getattr(args, "node_id", None) or os.environ.get("RSWARM_NODE_ID")
    sock = Path(os.environ.get("RSWARM_SOCK") or (ws / "state" / "rswarm.sock"))

    sub = args.agent_cmd
    if sub == "eval":
        payload: dict[str, Any] = {
            "action": "eval",
            "node_id": node_id,
            "candidate_path": str(Path(args.candidate).resolve()),
        }
    elif sub in ("propose", "submit-hypothesis"):
        payload = {
            "action": "propose",
            "parent_id": args.parent_id or node_id,
            "island_id": args.island_id,
            "role": args.role,
            "title": args.title,
            "hypothesis": getattr(args, "hypothesis", "") or "",
            "rationale": args.rationale,
            "novel_insight": getattr(args, "novel_insight", "") or "",
            "target_files": getattr(args, "target_files", "") or "",
            "exact_changes": getattr(args, "exact_changes", "") or "",
            "verification_plan": getattr(args, "verification_plan", "") or "",
            "require_spec": (sub == "submit-hypothesis"),
            "origin": getattr(args, "origin", "") or "",
            "target_slice": getattr(args, "target_slice", "") or "",
        }
    elif sub == "directive":
        payload = {
            "action": "directive",
            "node_id": node_id,
            "directive": args.directive,
        }
    elif sub == "island":
        payload = {
            "action": "island",
            "node_id": node_id,
            "name": args.name,
            "focus": args.focus,
            "retire_island_id": args.retire_island,
        }
    elif sub == "search":
        lit_dir = ws / "shared" / "literature"
        res = search_literature_and_web(
            args.query,
            source=args.source,
            limit=args.limit,
            literature_dir=lit_dir,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    elif sub == "fetch":
        lit_dir = ws / "shared" / "literature"
        res = fetch_paper_or_url(
            args.target,
            literature_dir=lit_dir,
            save_slug=args.save_slug or "",
            max_chars=args.max_chars,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    elif sub == "check":
        patch_p = Path(args.patch).resolve()
        cand_p = Path(args.candidate).resolve() if args.candidate else (Path.cwd() / "candidate" / "overrides.json")
        qc_script = ws / "eval" / "quick_check.sh"
        if qc_script.is_file():
            cmd = ["bash", str(qc_script), str(patch_p), str(cand_p)]
            if args.apply:
                cmd.append("--apply")
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out_b, _ = await proc.communicate()
            out_s = out_b.decode("utf-8", errors="replace").strip()
            print(out_s)
            if args.apply and proc.returncode == 0 and cand_p.exists() and sock.exists():
                try:
                    send_rpc_request(sock, {"action": "pool_merge", "node_id": node_id, "candidate_path": str(cand_p)})
                except OSError:
                    pass
            return proc.returncode
        # Generic fallback when workspace has no quick_check.sh: run standard eval on patch_p
        payload = {"action": "eval", "node_id": node_id, "candidate_path": str(patch_p)}
    elif sub == "submit":
        payload = {
            "action": "submit",
            "node_id": node_id,
            "candidate_path": str(Path(args.candidate).resolve()),
            "summary": args.summary,
            "request_hosted": args.request_hosted,
        }
    elif sub == "hosted-submit":
        payload = {
            "action": "hosted_submit",
            "node_id": node_id,
            "candidate_path": str(Path(args.candidate).resolve()),
            "note": args.note,
            "force": bool(getattr(args, "force", False)),
        }
    else:
        raise ValueError(f"Unknown agent subcommand: {sub}")

    if sock.exists():
        try:
            res = send_rpc_request(sock, payload)
            print(json.dumps(res, indent=2))
            return 0 if res.get("ok", True) else 1
        except OSError:
            pass

    # Offline fallback directly against workspace SQLite & evaluator if daemon socket is inactive!
    cfg, db = _load_workspace_cfg(ws)
    svc = EvalAndTreeService(cfg, db)
    if sub == "eval":
        res = await svc.run_local_eval(node_id=node_id, candidate_path=payload["candidate_path"])
    elif sub in ("propose", "submit-hypothesis"):
        res = await svc.handle_propose(payload)
    elif sub == "directive":
        res = await svc.handle_directive(payload)
    elif sub == "island":
        res = await svc.handle_island(payload)
    elif sub == "submit":
        res = await svc.handle_submit(payload)
    else:
        res = await svc.handle_hosted_submit(payload)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok", True) else 1


def _add_shared_init_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", help="Short slug for workspace & tmux session.")
    p.add_argument("--brief", help="Plain English research problem brief.")
    p.add_argument("--brief-file", help="Path to Markdown/text file containing the problem brief.")
    p.add_argument("--workspace", help="Explicit workspace directory path (overrides --workspace-root).")
    p.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT), help="Parent folder for auto-created workspaces.")
    p.add_argument("--direction", choices=["minimize", "maximize"], default="minimize", help="Score optimization direction.")
    p.add_argument("--target", type=float, default=None, help="Goal score milestone.")
    p.add_argument("--leaderboard-one", type=float, default=None, help="Current #1 score on the public competition leaderboard.")
    p.add_argument("--submit-threshold", type=float, default=None, help="Score threshold (e.g. Top-5) required before hosted leaderboard submission fires.")
    p.add_argument("--harness", default="claude-mux-scale", choices=["claude-mux", "claude-mux-scale", "claude-mux-lite", "agy", "custom"])
    p.add_argument("--custom-cmd", default="", help="Command template for --harness custom ({prompt_file}, {model}, {workdir}).")
    p.add_argument("--researcher-model", default=DEFAULT_DEEPSEEK_MODEL, help="Mux model ID for Researcher (EXPLORE) agents.")
    p.add_argument("--researcher-effort", default="max", choices=list(VALID_EFFORTS), help="Effort for Researcher agents (default: max).")
    p.add_argument("--implementer-model", default=DEFAULT_DEEPSEEK_MODEL, help="Mux model ID for Implementer agents.")
    p.add_argument("--implementer-effort", default="minimal", choices=list(VALID_EFFORTS), help="Effort for Implementer agents (default: minimal).")
    p.add_argument("--deepen-model", default=None, help="Mux model ID for Deepen agents (defaults to researcher-model).")
    p.add_argument("--deepen-effort", default="max", choices=list(VALID_EFFORTS), help="Effort for Deepen agents (default: max).")
    p.add_argument("--debugger-model", default=None, help="Mux model ID for Debugger agents (defaults to implementer-model).")
    p.add_argument("--debugger-effort", default="minimal", choices=list(VALID_EFFORTS), help="Effort for Debugger agents (default: minimal).")
    p.add_argument("--crossover-model", default=None, help="Mux model ID for Crossover agents (defaults to researcher-model).")
    p.add_argument("--crossover-effort", default="high", choices=list(VALID_EFFORTS), help="Effort for Crossover agents (default: high).")
    p.add_argument("--concurrency", type=int, default=11, help="Max parallel coding/research agents.")
    p.add_argument("--max-heavy-procs", type=int, default=24, help="Max full claude-mux Node CLI OS processes before bursting onto mux-lite.")
    p.add_argument("--eval-workers", type=int, default=4, help="Max concurrent local evaluation/benchmark workers.")
    p.add_argument("--islands", type=int, default=5, help="Number of independent Research Directions (Islands).")
    p.add_argument("--hosted-quota", type=int, default=5, help="Max hosted leaderboard submissions per 24 hours.")
    p.add_argument("--auto-hosted-on-sota", action="store_true", help="Automatically trigger hosted_submit.sh when global SOTA improves.")
    p.add_argument("--agent-timeout", type=int, default=1200, help="Per-agent window & HTTP retry timeout in seconds (default: 1200s / 20m).")
    p.add_argument("--eval-timeout", type=int, default=180, help="Per-eval timeout in seconds.")
    p.add_argument("--max-slice-gib", type=float, default=72.0, help="Effective non-reclaimable user-slice RAM ceiling in GiB.")
    p.add_argument("--min-avail-gib", type=float, default=20.0, help="System MemAvailable floor in GiB.")
    p.add_argument("--dashboard-host", default=DEFAULT_TAILSCALE_BIND)
    p.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    p.add_argument("--local-eval", help="Optional path to pre-built local_eval.sh script.")
    p.add_argument("--hosted-eval", help="Optional path to pre-built hosted_submit.sh script.")
    p.add_argument("--merge-pool", help="Optional path to pre-built merge_pool.sh per-instance union script.")
    p.add_argument("--baseline", help="Optional path to initial baseline candidate file or folder.")
    p.add_argument("--use-gpu-gate", action="store_true", help="Wrap local evaluations in Sparky gatectl run.")
    p.add_argument("--force-rebuild", action="store_true", help="Re-run Phase-0 harness synthesis even if already initialized.")
    p.add_argument("--allow-model-override", action="store_true", help="Allow overriding the default Sparky model blocklist.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rswarm",
        description="Standalone Autonomous Research Swarm CLI & Radial Diary (claude-mux + DeepSeek).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create workspace, synthesize test harnesses from English brief, and lock baseline.")
    _add_shared_init_flags(p_init)

    p_start = sub.add_parser("start", help="Initialize (if needed) and run the interactive research swarm + radial dashboard.")
    _add_shared_init_flags(p_start)
    p_start.add_argument("--max-evals", type=int, default=None, help="Stop after N evaluated nodes.")
    p_start.add_argument("--duration-sec", type=float, default=None, help="Stop after S seconds.")
    p_start.add_argument("--no-tui", action="store_true", help="Stream structured logs instead of full-screen Rich TUI.")

    p_run = sub.add_parser("run", help="Run or resume an existing initialized research workspace.")
    p_run.add_argument("--workspace", required=True, help="Path to initialized workspace directory.")
    p_run.add_argument("--concurrency", type=int, default=None)
    p_run.add_argument("--harness", choices=["claude-mux", "claude-mux-scale", "claude-mux-lite", "agy", "custom"], default=None)
    p_run.add_argument("--researcher-model", default=None)
    p_run.add_argument("--implementer-model", default=None)
    p_run.add_argument("--dashboard-port", type=int, default=None)
    p_run.add_argument("--max-evals", type=int, default=None)
    p_run.add_argument("--duration-sec", type=float, default=None)
    p_run.add_argument("--no-tui", action="store_true")

    p_dash = sub.add_parser("dashboard", help="Serve the Radial Outward-Branching Research Diary web UI.")
    p_dash.add_argument("--workspace", required=True)
    p_dash.add_argument("--host", default=None)
    p_dash.add_argument("--port", type=int, default=None)
    p_dash.add_argument("--duration-sec", type=float, default=None)

    p_stat = sub.add_parser("status", help="Print current research tree, island elites, and speedup summary.")
    p_stat.add_argument("--workspace", required=True)

    p_scale = sub.add_parser("scale", help="Live non-destructive concurrency scaling on a running workspace.")
    p_scale.add_argument("--workspace", required=True)
    p_scale.add_argument("--concurrency", type=int, required=True, help="New max parallel agent count.")

    p_setm = sub.add_parser("set-model", help="Live hot-switch model and/or effort for a specific agent role.")
    p_setm.add_argument("--workspace", required=True)
    p_setm.add_argument("--role", required=True, choices=["researcher", "implementer", "deepen", "debugger", "crossover", "all"])
    p_setm.add_argument("--model", default=None, help="New claude-mux model ID (e.g. anthropic-morph-dsv41flash, anthropic-morph-kimik31, anthropic-glm-5-3).")
    p_setm.add_argument("--effort", default=None, choices=list(VALID_EFFORTS), help="New reasoning effort (minimal, low, medium, high, xhigh, max).")
    p_setm.add_argument("--allow-model-override", action="store_true", help="Explicitly permit claude-* / Fable models despite default Sparky blocklist.")

    p_mods = sub.add_parser("models", help="List available claude-mux gateway models and active workspace role routing.")
    p_mods.add_argument("--workspace", default=None)

    p_ag = sub.add_parser("agent", help="Internal skill callbacks invoked by swarm agents (`swarm-*`).")
    ag_sub = p_ag.add_subparsers(dest="agent_cmd", required=True)

    ag_eval = ag_sub.add_parser("eval")
    ag_eval.add_argument("candidate")
    ag_eval.add_argument("--workspace", default=None)
    ag_eval.add_argument("--node-id", default=None)

    ag_prop = ag_sub.add_parser("propose")
    ag_prop.add_argument("--rationale", default="")
    ag_prop.add_argument("--novel-insight", default="")
    ag_prop.add_argument("--target-files", default="")
    ag_prop.add_argument("--exact-changes", default="")
    ag_prop.add_argument("--verification-plan", default="")
    ag_prop.add_argument("--hypothesis", required=True)
    ag_prop.add_argument("--target-slice", default="")
    ag_prop.add_argument("--title", required=True)
    ag_prop.add_argument("--role", default="IMPLEMENT")
    ag_prop.add_argument("--origin", default="")
    ag_prop.add_argument("--parent-id", default=None)
    ag_prop.add_argument("--island-id", type=int, default=None)
    ag_prop.add_argument("--workspace", default=None)
    ag_prop.add_argument("--node-id", default=None)

    ag_shyp = ag_sub.add_parser("submit-hypothesis")
    ag_shyp.add_argument("--rationale", required=True)
    ag_shyp.add_argument("--novel-insight", required=True)
    ag_shyp.add_argument("--target-files", required=True)
    ag_shyp.add_argument("--exact-changes", required=True)
    ag_shyp.add_argument("--verification-plan", required=True)
    ag_shyp.add_argument("--target-slice", required=True)
    ag_shyp.add_argument("--title", required=True)
    ag_shyp.add_argument("--hypothesis", default="")
    ag_shyp.add_argument("--role", default="IMPLEMENT")
    ag_shyp.add_argument("--origin", default="")
    ag_shyp.add_argument("--parent-id", default=None)
    ag_shyp.add_argument("--island-id", type=int, default=None)
    ag_shyp.add_argument("--workspace", default=None)
    ag_shyp.add_argument("--node-id", default=None)

    ag_dir = ag_sub.add_parser("directive")
    ag_dir.add_argument("--directive", required=True)
    ag_dir.add_argument("--workspace", default=None)
    ag_dir.add_argument("--node-id", default=None)

    ag_isl = ag_sub.add_parser("island")
    ag_isl.add_argument("--name", required=True)
    ag_isl.add_argument("--focus", required=True)
    ag_isl.add_argument("--retire-island", type=int, default=None)
    ag_isl.add_argument("--workspace", default=None)
    ag_isl.add_argument("--node-id", default=None)

    ag_srch = ag_sub.add_parser("search")
    ag_srch.add_argument("query")
    ag_srch.add_argument("--source", default="all", choices=["all", "papers", "mathoverflow", "github", "wiki"])
    ag_srch.add_argument("--limit", type=int, default=6)
    ag_srch.add_argument("--workspace", default=None)
    ag_srch.add_argument("--node-id", default=None)

    ag_ftch = ag_sub.add_parser("fetch")
    ag_ftch.add_argument("target")
    ag_ftch.add_argument("--save-slug", default="")
    ag_ftch.add_argument("--max-chars", type=int, default=24000)
    ag_ftch.add_argument("--workspace", default=None)
    ag_ftch.add_argument("--node-id", default=None)

    ag_chk = ag_sub.add_parser("check")
    ag_chk.add_argument("patch")
    ag_chk.add_argument("--apply", action="store_true")
    ag_chk.add_argument("--candidate", default=None)
    ag_chk.add_argument("--workspace", default=None)
    ag_chk.add_argument("--node-id", default=None)

    ag_subm = ag_sub.add_parser("submit")
    ag_subm.add_argument("candidate")
    ag_subm.add_argument("--summary", required=True)
    ag_subm.add_argument("--request-hosted", action="store_true")
    ag_subm.add_argument("--workspace", default=None)
    ag_subm.add_argument("--node-id", default=None)

    ag_host = ag_sub.add_parser("hosted-submit")
    ag_host.add_argument("candidate")
    ag_host.add_argument("--note", default="rswarm submission")
    ag_host.add_argument("--force", action="store_true", help="Run official hosted evaluator/submitter immediately, bypassing threshold gate.")
    ag_host.add_argument("--workspace", default=None)
    ag_host.add_argument("--node-id", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return asyncio.run(_cmd_init(args))
    if args.cmd == "start":
        return asyncio.run(_cmd_start_or_run(args, do_init=True))
    if args.cmd == "run":
        return asyncio.run(_cmd_start_or_run(args, do_init=False))
    if args.cmd == "dashboard":
        return asyncio.run(_cmd_dashboard(args))
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "scale":
        return _cmd_scale(args)
    if args.cmd == "set-model":
        return _cmd_set_model(args)
    if args.cmd == "models":
        return _cmd_models(args)
    if args.cmd == "agent":
        return asyncio.run(_cmd_agent_rpc(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
