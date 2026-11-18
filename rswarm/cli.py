"""Command-line entrypoint for `rswarm`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .bootstrap import bootstrap_workspace
from .coordinator import SwarmCoordinator
from .dashboard import DashboardServer
from .db import SwarmDB
from .governor import ResourceGovernor
from .models import (
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_TAILSCALE_BIND,
    DEFAULT_WORKSPACE_ROOT,
    SwarmConfig,
    slugify,
)
from .rpc import EvalAndTreeService, send_rpc_request
from .tmux_pool import TmuxPool
from .tui import run_with_tui


def _rswarm_bin_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "bin" / "rswarm").resolve()


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
        harness=args.harness,
        custom_harness_cmd=args.custom_cmd or "",
        researcher_model=args.researcher_model,
        implementer_model=args.implementer_model,
        effort=args.effort,
        concurrency=args.concurrency,
        max_heavy_mux_procs=args.max_heavy_procs,
        eval_workers=args.eval_workers,
        num_islands=args.islands,
        hosted_quota_per_day=args.hosted_quota,
        auto_hosted_on_sota=args.auto_hosted_on_sota,
        agent_timeout_sec=args.agent_timeout,
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
    console.print(
        f"  Baseline: [white]{st['baseline_score']}[/] (1.0000x)  |  "
        f"Global Best: [bold green]{st['best_score']}[/] ([bold green]{float(st['best_speedup'] or 1.0):.4f}x[/])  |  "
        f"Target: [gold1]{st['leaderboard_target']}[/] ({cfg.direction})"
    )
    console.print(
        f"  Nodes -> Evaluated: {st['evaluated_total']}  Breakthroughs: [green]{st['breakthroughs']}[/]  "
        f"Running: [yellow]{st['running']}[/]  Queued: {st['queued']}  "
        f"Novelty Rejected: {st['novelty_rejected']}"
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
    elif sub == "propose":
        payload = {
            "action": "propose",
            "parent_id": args.parent_id or node_id,
            "island_id": args.island_id,
            "role": args.role,
            "title": args.title,
            "hypothesis": args.hypothesis,
            "rationale": args.rationale,
        }
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
    elif sub == "propose":
        res = await svc.handle_propose(payload)
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
    p.add_argument("--target", type=float, default=None, help="Leaderboard #1 or goal score to beat.")
    p.add_argument("--harness", default="claude-mux-scale", choices=["claude-mux", "claude-mux-scale", "claude-mux-lite", "custom"])
    p.add_argument("--custom-cmd", default="", help="Command template for --harness custom ({prompt_file}, {model}, {workdir}).")
    p.add_argument("--researcher-model", default=DEFAULT_DEEPSEEK_MODEL, help="Mux model ID for Researcher agents.")
    p.add_argument("--implementer-model", default=DEFAULT_DEEPSEEK_MODEL, help="Mux model ID for Implementer/Debugger agents.")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--concurrency", type=int, default=16, help="Max parallel coding/research agents (supports 100-300+).")
    p.add_argument("--max-heavy-procs", type=int, default=24, help="Max full claude-mux Node CLI OS processes before bursting onto mux-lite.")
    p.add_argument("--eval-workers", type=int, default=4, help="Max concurrent local evaluation/benchmark workers.")
    p.add_argument("--islands", type=int, default=5, help="Number of independent Research Directions (Islands).")
    p.add_argument("--hosted-quota", type=int, default=5, help="Max hosted leaderboard submissions per 24 hours.")
    p.add_argument("--auto-hosted-on-sota", action="store_true", help="Automatically trigger hosted_submit.sh when global SOTA improves.")
    p.add_argument("--agent-timeout", type=int, default=900, help="Per-agent timeout in seconds.")
    p.add_argument("--eval-timeout", type=int, default=180, help="Per-eval timeout in seconds.")
    p.add_argument("--max-slice-gib", type=float, default=72.0, help="Effective non-reclaimable user-slice RAM ceiling in GiB.")
    p.add_argument("--min-avail-gib", type=float, default=20.0, help="System MemAvailable floor in GiB.")
    p.add_argument("--dashboard-host", default=DEFAULT_TAILSCALE_BIND)
    p.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    p.add_argument("--local-eval", help="Optional path to pre-built local_eval.sh script.")
    p.add_argument("--hosted-eval", help="Optional path to pre-built hosted_submit.sh script.")
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
    p_run.add_argument("--harness", choices=["claude-mux", "claude-mux-scale", "claude-mux-lite", "custom"], default=None)
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

    p_ag = sub.add_parser("agent", help="Internal skill callbacks invoked by swarm agents (`swarm-*`).")
    ag_sub = p_ag.add_subparsers(dest="agent_cmd", required=True)

    ag_eval = ag_sub.add_parser("eval")
    ag_eval.add_argument("candidate")
    ag_eval.add_argument("--workspace", default=None)
    ag_eval.add_argument("--node-id", default=None)

    ag_prop = ag_sub.add_parser("propose")
    ag_prop.add_argument("--title", required=True)
    ag_prop.add_argument("--hypothesis", required=True)
    ag_prop.add_argument("--rationale", default="")
    ag_prop.add_argument("--role", default="IMPLEMENT")
    ag_prop.add_argument("--parent-id", default=None)
    ag_prop.add_argument("--island-id", type=int, default=None)
    ag_prop.add_argument("--workspace", default=None)
    ag_prop.add_argument("--node-id", default=None)

    ag_subm = ag_sub.add_parser("submit")
    ag_subm.add_argument("candidate")
    ag_subm.add_argument("--summary", required=True)
    ag_subm.add_argument("--request-hosted", action="store_true")
    ag_subm.add_argument("--workspace", default=None)
    ag_subm.add_argument("--node-id", default=None)

    ag_host = ag_sub.add_parser("hosted-submit")
    ag_host.add_argument("candidate")
    ag_host.add_argument("--note", default="rswarm submission")
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
    if args.cmd == "agent":
        return asyncio.run(_cmd_agent_rpc(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
