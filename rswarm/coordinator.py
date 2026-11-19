"""Multiplying Research Tree Coordinator Loop (`EXPLORE` -> `IMPLEMENT` -> `DEBUG`/`DEEPEN`/`CROSSOVER`)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .bootstrap import install_workspace_skills_and_wrappers
from .dashboard import DashboardServer
from .db import SwarmDB
from .governor import ResourceGovernor
from .harnesses import HarnessRunner
from .models import (
    SwarmConfig,
    is_gemini_agy_model,
    is_rate_limit_or_transient_text,
    make_tmux_window_name,
    resolve_agy_model_id,
)
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
        self.active_roles: dict[str, str] = {}
        self.active_heavy_procs: int = 0
        self.active_lite_workers: int = 0
        self.paused: bool = False
        self.stop_event = asyncio.Event()
        self.started_at: float = time.time()
        self._island_rr: int = 0
        self._slice_rr: int = 0
        self._model_cooldown_until: dict[str, float] = {}
        self._model_rl_streak: dict[str, int] = {}
        self._last_rescue_check: float = 0.0
        self._bg_polish_task: asyncio.Task[None] | None = None
        self._bg_polish_proc: asyncio.subprocess.Process | None = None
        self._cfg_path: Path = self.ws / "swarm.json"
        self._cfg_mtime: float = self._cfg_path.stat().st_mtime if self._cfg_path.is_file() else 0.0
        self._sync_config_meta(self.cfg)

    def _sync_config_meta(self, c: SwarmConfig) -> None:
        self.db.set_meta("concurrency", c.concurrency)
        self.db.set_meta("harness", c.harness)
        self.db.set_meta("researcher_model", c.researcher_model)
        self.db.set_meta("implementer_model", c.implementer_model)
        self.db.set_meta("role_routing", c.role_routing_dict())
        self.db.set_meta("leaderboard_target", c.leaderboard_target)
        self.db.set_meta("leaderboard_number_one", c.leaderboard_number_one)
        self.db.set_meta("leaderboard_submit_threshold", c.leaderboard_submit_threshold)

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
        self.runner.allow_model_override = new_cfg.allow_model_override
        self.governor.max_slice_mem_gib = new_cfg.max_slice_mem_gib
        self.governor.min_avail_mem_gib = new_cfg.min_avail_mem_gib
        self.governor.max_heavy_mux_procs = new_cfg.max_heavy_mux_procs

        self._sync_config_meta(new_cfg)
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
        is_resume = wdir.exists() and (
            int(node.get("timeout_retries") or 0) > 0
            or (wdir / ".rswarm_checkpoint.json").is_file()
            or (wdir / ".rswarm_started").is_file()
        )
        if not is_resume:
            if wdir.exists():
                shutil.rmtree(wdir)
            wdir.mkdir(parents=True, exist_ok=True)

        # Symlink CLAUDE.md, skills, shared read-only assets, and vault/solvers (zero disk/RAM bloat!).
        solvers_dir = self.ws / "vault" / "solvers"
        solvers_dir.mkdir(parents=True, exist_ok=True)
        (self.ws / "shared" / "seeds_queue").mkdir(parents=True, exist_ok=True)
        (self.ws / "shared" / "literature").mkdir(parents=True, exist_ok=True)
        (self.ws / "shared" / "cache").mkdir(parents=True, exist_ok=True)
        self._refresh_workspace_index()
        for link_item in ("CLAUDE.md", ".claude", ".agents", "shared"):
            src = self.ws / link_item
            dst = wdir / link_item
            if src.exists() and not dst.exists():
                os.symlink(src, dst)
        sv_link = wdir / "solvers_vault"
        if not sv_link.exists():
            os.symlink(solvers_dir, sv_link)

        # Seed candidate from global pool (vault/best/*) if present & better, else parent candidate!
        parent = self.db.get_node(node["parent_id"]) if node.get("parent_id") else None
        best_dir = self.ws / "vault" / "best"
        pool_files = sorted([p for p in best_dir.iterdir() if not p.name.startswith(".")]) if best_dir.is_dir() else []
        pool_cand_str = str(pool_files[0]) if len(pool_files) == 1 else ""
        explicit_cand = str(node.get("candidate_path") or "")
        parent_cand = str(parent.get("candidate_path") or "") if parent else ""
        base_cand = str(self.db.get_meta("baseline_candidate_path", "") or "")

        used_pool_seed = False
        if explicit_cand and Path(explicit_cand).exists():
            cand_src_str = explicit_cand
        elif pool_cand_str and Path(pool_cand_str).exists():
            cand_src_str = pool_cand_str
            used_pool_seed = True
        elif parent_cand and Path(parent_cand).exists():
            cand_src_str = parent_cand
        else:
            cand_src_str = base_cand

        copied_cand: Path | None = None
        if cand_src_str and Path(cand_src_str).exists():
            csrc = Path(cand_src_str)
            cand_dir = wdir / "candidate"
            cand_dir.mkdir(parents=True, exist_ok=True)
            cdst = cand_dir / csrc.name
            if not cdst.exists():
                if csrc.is_dir():
                    shutil.copytree(csrc, cdst, symlinks=True)
                else:
                    shutil.copy2(csrc, cdst)
            copied_cand = cdst

        # Record exact starting seed_score so unchanged pool copies never trigger fake breakthroughs!
        if node.get("seed_score") is None:
            if used_pool_seed and self.db.get_meta("best_score") is not None:
                seed_sc = float(self.db.get_meta("best_score"))
            elif parent and parent.get("score") is not None:
                seed_sc = float(parent["score"])
            elif self.db.get_meta("baseline_score") is not None:
                seed_sc = float(self.db.get_meta("baseline_score"))
            else:
                seed_sc = None
            if seed_sc is not None:
                self.db.update_node_runtime(nid, seed_score=seed_sc)
                node["seed_score"] = seed_sc

        # Auto-assign a high-yield target slice to IMPLEMENT / DEEPEN nodes when subproblem gap metrics exist!
        role_up = str(node.get("role") or "").upper()
        if role_up in ("IMPLEMENT", "DEEPEN") and not str(node.get("target_slice") or "").strip():
            assigned = self._next_target_slice()
            if assigned:
                self.db.update_node_runtime(nid, target_slice=assigned)
                node["target_slice"] = assigned

        (wdir / ".rswarm_started").write_text(str(time.time()))

        # If CROSSOVER has parents with solver code, expose both solver folders + secondary candidate!
        sec_id = node.get("secondary_parent_id")
        if node.get("parent_id"):
            psolv = solvers_dir / str(node["parent_id"])
            plink = wdir / "primary_solvers"
            if psolv.is_dir() and not plink.exists():
                os.symlink(psolv, plink)
        if sec_id:
            ssolv = solvers_dir / str(sec_id)
            slink = wdir / "secondary_solvers"
            if ssolv.is_dir() and not slink.exists():
                os.symlink(ssolv, slink)
            sec_node = self.db.get_node(sec_id)
            if sec_node and sec_node.get("candidate_path") and Path(sec_node["candidate_path"]).exists():
                ssrc = Path(sec_node["candidate_path"])
                sdir = wdir / "secondary_parent"
                sdir.mkdir(parents=True, exist_ok=True)
                sdst = sdir / ssrc.name
                if not sdst.exists():
                    shutil.copytree(ssrc, sdst, symlinks=True) if ssrc.is_dir() else shutil.copy2(ssrc, sdst)

        return wdir, copied_cand

    def _refresh_workspace_index(self) -> str:
        """Maintain `<workspace>/shared/WORKSPACE_INDEX.md` so agents never waste turns running ls/find/cat."""
        shared_dir = self.ws / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        solvers_root = self.ws / "vault" / "solvers"
        lit_dir = shared_dir / "literature"

        shared_files = sorted([p.name for p in shared_dir.iterdir() if not p.name.startswith(".")])[:20] if shared_dir.is_dir() else []
        solver_entries: list[str] = []
        if solvers_root.is_dir():
            for d in sorted([x for x in solvers_root.iterdir() if x.is_dir()])[-10:]:
                sub_f = sorted([f.name for f in d.rglob("*") if f.is_file()])[:8]
                solver_entries.append(f"`{d.name}/` ({', '.join(sub_f)})")
        lit_files = sorted([p.name for p in lit_dir.glob("*.md")])[:16] if lit_dir.is_dir() else []

        digest = (
            "## Zero-Recon Workspace Map (`./shared/WORKSPACE_INDEX.md`)\n"
            "- **DO NOT waste turns running `ls`, `find`, or baseline `swarm-eval` on your unmodified starting `./candidate/`!** Everything is indexed right here:\n"
            "- **Literature & Internet Search CLI**:\n"
            "  - `swarm-search \"<query>\" [--source all|papers|mathoverflow|github|wiki]` -> searches OpenAlex (178k+ papers), Semantic Scholar, Crossref, MathOverflow, GitHub, and Wikipedia in `<1.5s`.\n"
            "  - `swarm-fetch \"<arxiv_id_or_pdf_or_url>\" --save-slug \"<slug>\"` -> extracts full PDF (`pdftotext`), arXiv `.tex`, or HTML into `./shared/literature/<slug>.md`.\n"
            + (f"  - Cached Papers Ready in `./shared/literature/`: `{', '.join(lit_files)}`\n" if lit_files else "")
            + "- **Instant `<15ms` Sparse Patch Verifier & Merger**:\n"
            + "  - `swarm-check <my_patch.json> --apply` -> verifies ONLY the modified subproblems in `<my_patch.json>` in `<15ms`, reports exact per-instance wins or overlap pairs, and atomically updates `./candidate/` + `vault/best` without rewriting 38 MB JSONs!\n"
            + "- **Dynamic Paradigm Islands**: `swarm-island --name \"<slug>\" --focus \"<orthogonal mathematical paradigm>\"` (for `META` agents).\n"
            + f"- **Shared Pre-Compiled Tools & SDK in `./shared/`**: `{', '.join(shared_files)}`\n"
            + (f"- **Winning Solvers in `./solvers_vault/`**: {'; '.join(solver_entries)}\n" if solver_entries else "")
        )
        try:
            (shared_dir / "WORKSPACE_INDEX.md").write_text(digest)
        except OSError:
            pass
        return digest

    def _next_target_slice(self) -> str:
        slices = self.db.get_gap_slices()
        if not slices:
            ledger_p = self.ws / "shared" / "gap_ledger.json"
            if ledger_p.is_file():
                try:
                    ld = json.loads(ledger_p.read_text())
                    raw_s = ld.get("recommended_slices") or []
                    if isinstance(raw_s, list) and raw_s:
                        pick = raw_s[self._slice_rr % len(raw_s)]
                        self._slice_rr += 1
                        return str(pick)
                except Exception:
                    pass
            return ""
        top_k = slices[: min(6, len(slices))]
        chosen = top_k[self._slice_rr % len(top_k)]
        self._slice_rr += 1
        return f"{chosen['slice']} (reducible_gap={chosen['gap']})"

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
            "CLI skills on your PATH: `swarm-search`, `swarm-fetch`, `swarm-check`, `swarm-eval`, `swarm-propose`, `swarm-submit`, `swarm-directive`, `swarm-island`, `swarm-hosted-submit`.\n"
            f"Never modify `eval/` (it is read-only) and do NOT read `{engine_root}` engine internals. "
            "Focus 100% on rigorous mathematical & algorithmic breakthroughs."
        )

        lb1 = stats.get("leaderboard_number_one")
        lb1_line = f"- Public Leaderboard #1 Score: `{lb1}` (Top-5 auto-submit gate: `{stats.get('leaderboard_submit_threshold')}`)\n" if lb1 is not None else ""
        solvers_root = self.ws / "vault" / "solvers"
        solver_dirs = sorted([d.name for d in solvers_root.iterdir() if d.is_dir()])[-8:] if solvers_root.is_dir() else []
        solvers_hint = (
            f"- Winning Reusable Solvers in `./solvers_vault/`: `{', '.join(solver_dirs)}` (inspect & extend proven fast solvers rather than starting from scratch!)\n"
            if solver_dirs
            else ""
        )
        # Surface latest Reducible-Gap Scoreboard from best/parent node metrics if available!
        gap_line = ""
        best_nid = stats.get("best_node_id")
        ref_node = (self.db.get_node(str(best_nid)) if best_nid else None) or parent
        if ref_node and ref_node.get("diagnostics"):
            gap_line = f"- Latest Verified Diagnostics & Reducible Gap Scoreboard: `{str(ref_node['diagnostics'])[:480]}`\n"

        directives = self.db.get_strategy_directives()
        dir_block = ""
        if directives:
            dir_lines = "\n".join(f"  {idx+1}. {d}" for idx, d in enumerate(directives[:4]))
            dir_block = f"\n## Active Pinned Swarm Strategy Directives (Execute High-Yield Targets!)\n{dir_lines}\n"

        tslice = str(node.get("target_slice") or "").strip()
        slice_block = ""
        if tslice and role in ("IMPLEMENT", "DEEPEN"):
            slice_block = (
                f"\n## Assigned High-Yield Target Slice (Focus Compute Here!)\n"
                f"- Your assigned shard is: **`{tslice}`**.\n"
                f"- Concentrate 100% of your constructive + optimization budget on this specific unsolved regime/band while preserving all other subproblems in `{copied_cand if copied_cand else './candidate/'}`!\n"
            )

        ledger_block = ""
        ledger_p = self.ws / "shared" / "gap_ledger.json"
        if ledger_p.is_file():
            try:
                ld = json.loads(ledger_p.read_text())
                summary_str = str(ld.get("prompt_summary") or "")[:900]
                if summary_str:
                    ledger_block = f"\n## Live Subproblem Gap Ledger (`./shared/gap_ledger.json`)\n{summary_str}\n"
            except Exception:
                pass
        elif self.db.get_gap_slices():
            gs_str = ", ".join(f"{x['slice']}={x['gap']}" for x in self.db.get_gap_slices()[:8])
            ledger_block = f"\n## Ranked Subproblem Gap Slices\n- `{gs_str}`\n"

        obs = self.db.recent_obstructions(island_id=int(island["island_id"]), limit=5)
        tabu_block = ""
        if obs:
            t_lines = [
                f"- `{o['title'][:75]}` (score={o['score']}): {str(o.get('code_summary') or '')[:130].replace(chr(10), ' ')}"
                for o in obs
            ]
            tabu_block = (
                "\n## Exhausted / Obstructed Approaches on This Island (TABU — Do NOT Repeat!)\n"
                + "\n".join(t_lines)
                + "\n"
            )

        offload_block = ""
        if (self.ws / "eval" / "background_polish.sh").is_file() and role in ("IMPLEMENT", "DEEPEN", "CROSSOVER"):
            offload_block = (
                "\n## Zero-Token Background Compaction Offload (`./shared/seeds_queue/`)\n"
                "- Do NOT block for 10+ minutes running slow Python loops! Write fast (`<30s`) parametric/constructive generators or targeted C++20/CUDA sweeps, evaluate & submit your immediate gain, AND drop promising structural seed candidates into `./shared/seeds_queue/<node_id>_<tag>.json` so Sparky's continuous background C++/CUDA polisher compacts them further into `vault/best` with zero token wait!\n"
            )

        ws_map_block = self._refresh_workspace_index()
        seed_note = f" (pre-verified starting seed_score=`{node.get('seed_score')}` — DO NOT run `swarm-eval` before making changes!)" if node.get("seed_score") is not None else ""

        common_context = f"""## Problem Brief
{self.cfg.brief}

