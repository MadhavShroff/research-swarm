"""Phase 0 Workspace Bootstrapper, English-Brief Harness Builder, and Skill Installer."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .db import SwarmDB
from .harnesses import HarnessRunner
from .models import SwarmConfig
from .rpc import EvalAndTreeService
from .tmux_pool import TmuxPool

DEFAULT_ISLANDS: list[dict[str, str]] = [
    {
        "name": "ideal-parametric",
        "strategy_focus": "Work backward from the theoretical Ideal bound to derive universal closed-form/parametric families spanning ALL problem scales at once.",
    },
    {
        "name": "upward-flow",
        "strategy_focus": "Initialize inside the infeasible Ideal bound and relax constraint violations upward via O(n) spatial-hash FIRE, L-BFGS, or analytical Jacobians.",
    },
    {
        "name": "gpu-parallel",
        "strategy_focus": "Batched CUDA/OpenMP multi-replica sweeps across entire instance cohorts simultaneously to overcome coordinated multi-body barriers.",
    },
    {
        "name": "combinatorial-dp",
        "strategy_focus": "Exact hierarchical decomposition, strip/staircase/guillotine recurrences, branch-and-bound, and symmetry-breaking pruning across full scale.",
    },
    {
        "name": "active-compaction",
        "strategy_focus": "Cross-scale warm-start transfer, sequential LP / active-set contact compaction, and multi-solver ensemble synthesis.",
    },
]


def _set_eval_writable(eval_dir: Path, writable: bool) -> None:
    if not eval_dir.exists():
        return
    if writable:
        os.chmod(eval_dir, 0o755)
        for p in eval_dir.rglob("*"):
            if p.is_symlink():
                continue
            mode = p.stat().st_mode
            os.chmod(p, mode | stat.S_IWUSR)
    else:
        for p in eval_dir.rglob("*"):
            if p.is_symlink():
                continue
            mode = p.stat().st_mode
            os.chmod(p, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        os.chmod(eval_dir, 0o555)


def install_workspace_skills_and_wrappers(ws: Path, rswarm_bin: Path, cfg: SwarmConfig) -> None:
    """Install `swarm-eval`, `swarm-propose`, `swarm-submit`, `swarm-hosted-submit` + SKILL.md files."""
    bin_dir = ws / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    wrappers = {
        "swarm-eval": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent eval --workspace "{ws}" "$@"
""",
        "swarm-propose": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent propose --workspace "{ws}" "$@"
""",
        "swarm-submit": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent submit --workspace "{ws}" "$@"
""",
        "swarm-hosted-submit": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent hosted-submit --workspace "{ws}" "$@"
""",
        "swarm-directive": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent directive --workspace "{ws}" "$@"
""",
        "swarm-island": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent island --workspace "{ws}" "$@"
""",
        "swarm-search": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent search --workspace "{ws}" "$@"
""",
        "swarm-fetch": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent fetch --workspace "{ws}" "$@"
""",
        "swarm-check": f"""#!/usr/bin/env bash
set -euo pipefail
exec {rswarm_bin} agent check --workspace "{ws}" "$@"
""",
    }
    for name, body in wrappers.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)

    skills_spec = {
        "swarm-eval": f"""---
name: swarm-eval
description: Run local hardware verification and benchmarking on a candidate file or directory through the rswarm coordinator.
---

# swarm-eval

Run deterministic local evaluation and benchmarking on Sparky hardware:

```bash
swarm-eval <candidate_path>
```

- Sends `<candidate_path>` to the coordinator's bounded Eval Semaphore (`eval/local_eval.sh`).
- Prints JSON containing `valid`, `score`, `speedup_vs_baseline`, `leaderboard_target`, `gap_to_target`, `eval_ms`, and `diagnostics`.
- Goal direction: **{cfg.direction}** (`leaderboard_target = {cfg.leaderboard_target}`).
- Never edit `eval/` directly; `eval/` is read-only.
""",
        "swarm-propose": """---
