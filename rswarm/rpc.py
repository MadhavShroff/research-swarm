"""Coordinator Eval & Tree RPC Server + Client (`swarm-eval`, `swarm-propose`, `swarm-submit`)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from .db import SwarmDB
from .models import SwarmConfig


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

        rec = self.db.record_evaluation(
            node_id,
            valid=raw["valid"],
            score=raw["score"],
            eval_ms=raw["eval_ms"],
            diagnostics=raw["diagnostics"],
            metrics=raw["metrics"],
            candidate_path=str(cpath),
            code_summary=code_summary,
            finalize=finalize,
        )
        self.db.log_event(
            "BREAKTHROUGH" if rec["is_breakthrough"] else ("INFO" if raw["valid"] else "WARN"),
            node_id,
            f"Eval valid={raw['valid']} score={raw['score']} speedup={rec['speedup_vs_baseline']:.4f}x ({raw['eval_ms']:.0f}ms)",
        )
        return {"ok": True, **rec, "metrics": raw["metrics"]}

    async def handle_propose(self, req: dict[str, Any]) -> dict[str, Any]:
        parent_id = req.get("parent_id") or self.db.get_meta("best_node_id") or self.db.get_meta("baseline_node_id")
        parent = self.db.get_node(parent_id) if parent_id else None
        island_id = int(req["island_id"]) if req.get("island_id") is not None else (int(parent["island_id"]) if parent else 0)
        title = str(req.get("title") or "Untitled Hypothesis").strip()
        hypothesis = str(req.get("hypothesis") or "").strip()
        rationale = str(req.get("rationale") or "").strip()
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
            vault_node_dir = self.ws / "vault" / node_id
            cpath = Path(cand_str).resolve()
            vault_target = vault_node_dir / cpath.name
            self._copy_candidate_snapshot(cpath, vault_target)
            self.db.update_node_runtime(node_id, candidate_path=str(vault_target))

            if eval_res["beat_global"]:
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

            # Multiply the tree automatically:
            # (a) DEEPEN child to push hyperparameters / second-order refinements on this winner
            # (b) EXPLORE child to branch orthogonal new hypotheses from this elevated frontier
            spawned_children: list[str] = []
            deep_id, ok1, _ = self.db.create_node(
                island_id=island_id,
                role="DEEPEN",
                title=f"Deepen & scale {node['title'][:100]}",
                hypothesis=f"Push algorithmic/numerical exploitation of winning mechanism in {node_id}: {summary[:400]}",
                rationale=f"Parent {node_id} reached {eval_res['speedup_vs_baseline']:.4f}x ({eval_res['score']}). Extract second-order gains.",
                parent_id=node_id,
                novelty_threshold=None,
            )
            if ok1:
                spawned_children.append(deep_id)

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

            # (c) Cross-island CROSSOVER if another island has a distinct verified elite!
            if eval_res["beat_island"]:
                other_elites = [
                    isl for isl in self.db.get_islands()
                    if int(isl["island_id"]) != island_id and isl.get("best_node_id")
                ]
                if other_elites:
                    partner = max(other_elites, key=lambda x: float(x.get("best_speedup") or 1.0))
                    x_id, ok3, _ = self.db.create_node(
                        island_id=island_id,
                        role="CROSSOVER",
                        title=f"Crossover {node_id} x {partner['best_node_id']}",
                        hypothesis=(
                            f"Combine {node_id} ({eval_res['speedup_vs_baseline']:.4f}x on island {island_id}) "
                            f"with {partner['best_node_id']} ({partner['best_speedup']:.4f}x on island {partner['name']})."
                        ),
                        rationale="AlphaEvolve cross-island synthesis of two independent breakthroughs.",
                        parent_id=node_id,
                        secondary_parent_id=str(partner["best_node_id"]),
                        novelty_threshold=self.cfg.novelty_threshold,
                    )
                    if ok3:
                        spawned_children.append(x_id)

            hosted_res = None
            if eval_res["beat_global"] and (req_hosted or self.cfg.auto_hosted_on_sota):
                hosted_res = await self.handle_hosted_submit(
                    {"node_id": node_id, "candidate_path": str(vault_target), "note": summary}
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
        local_score = node["score"] if node else None

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