## Island #{island['island_id']}: `{island['name']}`
Focus: {island['strategy_focus']}

## Current Lineage & Leaderboard State
- {parent_info}
- Global Best Score: `{stats['best_score']}` (`{stats['best_speedup']:.4f}x` over baseline)
- Milestone Goal Score: `{stats['leaderboard_target']}` (`{self.cfg.direction}`)
{lb1_line}{solvers_hint}{gap_line}- Starting Candidate in your workdir: `{copied_cand if copied_cand else './candidate/'}`{seed_note}

{ws_map_block}{dir_block}{slice_block}{ledger_block}{tabu_block}{offload_block}
## IDEAL-BACKTRACKING DISCOVERY DOCTRINE (Work Backward From the Ideal!)
Do NOT make blind single-element random tweaks from the baseline! Achieve large structural leaps by working backward from the theoretical Ideal:
1. **Ideal Lower Bound & Literature Grounding**: State the mathematical Ideal, check `./shared/literature/` or run `swarm-search` / `swarm-fetch` for proven theorems/constructions, and target the highest marginal-weight **reducible gap**.
2. **Obstruction Diagnosis**: Identify the exact geometric, combinatorial, or numerical obstruction blocking current `./solvers_vault/` elites from reaching the Ideal.
3. **Universal Parametric Families + Fast `<15ms` Patch Loop**: Test small/targeted patches instantly via `swarm-check patch.json --apply`, scale winners across full regimes, then submit!