name: swarm-propose
description: Propose a genuinely novel frontier hypothesis (beyond online/known methods) to branch and multiply the research tree.
---

# swarm-propose

Queue a novel first-principles hypothesis for implementation in the multiplying research tree (order reasoning -> novel insight -> mechanism -> title):

```bash
swarm-propose \\
  --rationale "First-principles diagnosis of why online/known methods plateau on this slice" \\
  --novel-insight "Hidden assumption broken + original mathematical structure, invariant, or operator invented" \\
  --hypothesis "Precise falsifiable mathematical construction and solver algorithm to build" \\
  --target-slice "Specific unsolved regime or scale band" \\
  --title "Short descriptive novel mechanism title" \\
  --role IMPLEMENT
```

- Known/online methods cannot advance the frontier—every proposal must articulate a genuine `--novel-insight`.
- Automatically passes through the pre-execution Novelty Gate and prioritizes high-novelty researcher leaps ahead of fallback builders.
""",
        "swarm-submit": """---
name: swarm-submit
description: Submit a verified candidate implementation to update the Vault, broadcast a Breakthrough, multiply child branches, and optionally submit to the hosted leaderboard.
---

# swarm-submit

When your candidate passes `swarm-eval` (or when you finish an implementation attempt), submit it:

```bash
swarm-submit <candidate_path> --summary "What you built, key math/code changes, and measured score" [--request-hosted]
```

- Runs canonical isolated verification.
- If it improves on your parent or Island Elite, promotes the artifact to `vault/`, broadcasts a `[BREAKTHROUGH]` to all sibling agents, and automatically multiplies child `DEEPEN`, `EXPLORE`, and `CROSSOVER` branches!
- Pass `--request-hosted` only when you set a new global best and want an official hosted competition check.
""",
        "swarm-hosted": """---
name: swarm-hosted
description: Trigger an official competition provider leaderboard submission guarded by daily quota limits.
---

# swarm-hosted-submit

Submit a top verified candidate to the external competition host (e.g. Kaggle leaderboard / remote hardware):

```bash
swarm-hosted-submit <candidate_path> --note "Brief description of breakthrough"
```

- Guarded by the workspace daily submission quota in SQLite.
- Always run `swarm-eval` locally first and verify `valid: true` before requesting hosted testing.
""",
    }

    for skill_root in (ws / ".claude" / "skills", ws / ".agents" / "skills"):
        for sname, smd in skills_spec.items():
            sdir = skill_root / sname
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "SKILL.md").write_text(smd)

    charter = f"""# Autonomous Research Swarm Charter (`{cfg.name}`)

## Mission & Frontier Novelty Axiom
Advance the mathematical and algorithmic frontier **far beyond Leaderboard #1**.
- Optimization direction: **`{cfg.direction}`**
- Milestone / Leaderboard Target: **`{cfg.leaderboard_target}`**
- **Frontier Novelty & Cross-Disciplinary Axiom**: Online papers, survey tables, public notebooks, and narrow in-domain heuristics are *only the competitive floor*—they CANNOT evolve the frontier. Actively dive into disparate fields of science, mathematics, physics (condensed matter, liquid crystals, parallel tempering, event-driven MD), biology (morphogenesis, epithelial T1 swaps, protein-folding metadynamics), crystallography (quasicrystals, twinning), topology, optimal transport, and global research traditions via `swarm-search` to import original mathematical structures and operators. Never treat an empirical public record as optimal without a formal lower-bound proof.

## Problem Brief
{cfg.brief}

