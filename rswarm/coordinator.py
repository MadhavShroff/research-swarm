"""Multiplying Research Tree Coordinator Loop (`EXPLORE` -> `IMPLEMENT` -> `DEBUG`/`DEEPEN`/`CROSSOVER`)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .dashboard import DashboardServer
from .db import SwarmDB
from .governor import ResourceGovernor
from .harnesses import HarnessRunner
from .models import SwarmConfig, make_tmux_window_name
from .rpc import EvalAndTreeService
from .tmux_pool import TmuxPool


class SwarmCoordinator:
    """Orchestrates parallel Researchers, Implementers, Debuggers, and Synthesizers."""

    def __init__(self, cfg: SwarmConfig, db: SwarmDB) -> None:
        self.cfg = cfg
        self.db = db
        self.ws = Path(cfg.workspace_dir).resolve()
        self.governor = ResourceGovernor(
            max_slice_mem_gib=cfg.max_slice_mem_gib,
            min_avail_mem_gib=cfg.min_avail_mem_gib,
            max_heavy_mux_procs=cfg.max_heavy_mux_procs,
        )
        self.tmux_pool = TmuxPool(
            session_name=f"rswarm-{cfg.name[:20]}",
            workspace_dir=self.ws,
            max_windows=cfg.max_tmux_windows,
            keep_window_sec=cfg.keep_window_sec,
        )
        self.runner = HarnessRunner(
            workspace_dir=self.ws,
            tmux_pool=self.tmux_pool,
            custom_harness_cmd=cfg.custom_harness_cmd,
            allow_model_override=cfg.allow_model_override,
        )
        self.eval_service = EvalAndTreeService(cfg, db)
        self.dashboard = DashboardServer(
            cfg=cfg,
            db=db,
            governor=self.governor,
            tmux_pool=self.tmux_pool,
        )

        self.active_tasks: dict[str, asyncio.Task[None]] = {}
        self.active_heavy_procs: int = 0
        self.active_lite_workers: int = 0
        self.paused: bool = False
        self.stop_event = asyncio.Event()
        self.started_at: float = time.time()
        self._island_rr: int = 0
        self._cfg_path: Path = self.ws / "swarm.json"
        self._cfg_mtime: float = self._cfg_path.stat().st_mtime if self._cfg_path.is_file() else 0.0

    def _reload_live_config_if_changed(self) -> None:
        """Hot-reload swarm.json live without interrupting in-flight agents."""
        if not self._cfg_path.is_file():
            return
        try:
            st = self._cfg_path.stat()
            if st.st_mtime <= self._cfg_mtime:
                return
            self._cfg_mtime = st.st_mtime
            new_cfg = SwarmConfig.from_dict(json.loads(self._cfg_path.read_text()))
        except Exception as exc:
            self.db.log_event("WARN", "config", f"Ignored invalid live swarm.json edit: {exc}")
            return

        changes: list[str] = []
        if new_cfg.concurrency != self.cfg.concurrency:
            changes.append(f"concurrency {self.cfg.concurrency}->{new_cfg.concurrency}")
        old_r = self.cfg.role_routing_dict()
        new_r = new_cfg.role_routing_dict()
        for rname, spec in new_r.items():
            if old_r.get(rname) != spec:
                changes.append(f"{rname}={spec['model']}[{spec['effort']}]")
        if new_cfg.harness != self.cfg.harness:
            changes.append(f"harness={new_cfg.harness}")

        self.cfg = new_cfg
        self.dashboard.cfg = new_cfg
        self.eval_service.cfg = new_cfg
        self.governor.max_slice_mem_gib = new_cfg.max_slice_mem_gib
        self.governor.min_avail_mem_gib = new_cfg.min_avail_mem_gib
        self.governor.max_heavy_mux_procs = new_cfg.max_heavy_mux_procs

        self.db.set_meta("concurrency", new_cfg.concurrency)
        self.db.set_meta("harness", new_cfg.harness)
        self.db.set_meta("researcher_model", new_cfg.researcher_model)
        self.db.set_meta("implementer_model", new_cfg.implementer_model)
        self.db.set_meta("role_routing", new_r)
        if changes:
            self.db.log_event("INFO", "hot-reload", "Live config applied: " + ", ".join(changes))

    def request_stop(self) -> None:
        self.stop_event.set()

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        self.db.set_meta("paused", self.paused)
        self.db.log_event("INFO", "coordinator", f"Spawning {'PAUSED' if self.paused else 'RESUMED'}")
        return self.paused

    def _prepare_agent_workdir(self, node: dict[str, Any]) -> tuple[Path, Path | None]:
        """Create a lightweight zero-copy sandbox for `node` with symlinks to shared read-only data."""
        nid = str(node["node_id"])
        wdir = self.ws / "work" / nid
        if wdir.exists():
            shutil.rmtree(wdir)
        wdir.mkdir(parents=True, exist_ok=True)

        # Symlink CLAUDE.md, skills, and shared read-only assets (zero disk/RAM bloat across 100s of agents!).
        for link_item in ("CLAUDE.md", ".claude", ".agents", "shared"):
            src = self.ws / link_item
            dst = wdir / link_item
            if src.exists() and not dst.exists():
                os.symlink(src, dst)

        # Copy mutable parent candidate so the agent can edit freely without mutating the vault.
        parent = self.db.get_node(node["parent_id"]) if node.get("parent_id") else None
        cand_src_str = (
            node.get("candidate_path")
            or (parent.get("candidate_path") if parent else "")
            or self.db.get_meta("baseline_candidate_path", "")
        )
        copied_cand: Path | None = None
        if cand_src_str and Path(cand_src_str).exists():
            csrc = Path(cand_src_str)
            cand_dir = wdir / "candidate"
            cand_dir.mkdir(parents=True, exist_ok=True)
            cdst = cand_dir / csrc.name
            if csrc.is_dir():
                shutil.copytree(csrc, cdst, symlinks=True)
            else:
                shutil.copy2(csrc, cdst)
            copied_cand = cdst

        # If CROSSOVER has a secondary parent, provide its artifact under ./secondary_parent/ for inspection!
        sec_id = node.get("secondary_parent_id")
        if sec_id:
            sec_node = self.db.get_node(sec_id)
            if sec_node and sec_node.get("candidate_path") and Path(sec_node["candidate_path"]).exists():
                ssrc = Path(sec_node["candidate_path"])
                sdir = wdir / "secondary_parent"
                sdir.mkdir(parents=True, exist_ok=True)
                sdst = sdir / ssrc.name
                shutil.copytree(ssrc, sdst, symlinks=True) if ssrc.is_dir() else shutil.copy2(ssrc, sdst)

        return wdir, copied_cand

    def _build_prompts(
        self,
        node: dict[str, Any],
        island: dict[str, Any],
        copied_cand: Path | None,
    ) -> tuple[str, str]:
        stats = self.db.get_summary_stats()
        bulletins = self.db.recent_bulletin(limit=10)
        bul_lines = [
            f"- [{b['kind']}] (node={b['node_id']}, speedup={b['speedup'] or 0:.4f}x): {b['message']}"
            for b in bulletins
        ] or ["- (No bulletin posts yet — you are on the first wave!)"]

        parent = self.db.get_node(node["parent_id"]) if node.get("parent_id") else None
        parent_info = (
            f"Parent `{parent['node_id']}` (`{parent['title']}`): score=`{parent['score']}`, speedup=`{parent['speedup']:.4f}x`"
            if parent and parent.get("score") is not None
            else f"Baseline score=`{stats['baseline_score']}` (`1.0000x`)"
        )

        role = str(node["role"]).upper()
        engine_root = Path(__file__).resolve().parent.parent
        system_prompt = (
            f"You are an autonomous {role} agent in the `{self.cfg.name}` research swarm.\n"
            f"Mission: Solve the challenge and FAR EXCEED leaderboard #1 "
            f"(direction={self.cfg.direction}, baseline={stats['baseline_score']}, "
            f"current_global_best={stats['best_score']} [{stats['best_speedup']:.4f}x], "
            f"target_to_beat={stats['leaderboard_target']}).\n"
            f"Your Node ID is `{node['node_id']}` on Research Island #{island['island_id']} (`{island['name']}`).\n"
            "CLI skills on your PATH: `swarm-eval`, `swarm-propose`, `swarm-submit`, `swarm-hosted-submit`.\n"
            f"Never modify `eval/` (it is read-only) and do NOT read `{engine_root}` engine internals. "
            "Focus 100% on rigorous mathematical & algorithmic breakthroughs."
        )

        common_context = f"""## Problem Brief
{self.cfg.brief}