## Live Breakthrough & Obstruction Bulletin (Learn from Sibling Streams!)
{chr(10).join(bul_lines)}
"""

        if role == "META":
            telem = self.db.compile_meta_telemetry()
            active_isl_ids = [int(i["island_id"]) for i in self.db.get_islands()]
            task_prompt = f"""{common_context}
## Your Assignment (`META` — Autonomous Chief Research Strategist, Literature Scout & Island Architect)
You are triggered automatically by the swarm's self-audit loop to search academic literature, retire stagnant islands, spawn orthogonal paradigm islands, and dispatch sharded breakthroughs toward `{lb1 or stats['leaderboard_target']}`.

### Live Quantitative Swarm Telemetry
```json
{json.dumps(telem, indent=2)[:4200]}
```

### Mandatory Actions (Execute in 2-3 Fast Tool Turns):
1. **Literature & Web Search (`swarm-search` / `swarm-fetch`)**: Search academic papers, MathOverflow, and GitHub for exact constructions targeting our largest remaining gap slices (check `./shared/literature/INDEX.md` first so you build on already-cached papers, then run `swarm-search "<focused mathematical query>"` and `swarm-fetch "<pdf_or_arxiv_url>" --save-slug "<slug>"` for any high-value paper!).
2. **Dynamic Island Evolution (`swarm-island`)**: Inspect `islands` in the telemetry above. If ANY active island has `consecutive_obstructions >= 12` OR your literature search uncovers a genuinely orthogonal paradigm not represented by active islands `{active_isl_ids}`, spawn a new paradigm island (automatically archiving the most-stagnant island if at cap):
   ```bash
   swarm-island --name "<2-3-word-slug>" --focus "<precise mathematical paradigm, paper reference, and solver family>"
   ```