## Installed Swarm Tools (on `PATH`)
1. **`swarm-check <patch.json> --apply`** — Fast sparse patch verification and atomic pool merge (`<15ms` when supported).
2. **`swarm-eval <candidate_path>`** — Full canonical verification and benchmarking via the coordinator.
3. **`swarm-propose --rationale "..." --novel-insight "..." --hypothesis "..." --target-slice "..." --title "..."`** — Queue novel frontier leaps (prioritized ahead of fallback builders).
4. **`swarm-search "<query>"` / `swarm-fetch "<target>"`** — Map prior art and extract full PDFs/arXiv `.tex` into `./shared/literature/`.
5. **`swarm-submit <candidate_path> --summary "..." [--request-hosted]`** — Submit verified breakthroughs for Vault promotion, Bulletin broadcast, and tree expansion.
6. **`swarm-hosted-submit <candidate_path> --note "..."`** — Request hosted leaderboard evaluation (quota-guarded).

## First-Principles Discovery & Compounding Rules
- Work strictly inside your assigned working directory (`./candidate/` or local files).
- Reuse fast low-level C++20/OpenMP/CUDA primitives in `./solvers_vault/` and `./shared/` so 100% of your effort goes into **novel mathematical structures and operators** rather than boilerplate.
- Work **backward from the theoretical Ideal bound**: break the symmetry, pitch, or relaxation assumptions of known methods across unsolved gap slices.
- `eval/` is read-only. Never attempt to modify or bypass `eval/local_eval.sh` or `eval/merge_pool.sh`.
- Always verify with `swarm-eval` and submit via `swarm-submit`.
"""
    (ws / "CLAUDE.md").write_text(charter)
    (ws / "AGENTS.md").write_text(charter)


def find_primary_candidate(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    files = sorted([p for p in directory.iterdir() if not p.name.startswith(".")])
    if not files:
        return None
    # If there is a single file or standard entry name, return it; otherwise pass the directory itself if multi-file.
    preferred = ("candidate", "submission.csv", "solution.py", "solver.py", "kernel.py", "main.cpp")
    for pref in preferred:
        if (directory / pref).exists():
            return directory / pref
    return files[0] if len(files) == 1 else directory


async def bootstrap_workspace(
    cfg: SwarmConfig,
    db: SwarmDB,
    rswarm_bin: Path,
    *,
    local_eval_src: str | None = None,
    hosted_eval_src: str | None = None,
    merge_pool_src: str | None = None,
    baseline_src: str | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Create workspace layout, install skills, synthesize or plug in test harnesses, and lock baseline."""
    ws = Path(cfg.workspace_dir).resolve()
    for sub in ("eval", "vault/baseline", "vault/best", "vault/solvers", "shared", "work", "logs", "state", "bin"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    (ws / "swarm.json").write_text(json.dumps(cfg.to_dict(), indent=2))
    (ws / "brief.md").write_text(cfg.brief + "\n")

    install_workspace_skills_and_wrappers(ws, rswarm_bin, cfg)

    db.set_meta("name", cfg.name)
    db.set_meta("direction", cfg.direction)
    db.set_meta("leaderboard_target", cfg.leaderboard_target)
    db.set_meta("leaderboard_number_one", cfg.leaderboard_number_one)
    db.set_meta("leaderboard_submit_threshold", cfg.leaderboard_submit_threshold)
    db.set_meta("harness", cfg.harness)
    db.set_meta("researcher_model", cfg.researcher_model)
    db.set_meta("implementer_model", cfg.implementer_model)
    db.set_meta("role_routing", cfg.role_routing_dict())
    db.set_meta("concurrency", cfg.concurrency)

    eval_dir = ws / "eval"
    local_eval_sh = eval_dir / "local_eval.sh"
    hosted_submit_sh = eval_dir / "hosted_submit.sh"
    merge_pool_sh = eval_dir / "merge_pool.sh"
    baseline_dir = ws / "vault" / "baseline"

    already_locked = (
        not force_rebuild
        and local_eval_sh.is_file()
        and db.get_meta("baseline_score") is not None
        and db.get_meta("baseline_node_id") is not None
    )
    if already_locked:
        return {
            "bootstrapped": False,
            "reused_existing": True,
            "baseline_score": db.get_meta("baseline_score"),
            "baseline_node_id": db.get_meta("baseline_node_id"),
        }

    _set_eval_writable(eval_dir, True)

    if local_eval_src:
        shutil.copy2(Path(local_eval_src).resolve(), local_eval_sh)
        local_eval_sh.chmod(0o755)

    if hosted_eval_src:
        shutil.copy2(Path(hosted_eval_src).resolve(), hosted_submit_sh)
        hosted_submit_sh.chmod(0o755)

    if merge_pool_src:
        shutil.copy2(Path(merge_pool_src).resolve(), merge_pool_sh)
        merge_pool_sh.chmod(0o755)

    if baseline_src:
        bsrc = Path(baseline_src).resolve()
        if baseline_dir.exists():
            shutil.rmtree(baseline_dir)
        baseline_dir.mkdir(parents=True, exist_ok=True)
        if bsrc.is_dir():
            for item in bsrc.iterdir():
                dst = baseline_dir / item.name
                shutil.copytree(item, dst) if item.is_dir() else shutil.copy2(item, dst)
        else:
            shutil.copy2(bsrc, baseline_dir / bsrc.name)

    if not hosted_submit_sh.is_file():
        hosted_submit_sh.write_text(
            """#!/usr/bin/env bash
# Default quota-guarded hosted competition submission adapter.
set -euo pipefail
CANDIDATE="${1:?candidate path required}"
NOTE="${2:-rswarm automated submission}"
KAGGLE_CMD=()
if command -v kaggle >/dev/null 2>&1; then
  KAGGLE_CMD=(kaggle)
elif [ -x "$HOME/.local/bin/uvx" ]; then
  KAGGLE_CMD=("$HOME/.local/bin/uvx" --from kaggle --with six kaggle)
fi
if [ -n "${RSWARM_KAGGLE_COMPETITION:-}" ] && [ "${#KAGGLE_CMD[@]}" -gt 0 ] && [ -f "$HOME/.kaggle/kaggle.json" ]; then
  OUT="$("${KAGGLE_CMD[@]}" competitions submit -c "$RSWARM_KAGGLE_COMPETITION" -f "$CANDIDATE" -m "$NOTE" 2>&1)"
  python3 -c 'import json,sys; print(json.dumps({"submitted": True, "hosted_score": None, "message": sys.argv[1]}))' "$OUT"
else
  python3 -c 'import json,sys; print(json.dumps({"submitted": False, "hosted_score": None, "message": "Hosted credentials not configured yet; candidate staged at " + sys.argv[1]}))' "$CANDIDATE"
fi
"""
        )
        hosted_submit_sh.chmod(0o755)

    # If local_eval.sh or baseline candidate is missing, synthesize them from the English brief via coding harness!
    needs_synth = (not local_eval_sh.is_file()) or (find_primary_candidate(baseline_dir) is None)
    tmux_pool = TmuxPool(f"rswarm-{cfg.name[:20]}", ws, max_windows=cfg.max_tmux_windows, keep_window_sec=cfg.keep_window_sec)
    runner = HarnessRunner(
        workspace_dir=ws,
        tmux_pool=tmux_pool,
        custom_harness_cmd=cfg.custom_harness_cmd,
        allow_model_override=cfg.allow_model_override,
    )
    svc = EvalAndTreeService(cfg, db)

    concrete_harness = "claude-mux-lite" if cfg.harness in ("claude-mux-lite", "claude-mux-scale") else cfg.harness

    last_diag = ""
    for attempt in range(1, 4):
        if needs_synth or attempt > 1:
            db.log_event("BOOTSTRAP", "phase0", f"Synthesizing test harness & baseline from English brief (attempt {attempt}/3)...")
            engine_root = Path(__file__).resolve().parent.parent
            sys_p = (
                "You are the Phase-0 Test-Harness Architect for `rswarm`. "
                "Build deterministic, self-contained evaluation scripts and a clean baseline candidate inside the workspace. "
                f"Do NOT inspect `{engine_root}` source files—every contract you need is stated below. "
                "Write the deliverable files immediately and test them."
            )
            repair_block = (
                f"\n\n## PREVIOUS ATTEMPT FAILED VALIDATION — FIX THIS:\n{last_diag}\n"
                if last_diag
                else ""
            )
            task_p = f"""Read the user's English problem brief below and create the plug-in test harness in `{ws}`:

## English Brief
{cfg.brief}

## Required Deliverables (create/overwrite these exact files)
1. `{ws}/eval/local_eval.sh`:
   - Executable bash script (`chmod +x`) taking `$1` = `<candidate_path>`.
   - Deterministically verifies ALL problem constraints with strict numerical rigor AND benchmarks score on local hardware.
   - Must compute **Reducible Gap vs. Theoretical Ideal Lower/Upper Bound** per subproblem/band, masking any provably rigid optima (`reducible_gap = 0`) so diagnostics never bait agents onto un-improvable instances!
   - Must exit `0` and print ONE single-line JSON object at the very end of stdout with this exact schema:
     `{{"valid": true, "score": <float>, "higher_is_better": {"false" if cfg.direction == "minimize" else "true"}, "eval_ms": <float>, "diagnostics": "concise summary including Reducible Gap by Band/Regime", "metrics": {{"reducible_by_regime": {{}}, "reducible_by_band": {{}}}}}}`
   - If `$1` violates constraints or crashes, print `{{"valid": false, "score": null, "diagnostics": "exact reason"}}` and exit `0` (or non-zero).
   - Fast execution (< 15 seconds). Compile any C++20/OpenMP/CUDA helper inside `{ws}/shared/` if beneficial.
2. `{ws}/eval/merge_pool.sh`:
   - Executable bash script (`chmod +x`) taking `$1` = `<candidate_path>` and `$2` = `<best_dir>` (`{ws}/vault/best`).
   - If the problem decomposes across multiple sub-instances/puzzles/tasks, independently verifies each improved sub-instance in `$1` and merges strictly better valid sub-instances into `$2/<candidate_name>`, then outputs JSON `{{"valid": true, "merged_count": <int>, "score": <float_or_null>, "best_path": "<path>", "diagnostics": "..."}}`! (If monolithic single-artifact, promotes `$1` into `$2/` iff verified score strictly improves upon `$2/`.)
3. `{ws}/eval/hosted_submit.sh`:
   - Executable bash script (`chmod +x`) taking `$1` = `<candidate_path>` and `$2` = `<note>`.
   - Submits `$1` to the competition provider's hosted leaderboard if CLI/API credentials exist on the machine, or stages `$1` into `{ws}/vault/hosted_outbox/` and prints JSON:
     `{{"submitted": bool, "hosted_score": float_or_null, "message": "..."}}`.
4. `{ws}/vault/baseline/`:
   - Put a clean, working, valid baseline candidate file inside `{ws}/vault/baseline/` so `{ws}/eval/local_eval.sh <baseline_candidate>` returns `"valid": true` and establishes the `1.00x` baseline score!
5. `{ws}/eval/islands.json`:
   - JSON array of `{cfg.num_islands}` diverse Ideal-Backtracking research directions: `[{{"name": "short-slug", "strategy_focus": "1-2 sentences working backward from Ideal across full scale"}}]` tailored specifically to solving and beating SOTA on this problem.

Test `{ws}/eval/local_eval.sh` on your `{ws}/vault/baseline/` candidate using bash before finishing!{repair_block}
"""
            await runner.run_agent(
                harness_mode=concrete_harness,
                model_id=cfg.implementer_model,
                effort=cfg.effort,
                node_id=f"phase0_try{attempt}",
                window_name=f"P0-harness-try{attempt}",
                workdir=ws,
                system_prompt=sys_p,
                task_prompt=task_p,
                timeout_sec=cfg.agent_timeout_sec,
                allow_eval_write=True,
            )

        if local_eval_sh.is_file():
            local_eval_sh.chmod(0o755)
        if hosted_submit_sh.is_file():
            hosted_submit_sh.chmod(0o755)
        if merge_pool_sh.is_file():
            merge_pool_sh.chmod(0o755)

        cand = find_primary_candidate(baseline_dir)
        if cand is None:
            last_diag = f"No baseline candidate found inside {baseline_dir}"
            continue

        check = await svc.execute_raw_local_eval(cand)
        if check["valid"] and check["score"] is not None:
            # Success! Lock eval/ read-only so no swarm agent can ever tamper with scoring!
            _set_eval_writable(eval_dir, False)

            best_cand_dst = ws / "vault" / "best" / cand.name
            if best_cand_dst.exists():
                shutil.rmtree(best_cand_dst) if best_cand_dst.is_dir() else best_cand_dst.unlink()
            shutil.copytree(cand, best_cand_dst) if cand.is_dir() else shutil.copy2(cand, best_cand_dst)

            islands_file = eval_dir / "islands.json"
            island_specs = DEFAULT_ISLANDS[: cfg.num_islands]
            if islands_file.is_file():
                try:
                    loaded = json.loads(islands_file.read_text())
                    if isinstance(loaded, list) and len(loaded) >= 2:
                        island_specs = [
                            {
                                "name": str(it.get("name", f"island-{i}")),
                                "strategy_focus": str(it.get("strategy_focus", "Explore novel approaches.")),
                            }
                            for i, it in enumerate(loaded[: cfg.num_islands])
                        ]
                except Exception:
                    pass
            db.seed_islands(island_specs)

            bscore = float(check["score"])
            db.set_meta("baseline_score", bscore)
            db.set_meta("best_score", bscore)
            db.set_meta("best_speedup", 1.0)
            db.set_meta("best_goal_progress", 0.0)
            db.set_meta("baseline_candidate_path", str(cand))

            root_id, _, _ = db.create_node(
                island_id=0,
                role="BASELINE",
                title="Baseline Origin (1.00x)",
                hypothesis="Initial verified baseline candidate.",
                rationale="Phase 0 verified reference point.",
                status="BREAKTHROUGH",
                candidate_path=str(cand),
            )
            db.record_evaluation(
                root_id,
                valid=True,
                score=bscore,
                eval_ms=check["eval_ms"],
                diagnostics=check["diagnostics"],
                metrics=check["metrics"],
                candidate_path=str(cand),
                code_summary="Verified Phase-0 Baseline.",
                finalize=True,
            )
            db.set_meta("baseline_node_id", root_id)
            db.set_meta("best_node_id", root_id)

            db.add_diary(
                "BASELINE",
                f"Baseline locked at score={bscore} (1.0000x)",
                f"Candidate: {cand}\nEval ms: {check['eval_ms']}\nDiagnostics: {check['diagnostics']}",
                node_id=root_id,
                island_id=0,
                score=bscore,
                speedup=1.0,
            )
            db.post_bulletin(
                "BREAKTHROUGH",
                f"[ORIGIN] Baseline locked at score={bscore} (1.00x). Target={cfg.leaderboard_target} ({cfg.direction}).",
                node_id=root_id,
                island_id=0,
                score=bscore,
                speedup=1.0,
            )

            # Seed initial wave across all Research Islands!
            for isl in db.get_islands():
                iid = int(isl["island_id"])
                db.create_node(
                    island_id=iid,
                    role="EXPLORE",
                    title=f"Seed hypotheses: {isl['name']}",
                    hypothesis=f"Generate 2-3 high-leverage hypotheses along '{isl['name']}': {isl['strategy_focus']}",
                    rationale="Initial multi-island tree expansion from baseline.",
                    parent_id=root_id,
                )

            db.log_event("BOOTSTRAP", root_id, f"Locked read-only eval/ and baseline score={bscore} (1.00x)")
            return {
                "bootstrapped": True,
                "baseline_score": bscore,
                "baseline_node_id": root_id,
                "islands": len(island_specs),
            }

        last_diag = f"Baseline validation failed: {json.dumps(check)}"

    raise RuntimeError(f"Phase 0 bootstrap failed after 3 attempts. Last diagnostics: {last_diag}")
