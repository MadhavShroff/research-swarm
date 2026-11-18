"""Interactive Long-Running Terminal TUI with Live Progress Bars & Log Stream."""

from __future__ import annotations

import asyncio
import sys
import termios
import time
import tty
from typing import TYPE_CHECKING

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from .coordinator import SwarmCoordinator


def _render_bar(ratio: float, width: int = 28, color: str = "cyan") -> Text:
    clamped = max(0.0, min(1.0, ratio))
    filled = int(round(clamped * width))
    empty = max(0, width - filled)
    t = Text()
    t.append("█" * filled, style=f"bold {color}")
    t.append("░" * empty, style="dim white")
    return t


def build_tui_layout(coord: "SwarmCoordinator", max_evals: int | None) -> Layout:
    stats = coord.db.get_summary_stats()
    islands = coord.db.get_islands()
    snap = coord.governor.sample()
    live_wins = coord.tmux_pool.list_live_window_names()
    events = coord.db.export_dashboard_state()["events"][:14]

    uptime_s = max(0, int(time.time() - coord.started_at))
    mm, ss = divmod(uptime_s, 60)
    hh, mm = divmod(mm, 60)

    baseline = stats["baseline_score"]
    best = stats["best_score"]
    speedup = float(stats["best_speedup"] or 1.0)
    goal_prog = float(stats["best_goal_progress"] or 0.0)
    target = stats["leaderboard_target"]
    dash_url = coord.db.get_meta("dashboard_url", "starting...")

    # 1. Header summary
    hdr = Text()
    hdr.append(f" RSWARM :: {coord.cfg.name} ", style="bold black on bright_cyan")
    hdr.append(f"  up {hh:02d}:{mm:02d}:{ss:02d}  ", style="bold white")
    if coord.paused:
        hdr.append(" [PAUSED] ", style="bold white on red")
    hdr.append(f"harness=", style="dim")
    hdr.append(f"{coord.cfg.harness} ", style="bold yellow")
    hdr.append(f"model=", style="dim")
    hdr.append(f"{coord.cfg.researcher_model} ", style="bold magenta")
    hdr.append(f"\n Workspace: ", style="dim")
    hdr.append(f"{coord.ws}  ", style="cyan")
    hdr.append("tmux: ", style="dim")
    hdr.append(f"tmux attach -t {coord.tmux_pool.session_name}  ", style="green")
    hdr.append("Radial UI: ", style="dim")
    hdr.append(f"{dash_url}", style="bold bright_green underline")
    hdr.append(f"\n Baseline: ", style="dim")
    hdr.append(f"{baseline} (1.0000x)  ", style="white")
    hdr.append("Global Best: ", style="dim")
    hdr.append(f"{best} ({speedup:.4f}x)  ", style="bold bright_green")
    hdr.append("Target SOTA: ", style="dim")
    hdr.append(f"{target if target is not None else 'open-ended'} ({coord.cfg.direction})  ", style="bold gold1")
    hdr.append("[Keys: p=pause/resume  q=quit]", style="dim italic")

    # 2. Four live progress bars
    pb_table = Table.grid(padding=(0, 2), expand=True)
    pb_table.add_column("Metric", width=26, style="bold white")
    pb_table.add_column("Bar", width=30)
    pb_table.add_column("Value", ratio=1)

    goal_pct = goal_prog * 100.0
    goal_badge = " (BEYOND LEADERBOARD TARGET!)" if goal_prog >= 1.0 else ""
    pb_table.add_row(
        "1. Goal / Speedup",
        _render_bar(goal_prog if target is not None else min(1.0, (speedup - 1.0) * 2.0), color="bright_green"),
        Text(f"{goal_pct:6.1f}%  speedup={speedup:.4f}x{goal_badge}", style="bold green"),
    )

    active_total = len(coord.active_tasks)
    cap_ratio = active_total / max(1, coord.cfg.concurrency)
    pb_table.add_row(
        "2. Parallel Agents",
        _render_bar(cap_ratio, color="bright_cyan"),
        Text(
            f"{active_total:3d}/{coord.cfg.concurrency:<3d} running  "
            f"(mux-cli={coord.active_heavy_procs}, mux-lite={coord.active_lite_workers}, "
            f"tmux_wins={len(live_wins)}, queued={stats['queued']})",
            style="cyan",
        ),
    )

    eval_done = int(stats["evaluated_total"])
    eval_denom = max_evals if max_evals else max(20, eval_done + stats["queued"] + active_total)
    pb_table.add_row(
        "3. Research Tree Evals",
        _render_bar(eval_done / max(1, eval_denom), color="magenta"),
        Text(
            f"{eval_done:4d}/{eval_denom:<4d} evals  "
            f"breakthroughs={stats['breakthroughs']}  obstructed={stats['obstructed']}  "
            f"novelty_filtered={stats['novelty_rejected']}",
            style="magenta",
        ),
    )

    ram_ratio = snap.slice_current_gib / max(1.0, coord.cfg.max_slice_mem_gib)
    ram_col = "red" if ram_ratio > 0.88 else ("yellow" if ram_ratio > 0.70 else "green")
    pb_table.add_row(
        "4. Sparky RAM & CPU",
        _render_bar(ram_ratio, color=ram_col),
        Text(
            f"slice={snap.slice_current_gib:.1f}/{coord.cfg.max_slice_mem_gib:.0f}GiB  "
            f"avail={snap.mem_avail_gib:.1f}GiB  load1m={snap.load_1m:.1f}/{snap.cpu_count}c",
            style=ram_col,
        ),
    )

    # 3. Islands frontier table
    isl_table = Table(expand=True, box=None, pad_edge=False)
    isl_table.add_column("ID", width=3, style="dim")
    isl_table.add_column("Research Direction (Island)", width=20, style="bold cyan")
    isl_table.add_column("Best Score", width=13, justify="right")
    isl_table.add_column("Speedup", width=10, justify="right", style="bold green")
    isl_table.add_column("Wins/Evals", width=11, justify="right")
    isl_table.add_column("Strategy Focus & Active Windows", ratio=1, style="dim white")

    for isl in islands:
        iid = int(isl["island_id"])
        b_sc = f"{isl['best_score']:.6g}" if isl["best_score"] is not None else "—"
        b_sp = f"{float(isl['best_speedup'] or 1.0):.4f}x"
        isl_slug = isl["name"][:7].lower()
        matching_wins = [w for w in live_wins if f"-{isl_slug}-" in w.lower()][:3]
        win_str = f"[tmux: {', '.join(matching_wins)}] " if matching_wins else ""
        isl_table.add_row(
            str(iid),
            str(isl["name"]),
            b_sc,
            b_sp,
            f"{isl['breakthrough_count']}/{isl['eval_count']}",
            f"{win_str}{isl['strategy_focus'][:72]}",
        )

    # 4. Scrolling event stream
    log_lines = Text()
    level_styles = {
        "BREAKTHROUGH": "bold bright_green",
        "PROPOSE": "bold cyan",
        "SPAWN": "yellow",
        "DEBUG": "bold orange1",
        "HOSTED": "bold gold1",
        "GOVERNOR": "bold red",
        "NOVELTY": "dim magenta",
        "ERROR": "bold red",
    }
    for ev in reversed(events):
        ts = time.strftime("%H:%M:%S", time.localtime(float(ev["created_at"])))
        lvl = str(ev["level"])
        st = level_styles.get(lvl, "white")
        log_lines.append(f"{ts} ", style="dim")
        log_lines.append(f"[{lvl:<12}] ", style=st)
        log_lines.append(f"{ev['source']:<12} ", style="bold blue")
        log_lines.append(f"{ev['message'][:115]}\n", style="white")

    layout = Layout()
    layout.split_column(
        Layout(Panel(hdr, border_style="bright_blue", padding=(0, 1)), name="header", size=5),
        Layout(Panel(pb_table, title="Internal Tracking & Progress Bars", border_style="cyan", padding=(0, 1)), name="bars", size=6),
        Layout(Panel(isl_table, title="Multiplying Research Directions (Islands)", border_style="magenta", padding=(0, 1)), name="islands", size=max(7, len(islands) + 3)),
        Layout(Panel(log_lines, title="Live Swarm Event & Evaluation Stream", border_style="green", padding=(0, 1)), name="logs"),
    )
    return layout