3. **Pin Strategy Directive (`swarm-directive`)** & **Dispatch 3-4 Sharded `IMPLEMENT` Builders (`swarm-propose`)**:
   ```bash
   swarm-directive --directive "<1-3 sentences citing exact literature constructions, target slices, and dead-end methods to stop repeating>"
   swarm-propose --island-id <active_island_id> --role IMPLEMENT \\
     --target-slice "<specific regime/band slice>" \\
     --title "<concise 4-8 word mathematical mechanism>" \\
     --hypothesis "IDEAL BOUND: <bound>. LITERATURE/THEOREM: <paper/formula in ./shared/literature/>. OBSTRUCTION: <exact bottleneck>. MECHANISM: <concrete C++/CUDA/Python algorithm>." \\
     --rationale "<quantified expected score drop on this slice>"
   ```
4. Stop immediately once `swarm-directive` and `swarm-propose` succeed.
"""
        elif role == "EXPLORE":
            task_prompt = f"""{common_context}
## Your Assignment (`EXPLORE` — Literature-Grounded Ideal-Backtracking Researcher)
CRITICAL EFFICIENCY RULE: All workspace structure, Gap Ledger, and Tabu Obstructions are already above—do NOT run `ls` or `cat` on workspace boilerplate!
1. **Search Papers & Proofs First (`swarm-search` / `swarm-fetch`)**: Run 1 focused `swarm-search "<mathematical construction query>"` (and `swarm-fetch` if a paper has an open PDF/arXiv link not yet in `./shared/literature/`) to ground your ideas in real mathematical literature rather than guessing!
2. Work backward from the Ideal bound toward Leaderboard #1 (`{lb1 or stats['leaderboard_target']}`): invent **2 to 3 distinct, literature-backed hypotheses** aligned with Island `{island['name']}` that target high-yield slices and avoid every item in the Tabu list.
3. Invoke `swarm-propose` 2 or 3 times back-to-back (including `--target-slice` and citing any `./shared/literature/` paper):
   ```bash
   swarm-propose --parent-id "{node['node_id']}" --island-id {island['island_id']} --role IMPLEMENT \\
     --target-slice "<target regime or scale band>" \\
     --title "<concise 4-8 word title>" \\
     --hypothesis "IDEAL BOUND: <bound>. LITERATURE: <paper/construction>. OBSTRUCTION: <why current elites fall short>. MECHANISM: <universal parametric construction / solver algorithm>." \\
     --rationale "<expected score drop on target slice and why it differs from Tabu items>"
   ```
