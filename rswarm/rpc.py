"""Coordinator Eval & Tree RPC Server + Client (`swarm-eval`, `swarm-propose`, `swarm-submit`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from .db import SwarmDB
from .models import SwarmConfig, compute_speedup_and_progress, is_better_score, meets_or_beats_score


def send_rpc_request(sock_path: str | Path, payload: dict[str, Any], timeout_sec: float = 240.0) -> dict[str, Any]:
    """Synchronous Unix-socket RPC client used by agent skill wrappers (`swarm-*`)."""
    sp = Path(sock_path)
    if not sp.exists():
        raise ConnectionError(f"Coordinator RPC socket not found at {sp}. Is `rswarm run` active?")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_sec)
        s.connect(str(sp))
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            buf = s.recv(65536)
            if not buf:
                break
            chunks.append(buf)
            if b"\n" in buf:
                break
    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        raise RuntimeError("Empty response from rswarm coordinator RPC.")
    return json.loads(raw.splitlines()[0])


def parse_eval_json_output(stdout_text: str) -> dict[str, Any]:
    """Extract the last valid JSON object printed by `eval/local_eval.sh` or `hosted_submit.sh`."""
    lines = [ln.strip() for ln in (stdout_text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("{") and ln.endswith("}"):
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    # Fallback scan for multi-line JSON block at end of output.
    idx = stdout_text.rfind("{")
    if idx >= 0:
        try:
            obj = json.loads(stdout_text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Evaluator did not emit valid JSON. Output tail:\n{stdout_text[-800:]}")


class EvalAndTreeService:
    """Owns the bounded Eval Semaphore, Vault promotions, Tree Multiplication, and Hosted Quotas."""

    def __init__(self, cfg: SwarmConfig, db: SwarmDB) -> None:
        self.cfg = cfg
        self.db = db
        self.ws = Path(cfg.workspace_dir)
        self.sock_path = self.ws / "state" / "rswarm.sock"
        self.eval_sem = asyncio.Semaphore(cfg.eval_workers)
        self._pool_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None

    async def start_rpc_server(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            self.sock_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.sock_path))
        os.chmod(self.sock_path, 0o600)

    async def stop_rpc_server(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not raw:
                return
            req = json.loads(raw.decode("utf-8"))
            action = req.get("action")
            if action == "eval":
                res = await self.run_local_eval(
                    node_id=req.get("node_id"),
                    candidate_path=str(req.get("candidate_path", "")),
                    finalize=False,
                )
            elif action == "propose":
                res = await self.handle_propose(req)
            elif action == "directive":
                res = await self.handle_directive(req)
            elif action == "submit":
                res = await self.handle_submit(req)
            elif action == "hosted_submit":
                res = await self.handle_hosted_submit(req)
            elif action == "ping":
                res = {"ok": True, "stats": self.db.get_summary_stats()}
            else:
                res = {"ok": False, "error": f"Unknown RPC action: {action}"}
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}

        try:
            writer.write((json.dumps(res) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()

    def _copy_candidate_snapshot(self, src: Path, dst: Path) -> Path:
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        return dst

    async def execute_raw_local_eval(self, candidate_path: Path) -> dict[str, Any]:
        """Execute `eval/local_eval.sh` in an isolated scratch directory behind the Eval Semaphore."""
        eval_script = self.ws / "eval" / "local_eval.sh"
        if not eval_script.is_file():
            raise FileNotFoundError(f"Missing local evaluator: {eval_script}")
        if not candidate_path.exists():
            return {
                "valid": False,
                "score": None,
                "eval_ms": 0.0,
                "diagnostics": f"Candidate path does not exist: {candidate_path}",
                "metrics": {},
            }

        scratch_id = f"eval_{int(time.time()*1000)}_{os.getpid()}_{id(candidate_path) % 10000}"
        scratch_dir = self.ws / "state" / "eval_scratch" / scratch_id
        snap_target = scratch_dir / candidate_path.name
        self._copy_candidate_snapshot(candidate_path, snap_target)

        async with self.eval_sem:
            t0 = time.monotonic()
            env = dict(os.environ)
            env["OMP_NUM_THREADS"] = "2"
            env["RSWARM_WORKSPACE"] = str(self.ws)
            cmd = ["bash", str(eval_script), str(snap_target)]
            gatectl_bin = shutil.which("gatectl") or str(Path.home() / ".local" / "bin" / "gatectl")
            if self.cfg.use_gpu_gate and Path(gatectl_bin).is_file():
                cmd = [
                    gatectl_bin,
                    "run",
                    "--job",
                    f"rswarm-{self.cfg.name[:16]}",
                    "--gpu-mib",
                    "4096",
                    "--ram-mib",
                    "4096",
                    "--cpus",
                    "2",
                    "--",
                    *cmd,
                ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(scratch_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                out_b, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.cfg.eval_timeout_sec
                )
                elapsed_ms = (time.monotonic() - t0) * 1000.0
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, 15)
                except OSError:
                    pass
                shutil.rmtree(scratch_dir, ignore_errors=True)
                return {
                    "valid": False,
                    "score": None,
                    "eval_ms": self.cfg.eval_timeout_sec * 1000.0,
                    "diagnostics": f"local_eval.sh timed out after {self.cfg.eval_timeout_sec}s",
                    "metrics": {},
                }

        shutil.rmtree(scratch_dir, ignore_errors=True)
        out_txt = out_b.decode("utf-8", errors="replace")
        try:
            parsed = parse_eval_json_output(out_txt)
        except ValueError as ve:
            return {
                "valid": False,
                "score": None,
                "eval_ms": round(elapsed_ms, 2),
                "diagnostics": f"exit={proc.returncode}; {ve}",
                "metrics": {},
            }

        valid = bool(parsed.get("valid", False)) and proc.returncode == 0
        raw_score = parsed.get("score")
        score = float(raw_score) if (raw_score is not None and valid) else None
        return {
            "valid": valid,
            "score": score,
            "eval_ms": float(parsed.get("eval_ms") or round(elapsed_ms, 2)),
            "diagnostics": str(parsed.get("diagnostics") or ("ok" if valid else out_txt[-600:])),
            "metrics": parsed.get("metrics") if isinstance(parsed.get("metrics"), dict) else {},
        }

    async def run_local_eval(
        self,
        *,
        node_id: str | None,
        candidate_path: str,
        code_summary: str | None = None,
        finalize: bool = False,
    ) -> dict[str, Any]:
        cpath = Path(candidate_path).resolve()
        raw = await self.execute_raw_local_eval(cpath)
        if not node_id or not self.db.get_node(node_id):
            return {"ok": True, **raw}

        pool_res = None
        eval_score = raw["score"]
        eval_diag = raw["diagnostics"]
        eval_metrics = raw["metrics"]

        if raw["valid"] and (self.ws / "eval" / "merge_pool.sh").is_file():
            pre_best = self.db.get_meta("best_score")
            pool_res = await self.try_pool_merge(node_id, cpath)
            merged_cnt = int(pool_res.get("merged_count") or 0) if isinstance(pool_res, dict) else 0
            if merged_cnt > 0 and isinstance(pool_res, dict) and pool_res.get("score") is not None:
                eval_score = float(pool_res["score"])
                eval_diag = str(pool_res.get("diagnostics") or eval_diag)
                if isinstance(pool_res.get("metrics"), dict) and pool_res["metrics"]:
                    eval_metrics = pool_res["metrics"]
            elif merged_cnt == 0 and pre_best is not None:
                # Guard against mid-run pool drift: if another worker / bg-polisher lowered vault/best while
                # this node ran and this node merely re-copied vault/best (`merged_count == 0`), clamp seed_score!
                cur_node = self.db.get_node(node_id)
                already_won = bool(
                    cur_node
                    and self.db.get_meta("best_node_id") == node_id
                    and eval_score is not None
                    and abs(float(eval_score) - float(pre_best)) <= 1e-12
                )
                if cur_node and not already_won:
                    cur_seed = cur_node.get("seed_score")
                    if cur_seed is None or is_better_score(float(pre_best), float(cur_seed), self.cfg.direction):
                        self.db.update_node_runtime(node_id, seed_score=float(pre_best))

        rec = self.db.record_evaluation(
            node_id,
            valid=raw["valid"],
            score=eval_score,
            eval_ms=raw["eval_ms"],
            diagnostics=eval_diag,
            metrics=eval_metrics,
            candidate_path=str(cpath),
            code_summary=code_summary,
            finalize=finalize,
        )
        self.db.log_event(
            "BREAKTHROUGH" if rec["is_breakthrough"] else ("INFO" if raw["valid"] else "WARN"),
            node_id,
            f"Eval valid={raw['valid']} score={eval_score} speedup={rec['speedup_vs_baseline']:.4f}x ({raw['eval_ms']:.0f}ms)",
        )
        return {"ok": True, **rec, "metrics": eval_metrics, "pool_merge": pool_res}

    def _promote_node_solvers_to_vault(self, node_id: str) -> None:
        """Archive winning C++/CUDA/Python solver code into vault/solvers/<node_id>/ so children compound algorithms."""
        wdir = self.ws / "work" / node_id
        if not wdir.is_dir():
            return
        exts = {".cpp", ".cc", ".cxx", ".h", ".hpp", ".cu", ".cuh", ".py", ".sh"}
        found: list[Path] = []
        for p in wdir.rglob("*"):
            if p.is_symlink() or not p.is_file():
                continue
            rel_parts = p.relative_to(wdir).parts
            if any(part.startswith(".") or part in ("candidate", "secondary_parent", "shared", "solvers_vault") for part in rel_parts):
                continue
            if p.suffix.lower() in exts and p.stat().st_size <= 512_000:
                found.append(p)
        if not found:
            return
        dest_root = self.ws / "vault" / "solvers" / node_id
        dest_root.mkdir(parents=True, exist_ok=True)
        for p in found[:16]:
            rel = p.relative_to(wdir)
            out_p = dest_root / rel
            out_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out_p)

    async def try_pool_merge(self, node_id: str | None, candidate_path: Path) -> dict[str, Any] | None:
        """If workspace provides `eval/merge_pool.sh`, merge per-subproblem elites into `vault/best/`."""
        merge_script = self.ws / "eval" / "merge_pool.sh"
        if not merge_script.is_file() or not candidate_path.exists():
            return None
        best_dir = self.ws / "vault" / "best"
        best_dir.mkdir(parents=True, exist_ok=True)
        async with self._pool_lock:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(merge_script),
                str(candidate_path),
                str(best_dir),
                cwd=str(self.ws),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.eval_timeout_sec)
            except asyncio.TimeoutError:
                proc.kill()
                return None
            if proc.returncode != 0:
                return None
            try:
                parsed = parse_eval_json_output(out_b.decode("utf-8", errors="replace"))
            except ValueError:
                return None
            if not parsed.get("valid") or parsed.get("score") is None:
                return parsed
            new_score = float(parsed["score"])
            merged_cnt = int(parsed.get("merged_count") or 0)
            if isinstance(parsed.get("metrics"), dict):
                self.db._update_gap_slices_from_metrics(parsed["metrics"])
            cur_best = self.db.get_meta("best_score")
            baseline = self.db.get_meta("baseline_score")
            target = self.db.get_meta("leaderboard_target")
            if cur_best is None or is_better_score(new_score, float(cur_best), self.cfg.direction):
                sp, gp = compute_speedup_and_progress(new_score, baseline, target, self.cfg.direction)
                self.db.set_meta("best_score", new_score)
                self.db.set_meta("best_speedup", sp)
                self.db.set_meta("best_goal_progress", gp)
                self.db.set_meta("evals_since_global_best", 0)
                if node_id:
                    self.db.set_meta("best_node_id", node_id)
                msg = (
                    f"[POOL UNION SOTA] Merged {merged_cnt} sub-instance win(s) from {node_id or candidate_path.name} "
                    f"-> new Global Best score={new_score} ({sp:.4f}x)!"
                )
                self.db.post_bulletin("BREAKTHROUGH", msg, node_id=node_id, score=new_score, speedup=sp)
                self.db.add_diary(
                    "BREAKTHROUGH",
                    f"POOL UNION SOTA: {new_score} ({sp:.4f}x)",
                    f"Merged {merged_cnt} improved sub-instances from {candidate_path} into {best_dir}.\nDiagnostics: {parsed.get('diagnostics', '')}",
                    node_id=node_id,
                    score=new_score,
                    speedup=sp,
                )
                self.db.log_event("BREAKTHROUGH", node_id or "pool", msg)
                sub_thresh = self.cfg.leaderboard_submit_threshold
                best_cand_path = str(parsed.get("best_path") or best_dir)
                if sub_thresh is not None and meets_or_beats_score(new_score, float(sub_thresh), self.cfg.direction):
                    await self.handle_hosted_submit(
                        {
                            "node_id": node_id,
                            "candidate_path": best_cand_path,
                            "note": f"Pool union reached Top-5 ({new_score})",
                            "verified_score": new_score,
                        }
                    )
            return parsed

    async def handle_directive(self, req: dict[str, Any]) -> dict[str, Any]:
        node_id = str(req.get("node_id") or "").strip() or None
        directive = str(req.get("directive") or "").strip()
        if not directive:
            return {"ok": False, "error": "directive cannot be empty"}
        directives = self.db.add_strategy_directive(directive, node_id=node_id)
        self.db.log_event("STRATEGY", node_id or "meta", f"Pinned strategy directive: {directive[:180]}")
        return {"ok": True, "directives": directives}

    async def handle_propose(self, req: dict[str, Any]) -> dict[str, Any]:
        parent_id = req.get("parent_id") or self.db.get_meta("best_node_id") or self.db.get_meta("baseline_node_id")
        parent = self.db.get_node(parent_id) if parent_id else None
        island_id = int(req["island_id"]) if req.get("island_id") is not None else (int(parent["island_id"]) if parent else 0)
        title = str(req.get("title") or "Untitled Hypothesis").strip()
        hypothesis = str(req.get("hypothesis") or "").strip()
        rationale = str(req.get("rationale") or "").strip()
        target_slice = str(req.get("target_slice") or "").strip()
        role = str(req.get("role") or "IMPLEMENT").upper()
        if role not in ("IMPLEMENT", "EXPLORE", "DEEPEN", "CROSSOVER"):
            role = "IMPLEMENT"

        nid, accepted, msg = self.db.create_node(
            island_id=island_id,
            role=role,
            title=title,
            hypothesis=hypothesis,
            rationale=rationale,
            parent_id=parent_id,
            target_slice=target_slice,
            novelty_threshold=self.cfg.novelty_threshold,
        )
        if accepted:
            self.db.add_diary(
                "HYPOTHESIS",
                f"[{role}] {title}",
                f"Parent: {parent_id}\nHypothesis: {hypothesis}\nRationale: {rationale}",
                node_id=nid,
                island_id=island_id,
            )
            self.db.log_event("PROPOSE", nid, f"Queued {role} on island {island_id}: {title}")
        else:
            self.db.log_event("NOVELTY", nid, f"Filtered duplicate proposal: {title} ({msg})")
        return {"ok": True, "accepted": accepted, "child_node_id": nid, "message": msg}

    async def handle_submit(self, req: dict[str, Any]) -> dict[str, Any]:
        node_id = str(req.get("node_id") or "").strip()
        cand_str = str(req.get("candidate_path") or "").strip()
        summary = str(req.get("summary") or "").strip()
        req_hosted = bool(req.get("request_hosted", False))

        if not node_id or not self.db.get_node(node_id):
            return {"ok": False, "error": f"Unknown node_id '{node_id}'"}
        node = self.db.get_node(node_id)
        assert node is not None
        island_id = int(node["island_id"])

        eval_res = await self.run_local_eval(
            node_id=node_id,
            candidate_path=cand_str,
            code_summary=summary,
            finalize=True,
        )

        # Case 1: Invalid / crashed candidate -> spawn bounded DEBUG repair child (AIDE / AI Scientist-v2).
        if not eval_res["valid"]:
            self.db.add_diary(
                "EVAL",
                f"Invalid candidate: {node['title']}",
                f"Summary: {summary}\nDiagnostics:\n{eval_res['diagnostics']}",
                node_id=node_id,
                island_id=island_id,
            )
            spawned_debug = None
            if int(node["debug_depth"]) < self.cfg.max_debug_depth:
                dbg_id, _, _ = self.db.create_node(
                    island_id=island_id,
                    role="DEBUG",
                    title=f"Repair: {node['title'][:120]}",
                    hypothesis=node["hypothesis"],
                    rationale=f"Fix validation/runtime failure from {node_id}:\n{eval_res['diagnostics'][:1000]}",
                    parent_id=node_id,
                    candidate_path=cand_str,
                    debug_depth=int(node["debug_depth"]) + 1,
                    novelty_threshold=None,
                )
                spawned_debug = dbg_id
                self.db.log_event("DEBUG", dbg_id, f"Spawned DEBUG repair child for broken {node_id}")
            return {**eval_res, "vault_promoted": False, "spawned_debug_node": spawned_debug}

        # Case 2: Breakthrough! Promote to Vault, broadcast Bulletin, and multiply the tree!
        if eval_res["is_breakthrough"]:
            self._promote_node_solvers_to_vault(node_id)
            vault_node_dir = self.ws / "vault" / node_id
            cpath = Path(cand_str).resolve()
            vault_target = vault_node_dir / cpath.name
            self._copy_candidate_snapshot(cpath, vault_target)
            self.db.update_node_runtime(node_id, candidate_path=str(vault_target))

            if eval_res["beat_global"] and not (self.ws / "eval" / "merge_pool.sh").is_file():
                best_dir = self.ws / "vault" / "best"
                self._copy_candidate_snapshot(cpath, best_dir / cpath.name)

            tag = "NEW GLOBAL BEST" if eval_res["beat_global"] else ("NEW ISLAND ELITE" if eval_res["beat_island"] else "LINEAGE GAIN")
            b_msg = (
                f"[{tag}] {node['title']} achieved score={eval_res['score']} "
                f"({eval_res['speedup_vs_baseline']:.4f}x over baseline). Summary: {summary[:350]}"
            )
            self.db.post_bulletin(
                "BREAKTHROUGH",
                b_msg,
                node_id=node_id,
                island_id=island_id,
                score=eval_res["score"],
                speedup=eval_res["speedup_vs_baseline"],
            )
            self.db.add_diary(
                "BREAKTHROUGH",
                f"{tag}: {node['title']} ({eval_res['speedup_vs_baseline']:.4f}x)",
                f"Score: {eval_res['score']} (Baseline: {eval_res['baseline_score']}, Target: {eval_res['leaderboard_target']})\n"
                f"Summary: {summary}\nVault Path: {vault_target}",
                node_id=node_id,
                island_id=island_id,
                score=eval_res["score"],
                speedup=eval_res["speedup_vs_baseline"],
            )

            # Multiply the tree automatically (bounded depth + code-distinct crossover):
            # (a) DEEPEN child to push hyperparameters / second-order refinements (capped at max_deepen_depth unless beat_global)
            # (b) EXPLORE child to branch orthogonal new hypotheses when beat_island or beat_global
            spawned_children: list[str] = []
            cur_deep_depth = int(node.get("deepen_depth") or 0)
            if cur_deep_depth < self.cfg.max_deepen_depth or eval_res["beat_global"]:
                clean_title = node["title"]
                while clean_title.startswith("Deepen & scale "):
                    clean_title = clean_title[len("Deepen & scale "):]
                deep_id, ok1, _ = self.db.create_node(
                    island_id=island_id,
                    role="DEEPEN",
                    title=f"Deepen & scale {clean_title[:96]}",
                    hypothesis=f"Push algorithmic/numerical exploitation of winning mechanism in {node_id}: {summary[:400]}",
                    rationale=f"Parent {node_id} reached {eval_res['speedup_vs_baseline']:.4f}x ({eval_res['score']}). Extract second-order gains.",
                    parent_id=node_id,
                    deepen_depth=cur_deep_depth + 1,
                    target_slice=str(node.get("target_slice") or ""),
                    novelty_threshold=None,
                )
                if ok1:
                    spawned_children.append(deep_id)

            if eval_res["beat_island"] or eval_res["beat_global"]:
                exp_id, ok2, _ = self.db.create_node(
                    island_id=island_id,
                    role="EXPLORE",
                    title=f"Branch new frontier from {node_id}",
                    hypothesis=f"Invent 2-3 orthogonal structural leaps atop {node_id} ({eval_res['speedup_vs_baseline']:.4f}x).",
                    rationale="Multiply winning lineage into distinct sub-directions.",
                    parent_id=node_id,
                    novelty_threshold=None,
                )
                if ok2:
                    spawned_children.append(exp_id)

            # (c) Cross-island Solver-Code CROSSOVER only when two islands hold distinct solver codebases & non-identical candidates!
            if eval_res["beat_island"] and (self.ws / "vault" / "solvers" / node_id).is_dir():
                st = self.db.get_summary_stats()
                with self.db._lock, self.db._connect() as conn:
                    active_x = conn.execute(
                        "SELECT COUNT(*) AS c FROM nodes WHERE role='CROSSOVER' AND status IN ('QUEUED','RUNNING')"
                    ).fetchone()["c"]
                if int(active_x or 0) < 2:
                    other_elites = [
                        isl for isl in self.db.get_islands()
                        if int(isl["island_id"]) != island_id
                        and isl.get("best_node_id")
                        and str(isl["best_node_id"]) != node_id
                        and (self.ws / "vault" / "solvers" / str(isl["best_node_id"])).is_dir()
                    ]
                    if other_elites:
                        partner = max(other_elites, key=lambda x: float(x.get("best_speedup") or 1.0))
                        p_nid = str(partner["best_node_id"])
                        p_node = self.db.get_node(p_nid)
                        same_bytes = False
                        if p_node and p_node.get("candidate_path") and Path(p_node["candidate_path"]).is_file() and vault_target.is_file():
                            try:
                                h1 = hashlib.sha256(vault_target.read_bytes()).hexdigest()
                                h2 = hashlib.sha256(Path(p_node["candidate_path"]).read_bytes()).hexdigest()
                                same_bytes = (h1 == h2)
                            except OSError:
                                same_bytes = False
                        if not same_bytes or not (self.ws / "eval" / "merge_pool.sh").is_file():
                            x_id, ok3, _ = self.db.create_node(
                                island_id=island_id,
                                role="CROSSOVER",
                                title=f"Algorithmic Crossover {node_id} x {p_nid}",
                                hypothesis=(
                                    f"Fuse solver algorithms from `./solvers_vault/{node_id}/` ({eval_res['speedup_vs_baseline']:.4f}x) "
                                    f"and `./solvers_vault/{p_nid}/` ({partner['best_speedup']:.4f}x on `{partner['name']}`) "
                                    f"into a chained pipeline (e.g. generator from one feeding optimizer of the other)."
                                ),
                                rationale="Cross-island algorithmic synthesis of two distinct solver codebases.",
                                parent_id=node_id,
                                secondary_parent_id=p_nid,
                                novelty_threshold=self.cfg.novelty_threshold,
                            )
                            if ok3:
                                spawned_children.append(x_id)

            hosted_res = None
            sub_thresh = self.cfg.leaderboard_submit_threshold
            if eval_res["beat_global"] and eval_res["score"] is not None:
                if sub_thresh is not None:
                    if meets_or_beats_score(float(eval_res["score"]), float(sub_thresh), self.cfg.direction):
                        hosted_res = await self.handle_hosted_submit(
                            {
                                "node_id": node_id,
                                "candidate_path": str(vault_target),
                                "note": summary,
                                "verified_score": float(eval_res["score"]),
                            }
                        )
                    elif req_hosted or self.cfg.auto_hosted_on_sota:
                        hosted_res = {
                            "ok": False,
                            "submitted": False,
                            "gated": True,
                            "threshold": sub_thresh,
                            "local_score": eval_res["score"],
                            "message": (
                                f"Held until leaderboard submit threshold ({sub_thresh}) is reached "
                                f"(current score: {eval_res['score']})."
                            ),
                        }
                elif req_hosted or self.cfg.auto_hosted_on_sota:
                    hosted_res = await self.handle_hosted_submit(
                        {
                            "node_id": node_id,
                            "candidate_path": str(vault_target),
                            "note": summary,
                            "verified_score": float(eval_res["score"]),
                        }
                    )

            return {
                **eval_res,
                "vault_promoted": True,
                "vault_path": str(vault_target),
                "spawned_children": spawned_children,
                "hosted_submission": hosted_res,
            }

        # Case 3: Valid run, no improvement -> record Obstruction so peers learn what did NOT help.
        obs_msg = (
            f"[OBSTRUCTION] {node['title']} scored {eval_res['score']} "
            f"({eval_res['speedup_vs_baseline']:.4f}x, did not beat parent). Lesson: {summary[:300]}"
        )
        self.db.post_bulletin(
            "OBSTRUCTION",
            obs_msg,
            node_id=node_id,
            island_id=island_id,
            score=eval_res["score"],
            speedup=eval_res["speedup_vs_baseline"],
        )
        self.db.add_diary(
            "OBSTRUCTION",
            f"No gain: {node['title']} ({eval_res['speedup_vs_baseline']:.4f}x)",
            f"Score: {eval_res['score']}\nSummary: {summary}\nDiagnostics: {eval_res['diagnostics']}",
            node_id=node_id,
            island_id=island_id,
            score=eval_res["score"],
            speedup=eval_res["speedup_vs_baseline"],
        )
        return {**eval_res, "vault_promoted": False}

    async def handle_hosted_submit(self, req: dict[str, Any]) -> dict[str, Any]:
        node_id = req.get("node_id")
        cand_str = str(req.get("candidate_path") or "").strip()
        note = str(req.get("note") or "")
        used = self.db.count_recent_hosted_submissions(86400.0)
        if used >= self.cfg.hosted_quota_per_day:
            msg = f"Hosted submission quota exhausted ({used}/{self.cfg.hosted_quota_per_day} in last 24h)."
            self.db.log_event("WARN", node_id or "hosted", msg)
            return {"ok": False, "submitted": False, "error": msg}

        hosted_script = self.ws / "eval" / "hosted_submit.sh"
        if not hosted_script.is_file():
            return {"ok": False, "submitted": False, "error": f"Missing {hosted_script}"}

        cpath = Path(cand_str).resolve()
        node = self.db.get_node(node_id) if node_id else None
        local_score = req.get("verified_score")
        if local_score is None and node and node.get("valid") and node.get("score") is not None:
            local_score = float(node["score"])

        sub_thresh = self.cfg.leaderboard_submit_threshold
        if sub_thresh is not None and not bool(req.get("force", False)):
            if local_score is None:
                check = await self.execute_raw_local_eval(cpath)
                if not check["valid"] or check["score"] is None:
                    msg = f"Cannot submit invalid candidate to leaderboard: {check['diagnostics']}"
                    self.db.log_event("WARN", node_id or "hosted", msg)
                    return {"ok": False, "submitted": False, "gated": True, "error": msg}
                local_score = float(check["score"])
            if not meets_or_beats_score(float(local_score), float(sub_thresh), self.cfg.direction):
                cmp_op = "<=" if self.cfg.direction == "minimize" else ">="
                msg = (
                    f"Blocked by Top-5 leaderboard gate: verified score {local_score} must be "
                    f"{cmp_op} {sub_thresh} before submitting to the competition leaderboard."
                )
                self.db.log_event("INFO", node_id or "hosted", msg)
                return {
                    "ok": False,
                    "submitted": False,
                    "gated": True,
                    "threshold": sub_thresh,
                    "local_score": local_score,
                    "error": msg,
                }

        proc = await asyncio.create_subprocess_exec(
            "bash",
            str(hosted_script),
            str(cpath),
            note,
            cwd=str(self.ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=180.0)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "submitted": False, "error": "hosted_submit.sh timed out"}

        out_txt = out_b.decode("utf-8", errors="replace")
        try:
            parsed = parse_eval_json_output(out_txt)
        except ValueError:
            parsed = {"submitted": False, "hosted_score": None, "message": out_txt[-500:]}

        submitted = bool(parsed.get("submitted", False)) and proc.returncode == 0
        h_score = float(parsed["hosted_score"]) if parsed.get("hosted_score") is not None else None
        status = "SUBMITTED" if submitted else "SKIPPED_OR_UNCONFIGURED"
        self.db.record_hosted_submission(
            node_id=node_id,
            candidate_path=str(cpath),
            local_score=local_score,
            hosted_score=h_score,
            status=status,
            response=parsed,
        )
        self.db.add_diary(
            "HOSTED_SUBMIT",
            f"Hosted submission ({status}): {cpath.name}",
            f"Local score: {local_score}, Hosted score: {h_score}\nResponse: {json.dumps(parsed)}",
            node_id=node_id,
            score=h_score or local_score,
        )
        self.db.log_event("HOSTED", node_id or "hosted", f"Hosted submit -> {status} (hosted_score={h_score})")
        return {"ok": True, "submitted": submitted, "hosted_score": h_score, "details": parsed}