## Island #{island['island_id']}: `{island['name']}`
Focus: {island['strategy_focus']}

## Current Lineage & Leaderboard State
- {parent_info}
- Global Best Score: `{stats['best_score']}` (`{stats['best_speedup']:.4f}x` over baseline)
- Target Leaderboard Score: `{stats['leaderboard_target']}` (`{self.cfg.direction}`)
- Starting Candidate in your workdir: `{copied_cand if copied_cand else './candidate/'}`

## Live Breakthrough & Obstruction Bulletin (Learn from Sibling Streams!)
{chr(10).join(bul_lines)}
"""

        if role == "EXPLORE":
            task_prompt = f"""{common_context}
## Your Assignment (`EXPLORE` — Fast 1-Turn Researcher Role)
CRITICAL EFFICIENCY RULE: All problem rules, scoring formulas, and Bulletin updates are already above. Do NOT spend turns running `ls` or `cat` on workspace files!
1. Immediately invent **2 to 3 distinct, concrete, high-upside hypotheses** aligned with Island `{island['name']}` targeting specific puzzle cohorts or structural mechanisms toward `{stats['leaderboard_target']}`.
2. In your VERY FIRST `bash` tool call, invoke `swarm-propose` 2 or 3 times back-to-back:
   ```bash
   swarm-propose --parent-id "{node['node_id']}" --island-id {island['island_id']} --role IMPLEMENT \\
     --title "<concise 4-8 word title>" \\
     --hypothesis "<exact mathematical construction, target N values, and algorithm steps to code>" \\
     --rationale "<expected score reduction and why it differs from recent Bulletin items>"
   ```