4. Stop immediately after confirming `swarm-propose` succeeded.
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
## Your Assignment (`CROSSOVER` — Cross-Island Algorithmic Synthesis)
Goal: **{node['title']}**
{node['hypothesis']}

- Primary parent solver code: `./primary_solvers/` (candidate in `./candidate/`).
- Secondary island solver code: `./secondary_solvers/` (candidate in `./secondary_parent/`).

CRITICAL RULE: `vault/best` already unions per-instance JSON outputs automatically—NEVER waste time merely taking per-key min/max of JSON files! Your job is **ALGORITHMIC FUSION**:
1. Inspect `./primary_solvers/` and `./secondary_solvers/` to identify each parent's mathematical mechanism (e.g. constructive parametric family vs. active-set SLP / Jacobian optimizer).
2. Write a fused pipeline that feeds one parent's geometric ansatz/generator into the other parent's compactor across unsolved Gap Ledger instances, writing strictly improved solutions to `{copied_cand}`.
3. Verify with `swarm-eval {copied_cand}` and submit via `swarm-submit {copied_cand} --summary "<fused algorithm & instances improved>"`.
"""
        else:
            # IMPLEMENT or DEEPEN
            task_prompt = f"""{common_context}
## Your Assignment (`{role}` — Ideal-Backtracking Builder & Optimizer)
Title: **{node['title']}**
Hypothesis:
{node['hypothesis']}

Rationale:
{node['rationale']}

