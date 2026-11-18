# `rswarm` — Autonomous Multiplying Research Swarm CLI & Radial Diary

`rswarm` is a standalone command-line orchestrator and radial web diary for autonomous mathematical, algorithmic, and kernel-optimization research swarms. `rswarm` takes a plain English problem brief, synthesizes a read-only local benchmark and hosted competition submission harness, installs CLI skills into every spawned coding agent, runs up to hundreds of parallel agents inside descriptive `tmux` windows, renders a live terminal TUI, and serves an interactive radial research diary.

## Hybrid Research Loop Design

`rswarm` combines proven mechanisms from modern discovery systems:
- **Immutable Evaluation Lock (`autoresearch`)**: Phase 0 generates `eval/local_eval.sh` and `eval/hosted_submit.sh`, verifies the `1.00x` baseline, and locks `eval/` read-only (`0555`).
- **Breakthrough & Obstruction Bulletin (OpenAI Math Swarm)**: Every verified improvement or failed hypothesis broadcasts to sibling streams so parallel agents build on wins and skip dead ends.
- **Multi-Island Evolution & Cross-Island Crossover (`FunSearch` / `AlphaEvolve` / `OpenEvolve`)**: Distinct research directions occupy dedicated islands and fuse independent breakthroughs through `CROSSOVER` nodes.
- **Pre-Execution Novelty Filter (`ShinkaEvolve`)**: Token + bigram Jaccard gating filters duplicate proposals before spending compute.
- **Multiplying Agentic Tree (`AIDE` / `AI Scientist-v2` / `RD-Agent`)**: Nodes branch across `EXPLORE` (Researcher), `IMPLEMENT` (Builder), `DEBUG` (bounded repair), `DEEPEN` (exploitation), and `CROSSOVER` (synthesis), selected via Entropic Max-Tail UCB (`TTT-Discover`).

## Repository Structure

```text
research-swarm/
├── bin/
│   └── rswarm              # CLI executable entrypoint
├── rswarm/
│   ├── cli.py              # CLI commands: init, start, run, dashboard, status, agent
│   ├── models.py           # Config schema, speedup/progress math, model guards
│   ├── bootstrap.py        # Phase-0 English-brief harness synthesizer & skill installer
│   ├── coordinator.py      # Multiplying tree scheduler & zero-copy agent sandboxes
│   ├── harnesses.py        # Pluggable coding harnesses (claude-mux, mux-lite, custom)
│   ├── rpc.py              # Unix socket server + Eval Semaphore + Vault + Hosted quota
│   ├── governor.py         # Linux cgroup v2 + /proc RAM & CPU admission governor
│   ├── tmux_pool.py        # Descriptive named tmux window lifecycle & PGID reaper
│   ├── tui.py              # Live Rich terminal UI with 4 progress bars & event stream
│   └── dashboard.py        # Radial outward-branching SVG/HTML5 research diary server
└── tests/
    └── test_rswarm.py      # End-to-end automated test suite
```

## Quick Start

```bash
# 1. Link `rswarm` onto your PATH:
ln -sfn "$(pwd)/bin/rswarm" ~/.local/bin/rswarm

# 2. Launch a new research problem from an English brief:
rswarm start \
  --name squares-in-squares \
  --direction minimize \
  --target 27.60 \
  --concurrency 48 \
  --islands 6 \
  --harness claude-mux-scale \
  --researcher-model anthropic-morph-dsv41flash \
  --implementer-model anthropic-morph-dsv41flash \
  --brief "Pack N unit squares inside the smallest outer square L_N for N=1..1000 with zero overlap."

# 3. Watch live named agent windows in tmux:
tmux attach -t rswarm-squares-in-squares

# 4. Run tests:
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## Auto-Installed Agent Skills

Every spawned agent receives four tools on `PATH` plus `.claude/skills/` and `.agents/skills/` definitions:
- `swarm-eval <candidate_path>` — runs isolated local verification and prints JSON score, speedup, and diagnostics.
- `swarm-propose --title "..." --hypothesis "..."` — queues a novel child hypothesis to multiply the tree.
- `swarm-submit <candidate_path> --summary "..." [--request-hosted]` — promotes winners to `vault/best/`, broadcasts a Bulletin, and spawns `DEEPEN`, `EXPLORE`, and `CROSSOVER` children.
- `swarm-hosted-submit <candidate_path> --note "..."` — submits top candidates to the competition leaderboard behind a 24-hour quota guard.