async def run_with_tui(
    coord: "SwarmCoordinator",
    *,
    max_evaluations: int | None = None,
    max_duration_sec: float | None = None,
    enable_tui: bool = True,
) -> dict:
    """Run coordinator alongside the interactive Rich TUI (or structured stream when non-TTY)."""
    is_tty = enable_tui and sys.stdout.isatty()
    coord_task = asyncio.create_task(
        coord.run(
            max_evaluations=max_evaluations,
            max_duration_sec=max_duration_sec,
            start_dashboard=True,
        )
    )

    if not is_tty:
        last_event_id = 0
        last_bar_ts = 0.0
        while not coord_task.done():
            state = coord.db.export_dashboard_state()
            new_evs = [e for e in reversed(state["events"]) if int(e["event_id"]) > last_event_id]
            for ev in new_evs:
                last_event_id = max(last_event_id, int(ev["event_id"]))
                ts = time.strftime("%H:%M:%S", time.localtime(float(ev["created_at"])))
                print(f"[{ts}] [{ev['level']}] ({ev['source']}) {ev['message']}", flush=True)

            if (time.monotonic() - last_bar_ts) >= 5.0:
                last_bar_ts = time.monotonic()
                st = state["stats"]
                snap = coord.governor.sample()
                sp = float(st["best_speedup"] or 1.0)
                gp = float(st["best_goal_progress"] or 0.0) * 100.0
                print(
                    f"  [PROGRESS] best={st['best_score']} ({sp:.4f}x | goal={gp:.1f}%) | "
                    f"agents={len(coord.active_tasks)}/{coord.cfg.concurrency} | "
                    f"evals={st['evaluated_total']} (wins={st['breakthroughs']}, q={st['queued']}) | "
                    f"slice_ram={snap.slice_current_gib:.1f}GiB | ui={coord.db.get_meta('dashboard_url', '')}",
                    flush=True,
                )
            await asyncio.sleep(0.6)
        return await coord_task

    console = Console()
    fd = sys.stdin.fileno()
    old_term = None
    if sys.stdin.isatty():
        try:
            old_term = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            old_term = None

    def _poll_key() -> None:
        if old_term is None:
            return
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0.0)
        if r:
            ch = sys.stdin.read(1).lower()
            if ch == "q":
                coord.request_stop()
            elif ch == "p":
                coord.toggle_pause()

    try:
        with Live(build_tui_layout(coord, max_evaluations), console=console, refresh_per_second=4, screen=True) as live:
            while not coord_task.done():
                _poll_key()
                live.update(build_tui_layout(coord, max_evaluations))
                await asyncio.sleep(0.25)
            live.update(build_tui_layout(coord, max_evaluations))
    finally:
        if old_term is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
            except Exception:
                pass

    return await coord_task