CRITICAL EFFICIENCY RULE: Read `./shared/WORKSPACE_INDEX.md` & `./shared/literature/` (or run `swarm-search` if you need an exact formula) and compound `./solvers_vault/` + `./shared/packing_sdk.hpp` instead of writing boilerplate from scratch!
Steps:
1. Build a fast C++20 (`g++ -O3 -fopenmp`, optionally `#include "shared/packing_sdk.hpp"`), CUDA (`nvcc -O3`), or Python solver in your working directory targeting your assigned slice, emitting ONLY your newly generated/improved subproblems into a small `patch.json`.
2. Verify & merge your patch in `<15ms` via `swarm-check patch.json --apply --candidate {copied_cand}` (checks exact 128-bit SAT clearance, prints per-instance wins or overlap pairs, and atomically merges valid wins into `{copied_cand}` + `vault/best`!).
3. Once your improvements are merged, run `swarm-submit {copied_cand} --summary "<literature/mechanism used, instances improved, and verified score>"`!
"""
        return system_prompt, task_prompt

    def _replenish_frontier_if_needed(self) -> None:
        """Keep the multiplying tree fed, ensuring IMPLEMENT builders never run dry when EXPLORE cools down."""
        stats = self.db.get_summary_stats()
        active_or_queued = stats["queued"] + stats["running"]
        with self.db._lock, self.db._connect() as conn:
            imp_supply = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM nodes WHERE role='IMPLEMENT' AND status IN ('QUEUED','RUNNING')"
                ).fetchone()["c"]
                or 0
            )
        min_buffer = max(self.cfg.num_islands, self.cfg.concurrency)
        need_total = max(0, min_buffer - active_or_queued)
        need_imp = max(0, self.cfg.concurrency - imp_supply)
        needed = max(need_total, need_imp)
        if needed <= 0:
            return

        islands = self.db.get_islands()
        if not islands:
            return

        only_builders = (active_or_queued >= min_buffer and need_imp > 0)
        for idx in range(needed):
            isl = islands[self._island_rr % len(islands)]
            self._island_rr += 1
            iid = int(isl["island_id"])
            parent = self.db.select_ucb_parent(iid)
            pid = parent["node_id"] if parent else self.db.get_meta("baseline_node_id")
            pscore = parent["score"] if parent else self.db.get_meta("baseline_score")
            pspeed = float(parent["speedup"] or 1.0) if parent else 1.0

            # Mix 1 EXPLORE with 2 IMPLEMENT normally; when queue already holds EXPLOREs, replenish IMPLEMENT directly!
            if (not only_builders) and (idx % 3 == 0):
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
                self.db.create_node(
                    island_id=iid,
                    role="IMPLEMENT",
                    title=f"Ideal-backtrack {isl['name']} full-scale solver",
                    hypothesis=(
                        f"IDEAL BOUND: Work backward from theoretical optimum across ALL problem scales. "
                        f"OBSTRUCTION: Overcome multi-body / parity bottlenecks on untouched regimes. "
                        f"MECHANISM: Apply `{isl['name']}` ({isl['strategy_focus']}) atop `{pid}` (score={pscore}, {pspeed:.4f}x), "
                        f"reusing `./solvers_vault/` and `./shared/` OpenMP/CUDA primitives across the entire instance range."
                    ),
                    rationale=f"Full-scale Ideal-Backtracking builder dispatch on island `{isl['name']}`.",
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
        # Gemini models (`gemini-*`) route automatically through Sparky's `agy` CLI (`--output-format stream-json`).
        # Anthropic subscription rows (`claude-*`) route through `claude -p` (`claude-mux`) to forward OAuth.
        if is_gemini_agy_model(model_id):
            concrete_harness = "agy"
            model_id = resolve_agy_model_id(model_id, effort_lvl)
        elif model_id.lower().startswith("claude-") and concrete_harness == "claude-mux-lite":
            concrete_harness = "claude-mux"
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
                http_timeout_sec=self.cfg.http_timeout_sec,
            )

            # Refresh node state from DB to see if agent already called `swarm-submit` or `swarm-propose`.
            refreshed = self.db.get_node(nid) or node
            retries = int(refreshed.get("timeout_retries") or 0)

            # Clear rate-limit streak on clean success.
            if res.exit_code == 0 and not res.rate_limited:
                self._model_rl_streak[model_id] = 0
                self._model_cooldown_until[model_id] = 0.0

            # ZERO-ABANDON RATE-LIMIT PROTECTION:
            # Never abandon or fail a line of reasoning due to API rate limits (429 / temporarily limiting requests / overload)!
            was_rate_limited = bool(
                res.rate_limited
                or res.exit_code == 75
                or (res.exit_code != 0 and is_rate_limit_or_transient_text(res.final_text[-2000:]))
            )
            if refreshed["status"] == "RUNNING" and was_rate_limited:
                streak = self._model_rl_streak.get(model_id, 0) + 1
                self._model_rl_streak[model_id] = streak
                cooldown = min(120.0, 25.0 * (1.45 ** min(streak - 1, 4)))
                self._model_cooldown_until[model_id] = max(
                    self._model_cooldown_until.get(model_id, 0.0),
                    time.time() + cooldown,
                )
                self.db.update_node_runtime(
                    nid,
                    status="QUEUED",
                    tmux_window="",
                    diagnostics=(
                        f"API rate-limited / busy; session & workdir preserved in queue without penalty "
                        f"(model cooldown {cooldown:.0f}s, streak={streak})."
                    ),
                )
                self.db.log_event(
                    "GOVERNOR",
                    nid,
                    f"Rate-limit on {model_id} -> preserved session & re-queued {nid} (cooldown {cooldown:.0f}s)",
                )
                return

            # If agent hit wall-clock timeout ceiling before finishing, preserve workdir + checkpoint and re-queue!
            if (
                refreshed["status"] == "RUNNING"
                and (res.timed_out or res.exit_code == 124)
                and retries < self.cfg.max_timeout_retries
            ):
                next_try = retries + 1
                self.db.update_node_runtime(
                    nid,
                    status="QUEUED",
                    tmux_window="",
                    timeout_retries=next_try,
                    diagnostics=(
                        f"Window timeout ({res.elapsed_sec:.0f}s); checkpoint & workdir preserved "
                        f"for automatic resume ({next_try}/{self.cfg.max_timeout_retries})."
                    ),
                )
                self.db.log_event(
                    "INFO",
                    nid,
                    f"Timeout after {res.elapsed_sec:.0f}s -> saved checkpoint & re-queued to resume ({next_try}/{self.cfg.max_timeout_retries})",
                )
                return

            if role in ("EXPLORE", "META"):
                self.db.update_node_runtime(
                    nid,
                    status="EVALUATED" if res.exit_code == 0 else "FAILED",
                    code_summary=res.final_text[-1200:],
                    finished=True,
                )
                self.db.log_event("DONE", nid, f"{role} finished in {res.elapsed_sec:.1f}s (exit={res.exit_code})")
            else:
                # Harvest any scratch candidate artifacts matching candidate suffix (or *.json) in wdir into the global pool!
                best_exit_cand = copied_cand
                base_size = copied_cand.stat().st_size if (copied_cand and copied_cand.exists()) else 0
                newest_mtime = copied_cand.stat().st_mtime if (copied_cand and copied_cand.exists()) else 0.0
                cand_ext = copied_cand.suffix.lower() if (copied_cand and copied_cand.is_file() and copied_cand.suffix) else ".json"
                target_exts = {cand_ext, ".json"}
                for jf in wdir.rglob("*"):
                    if jf.is_symlink() or not jf.is_file() or jf.suffix.lower() not in target_exts:
                        continue
                    rel_parts = jf.relative_to(wdir).parts
                    if any(part.startswith(".") or part in ("secondary_parent", "shared", "solvers_vault") for part in rel_parts):
                        continue
                    await self.eval_service.try_pool_merge(nid, jf)
                    mt = jf.stat().st_mtime
                    # Only promote a scratch file as primary submit artifact if it is comparable in size to full candidate!
                    if jf.suffix.lower() == cand_ext and mt > newest_mtime + 0.5 and jf.stat().st_size >= max(16, int(base_size * 0.5)):
                        newest_mtime = mt
                        best_exit_cand = jf

                # Belt-and-suspenders: if implementer exited without calling `swarm-submit`, auto-submit!
                if refreshed["status"] == "RUNNING":
                    if best_exit_cand and best_exit_cand.exists():
                        await self.eval_service.handle_submit(
                            {
                                "node_id": nid,
                                "candidate_path": str(best_exit_cand),
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
            self.active_roles.pop(nid, None)

    def _maybe_schedule_meta_audit(self, stats: dict[str, Any]) -> None:
        """Automatically schedule a META Strategic Diagnostician node periodically or upon stagnation."""
        eval_total = int(stats.get("evaluated_total") or 0)
        if eval_total < max(6, self.cfg.meta_interval_evals // 2):
            return
        with self.db._lock, self.db._connect() as conn:
            in_flight = conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE role='META' AND status IN ('QUEUED','RUNNING')"
            ).fetchone()["c"]
        if int(in_flight or 0) > 0:
            return
        last_meta = int(self.db.get_meta("last_meta_eval") or 0)
        stall = int(self.db.get_meta("evals_since_global_best") or 0)
        due_periodic = (eval_total - last_meta) >= max(8, int(self.cfg.meta_interval_evals))
        due_stall = stall >= max(5, int(self.cfg.stall_evals_for_meta)) and (eval_total - last_meta) >= 6
        if not (due_periodic or due_stall):
            return
        best_nid = stats.get("best_node_id") or self.db.get_meta("baseline_node_id")
        reason_tag = f"stagnation={stall} evals" if due_stall else f"periodic@{eval_total} evals"
        mid, ok, _ = self.db.create_node(
            island_id=0,
            role="META",
            title=f"Meta-Research Audit & Strategic Pivot ({reason_tag})",
            hypothesis="Audit subproblem gap ledger, retire obstructed dead ends, pin strategy directive, and dispatch sharded IMPLEMENT breakthroughs.",
            rationale=f"Triggered automatically ({reason_tag}) to self-optimize research loop.",
            parent_id=best_nid,
            novelty_threshold=None,
        )
        if ok:
            self.db.set_meta("last_meta_eval", eval_total)
            self.db.log_event("STRATEGY", mid, f"Scheduled autonomous META Strategic Diagnostician ({reason_tag})")

    async def _background_polisher_loop(self) -> None:
        """Supervise optional `<workspace>/eval/background_polish.sh` zero-token C++/CUDA compaction daemon."""
        script = self.ws / "eval" / "background_polish.sh"
        if not script.is_file():
            return
        out_dir = self.ws / "shared" / "polish_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (self.ws / "shared" / "seeds_queue").mkdir(parents=True, exist_ok=True)
        seen_mtimes: dict[str, float] = {}
        log_file = self.ws / "logs" / "background_polisher.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        while not self.stop_event.is_set():
            if self._bg_polish_proc is None or self._bg_polish_proc.returncode is not None:
                env = dict(os.environ)
                env["RSWARM_WORKSPACE"] = str(self.ws)
                env["OMP_NUM_THREADS"] = "4"
                env["PATH"] = f"{self.ws / 'bin'}:/usr/local/cuda/bin:{env.get('PATH', '/usr/bin:/bin')}"
                lf = log_file.open("a", encoding="utf-8")
                self._bg_polish_proc = await asyncio.create_subprocess_exec(
                    "nice",
                    "-n",
                    "19",
                    "bash",
                    str(script),
                    cwd=str(self.ws),
                    env=env,
                    stdout=lf,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                lf.close()
                self.db.log_event("INFO", "bg-polisher", f"Started zero-token background compaction daemon (pid={self._bg_polish_proc.pid})")

            # Harvest any newly written polished candidates into `vault/best`!
            for p in sorted(out_dir.glob("*")):
                if not p.is_file() or p.name.startswith("."):
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt > seen_mtimes.get(p.name, 0.0) + 0.2:
                    seen_mtimes[p.name] = mt
                    await self.eval_service.try_pool_merge("bg-polisher", p)

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                pass

    def _stop_background_polisher(self) -> None:
        proc = self._bg_polish_proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._bg_polish_proc = None

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

        # Refresh CLI wrappers (including `swarm-directive`) in `<workspace>/bin/`.
        rswarm_bin = Path(shutil.which("rswarm") or sys.argv[0]).resolve()
        if rswarm_bin.is_file():
            try:
                install_workspace_skills_and_wrappers(self.ws, rswarm_bin, self.cfg)
            except Exception:
                pass

        self.tmux_pool.ensure_session()
        reaped = self.tmux_pool.reap_orphan_windows_sync()
        if reaped:
            self.db.log_event("INFO", "coordinator", f"Reaped {reaped} orphaned tmux worker window(s) from prior run.")
        recovered = self.db.recover_interrupted_nodes()
        if recovered:
            self.db.log_event("INFO", "coordinator", f"Recovered {recovered} interrupted nodes back to QUEUED.")
        rescued = self.db.recover_rate_limited_nodes(self.ws / "logs")
        if rescued:
            self.db.log_event("INFO", "coordinator", f"Resurrected {rescued} rate-limited node(s) back to QUEUED.")
        self._last_rescue_check = time.time()

        await self.eval_service.start_rpc_server()
        if (self.ws / "eval" / "background_polish.sh").is_file():
            self._bg_polish_task = asyncio.create_task(self._background_polisher_loop())

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
                now_ts = time.time()
                if now_ts - self._last_rescue_check >= 45.0:
                    self._last_rescue_check = now_ts
                    r_cnt = self.db.recover_rate_limited_nodes(self.ws / "logs")
                    if r_cnt:
                        self.db.log_event("INFO", "coordinator", f"Auto-resurrected {r_cnt} rate-limited node(s) back to QUEUED.")

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
                    self._maybe_schedule_meta_audit(stats)
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

                        active_meta = sum(1 for r in self.active_roles.values() if r == "META")
                        active_exp = sum(1 for r in self.active_roles.values() if r == "EXPLORE")
                        active_imp = sum(1 for r in self.active_roles.values() if r == "IMPLEMENT")
                        active_deep = sum(1 for r in self.active_roles.values() if r == "DEEPEN")
                        active_cross = sum(1 for r in self.active_roles.values() if r == "CROSSOVER")
                        excl_set: set[str] = set()
                        if active_meta >= 1:
                            excl_set.add("META")
                        if active_exp >= self.cfg.max_concurrent_researchers:
                            excl_set.add("EXPLORE")
                        if self.cfg.concurrency >= 4:
                            if active_deep >= max(2, self.cfg.concurrency // 3):
                                excl_set.add("DEEPEN")
                            if active_cross >= 1:
                                excl_set.add("CROSSOVER")
                        eff_reserve = min(max(0, int(self.cfg.reserved_frontier_slots)), max(0, self.cfg.concurrency - 1)) if self.cfg.concurrency >= 4 else 0
                        if eff_reserve > 0 and active_imp >= (self.cfg.concurrency - eff_reserve):
                            excl_set.add("IMPLEMENT")

                        # Skip roles whose routed model is currently in rate-limit cooldown so unthrottled models keep slots busy!
                        now_m = time.time()
                        for r_cand in ("META", "DEBUG", "IMPLEMENT", "DEEPEN", "EXPLORE", "CROSSOVER"):
                            m_id, _ = self.cfg.model_and_effort_for_role(r_cand)
                            if now_m < self._model_cooldown_until.get(m_id, 0.0):
                                excl_set.add(r_cand)

                        next_node = self.db.claim_next_queued_node(
                            preferred_island=self._island_rr % max(1, self.cfg.num_islands),
                            exclude_roles=tuple(sorted(excl_set)),
                        )
                        if next_node is None:
                            break
                        self._island_rr += 1
                        nid = str(next_node["node_id"])
                        self.active_roles[nid] = str(next_node["role"]).upper()
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
            self._stop_background_polisher()
            if self._bg_polish_task is not None:
                self._bg_polish_task.cancel()
            await self.eval_service.stop_rpc_server()
            if started_own_dash:
                await self.dashboard.stop()
            self.tmux_pool.cleanup_all_sync(kill_session=False)

        return self.db.get_summary_stats()
