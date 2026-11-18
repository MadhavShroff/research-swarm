"""Core configuration, score/speedup math, and model safety guards for rswarm."""

from __future__ import annotations

import dataclasses
import math
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_DEEPSEEK_MODEL = "anthropic-morph-dsv41flash"
DEFAULT_WORKSPACE_ROOT = Path.home() / "research-workspaces"
DEFAULT_TAILSCALE_BIND = os.environ.get("RSWARM_DASHBOARD_HOST", "0.0.0.0")
DEFAULT_DASHBOARD_PORT = 8988

# Standing Sparky model safety blocklist (docs/AGENT-RULES.md + founder order).
FORBIDDEN_MODEL_PREFIXES = (
    "claude-opus",
    "claude-sonnet",
    "claude-haiku",
    "claude-fable",
    "fable",
    "astra",
    "anthropic-gpt-",
    "gpt-6-astra",
)
FORBIDDEN_HARNESS_NAMES = ("agy", "antigravity")


def validate_model_choice(model_id: str, allow_override: bool = False) -> str:
    """Refuse forbidden paid/retired models unless explicitly overridden."""
    cleaned = (model_id or "").strip()
    if not cleaned:
        raise ValueError("Model ID cannot be empty.")
    low = cleaned.lower()
    if not allow_override:
        for bad in FORBIDDEN_MODEL_PREFIXES:
            if low == bad or low.startswith(bad):
                raise ValueError(
                    f"Model '{cleaned}' is blocked by the Sparky model guard. "
                    f"Use '{DEFAULT_DEEPSEEK_MODEL}' (DeepSeek V4.1 Flash) on claude-mux."
                )
    return cleaned


def validate_harness_choice(harness: str, allow_override: bool = False) -> str:
    """Validate harness selection and block agy by default per founder config."""
    cleaned = (harness or "").strip().lower()
    valid = ("claude-mux", "claude-mux-scale", "claude-mux-lite", "custom")
    if not allow_override and cleaned in FORBIDDEN_HARNESS_NAMES:
        raise ValueError(
            f"Harness '{cleaned}' is disabled by default. Use 'claude-mux' or 'claude-mux-scale'."
        )
    if cleaned not in valid:
        raise ValueError(f"Unknown harness '{cleaned}'. Expected one of: {', '.join(valid)}")
    return cleaned


def slugify(text: str, max_len: int = 28) -> str:
    """Convert arbitrary text into a clean ASCII slug suitable for directories and tmux windows."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    if not slug:
        slug = "task"
    return slug[:max_len].rstrip("-") or "task"


def make_tmux_window_name(role: str, island_name: str, title: str, node_id: str) -> str:
    """Create a descriptive tmux window name showing role, stream, topic, and short ID."""
    role_code = {
        "BASELINE": "B",
        "BOOTSTRAP": "P0",
        "EXPLORE": "R",
        "IMPLEMENT": "I",
        "DEBUG": "D",
        "DEEPEN": "A",
        "CROSSOVER": "X",
        "EVAL": "E",
    }.get((role or "").upper(), "W")
    isl = slugify(island_name, max_len=7)
    topic = slugify(title, max_len=14)
    short_id = re.sub(r"[^a-zA-Z0-9]", "", node_id)[-4:] or "0000"
    return f"{role_code}-{isl}-{topic}-{short_id}"[:32]


def is_better_score(candidate: float, reference: float, direction: str) -> bool:
    """Return True if candidate strictly improves upon reference along direction."""
    if math.isnan(candidate) or math.isinf(candidate):
        return False
    if direction == "minimize":
        return candidate < reference - 1e-12
    return candidate > reference + 1e-12


def compute_speedup_and_progress(
    score: float | None,
    baseline_score: float | None,
    target_score: float | None,
    direction: str,
) -> tuple[float, float]:
    """Compute (speedup_vs_baseline, goal_progress_ratio).

    - speedup_vs_baseline = 1.0 at baseline, > 1.0 when improved, < 1.0 when worse.
      For positive minimize metrics (e.g. runtime ms or total side sum 28.937 -> 27.200),
      speedup = baseline / score. For positive maximize metrics, speedup = score / baseline.
      If zero/negative values occur, falls back smoothly to exp(normalized_gain).
    - goal_progress_ratio = 0.0 at baseline, 1.0 at leaderboard target_score, > 1.0 when
      exceeding leaderboard target_score! If target_score is None, maps speedup smoothly.
    """
    if score is None or baseline_score is None or math.isnan(score) or math.isinf(score):
        return 0.0, 0.0

    if direction == "minimize":
        if score > 1e-12 and baseline_score > 1e-12:
            speedup = baseline_score / score
        else:
            denom = max(abs(baseline_score), 1.0)
            speedup = math.exp((baseline_score - score) / denom)
    else:
        if score > 1e-12 and baseline_score > 1e-12:
            speedup = score / baseline_score
        else:
            denom = max(abs(baseline_score), 1.0)
            speedup = math.exp((score - baseline_score) / denom)

    speedup = max(0.01, min(speedup, 1e6))

    if target_score is not None and abs(target_score - baseline_score) > 1e-12:
        if direction == "minimize":
            progress = (baseline_score - score) / (baseline_score - target_score)
        else:
            progress = (score - baseline_score) / (target_score - baseline_score)
    else:
        # Without an explicit target, 2.0x speedup maps to 1.0 reference ring.
        progress = math.log2(max(speedup, 1.0)) if speedup >= 1.0 else (speedup - 1.0)

    return round(speedup, 6), round(progress, 6)


@dataclasses.dataclass
class SwarmConfig:
    """Persistent configuration stored at <workspace>/swarm.json."""

    name: str
    workspace_dir: str
    brief: str
    direction: str = "minimize"  # "minimize" | "maximize"
    leaderboard_target: float | None = None
    harness: str = "claude-mux-scale"
    custom_harness_cmd: str = ""
    researcher_model: str = DEFAULT_DEEPSEEK_MODEL
    implementer_model: str = DEFAULT_DEEPSEEK_MODEL
    effort: str = "low"
    concurrency: int = 16
    max_heavy_mux_procs: int = 24
    eval_workers: int = 4
    num_islands: int = 5
    max_debug_depth: int = 2
    novelty_threshold: float = 0.72
    hosted_quota_per_day: int = 5
    auto_hosted_on_sota: bool = False
    agent_timeout_sec: int = 900
    eval_timeout_sec: int = 180
    max_slice_mem_gib: float = 72.0
    min_avail_mem_gib: float = 20.0
    max_tmux_windows: int = 128
    keep_window_sec: float = 1.5
    dashboard_host: str = DEFAULT_TAILSCALE_BIND
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    use_gpu_gate: bool = False
    allow_model_override: bool = False

    def validate(self) -> None:
        if self.direction not in ("minimize", "maximize"):
            raise ValueError("direction must be 'minimize' or 'maximize'")
        self.harness = validate_harness_choice(self.harness, self.allow_model_override)
        self.researcher_model = validate_model_choice(
            self.researcher_model, self.allow_model_override
        )
        self.implementer_model = validate_model_choice(
            self.implementer_model, self.allow_model_override
        )
        self.concurrency = max(1, int(self.concurrency))
        self.eval_workers = max(1, int(self.eval_workers))
        self.num_islands = max(1, int(self.num_islands))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SwarmConfig":
        fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in fields}
        cfg = cls(**filtered)
        cfg.validate()
        return cfg
