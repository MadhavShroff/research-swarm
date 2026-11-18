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
        "name": "algebraic-exact",
        "strategy_focus": "Closed-form constructions, lattice/crystallographic tilings, exact geometric invariants, and symmetry bounds.",
    },
    {
        "name": "continuous-opt",
        "strategy_focus": "Smooth penalty relaxations, L-BFGS / Adam / gradient homotopies, and analytic overlap Jacobians.",
    },
    {
        "name": "metaheuristic-sa",
        "strategy_focus": "Simulated annealing, basin hopping, Ruin-and-Recreate, adaptive move schedules, and multi-start search.",
    },
    {
        "name": "combinatorial-dp",
        "strategy_focus": "Hierarchical decomposition, strip/shelf/guillotine recurrences, branch-and-bound, and SAT/MIP pruning.",
    },
    {
        "name": "hybrid-polish",
        "strategy_focus": "Cross-N transfer, squeeze/compaction post-optimizers, boundary repair, and multi-solver ensembles.",
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
description: Propose a new research hypothesis to branch and multiply the autonomous research tree.
---

# swarm-propose

Queue a novel hypothesis for implementation in the multiplying research tree:

```bash
swarm-propose \\
  --title "Short descriptive hypothesis title" \\
  --hypothesis "Precise mathematical or algorithmic construction to build" \\
  --rationale "Why this overcomes current bottlenecks and approaches SOTA" \\
  --role IMPLEMENT
```

- Automatically passes through the pre-execution Novelty Gate so duplicate ideas are filtered.
- Accepted ideas immediately spawn parallel Implementer agents in new tmux windows.
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

## Mission
Solve the problem below and **far exceed the best leaderboard performance**.
- Optimization direction: **`{cfg.direction}`**
- Target / Leaderboard Score to Beat: **`{cfg.leaderboard_target}`**

## Problem Brief
{cfg.brief}

## Installed Swarm Tools (on `PATH`)
1. **`swarm-eval <candidate_path>`** — Test and benchmark your candidate locally via the coordinator.
2. **`swarm-propose --title "..." --hypothesis "..." --rationale "..."`** — Queue new child hypotheses to multiply the research tree.
3. **`swarm-submit <candidate_path> --summary "..." [--request-hosted]`** — Submit your best verified candidate for Vault promotion, Bulletin broadcast, and tree expansion.
4. **`swarm-hosted-submit <candidate_path> --note "..."`** — Request hosted leaderboard evaluation (quota-guarded).

## Rules
- Work strictly inside your assigned current working directory (`./candidate/` or local files).
- `eval/` is read-only. Never attempt to modify or bypass `eval/local_eval.sh`.
- Always verify with `swarm-eval` before calling `swarm-submit`.
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
    baseline_src: str | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Create workspace layout, install skills, synthesize or plug in test harnesses, and lock baseline."""
    ws = Path(cfg.workspace_dir).resolve()
    for sub in ("eval", "vault/baseline", "vault/best", "shared", "work", "logs", "state", "bin"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    (ws / "swarm.json").write_text(json.dumps(cfg.to_dict(), indent=2))
    (ws / "brief.md").write_text(cfg.brief + "\n")

    install_workspace_skills_and_wrappers(ws, rswarm_bin, cfg)

    db.set_meta("name", cfg.name)
    db.set_meta("direction", cfg.direction)
    db.set_meta("leaderboard_target", cfg.leaderboard_target)
    db.set_meta("harness", cfg.harness)
    db.set_meta("researcher_model", cfg.researcher_model)
    db.set_meta("implementer_model", cfg.implementer_model)
    db.set_meta("role_routing", cfg.role_routing_dict())
    db.set_meta("concurrency", cfg.concurrency)

    eval_dir = ws / "eval"
    local_eval_sh = eval_dir / "local_eval.sh"
    hosted_submit_sh = eval_dir / "hosted_submit.sh"
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
# Customize or configure competition credentials (e.g., ~/.kaggle/kaggle.json) for live remote scoring.
set -euo pipefail
CANDIDATE="${1:?candidate path required}"
NOTE="${2:-rswarm automated submission}"
if [ -n "${RSWARM_KAGGLE_COMPETITION:-}" ] && command -v kaggle >/dev/null 2>&1 && [ -f "$HOME/.kaggle/kaggle.json" ]; then
  OUT="$(kaggle competitions submit -c "$RSWARM_KAGGLE_COMPETITION" -f "$CANDIDATE" -m "$NOTE" 2>&1)"
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
   - Must exit `0` and print ONE single-line JSON object at the very end of stdout with this exact schema:
     `{{"valid": true, "score": <float>, "higher_is_better": {"false" if cfg.direction == "minimize" else "true"}, "eval_ms": <float>, "diagnostics": "concise summary", "metrics": {{}}}}`
   - If `$1` violates constraints or crashes, print `{{"valid": false, "score": null, "diagnostics": "exact reason"}}` and exit `0` (or non-zero).
   - Fast execution (< 15 seconds). Compile any C++ verifier helper inside `{ws}/shared/` if needed.
2. `{ws}/eval/hosted_submit.sh`:
   - Executable bash script (`chmod +x`) taking `$1` = `<candidate_path>` and `$2` = `<note>`.
   - Submits `$1` to the competition provider's hosted leaderboard if CLI/API credentials exist on the machine, or stages `$1` into `{ws}/vault/hosted_outbox/` and prints JSON:
     `{{"submitted": bool, "hosted_score": float_or_null, "message": "..."}}`.
3. `{ws}/vault/baseline/`:
   - Put a clean, working, valid baseline candidate file inside `{ws}/vault/baseline/` (for example `solution.py` or `submission.csv`) so `{ws}/eval/local_eval.sh <baseline_candidate>` returns `"valid": true` and establishes the `1.00x` baseline score!
4. `{ws}/eval/islands.json`:
   - JSON array of `{cfg.num_islands}` diverse research directions: `[{{"name": "short-slug", "strategy_focus": "1-2 sentences"}}]` tailored specifically to solving and beating SOTA on this problem.

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
