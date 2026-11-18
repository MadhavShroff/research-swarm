"""SQLite WAL Research Tree, Diary, Bulletin, Novelty Gate, and UCB Selector."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import compute_speedup_and_progress, is_better_score

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "use",
    "try", "test", "make", "improve", "better", "score", "faster", "new", "idea",
    "hypothesis", "method", "algorithm", "approach", "will", "can", "should", "over",
}


def _tokenize_idea(text: str) -> set[str]:
    words = [
        w for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower())
        if w not in _STOPWORDS
    ]
    tokens = set(words)
    # Include word bigrams so distinct structural combinations differ from bag-of-words swaps.
    for i in range(len(words) - 1):
        tokens.add(f"{words[i]}_{words[i+1]}")
    return tokens


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class SwarmDB:
    """Concurrent-safe SQLite WAL database backing the research tree, diary, and dashboard."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS islands (
                    island_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    strategy_focus TEXT NOT NULL,
                    color TEXT NOT NULL,
                    angle_start_deg REAL NOT NULL,
                    angle_end_deg REAL NOT NULL,
                    best_score REAL,
                    best_speedup REAL DEFAULT 1.0,
                    best_node_id TEXT,
                    eval_count INTEGER DEFAULT 0,
                    breakthrough_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    secondary_parent_id TEXT,
                    island_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    depth INTEGER DEFAULT 0,
                    debug_depth INTEGER DEFAULT 0,
                    title TEXT NOT NULL,
                    hypothesis TEXT DEFAULT '',
                    rationale TEXT DEFAULT '',
                    code_summary TEXT DEFAULT '',
                    candidate_path TEXT DEFAULT '',
                    score REAL,
                    speedup REAL DEFAULT 0.0,
                    goal_progress REAL DEFAULT 0.0,
                    valid INTEGER DEFAULT 0,
                    eval_ms REAL DEFAULT 0.0,
                    diagnostics TEXT DEFAULT '',
                    metrics_json TEXT DEFAULT '{}',
                    hosted_score REAL,
                    tmux_window TEXT DEFAULT '',
                    harness_used TEXT DEFAULT '',
                    model_used TEXT DEFAULT '',
                    effort_used TEXT DEFAULT '',
                    visits INTEGER DEFAULT 0,
                    max_subtree_speedup REAL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
                CREATE INDEX IF NOT EXISTS idx_nodes_island ON nodes(island_id);
                CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

                CREATE TABLE IF NOT EXISTS diary (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT,
                    island_id INTEGER,
                    entry_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    score REAL,
                    speedup REAL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bulletin (
                    post_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT,
                    island_id INTEGER,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    score REAL,
                    speedup REAL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hosted_submissions (
                    sub_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT,
                    candidate_path TEXT NOT NULL,
                    local_score REAL,
                    hosted_score REAL,
                    status TEXT NOT NULL,
                    response_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eval_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT,
                    valid INTEGER NOT NULL,
                    score REAL,
                    eval_ms REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
            if "effort_used" not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN effort_used TEXT DEFAULT ''")
            existing_runs = conn.execute("SELECT COUNT(*) AS c FROM eval_runs").fetchone()["c"]
            if int(existing_runs) == 0:
                conn.execute(
                    """
                    INSERT INTO eval_runs(node_id, valid, score, eval_ms, created_at)
                    SELECT node_id, valid, score, eval_ms, COALESCE(finished_at, created_at)
                    FROM nodes WHERE eval_ms > 0
                    """
                )

    def set_meta(self, key: str, value: Any) -> None:
        raw = json.dumps(value)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, raw),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except json.JSONDecodeError:
                return row["value"]

    def get_all_meta(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def log_event(self, level: str, source: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events(level, source, message, created_at) VALUES(?, ?, ?, ?)",
                (level, source, message[:1500], time.time()),
            )

    def add_diary(
        self,
        entry_type: str,
        title: str,
        body: str,
        node_id: str | None = None,
        island_id: int | None = None,
        score: float | None = None,
        speedup: float | None = None,
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO diary(node_id, island_id, entry_type, title, body, score, speedup, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, island_id, entry_type, title[:240], body[:8000], score, speedup, time.time()),
            )
            return int(cur.lastrowid or 0)

    def post_bulletin(
        self,
        kind: str,
        message: str,
        node_id: str | None = None,
        island_id: int | None = None,
        score: float | None = None,
        speedup: float | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bulletin(node_id, island_id, kind, message, score, speedup, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, island_id, kind, message[:1200], score, speedup, time.time()),
            )

    def seed_islands(self, island_specs: list[dict[str, str]]) -> None:
        palette = [
            "#38bdf8", "#a78bfa", "#34d399", "#fbbf24",
            "#f472b6", "#fb7185", "#2dd4bf", "#818cf8",
        ]
        n = max(1, len(island_specs))
        wedge = 360.0 / n
        with self._lock, self._connect() as conn:
            for idx, spec in enumerate(island_specs):
                conn.execute(
                    """
                    INSERT INTO islands(island_id, name, strategy_focus, color, angle_start_deg, angle_end_deg)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(island_id) DO UPDATE SET
                        name=excluded.name,
                        strategy_focus=excluded.strategy_focus,
                        color=excluded.color,
                        angle_start_deg=excluded.angle_start_deg,
                        angle_end_deg=excluded.angle_end_deg
                    """,
                    (
                        idx,
                        spec["name"],
                        spec["strategy_focus"],
                        palette[idx % len(palette)],
                        round(idx * wedge, 2),
                        round((idx + 1) * wedge, 2),
                    ),
                )

    def get_islands(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM islands ORDER BY island_id ASC").fetchall()
        return [dict(r) for r in rows]

    def new_node_id(self, prefix: str = "n") -> str:
        with self._lock, self._connect() as conn:
            cnt = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
        short_rand = uuid.uuid4().hex[:4]
        return f"{prefix}{cnt + 1:04d}_{short_rand}"

    def check_novelty(self, title: str, hypothesis: str, threshold: float = 0.72) -> tuple[bool, str | None, float]:
        """ShinkaEvolve pre-execution novelty gate: reject near-duplicate ideas before spawn."""
        cand_tokens = _tokenize_idea(f"{title} {hypothesis}")
        if len(cand_tokens) < 3:
            return True, None, 0.0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT node_id, title, hypothesis FROM nodes WHERE status != 'REJECTED_NOVELTY'"
            ).fetchall()
        best_sim = 0.0
        best_match: str | None = None
        for r in rows:
            existing_tokens = _tokenize_idea(f"{r['title']} {r['hypothesis']}")
            sim = jaccard_similarity(cand_tokens, existing_tokens)
            if sim > best_sim:
                best_sim = sim
                best_match = r["node_id"]
        return (best_sim < threshold, best_match, round(best_sim, 3))

    def create_node(
        self,
        *,
        island_id: int,
        role: str,
        title: str,
        hypothesis: str = "",
        rationale: str = "",
        parent_id: str | None = None,
        secondary_parent_id: str | None = None,
        status: str = "QUEUED",
        candidate_path: str = "",
        debug_depth: int = 0,
        novelty_threshold: float | None = None,
    ) -> tuple[str, bool, str]:
        """Insert a research tree node; runs novelty gate if novelty_threshold is provided."""
        with self._lock:
            depth = 0
            if parent_id:
                parent = self.get_node(parent_id)
                if parent:
                    depth = int(parent["depth"]) + 1

            if novelty_threshold is not None and role in ("EXPLORE", "IMPLEMENT", "DEEPEN", "CROSSOVER"):
                is_novel, dup_id, sim = self.check_novelty(title, hypothesis, novelty_threshold)
                if not is_novel:
                    nid = self.new_node_id("dup")
                    with self._connect() as conn:
                        conn.execute(
                            """
                            INSERT INTO nodes(
                                node_id, parent_id, secondary_parent_id, island_id, role, status,
                                depth, debug_depth, title, hypothesis, rationale, diagnostics, created_at, finished_at
                            ) VALUES(?, ?, ?, ?, ?, 'REJECTED_NOVELTY', ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                nid, parent_id, secondary_parent_id, island_id, role,
                                depth, debug_depth, title[:200], hypothesis[:4000], rationale[:4000],
                                f"Rejected by novelty gate (similarity={sim} with {dup_id})",
                                time.time(), time.time(),
                            ),
                        )
                    self.add_diary(
                        "NOVELTY_DROP",
                        f"Duplicate skipped: {title[:100]}",
                        f"Similarity {sim:.2f} >= {novelty_threshold:.2f} vs {dup_id}.\nHypothesis: {hypothesis[:600]}",
                        node_id=nid,
                        island_id=island_id,
                    )
                    return nid, False, f"Duplicate of {dup_id} (similarity={sim})"

            nid = self.new_node_id("n")
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO nodes(
                        node_id, parent_id, secondary_parent_id, island_id, role, status,
                        depth, debug_depth, title, hypothesis, rationale, candidate_path, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nid, parent_id, secondary_parent_id, island_id, role, status,
                        depth, debug_depth, title[:200], hypothesis[:4000], rationale[:4000],
                        candidate_path, time.time(),
                    ),
                )
            return nid, True, "accepted"

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        return dict(row) if row else None

    def recover_interrupted_nodes(self) -> int:
        """Reset any stranded RUNNING nodes from a prior interrupted session back to QUEUED."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE nodes SET status='QUEUED', tmux_window='' WHERE status='RUNNING'"
            )
            return int(cur.rowcount or 0)

    def claim_next_queued_node(
        self,
        preferred_island: int | None = None,
        exclude_roles: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Atomically transition the highest-priority QUEUED node to RUNNING."""
        role_priority = {
            "DEBUG": 0,
            "IMPLEMENT": 1,
            "DEEPEN": 2,
            "CROSSOVER": 3,
            "EXPLORE": 4,
        }
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if exclude_roles:
                placeholders = ",".join("?" for _ in exclude_roles)
                rows = conn.execute(
                    f"SELECT * FROM nodes WHERE status='QUEUED' AND role NOT IN ({placeholders}) ORDER BY created_at ASC LIMIT 64",
                    exclude_roles,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE status='QUEUED' ORDER BY created_at ASC LIMIT 64"
                ).fetchall()
            if not rows:
                conn.execute("COMMIT")
                return None

            def _sort_key(r: sqlite3.Row) -> tuple[int, int, float]:
                isl_match = 0 if (preferred_island is not None and r["island_id"] == preferred_island) else 1
                rp = role_priority.get(r["role"], 5)
                return (isl_match, rp, float(r["created_at"]))

            chosen = sorted(rows, key=_sort_key)[0]
            now = time.time()
            conn.execute(
                "UPDATE nodes SET status='RUNNING', started_at=? WHERE node_id=?",
                (now, chosen["node_id"]),
            )
            conn.execute("COMMIT")
            d = dict(chosen)
            d["status"] = "RUNNING"
            d["started_at"] = now
            return d

    def update_node_runtime(
        self,
        node_id: str,
        *,
        tmux_window: str | None = None,
        harness_used: str | None = None,
        model_used: str | None = None,
        effort_used: str | None = None,
        status: str | None = None,
        diagnostics: str | None = None,
        code_summary: str | None = None,
        candidate_path: str | None = None,
        finished: bool = False,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        if tmux_window is not None:
            sets.append("tmux_window=?")
            vals.append(tmux_window)
        if harness_used is not None:
            sets.append("harness_used=?")
            vals.append(harness_used)
        if model_used is not None:
            sets.append("model_used=?")
            vals.append(model_used)
        if effort_used is not None:
            sets.append("effort_used=?")
            vals.append(effort_used)
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if diagnostics is not None:
            sets.append("diagnostics=?")
            vals.append(diagnostics[:4000])
        if code_summary is not None:
            sets.append("code_summary=?")
            vals.append(code_summary[:4000])
        if candidate_path is not None:
            sets.append("candidate_path=?")
            vals.append(candidate_path)
        if finished:
            sets.append("finished_at=?")
            vals.append(time.time())
        if not sets:
            return
        vals.append(node_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE node_id=?", vals)

    def record_evaluation(
        self,
        node_id: str,
        *,
        valid: bool,
        score: float | None,
        eval_ms: float,
        diagnostics: str,
        metrics: dict[str, Any] | None = None,
        candidate_path: str | None = None,
        code_summary: str | None = None,
        finalize: bool = False,
    ) -> dict[str, Any]:
        """Record an eval result, update UCB backprop stats, and detect island/global breakthroughs."""
        with self._lock:
            baseline = self.get_meta("baseline_score")
            target = self.get_meta("leaderboard_target")
            direction = self.get_meta("direction", "minimize")
            global_best = self.get_meta("best_score")

            node = self.get_node(node_id)
            if not node:
                raise KeyError(f"Unknown node_id: {node_id}")

            island_id = int(node["island_id"])
            parent = self.get_node(node["parent_id"]) if node.get("parent_id") else None
            parent_score = parent["score"] if (parent and parent.get("valid") and parent.get("score") is not None) else baseline

            speedup, progress = (
                compute_speedup_and_progress(score, baseline, target, direction)
                if (valid and score is not None)
                else (0.0, 0.0)
            )

            beat_parent = bool(
                valid and score is not None and parent_score is not None and is_better_score(score, float(parent_score), direction)
            )

            with self._connect() as conn:
                isl = conn.execute("SELECT * FROM islands WHERE island_id=?", (island_id,)).fetchone()
                isl_best = isl["best_score"] if isl else None
                beat_island = bool(
                    valid and score is not None and (isl_best is None or is_better_score(score, float(isl_best), direction))
                )
                beat_global = bool(
                    valid and score is not None and (global_best is None or is_better_score(score, float(global_best), direction))
                )

                is_breakthrough = beat_parent or beat_island or beat_global

                if finalize:
                    if not valid:
                        new_status = "FAILED"
                    elif is_breakthrough:
                        new_status = "BREAKTHROUGH"
                    else:
                        new_status = "OBSTRUCTED"
                else:
                    new_status = node["status"]

                conn.execute(
                    """
                    UPDATE nodes SET
                        valid=?, score=?, speedup=?, goal_progress=?, eval_ms=?,
                        diagnostics=?, metrics_json=?,
                        candidate_path=COALESCE(?, candidate_path),
                        code_summary=COALESCE(?, code_summary),
                        status=?,
                        finished_at=CASE WHEN ? THEN ? ELSE finished_at END
                    WHERE node_id=?
                    """,
                    (
                        1 if valid else 0,
                        score,
                        speedup,
                        progress,
                        eval_ms,
                        diagnostics[:4000],
                        json.dumps(metrics or {}),
                        candidate_path,
                        code_summary[:4000] if code_summary else None,
                        new_status,
                        1 if finalize else 0,
                        time.time(),
                        node_id,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO eval_runs(node_id, valid, score, eval_ms, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (node_id, 1 if valid else 0, score, eval_ms, time.time()),
                )

                conn.execute(
                    "UPDATE islands SET eval_count = eval_count + 1 WHERE island_id=?",
                    (island_id,),
                )
                if beat_island and score is not None:
                    conn.execute(
                        """
                        UPDATE islands SET
                            best_score=?, best_speedup=?, best_node_id=?,
                            breakthrough_count = breakthrough_count + 1
                        WHERE island_id=?
                        """,
                        (score, speedup, node_id, island_id),
                    )
                elif is_breakthrough:
                    conn.execute(
                        "UPDATE islands SET breakthrough_count = breakthrough_count + 1 WHERE island_id=?",
                        (island_id,),
                    )

                # Backpropagate visits and max_subtree_speedup up to root (UCB + TTT-Discover max-tail).
                cur_id: str | None = node_id
                seen: set[str] = set()
                while cur_id and cur_id not in seen:
                    seen.add(cur_id)
                    conn.execute(
                        """
                        UPDATE nodes SET
                            visits = visits + 1,
                            max_subtree_speedup = MAX(max_subtree_speedup, ?)
                        WHERE node_id=?
                        """,
                        (max(1.0, speedup), cur_id),
                    )
                    prow = conn.execute("SELECT parent_id FROM nodes WHERE node_id=?", (cur_id,)).fetchone()
                    cur_id = prow["parent_id"] if prow else None

            if beat_global and score is not None:
                self.set_meta("best_score", score)
                self.set_meta("best_node_id", node_id)
                self.set_meta("best_speedup", speedup)
                self.set_meta("best_goal_progress", progress)

            gap_to_target = None
            if valid and score is not None and target is not None:
                gap_to_target = round(
                    (score - float(target)) if direction == "minimize" else (float(target) - score),
                    6,
                )

            return {
                "node_id": node_id,
                "valid": valid,
                "score": score,
                "baseline_score": baseline,
                "best_score": self.get_meta("best_score"),
                "leaderboard_target": target,
                "gap_to_target": gap_to_target,
                "speedup_vs_baseline": speedup,
                "goal_progress": progress,
                "beat_parent": beat_parent,
                "beat_island": beat_island,
                "beat_global": beat_global,
                "is_breakthrough": is_breakthrough,
                "eval_ms": round(eval_ms, 2),
                "diagnostics": diagnostics[:1500],
            }

    def select_ucb_parent(self, island_id: int, beta: float = 1.6, c_ucb: float = 0.45) -> dict[str, Any] | None:
        """Select the best parent node in island_id (fallback to global baseline) via Entropic UCB."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM nodes
                WHERE valid=1 AND (island_id=? OR role='BASELINE')
                ORDER BY speedup DESC
                """,
                (island_id,),
            ).fetchall()
        if not rows:
            return None
        total_visits = sum(max(1, int(r["visits"])) for r in rows)
        best_row = None
        best_val = -1e18
        for r in rows:
            max_tail = max(0.5, float(r["max_subtree_speedup"] or r["speedup"] or 1.0)) ** beta
            own_gain = max(0.5, float(r["speedup"] or 1.0))
            visits = int(r["visits"] or 0)
            explore = c_ucb * math.sqrt(math.log(1.0 + total_visits) / (1.0 + visits))
            # Slightly prefer island-native nodes over generic baseline once island has elites.
            isl_bonus = 0.08 if int(r["island_id"]) == island_id and r["role"] != "BASELINE" else 0.0
            val = 0.65 * max_tail + 0.35 * own_gain + explore + isl_bonus - 0.12 * int(r["debug_depth"] or 0)
            if val > best_val:
                best_val = val
                best_row = dict(r)
        return best_row

    def recent_bulletin(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bulletin ORDER BY post_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_recent_hosted_submissions(self, window_sec: float = 86400.0) -> int:
        cutoff = time.time() - window_sec
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM hosted_submissions WHERE created_at >= ? AND status='SUBMITTED'",
                (cutoff,),
            ).fetchone()
        return int(row["c"] if row else 0)

    def record_hosted_submission(
        self,
        node_id: str | None,
        candidate_path: str,
        local_score: float | None,
        hosted_score: float | None,
        status: str,
        response: dict[str, Any],
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO hosted_submissions(node_id, candidate_path, local_score, hosted_score, status, response_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, candidate_path, local_score, hosted_score, status, json.dumps(response), time.time()),
            )
            if node_id and hosted_score is not None:
                conn.execute(
                    "UPDATE nodes SET hosted_score=? WHERE node_id=?",
                    (hosted_score, node_id),
                )
            return int(cur.lastrowid or 0)

    def get_summary_stats(self) -> dict[str, Any]:
        exp_roles = "('IMPLEMENT','DEEPEN','CROSSOVER','DEBUG')"
        with self._lock, self._connect() as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM nodes GROUP BY status"
            ).fetchall()
            counts = {r["status"]: int(r["c"]) for r in status_rows}
            eval_total = conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE score IS NOT NULL OR status IN ('BREAKTHROUGH','OBSTRUCTED','FAILED')"
            ).fetchone()["c"]
            hyp_created = conn.execute(
                f"SELECT COUNT(*) AS c FROM nodes WHERE role IN {exp_roles} AND status != 'REJECTED_NOVELTY'"
            ).fetchone()["c"]
            hyp_tested = conn.execute(
                f"SELECT COUNT(*) AS c FROM nodes WHERE role IN {exp_roles} AND (score IS NOT NULL OR status IN ('BREAKTHROUGH','OBSTRUCTED','FAILED','EVALUATED'))"
            ).fetchone()["c"]
            hyp_testing_now = conn.execute(
                f"SELECT COUNT(*) AS c FROM nodes WHERE role IN {exp_roles} AND status = 'RUNNING'"
            ).fetchone()["c"]
            hyp_queued = conn.execute(
                f"SELECT COUNT(*) AS c FROM nodes WHERE role IN {exp_roles} AND status = 'QUEUED'"
            ).fetchone()["c"]

            run_row = conn.execute(
                "SELECT COUNT(*) AS cnt, AVG(eval_ms) AS avg_ms FROM eval_runs WHERE eval_ms > 0"
            ).fetchone()
            eval_runs_cnt = int(run_row["cnt"] or 0)
            avg_eval_ms = float(run_row["avg_ms"]) if run_row["avg_ms"] is not None else None

            # Fallback to nodes.eval_ms + events history for runs executed prior to eval_runs table
            ev_eval_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE message LIKE 'Eval valid=%'"
            ).fetchone()["c"]
            eval_runs_total = max(eval_runs_cnt, int(ev_eval_cnt), int(hyp_tested))
            if avg_eval_ms is None:
                n_avg = conn.execute(
                    "SELECT AVG(eval_ms) AS avg_ms FROM nodes WHERE eval_ms > 0"
                ).fetchone()["avg_ms"]
                avg_eval_ms = float(n_avg) if n_avg is not None else 0.0

        meta = self.get_all_meta()
        return {
            "counts": counts,
            "queued": counts.get("QUEUED", 0),
            "running": counts.get("RUNNING", 0),
            "breakthroughs": counts.get("BREAKTHROUGH", 0),
            "obstructed": counts.get("OBSTRUCTED", 0),
            "failed": counts.get("FAILED", 0),
            "novelty_rejected": counts.get("REJECTED_NOVELTY", 0),
            "evaluated_total": int(eval_total),
            "hypotheses_created": int(hyp_created),
            "hypotheses_tested": int(hyp_tested),
            "hypotheses_testing_now": int(hyp_testing_now),
            "hypotheses_queued": int(hyp_queued),
            "eval_suite_runs": int(eval_runs_total),
            "avg_eval_ms": round(avg_eval_ms, 2),
            "baseline_score": meta.get("baseline_score"),
            "best_score": meta.get("best_score"),
            "best_node_id": meta.get("best_node_id"),
            "best_speedup": meta.get("best_speedup", 1.0),
            "best_goal_progress": meta.get("best_goal_progress", 0.0),
            "leaderboard_target": meta.get("leaderboard_target"),
            "direction": meta.get("direction", "minimize"),
        }

    def export_dashboard_state(self) -> dict[str, Any]:
        """Export complete radial-tree geometry, islands, diary, bulletin, and live events."""
        with self._lock, self._connect() as conn:
            meta = self.get_all_meta()
            islands = [dict(r) for r in conn.execute("SELECT * FROM islands ORDER BY island_id ASC").fetchall()]
            nodes = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM nodes WHERE status != 'REJECTED_NOVELTY' ORDER BY created_at ASC"
                ).fetchall()
            ]
            diary = [
                dict(r)
                for r in conn.execute("SELECT * FROM diary ORDER BY entry_id DESC LIMIT 200").fetchall()
            ]
            bulletin = [
                dict(r)
                for r in conn.execute("SELECT * FROM bulletin ORDER BY post_id DESC LIMIT 50").fetchall()
            ]
            events = [
                dict(r)
                for r in conn.execute("SELECT * FROM events ORDER BY event_id DESC LIMIT 120").fetchall()
            ]
            hosted = [
                dict(r)
                for r in conn.execute("SELECT * FROM hosted_submissions ORDER BY sub_id DESC LIMIT 40").fetchall()
            ]

        # Compute non-overlapping angular coordinates + progress-proportional radial lengths!
        island_map = {int(i["island_id"]): i for i in islands}
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        node_by_id = {n["node_id"]: n for n in nodes}
        baseline_node = next((n for n in nodes if n["role"] == "BASELINE"), None)
        root_id = baseline_node["node_id"] if baseline_node else None

        for n in nodes:
            pid = n.get("parent_id")
            if pid and pid in node_by_id and n["node_id"] != pid:
                children_by_parent.setdefault(pid, []).append(n)

        leaf_Memo: dict[str, int] = {}

        def _count_leaves(nid: str, visited: set[str]) -> int:
            if nid in leaf_Memo:
                return leaf_Memo[nid]
            if nid in visited:
                return 1
            visited = visited | {nid}
            kids = children_by_parent.get(nid, [])
            if not kids:
                leaf_Memo[nid] = 1
                return 1
            total = sum(_count_leaves(k["node_id"], visited) for k in kids)
            leaf_Memo[nid] = max(1, total)
            return leaf_Memo[nid]

        for n in nodes:
            _count_leaves(n["node_id"], set())

        def _assign_angles(nid: str, a0: float, a1: float, visited: set[str]) -> None:
            if nid in visited or nid not in node_by_id:
                return
            visited = visited | {nid}
            node = node_by_id[nid]
            node["angle_deg"] = round(0.5 * (a0 + a1), 3)
            kids = children_by_parent.get(nid, [])
            if not kids:
                return
            total_w = sum(leaf_Memo.get(k["node_id"], 1) for k in kids)
            cursor = a0
            for k in kids:
                span = (a1 - a0) * (leaf_Memo.get(k["node_id"], 1) / max(1, total_w))
                _assign_angles(k["node_id"], cursor, cursor + span, visited)
                cursor += span

        if root_id:
            node_by_id[root_id]["angle_deg"] = 0.0
            node_by_id[root_id]["radius_norm"] = 0.0
            # Partition root's children by island angular sector first so every island owns its ray wedge!
            root_kids = children_by_parent.get(root_id, [])
            by_isl: dict[int, list[dict[str, Any]]] = {}
            for rk in root_kids:
                by_isl.setdefault(int(rk["island_id"]), []).append(rk)
            for isl_id, ikids in by_isl.items():
                isl = island_map.get(isl_id, {"angle_start_deg": 0.0, "angle_end_deg": 360.0})
                s0 = float(isl["angle_start_deg"]) + 4.0
                s1 = float(isl["angle_end_deg"]) - 4.0
                if s1 <= s0:
                    s0, s1 = float(isl["angle_start_deg"]), float(isl["angle_end_deg"])
                tot = sum(leaf_Memo.get(k["node_id"], 1) for k in ikids)
                cur = s0
                for k in ikids:
                    span = (s1 - s0) * (leaf_Memo.get(k["node_id"], 1) / max(1, tot))
                    _assign_angles(k["node_id"], cur, cur + span, {root_id})
                    cur += span

        # Fallback angles for any orphan/rootless nodes.
        for idx, n in enumerate(nodes):
            if "angle_deg" not in n:
                isl = island_map.get(int(n["island_id"]), {"angle_start_deg": 0.0, "angle_end_deg": 360.0})
                n["angle_deg"] = round(0.5 * (float(isl["angle_start_deg"]) + float(isl["angle_end_deg"])) + (idx % 7 - 3) * 3.5, 2)

        # Compute radial distance from center strictly monotonic along outward lineages,
        # driven by verified speedup over baseline and goal progress!
        max_speedup = max([1.05] + [float(n.get("speedup") or 1.0) for n in nodes if n.get("valid")])
        for n in nodes:
            if n["role"] == "BASELINE":
                n["radius_norm"] = 0.0
                continue
            parent = node_by_id.get(n.get("parent_id") or "")
            p_rad = float(parent.get("radius_norm", 0.0)) if parent else 0.0
            sp = float(n.get("speedup") or 0.0)
            gp = float(n.get("goal_progress") or 0.0)
            if n.get("valid") and sp > 1.000001:
                # Map speedup & goal progress onto [0.12 .. 1.25] (1.0 = Leaderboard Target Ring!)
                if meta.get("leaderboard_target") is not None and gp > 0:
                    metric_r = 0.10 + 0.90 * gp
                else:
                    denom = max(0.05, math.log(max_speedup))
                    metric_r = 0.12 + 0.88 * (math.log(sp) / denom)
                n["radius_norm"] = round(max(p_rad + 0.045, min(metric_r, 1.35)), 4)
            else:
                # Exploratory / running / obstructed twigs extend slightly from parent so lineage stays visible.
                step = 0.075 if n["status"] == "RUNNING" else (0.055 if n["role"] == "EXPLORE" else 0.04)
                n["radius_norm"] = round(min(1.30, max(0.06, p_rad + step)), 4)

        return {
            "meta": meta,
            "stats": self.get_summary_stats(),
            "islands": islands,
            "nodes": nodes,
            "diary": diary,
            "bulletin": bulletin,
            "events": events,
            "hosted_submissions": hosted,
            "server_time": time.time(),
        }