3. Stop immediately after confirming `swarm-propose` succeeded.
"""
        elif role == "DEBUG":
            task_prompt = f"""{common_context}
## Your Assignment (`DEBUG` — Repair Broken Candidate)
Hypothesis being repaired: **{node['title']}**
{node['hypothesis']}

Failure Diagnostics to Fix:
{node['rationale']}

Steps:
1. Inspect `{copied_cand}` and diagnose the exact bug, constraint violation, or timeout.
2. Fix the code/candidate cleanly while preserving the core mathematical idea.
3. Run `swarm-eval {copied_cand}` via bash until `"valid": true`!
4. Once valid, run:
   ```bash
   swarm-submit {copied_cand} --summary "Fixed bug and verified candidate: <details>"
   ```
"""
        elif role == "CROSSOVER":
            task_prompt = f"""{common_context}
## Your Assignment (`CROSSOVER` — Multi-Island Synthesis)
Goal: **{node['title']}**
{node['hypothesis']}

- Primary parent candidate is in `./candidate/`.
- Secondary island elite candidate is in `./secondary_parent/`.

Steps:
1. Inspect both `./candidate/` and `./secondary_parent/` to see what made each elite succeed.
2. Synthesize a unified solver/candidate in `{copied_cand}` that combines the strongest mechanisms of both (e.g. taking the best per-instance configuration or chaining one solver's geometry into the other's optimizer).
3. Benchmark iteratively with `swarm-eval {copied_cand}`.
4. Submit your best verified result via:
   ```bash
   swarm-submit {copied_cand} --summary "Synthesized cross-island breakthrough: <details>"
   ```
"""
        else:
            # IMPLEMENT or DEEPEN
            task_prompt = f"""{common_context}
## Your Assignment (`{role}` — Hypothesis Builder & Optimizer)
Title: **{node['title']}**
Hypothesis:
{node['hypothesis']}

Rationale:
{node['rationale']}

CRITICAL EFFICIENCY RULE: All problem contracts are already above—do NOT spend turns reading `eval/` or `shared/` source files! Go straight to writing and running your solver code.
Steps:
1. Write a fast Python or C++20 (`g++ -O3`) solver in your working directory that loads `{copied_cand}`, computes strictly valid improved configurations (`SAT gap >= 1e-9`) for your target instances while preserving all existing keys in `{copied_cand}` where your new solution is not strictly better, and writes the updated `{copied_cand}`.
2. Run `swarm-eval {copied_cand}` via `bash` to verify `"valid": true` and check your new score and speedup!
3. If `"valid": true` and score improved (or after 1 quick refinement pass), immediately submit via:
   ```bash
   swarm-submit {copied_cand} --summary "<what you built, which instances improved, and verified score>"
   ```
"""
        return system_prompt, task_prompt

    def _replenish_frontier_if_needed(self) -> None:
        """Keep the multiplying tree fed using Entropic UCB parent selection across all islands."""
        stats = self.db.get_summary_stats()
        active_or_queued = stats["queued"] + stats["running"]
        min_buffer = max(self.cfg.num_islands, self.cfg.concurrency)
        if active_or_queued >= min_buffer:
            return

        islands = self.db.get_islands()
        if not islands:
            return

        needed = min_buffer - active_or_queued
        for idx in range(needed):
            isl = islands[self._island_rr % len(islands)]
            self._island_rr += 1
            iid = int(isl["island_id"])
            parent = self.db.select_ucb_parent(iid)
            pid = parent["node_id"] if parent else self.db.get_meta("baseline_node_id")
            pscore = parent["score"] if parent else self.db.get_meta("baseline_score")
            pspeed = float(parent["speedup"] or 1.0) if parent else 1.0

            # Mix 1 EXPLORE (spawns 2-3 tailored hypotheses) with 2 direct IMPLEMENT builders
            # so builders immediately push the score while researchers expand the tree!
            if idx % 3 == 0:
                self.db.create_node(
                    island_id=iid,
                    role="EXPLORE",
                    title=f"Explore wave on {isl['name']} (from {pid})",
                    hypothesis=(
                        f"Propose 2-3 novel mathematical/algorithmic leaps along `{isl['name']}` "
                        f"starting from `{pid}` (score={pscore}, {pspeed:.4f}x)."
                    ),
                    rationale=f"Entropic UCB selected parent {pid} for frontier expansion.",
                    parent_id=pid,
                    novelty_threshold=None,
                )
            else:
                cohort_hint = f"cohort-{self._island_rr % 12 + 1}"
                self.db.create_node(
                    island_id=iid,
                    role="IMPLEMENT",
                    title=f"Build {isl['name']} solver ({cohort_hint})",
                    hypothesis=(
                        f"Apply `{isl['name']}` ({isl['strategy_focus']}) to improve high-gap instances "
                        f"in `{cohort_hint}` atop parent `{pid}` (score={pscore}, {pspeed:.4f}x)."
                    ),
                    rationale=f"Direct builder dispatch on island `{isl['name']}`.",
                    parent_id=pid,
                    novelty_threshold=None,
                )

    async def _run_node_worker(
        self,
        node: dict[str, Any],
        concrete_harness: str,
    ) -> None:
        nid = str(node["node_id"])
        role = str(node["role"]).upper()
        islands = {int(i["island_id"]): i for i in self.db.get_islands()}
        island = islands.get(int(node["island_id"]), {"island_id": 0, "name": "main", "strategy_focus": ""})
        model_id, effort_lvl = self.cfg.model_and_effort_for_role(role)
        win_name = make_tmux_window_name(role, str(island["name"]), str(node["title"]), nid)

        if concrete_harness == "claude-mux-lite":
            self.active_lite_workers += 1
        else:
            self.active_heavy_procs += 1

        try:
            wdir, copied_cand = self._prepare_agent_workdir(node)
            sys_prompt, task_prompt = self._build_prompts(node, island, copied_cand)

            self.db.update_node_runtime(
                nid,
                tmux_window=win_name,
                harness_used=concrete_harness,
                model_used=model_id,
                effort_used=effort_lvl,
            )
            self.db.log_event(
                "SPAWN",
                nid,
                f"[{role}] win={win_name} model={model_id}[{effort_lvl}] :: {node['title'][:65]}",
            )

            res = await self.runner.run_agent(
                harness_mode=concrete_harness,
                model_id=model_id,
                effort=effort_lvl,
                node_id=nid,
                window_name=win_name,
                workdir=wdir,
                system_prompt=sys_prompt,
                task_prompt=task_prompt,
                timeout_sec=self.cfg.agent_timeout_sec,
            )

            # Refresh node state from DB to see if agent already called `swarm-submit` or `swarm-propose`.
            refreshed = self.db.get_node(nid) or node
            if role == "EXPLORE":
                self.db.update_node_runtime(
                    nid,
                    status="EVALUATED" if res.exit_code == 0 else "FAILED",
                    code_summary=res.final_text[-1200:],
                    finished=True,
                )
                self.db.log_event("DONE", nid, f"Explorer finished in {res.elapsed_sec:.1f}s (exit={res.exit_code})")
            else:
                # Belt-and-suspenders: if implementer edited `copied_cand` without calling `swarm-submit`, auto-submit!
                if refreshed["status"] == "RUNNING":
                    if copied_cand and copied_cand.exists():
                        await self.eval_service.handle_submit(
                            {
                                "node_id": nid,
                                "candidate_path": str(copied_cand),
                                "summary": f"(Auto-harvested on agent exit={res.exit_code}) {res.final_text[-500:]}",
                                "request_hosted": False,
                            }
                        )
                    else:
                        self.db.update_node_runtime(
                            nid,
                            status="FAILED",
                            diagnostics=f"Agent exited (rc={res.exit_code}, timeout={res.timed_out}) without submitting candidate.",
                            finished=True,
                        )
        except asyncio.CancelledError:
            cur = self.db.get_node(nid)
            if cur and cur.get("status") == "RUNNING":
                self.db.update_node_runtime(nid, status="QUEUED", tmux_window="")
            raise
        except Exception as exc:
            self.db.update_node_runtime(
                nid,
                status="FAILED",
                diagnostics=f"Worker exception: {exc}",
                finished=True,
            )
            self.db.log_event("ERROR", nid, f"Worker error: {exc}")
        finally:
            if concrete_harness == "claude-mux-lite":
                self.active_lite_workers = max(0, self.active_lite_workers - 1)
            else:
                self.active_heavy_procs = max(0, self.active_heavy_procs - 1)
            self.active_tasks.pop(nid, None)

    async def run(
        self,
        *,
        max_evaluations: int | None = None,
        max_duration_sec: float | None = None,
        start_dashboard: bool = True,
    ) -> dict[str, Any]:
        """Main long-running coordinator loop."""
        self.started_at = time.time()
        self.db.set_meta("started_at", self.started_at)
        self.db.set_meta("concurrency", self.cfg.concurrency)
        self.db.set_meta("role_routing", self.cfg.role_routing_dict())
        recovered = self.db.recover_interrupted_nodes()
        if recovered:
            self.db.log_event("INFO", "coordinator", f"Recovered {recovered} interrupted nodes back to QUEUED.")
        self.tmux_pool.ensure_session()
        await self.eval_service.start_rpc_server()

        dash_url = ""
        started_own_dash = False
        if start_dashboard:
            reg_path = Path.home() / ".tangram-dev" / "rswarm-dashboard.json"
            reused = False
            if reg_path.is_file():
                try:
                    reg = json.loads(reg_path.read_text())
                    os.kill(int(reg["pid"]), 0)
                    if Path(reg.get("workspace", "")).resolve() == self.ws:
                        dash_url = str(reg.get("tailscale_url") or reg.get("url"))
                        reused = True
                except Exception:
                    reused = False
            if not reused:
                dash_url = await self.dashboard.start()
                started_own_dash = True
            self.db.set_meta("dashboard_url", dash_url)
            self.db.log_event("INFO", "dashboard", f"Live Radial Research Diary serving at {dash_url}")

        try:
            while not self.stop_event.is_set():
                self._reload_live_config_if_changed()
                stats = self.db.get_summary_stats()
                if max_evaluations is not None and stats["evaluated_total"] >= max_evaluations:
                    self.db.log_event(
                        "INFO",
                        "coordinator",
                        f"Reached max_evaluations={max_evaluations}. Stopping new spawns.",
                    )
                    break
                if max_duration_sec is not None and (time.time() - self.started_at) >= max_duration_sec:
                    self.db.log_event(
                        "INFO",
                        "coordinator",
                        f"Reached max_duration_sec={max_duration_sec}s. Stopping new spawns.",
                    )
                    break

                if not self.paused:
                    self._replenish_frontier_if_needed()

                    while len(self.active_tasks) < self.cfg.concurrency and not self.stop_event.is_set():
                        mode, reason, _snap = self.governor.admit(
                            self.cfg.harness,
                            self.active_heavy_procs,
                            self.active_lite_workers,
                        )
                        if mode == "WAIT":
                            self.db.log_event("GOVERNOR", "throttle", f"Holding spawn: {reason}")
                            break

                        next_node = self.db.claim_next_queued_node(
                            preferred_island=self._island_rr % max(1, self.cfg.num_islands)
                        )
                        if next_node is None:
                            break
                        self._island_rr += 1
                        nid = str(next_node["node_id"])
                        t = asyncio.create_task(self._run_node_worker(next_node, mode))
                        self.active_tasks[nid] = t

                await asyncio.sleep(0.5)

            # Drain or cancel in-flight tasks cleanly on exit.
            if self.active_tasks:
                self.db.log_event("INFO", "coordinator", f"Waiting up to 8s for {len(self.active_tasks)} active agents...")
                done, pending = await asyncio.wait(list(self.active_tasks.values()), timeout=8.0)
                for p in pending:
                    p.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await self.eval_service.stop_rpc_server()
            if started_own_dash:
                await self.dashboard.stop()
            self.tmux_pool.cleanup_all_sync(kill_session=False)

        return self.db.get_summary_stats()
