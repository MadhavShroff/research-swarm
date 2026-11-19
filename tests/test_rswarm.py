"""End-to-end automated unit & integration tests for `rswarm`."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from rswarm.bootstrap import bootstrap_workspace
from rswarm.coordinator import SwarmCoordinator
from rswarm.dashboard import DashboardServer
from rswarm.db import SwarmDB
from rswarm.governor import ResourceGovernor
from rswarm.harnesses import HarnessResult
from rswarm.models import (
    SwarmConfig,
    compute_speedup_and_progress,
    is_gemini_agy_model,
    is_rate_limit_or_transient_text,
    make_tmux_window_name,
    resolve_agy_model_id,
    validate_harness_choice,
    validate_model_choice,
)
from rswarm.rpc import EvalAndTreeService, send_rpc_request
from rswarm.tmux_pool import TmuxPool


class TestRswarmCore(unittest.IsolatedAsyncioTestCase):
    def test_model_and_harness_guards(self) -> None:
        self.assertEqual(
            validate_model_choice("anthropic-morph-dsv41flash"),
            "anthropic-morph-dsv41flash",
        )
        for forbidden in ("claude-opus-4-6", "claude-sonnet-4-6", "fable", "astra", "anthropic-gpt-6-1-sol"):
            with self.assertRaises(ValueError):
                validate_model_choice(forbidden)
        self.assertEqual(validate_harness_choice("agy"), "agy")
        self.assertEqual(validate_harness_choice("claude-mux-scale"), "claude-mux-scale")
        self.assertTrue(is_gemini_agy_model("gemini-4-carbon"))
        self.assertTrue(is_gemini_agy_model("gemini-4-carbon-high"))
        self.assertFalse(is_gemini_agy_model("anthropic-morph-dsv41flash"))
        self.assertEqual(resolve_agy_model_id("gemini-4-carbon", "max"), "gemini-4-carbon-high")
        self.assertEqual(resolve_agy_model_id("gemini-4-carbon-high", "max"), "gemini-4-carbon-high")
        self.assertEqual(resolve_agy_model_id("gemini-4-neon", "max"), "gemini-4-neon-xhigh")

    def test_live_role_model_routing_and_non_destructive_scale(self) -> None:
        from rswarm.cli import main as cli_main
        from rswarm.coordinator import SwarmCoordinator

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws-live"
            ws.mkdir(parents=True)
            cfg = SwarmConfig(name="live-test", workspace_dir=str(ws), brief="Test brief")
            self.assertEqual(cfg.model_and_effort_for_role("EXPLORE"), ("anthropic-morph-dsv41flash", "max"))
            self.assertEqual(cfg.model_and_effort_for_role("IMPLEMENT"), ("anthropic-morph-dsv41flash", "minimal"))
            self.assertEqual(cfg.model_and_effort_for_role("DEBUG"), ("anthropic-morph-dsv41flash", "minimal"))

            (ws / "swarm.json").write_text(json.dumps(cfg.to_dict(), indent=2))
            db = SwarmDB(ws / "state" / "swarm.db")
            coord = SwarmCoordinator(cfg, db)
            coord._cfg_mtime -= 5.0  # ensure mtime delta triggers reload

            # Live non-destructive scale + live per-role model switch
            self.assertEqual(cli_main(["scale", "--workspace", str(ws), "--concurrency", "11"]), 0)
            self.assertEqual(
                cli_main([
                    "set-model", "--workspace", str(ws),
                    "--role", "researcher", "--model", "anthropic-morph-kimik31", "--effort", "max",
                ]),
                0,
            )
            self.assertEqual(
                cli_main([
                    "set-model", "--workspace", str(ws),
                    "--role", "implementer", "--model", "anthropic-morph-dsv41flash", "--effort", "minimal",
                ]),
                0,
            )
            coord._reload_live_config_if_changed()
            self.assertEqual(coord.cfg.concurrency, 11)
            self.assertEqual(coord.cfg.model_and_effort_for_role("EXPLORE"), ("anthropic-morph-kimik31", "max"))
            self.assertEqual(coord.cfg.model_and_effort_for_role("IMPLEMENT"), ("anthropic-morph-dsv41flash", "minimal"))

    def test_speedup_and_window_naming(self) -> None:
        sp, prog = compute_speedup_and_progress(28.0, 28.93767, 27.60, "minimize")
        self.assertGreater(sp, 1.03)
        self.assertGreater(prog, 0.65)
        self.assertLess(prog, 1.0)

        sp_beyond, prog_beyond = compute_speedup_and_progress(27.20, 28.93767, 27.60, "minimize")
        self.assertGreater(sp_beyond, 1.06)
        self.assertGreater(prog_beyond, 1.20)

        wname = make_tmux_window_name("IMPLEMENT", "algebraic-exact", "Diamond 45deg lattice tilt", "n0014_ab12")
        self.assertTrue(wname.startswith("I-algebra-diamond-45deg-"))
        self.assertLessEqual(len(wname), 32)

    def test_governor_burst_scaling_to_hundreds(self) -> None:
        gov = ResourceGovernor(max_slice_mem_gib=75.0, min_avail_mem_gib=2.0, max_heavy_mux_procs=4)
        m1, _, _ = gov.admit("claude-mux-scale", active_heavy_procs=1, active_lite_workers=0)
        self.assertIn(m1, ("claude-mux", "claude-mux-lite"))

        # At 4 heavy procs + 180 lite workers (184 parallel agents!), governor bursts onto claude-mux-lite!
        m2, reason2, _ = gov.admit("claude-mux-scale", active_heavy_procs=4, active_lite_workers=180)
        self.assertEqual(m2, "claude-mux-lite", msg=reason2)

        # Strict slice ceiling triggers WAIT
        tight_gov = ResourceGovernor(max_slice_mem_gib=0.001, min_avail_mem_gib=2.0)
        m3, _, _ = tight_gov.admit("claude-mux-scale", active_heavy_procs=0, active_lite_workers=0)
        self.assertEqual(m3, "WAIT")

    async def test_bootstrap_rpc_skills_multiplying_tree_and_radial_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws-demo"
            src_eval = tmp_path / "local_eval.sh"
            src_base = tmp_path / "candidate.json"

            # Toy minimize problem: score = sum of values in JSON list; must have length 3 and all > 0.
            src_eval.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
python3 - "$1" <<'PY'
import json, sys, time
p = sys.argv[1]
arr = json.load(open(p))
if not isinstance(arr, list) or len(arr) != 3 or any(x <= 0 for x in arr):
    print(json.dumps({"valid": False, "score": None, "diagnostics": "Expected 3 positive numbers"}))
    sys.exit(0)
print(json.dumps({"valid": True, "score": float(sum(arr)), "eval_ms": 1.2, "diagnostics": "verified"}))
PY
"""
            )
            src_eval.chmod(0o755)
            src_base.write_text(json.dumps([10.0, 10.0, 10.0]))  # baseline = 30.0

            cfg = SwarmConfig(
                name="unit-pack",
                workspace_dir=str(ws),
                brief="Minimize sum of 3 positive numbers toward target 15.0.",
                direction="minimize",
                leaderboard_target=15.0,
                num_islands=3,
                hosted_quota_per_day=1,
                dashboard_host="127.0.0.1",
                dashboard_port=18991,
            )
            db = SwarmDB(ws / "state" / "swarm.db")
            rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"

            boot = await bootstrap_workspace(
                cfg,
                db,
                rbin,
                local_eval_src=str(src_eval),
                baseline_src=str(src_base),
            )
            self.assertTrue(boot["bootstrapped"])
            self.assertEqual(boot["baseline_score"], 30.0)
            self.assertTrue((ws / "bin" / "swarm-eval").is_file())
            self.assertTrue((ws / ".claude" / "skills" / "swarm-propose" / "SKILL.md").is_file())

            svc = EvalAndTreeService(cfg, db)
            await svc.start_rpc_server()
            try:
                # 1. Propose novel hypothesis via RPC socket
                p1 = await asyncio.to_thread(
                    send_rpc_request,
                    svc.sock_path,
                    {
                        "action": "propose",
                        "island_id": 0,
                        "role": "IMPLEMENT",
                        "title": "Halve geometric spacing via shear packing",
                        "hypothesis": "Apply 30-degree shear transformation to shrink outer box.",
                        "rationale": "Reduces dead corner area by 33%.",
                    },
                )
                self.assertTrue(p1["accepted"])
                child_id = p1["child_node_id"]

                # 2. Propose near-duplicate -> rejected by ShinkaEvolve novelty gate!
                p_dup = await asyncio.to_thread(
                    send_rpc_request,
                    svc.sock_path,
                    {
                        "action": "propose",
                        "island_id": 0,
                        "role": "IMPLEMENT",
                        "title": "Halve geometric spacing via shear packing",
                        "hypothesis": "Apply 30-degree shear transformation to shrink outer box.",
                        "rationale": "Same idea.",
                    },
                )
                self.assertFalse(p_dup["accepted"])

                # 3. Submit improved candidate on island 0 -> promotes to vault + spawns DEEPEN & EXPLORE!
                better_cand = tmp_path / "cand_win.json"
                better_cand.write_text(json.dumps([6.0, 6.0, 6.0]))  # score = 18.0 (1.6667x!)
                sub_res = await asyncio.to_thread(
                    send_rpc_request,
                    svc.sock_path,
                    {
                        "action": "submit",
                        "node_id": child_id,
                        "candidate_path": str(better_cand),
                        "summary": "Reduced score from 30.0 to 18.0",
                        "request_hosted": True,
                    },
                )
                self.assertTrue(sub_res["vault_promoted"])
                self.assertAlmostEqual(sub_res["speedup_vs_baseline"], 30.0 / 18.0, places=4)
                self.assertGreaterEqual(len(sub_res["spawned_children"]), 2)

                # 4. Submit broken candidate -> spawns bounded DEBUG repair child!
                p_bad = await asyncio.to_thread(
                    send_rpc_request,
                    svc.sock_path,
                    {
                        "action": "propose",
                        "island_id": 1,
                        "role": "IMPLEMENT",
                        "title": "Continuous penalty homotopy on Fourier basis",
                        "hypothesis": "Expand coordinates in Chebyshev polynomials.",
                    },
                )
                bad_cand = tmp_path / "cand_bad.json"
                bad_cand.write_text(json.dumps([-1.0, 2.0]))  # invalid!
                bad_res = await asyncio.to_thread(
                    send_rpc_request,
                    svc.sock_path,
                    {
                        "action": "submit",
                        "node_id": p_bad["child_node_id"],
                        "candidate_path": str(bad_cand),
                        "summary": "Broken negative coordinate test",
                    },
                )
                self.assertFalse(bad_res["valid"])
                self.assertIsNotNone(bad_res["spawned_debug_node"])

                # 5. Verify Radial Dashboard state & HTTP endpoints
                gov = ResourceGovernor()
                tpool = TmuxPool("rswarm-unit-test", ws, keep_window_sec=0.1)
                dash = DashboardServer(cfg=cfg, db=db, governor=gov, tmux_pool=tpool)
                url = await dash.start()
                try:
                    html = await asyncio.to_thread(
                        lambda: urllib.request.urlopen(f"{url}/", timeout=5).read().decode()
                    )
                    self.assertIn("radialSvg", html)
                    api_state = await asyncio.to_thread(
                        lambda: json.loads(urllib.request.urlopen(f"{url}/api/state", timeout=5).read().decode())
                    )
                    st_out = api_state["stats"]
                    self.assertGreaterEqual(st_out["hypotheses_created"], 4)
                    self.assertGreaterEqual(st_out["hypotheses_tested"], 2)
                    self.assertGreater(st_out["avg_eval_ms"], 0.0)
                    nodes_by_id = {n["node_id"]: n for n in api_state["nodes"]}
                    root_n = nodes_by_id[boot["baseline_node_id"]]
                    win_n = nodes_by_id[child_id]
                    self.assertEqual(root_n["radius_norm"], 0.0)
                    self.assertGreater(win_n["radius_norm"], 0.65)
                finally:
                    await dash.stop()

                # 6. Verify named tmux window lifecycle (spawn -> log -> clean reap)
                tscript = tmp_path / "hello_agent.sh"
                tscript.write_text("#!/usr/bin/env bash\necho 'HELLO_FROM_NAMED_TMUX_WINDOW'\n")
                tscript.chmod(0o755)
                tlog = ws / "logs" / "tmux_test.log"
                texit = tmp_path / "tmux_test.exit"
                rc, _, timed_out = await tpool.run_script_in_window(
                    window_name="I-alg-unit-probe-01",
                    node_id="tmux_probe",
                    workdir=tmp_path,
                    script_path=tscript,
                    log_path=tlog,
                    exit_file=texit,
                    timeout_sec=10,
                )
                self.assertEqual(rc, 0)
                self.assertFalse(timed_out)
                self.assertIn("HELLO_FROM_NAMED_TMUX_WINDOW", tlog.read_text())
                self.assertEqual(tpool.list_live_window_names(), [])
                tpool.cleanup_all_sync(kill_session=True)
            finally:
                await svc.stop_rpc_server()

    async def test_leaderboard_number_one_and_top5_submission_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws-gate"
            local_ev = tmp_path / "local_eval.sh"
            local_ev.write_text(
                '#!/usr/bin/env bash\n'
                'SCORE="$(cat "$1")"\n'
                'printf \'{"valid": true, "score": %s, "eval_ms": 1.2, "diagnostics": "ok", "metrics": {}}\\n\' "$SCORE"\n'
            )
            local_ev.chmod(0o755)
            hosted_ev = tmp_path / "hosted_submit.sh"
            hosted_ev.write_text(
                '#!/usr/bin/env bash\n'
                'echo \'{"submitted": true, "hosted_score": 22.05, "message": "submitted to kaggle"}\'\n'
            )
            hosted_ev.chmod(0o755)
            base_c = tmp_path / "cand.txt"
            base_c.write_text("28.9377\n")

            cfg = SwarmConfig(
                name="ws-gate",
                workspace_dir=str(ws),
                brief="Minimize packing score.",
                direction="minimize",
                leaderboard_target=27.2,
                leaderboard_number_one=22.039951,
                leaderboard_submit_threshold=22.056609,
                concurrency=10,
            )
            db = SwarmDB(ws / "state" / "swarm.db")
            rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
            boot = await bootstrap_workspace(
                cfg,
                db,
                rbin,
                local_eval_src=str(local_ev),
                hosted_eval_src=str(hosted_ev),
                baseline_src=str(base_c),
            )
            svc = EvalAndTreeService(cfg, db)

            # 1. Breakthrough at 26.5000 (beats baseline, NOT yet in Top 5 <= 22.056609) -> held by gate!
            n1, _, _ = db.create_node(
                island_id=0,
                role="IMPLEMENT",
                title="Mid gain",
                parent_id=boot["baseline_node_id"],
                novelty_threshold=None,
            )
            c1 = tmp_path / "c1.txt"
            c1.write_text("26.5000\n")
            r1 = await svc.handle_submit(
                {"node_id": n1, "candidate_path": str(c1), "summary": "26.5 gain", "request_hosted": True}
            )
            self.assertTrue(r1["vault_promoted"])
            self.assertTrue(r1["hosted_submission"]["gated"])
            self.assertFalse(r1["hosted_submission"]["submitted"])
            self.assertEqual(db.count_recent_hosted_submissions(86400.0), 0)

            # 2. Breakthrough at 22.0500: call run_local_eval FIRST (like swarm-eval), then handle_submit!
            n2, _, _ = db.create_node(
                island_id=0,
                role="DEEPEN",
                title="Top-5 breakthrough",
                parent_id=n1,
                novelty_threshold=None,
            )
            c2 = tmp_path / "c2.txt"
            c2.write_text("22.0500\n")
            pre_eval = await svc.run_local_eval(node_id=n2, candidate_path=str(c2))
            self.assertTrue(pre_eval["beat_global"])
            r2 = await svc.handle_submit(
                {"node_id": n2, "candidate_path": str(c2), "summary": "Entered Top 5!"}
            )
            self.assertTrue(r2["beat_global"])
            self.assertTrue(r2["vault_promoted"])
            self.assertTrue(r2["hosted_submission"]["submitted"])
            self.assertEqual(db.count_recent_hosted_submissions(86400.0), 1)

    async def test_429_retry_and_checkpoint_resume_on_timeout(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from rswarm.coordinator import SwarmCoordinator

        calls: list[dict] = []

        class MockMuxHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode())
                calls.append(body)
                n = len(calls)
                if n == 1:
                    # 1st call: simulate saturated 429 with Retry-After: 1
                    self.send_response(429)
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    self.wfile.write(b'{"error":"rate_limited"}')
                    return
                if n == 2:
                    # 2nd call (same turn 1 after 429 retry!): write improved candidate + state file BEFORE timeout!
                    resp = {
                        "stop_reason": "tool_use",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_1",
                                "name": "write_file",
                                "input": {"path": "step1_state.txt", "content": "PARTIAL_SOLVER_READY"},
                            },
                            {
                                "type": "tool_use",
                                "id": "tu_2",
                                "name": "write_file",
                                "input": {"path": "candidate/cand.txt", "content": "27.5\n"},
                            },
                        ],
                    }
                elif n == 3:
                    # 3rd call: hold past 5s window to trigger timeout checkpoint!
                    import time as _t
                    _t.sleep(3.6)
                    self.send_response(429)
                    self.send_header("Retry-After", "10")
                    self.end_headers()
                    self.wfile.write(b'{"error":"still_saturated"}')
                    return
                else:
                    # 4th call (AFTER coordinator re-queues & resumes from checkpoint!):
                    # Verify previous messages + step1_state.txt survived, then finish!
                    resp = {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "Resumed cleanly from checkpoint and finished!"}],
                    }
                raw = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = HTTPServer(("127.0.0.1", 0), MockMuxHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                ws = tmp_path / "ws-resume"
                local_ev = tmp_path / "local_eval.sh"
                local_ev.write_text('#!/usr/bin/env bash\nS="$(cat "$1")"\nprintf \'{"valid": true, "score": %s, "eval_ms": 1.0, "diagnostics": "ok", "metrics": {}}\\n\' "$S"\n')
                local_ev.chmod(0o755)
                hosted_ev = tmp_path / "hosted_submit.sh"
                hosted_ev.write_text('#!/usr/bin/env bash\necho \'{"submitted": false}\'\n')
                hosted_ev.chmod(0o755)
                base_c = tmp_path / "cand.txt"
                base_c.write_text("28.9\n")

                cfg = SwarmConfig(
                    name="ws-resume",
                    workspace_dir=str(ws),
                    brief="Test 429 & checkpoint resume.",
                    harness="claude-mux-lite",
                    agent_timeout_sec=5,
                    http_timeout_sec=5,
                    max_timeout_retries=2,
                    keep_window_sec=0.1,
                )
                db = SwarmDB(ws / "state" / "swarm.db")
                rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
                boot = await bootstrap_workspace(
                    cfg, db, rbin, local_eval_src=str(local_ev), hosted_eval_src=str(hosted_ev), baseline_src=str(base_c)
                )
                coord = SwarmCoordinator(cfg, db)
                import unittest.mock as mock
                with mock.patch("rswarm.harnesses._load_mux_proxy_credentials", return_value=(f"http://127.0.0.1:{port}", "tok")):
                    nid, _, _ = db.create_node(
                        island_id=0,
                        role="IMPLEMENT",
                        title="Timeout resume probe",
                        parent_id=boot["baseline_node_id"],
                        novelty_threshold=None,
                    )
                    claimed1 = db.claim_next_queued_node(preferred_island=0, exclude_roles=("EXPLORE",))
                    self.assertEqual(claimed1["node_id"], nid)

                    # Run 1: survives initial 429, writes step1_state.txt, hits 429 at window end, saves checkpoint & re-queues WITHOUT burning timeout_retries!
                    await coord._run_node_worker(claimed1, "claude-mux-lite")
                    after1 = db.get_node(nid)
                    self.assertEqual(after1["status"], "QUEUED")
                    self.assertEqual(after1["timeout_retries"], 0)
                    self.assertTrue((ws / "work" / nid / "step1_state.txt").is_file())
                    self.assertTrue((ws / "work" / nid / ".rswarm_checkpoint.json").is_file())

                    # Run 2: picks up same node, preserves step1_state.txt + prior messages, and completes!
                    cfg.agent_timeout_sec = 10
                    cfg.http_timeout_sec = 10
                    claimed2 = db.claim_next_queued_node(preferred_island=0, exclude_roles=("EXPLORE",))
                    self.assertEqual(claimed2["node_id"], nid)
                    await coord._run_node_worker(claimed2, "claude-mux-lite")
                    after2 = db.get_node(nid)
                    self.assertEqual(after2["status"], "BREAKTHROUGH")
                    # Call #4 received multi-turn history from checkpoint!
                    self.assertGreaterEqual(len(calls[-1]["messages"]), 3)
                coord.tmux_pool.cleanup_all_sync(kill_session=True)
        finally:
            srv.shutdown()
            srv.server_close()

    async def test_priority_unstarvation_and_pool_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws-pool"
            local_ev = tmp_path / "local_eval.sh"
            local_ev.write_text(
                '#!/usr/bin/env bash\n'
                'python3 - "$1" <<\'PY\'\n'
                'import json, sys\n'
                'd = json.loads(open(sys.argv[1]).read())\n'
                'sc = float(d.get("a", 10.0)) + float(d.get("b", 10.0))\n'
                'print(json.dumps({"valid": True, "score": sc, "eval_ms": 1.0, "diagnostics": "ok", "metrics": {}}))\n'
                'PY\n'
            )
            local_ev.chmod(0o755)
            hosted_ev = tmp_path / "hosted_submit.sh"
            hosted_ev.write_text('#!/usr/bin/env bash\necho \'{"submitted": false}\'\n')
            hosted_ev.chmod(0o755)
            base_c = tmp_path / "overrides.json"
            base_c.write_text('{"a": 10.0, "b": 10.0}\n')

            mp = tmp_path / "merge_pool.sh"
            mp.write_text(
                '#!/usr/bin/env bash\n'
                'python3 - "$1" "$2" <<\'PY\'\n'
                'import json, sys\n'
                'from pathlib import Path\n'
                'cand = json.loads(Path(sys.argv[1]).read_text())\n'
                'best_f = Path(sys.argv[2]) / "overrides.json"\n'
                'cur = json.loads(best_f.read_text()) if best_f.is_file() else {"a": 10.0, "b": 10.0}\n'
                'cnt = 0\n'
                'for k in ("a", "b"):\n'
                '    if k in cand and float(cand[k]) < float(cur.get(k, 10.0)):\n'
                '        cur[k] = float(cand[k]); cnt += 1\n'
                'best_f.write_text(json.dumps(cur))\n'
                'sc = float(cur["a"]) + float(cur["b"])\n'
                'print(json.dumps({"valid": True, "merged_count": cnt, "score": sc, "best_path": str(best_f), "diagnostics": "merged"}))\n'
                'PY\n'
            )
            mp.chmod(0o755)

            cfg = SwarmConfig(name="ws-pool", workspace_dir=str(ws), brief="Minimize a+b.", direction="minimize")
            db = SwarmDB(ws / "state" / "swarm.db")
            rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
            boot = await bootstrap_workspace(
                cfg,
                db,
                rbin,
                local_eval_src=str(local_ev),
                hosted_eval_src=str(hosted_ev),
                merge_pool_src=str(mp),
                baseline_src=str(base_c),
            )
            self.assertTrue((ws / "eval" / "merge_pool.sh").is_file())
            self.assertTrue((ws / "vault" / "solvers").is_dir())

            # 1. Verify META & IMPLEMENT take primary throughput while reserved slots serve DEEPEN/CROSSOVER!
            imp_id, _, _ = db.create_node(island_id=0, role="IMPLEMENT", title="Imp", parent_id=boot["baseline_node_id"], novelty_threshold=None)
            deep_id, _, _ = db.create_node(island_id=0, role="DEEPEN", title="Deep", parent_id=boot["baseline_node_id"], novelty_threshold=None)
            cross_id, _, _ = db.create_node(island_id=0, role="CROSSOVER", title="Cross", parent_id=boot["baseline_node_id"], novelty_threshold=None)
            meta_id, _, _ = db.create_node(island_id=0, role="META", title="Meta", parent_id=boot["baseline_node_id"], novelty_threshold=None)

            c_meta = db.claim_next_queued_node(preferred_island=0, exclude_roles=("EXPLORE",))
            c_first = db.claim_next_queued_node(preferred_island=0, exclude_roles=("EXPLORE",))
            c_second = db.claim_next_queued_node(preferred_island=0, exclude_roles=("EXPLORE", "IMPLEMENT"))
            self.assertEqual(c_meta["node_id"], meta_id)
            self.assertEqual(c_first["node_id"], imp_id)
            self.assertEqual(c_second["node_id"], deep_id)

            # 2. Verify disjoint sub-instance wins ("a"=6 on imp_id, "b"=5 on deep_id) combine to score 11.0!
            svc = EvalAndTreeService(cfg, db)
            f1 = tmp_path / "c_a.json"
            f1.write_text('{"a": 6.0, "b": 10.0}\n')
            await svc.handle_submit({"node_id": imp_id, "candidate_path": str(f1), "summary": "improved a"})
            self.assertAlmostEqual(float(db.get_meta("best_score")), 16.0, places=4)

            f2 = tmp_path / "c_b.json"
            f2.write_text('{"a": 10.0, "b": 5.0}\n')
            await svc.handle_submit({"node_id": deep_id, "candidate_path": str(f2), "summary": "improved b"})
            self.assertAlmostEqual(float(db.get_meta("best_score")), 11.0, places=4)

    async def test_rate_limit_never_abandoned_and_resurrects_failed_nodes(self) -> None:
        msg = (
            "API Error: Server is temporarily limiting requests (not your usage limit) · "
            "This request would exceed your account's rate limit. Please try again later."
        )
        self.assertTrue(is_rate_limit_or_transient_text(msg))
        self.assertFalse(is_rate_limit_or_transient_text("SyntaxError: invalid decimal literal"))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws-rl"
            local_ev = tmp_path / "local_eval.sh"
            local_ev.write_text('#!/usr/bin/env bash\necho \'{"valid": true, "score": 10.0, "eval_ms": 1.0, "diagnostics": "ok", "metrics": {}}\'\n')
            local_ev.chmod(0o755)
            hosted_ev = tmp_path / "hosted_submit.sh"
            hosted_ev.write_text('#!/usr/bin/env bash\necho \'{"submitted": false}\'\n')
            hosted_ev.chmod(0o755)
            base_c = tmp_path / "cand.json"
            base_c.write_text("{}\n")

            cfg = SwarmConfig(name="ws-rl", workspace_dir=str(ws), brief="Test RL.", direction="minimize")
            db = SwarmDB(ws / "state" / "swarm.db")
            rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
            boot = await bootstrap_workspace(
                cfg, db, rbin,
                local_eval_src=str(local_ev),
                hosted_eval_src=str(hosted_ev),
                baseline_src=str(base_c),
            )

            # 1. Historical FAILED node with rate-limit log/summary is resurrected to QUEUED; invalid-eval FAILED stays FAILED!
            rl_nid, _, _ = db.create_node(island_id=0, role="EXPLORE", title="RL victim", parent_id=boot["baseline_node_id"], status="FAILED", novelty_threshold=None)
            db.update_node_runtime(rl_nid, code_summary=msg, finished=True)
            bad_nid, _, _ = db.create_node(island_id=0, role="IMPLEMENT", title="Broken code", parent_id=boot["baseline_node_id"], novelty_threshold=None)
            db.record_evaluation(bad_nid, valid=False, score=None, eval_ms=12.0, diagnostics="SyntaxError", finalize=True)

            rescued = db.recover_rate_limited_nodes(ws / "logs")
            self.assertEqual(rescued, 1)
            self.assertEqual(db.get_node(rl_nid)["status"], "QUEUED")
            self.assertEqual(db.get_node(bad_nid)["status"], "FAILED")

            # 2. Live worker hitting rate_limited=True is re-queued WITHOUT burning timeout_retries or failing!
            coord = SwarmCoordinator(cfg, db)
            db.update_node_runtime(rl_nid, status="RUNNING")
            claimed = db.get_node(rl_nid)

            async def _fake_rl_run(**kwargs):
                return HarnessResult(
                    exit_code=75,
                    elapsed_sec=3.0,
                    timed_out=False,
                    harness_used="claude-mux",
                    model_used=cfg.researcher_model,
                    tmux_window="R-test",
                    final_text=msg,
                    log_path=str(ws / "logs" / f"{rl_nid}.log"),
                    rate_limited=True,
                )

            coord.runner.run_agent = _fake_rl_run  # type: ignore[method-assign]
            await coord._run_node_worker(claimed, "claude-mux")
            after = db.get_node(rl_nid)
            self.assertEqual(after["status"], "QUEUED")
            self.assertEqual(int(after["timeout_retries"] or 0), 0)
            self.assertGreater(coord._model_cooldown_until.get(cfg.researcher_model, 0.0), 0.0)

    async def test_seed_relative_breakthrough_gate_and_meta_directives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws-seed"
            local_ev = tmp_path / "local_eval.sh"
            local_ev.write_text(
                '#!/usr/bin/env bash\n'
                'SCORE="$(cat "$1")"\n'
                'printf \'{"valid": true, "score": %s, "eval_ms": 1.0, "diagnostics": "ok", "metrics": {"reducible_by_regime": {"upper_shell": 7.0, "mid_shell": 13.7}}}\\n\' "$SCORE"\n'
            )
            local_ev.chmod(0o755)
            hosted_ev = tmp_path / "hosted_submit.sh"
            hosted_ev.write_text('#!/usr/bin/env bash\necho \'{"submitted": false}\'\n')
            hosted_ev.chmod(0o755)
            base_c = tmp_path / "cand.txt"
            base_c.write_text("28.9377\n")

            cfg = SwarmConfig(name="ws-seed", workspace_dir=str(ws), brief="Minimize.", direction="minimize", leaderboard_target=22.04)
            db = SwarmDB(ws / "state" / "swarm.db")
            rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
            boot = await bootstrap_workspace(
                cfg, db, rbin,
                local_eval_src=str(local_ev),
                hosted_eval_src=str(hosted_ev),
                baseline_src=str(base_c),
            )
            svc = EvalAndTreeService(cfg, db)

            # Parent scored 22.986, while pool best_score advanced to 22.754.
            p_id, _, _ = db.create_node(island_id=1, role="IMPLEMENT", title="Parent 22.986", parent_id=boot["baseline_node_id"], novelty_threshold=None)
            db.record_evaluation(p_id, valid=True, score=22.986, eval_ms=1.0, diagnostics="ok", finalize=True)
            db.set_meta("best_score", 22.754)

            # Child seeded from pool (seed_score=22.754) submitting 22.754 (zero delta!) MUST be OBSTRUCTED, NOT BREAKTHROUGH!
            z_id, _, _ = db.create_node(island_id=2, role="DEEPEN", title="Zero delta pool copy", parent_id=p_id, seed_score=22.754, novelty_threshold=None)
            cz = tmp_path / "cz.txt"
            cz.write_text("22.754\n")
            rz = await svc.handle_submit({"node_id": z_id, "candidate_path": str(cz), "summary": "No new packing"})
            self.assertFalse(rz["is_breakthrough"])
            self.assertEqual(db.get_node(z_id)["status"], "OBSTRUCTED")

            # Child seeded at 22.754 that strictly improves to 22.610 IS a real BREAKTHROUGH and populates gap_slices!
            w_id, _, _ = db.create_node(island_id=2, role="IMPLEMENT", title="Gobel upper shell", parent_id=p_id, seed_score=22.754, novelty_threshold=None)
            cw = tmp_path / "cw.txt"
            cw.write_text("22.610\n")
            rw = await svc.handle_submit({"node_id": w_id, "candidate_path": str(cw), "summary": "Gobel strip gain"})
            self.assertTrue(rw["is_breakthrough"])
            self.assertEqual(db.get_node(w_id)["status"], "BREAKTHROUGH")
            self.assertGreaterEqual(len(db.get_gap_slices()), 2)

            # Verify strategy directive RPC & prompt injection!
            d_res = await svc.handle_directive({"node_id": w_id, "directive": "Focus on k^2+k+1..k^2+2k Gobel strips."})
            self.assertTrue(d_res["ok"])
            self.assertIn("Gobel strips", db.get_strategy_directives()[0])

    async def test_literature_fetch_dynamic_islands_and_affordances(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from rswarm.harnesses import HarnessRunner
        from rswarm.research_tools import fetch_paper_or_url

        class PaperHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                pass

            def do_GET(self) -> None:
                body = b"<html><body><h1>Packing Squares Below Integer</h1><p>Theorem 1: W = O(k^{7/11}).</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), PaperHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                ws = tmp_path / "ws-lit"
                local_ev = tmp_path / "local_eval.sh"
                local_ev.write_text('#!/usr/bin/env bash\necho \'{"valid": true, "score": 25.0, "eval_ms": 1.0, "diagnostics": "ok", "metrics": {}}\'\n')
                local_ev.chmod(0o755)
                hosted_ev = tmp_path / "hosted_submit.sh"
                hosted_ev.write_text('#!/usr/bin/env bash\necho \'{"submitted": false}\'\n')
                hosted_ev.chmod(0o755)
                base_c = tmp_path / "cand.json"
                base_c.write_text("{}\n")

                cfg = SwarmConfig(name="ws-lit", workspace_dir=str(ws), brief="Lit test.", max_active_islands=5)
                db = SwarmDB(ws / "state" / "swarm.db")
                rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
                await bootstrap_workspace(
                    cfg, db, rbin,
                    local_eval_src=str(local_ev),
                    hosted_eval_src=str(hosted_ev),
                    baseline_src=str(base_c),
                )
                for wrap in ("swarm-search", "swarm-fetch", "swarm-check", "swarm-island"):
                    self.assertTrue((ws / "bin" / wrap).is_file())

                # 1. Verify fetch_paper_or_url saves clean text + updates INDEX.md
                lit_dir = ws / "shared" / "literature"
                fres = fetch_paper_or_url(f"http://127.0.0.1:{port}/paper.html", literature_dir=lit_dir, save_slug="below-integer")
                self.assertTrue(fres["ok"])
                self.assertIn("Theorem 1: W = O(k^{7/11})", fres["excerpt"])
                self.assertTrue((lit_dir / "below-integer.md").is_file())
                self.assertIn("below-integer.md", (lit_dir / "INDEX.md").read_text())

                # 2. Verify capped dynamic island replacement retires the most-stagnant island!
                with db._lock, db._connect() as conn:
                    conn.execute("UPDATE islands SET consecutive_obstructions=18 WHERE island_id=4")
                svc = EvalAndTreeService(cfg, db)
                ires = await svc.handle_island({
                    "node_id": "meta-test",
                    "name": "weierstrass-rational",
                    "focus": "Exact rational t = tan(theta/2) contact systems.",
                })
                self.assertTrue(ires["created"])
                self.assertEqual(ires["retired_island_id"], 4)
                active_ids = [int(i["island_id"]) for i in db.get_islands()]
                self.assertNotIn(4, active_ids)
                self.assertEqual(len(active_ids), 5)

                # 3. Verify isolated TMPDIR + partial stdout returned on bash timeout!
                tpool = TmuxPool("rswarm-lit-test", ws)
                runner = HarnessRunner(workspace_dir=ws, tmux_pool=tpool)
                wdir = ws / "work" / "n_probe"
                wdir.mkdir(parents=True)
                out_tmp = await runner._exec_lite_tool("bash", {"command": "echo $TMPDIR"}, wdir, "n_probe", False)
                self.assertIn(str(wdir / ".tmp"), out_tmp)
                out_to = await runner._exec_lite_tool(
                    "bash",
                    {"command": "echo 'STEP_PROGRESS_42'; sleep 10", "timeout_sec": 5},
                    wdir,
                    "n_probe",
                    False,
                )
                self.assertIn("timed out after 5s", out_to)
                self.assertIn("STEP_PROGRESS_42", out_to)
        finally:
            srv.shutdown()

    async def test_frontier_novelty_doctrine_and_scheduler_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws-novelty"
            local_ev = tmp_path / "local_eval.sh"
            local_ev.write_text('#!/usr/bin/env bash\necho \'{"valid": true, "score": 22.1, "eval_ms": 1.0, "diagnostics": "ok", "metrics": {}}\'\n')
            local_ev.chmod(0o755)
            base_c = tmp_path / "cand.json"
            base_c.write_text("{}\n")

            cfg = SwarmConfig(name="ws-novelty", workspace_dir=str(ws), brief="Test frontier novelty priority.", num_islands=2)
            db = SwarmDB(ws / "state" / "swarm.db")
            rbin = Path(__file__).resolve().parent.parent / "bin" / "rswarm"
            boot = await bootstrap_workspace(cfg, db, rbin, local_eval_src=str(local_ev), baseline_src=str(base_c))
            svc = EvalAndTreeService(cfg, db)
            coord = SwarmCoordinator(cfg, db)

            # 1. Queue an auto_replenish fallback IMPLEMENT node + a novel researcher IMPLEMENT node
            auto_id, _, _ = db.create_node(
                island_id=0,
                role="IMPLEMENT",
                title="Auto fallback builder",
                hypothesis="Generic fallback run.",
                origin="auto_replenish",
                parent_id=boot["baseline_node_id"],
                novelty_threshold=None,
            )
            prop = await svc.handle_propose({
                "parent_id": boot["baseline_node_id"],
                "island_id": 0,
                "role": "IMPLEMENT",
                "rationale": "Known single-angle strip templates stall at W = O(L) due to rigid parallel pitch.",
                "novel_insight": "Break uniform-angle assumption via chirped curvature-varying contact foliations.",
                "hypothesis": "Construct continuous chirped-angle foliation coupled with active contact-graph eigenmode relaxation.",
                "target_slice": "k=18..31",
                "title": "Chirped contact foliation eigenmode solver",
            })
            self.assertTrue(prop["accepted"])
            novel_id = prop["child_node_id"]
            novel_node = db.get_node(novel_id)
            self.assertIn("chirped curvature-varying", novel_node["novel_insight"])
            self.assertGreater(float(novel_node["novelty_score"]), 0.5)

            # 2. Verify prompts embed FRONTIER NOVELTY AXIOM and Core Novel Frontier Insight!
            isl0 = db.get_islands()[0]
            sys_p, task_p = coord._build_prompts(novel_node, isl0, None)
            self.assertIn("FRONTIER NOVELTY AXIOM", sys_p)
            self.assertIn("Known & Online Methods Cannot Win", task_p)
            self.assertIn("Core Novel Frontier Insight to Realize", task_p)
            self.assertIn("chirped curvature-varying", task_p)

            # 3. Verify boost_explore=True claims freshest EXPLORE ahead of IMPLEMENT so researchers never starve!
            for idx in range(5):
                db.create_node(
                    island_id=0,
                    role="EXPLORE",
                    title=f"Explore wave #{idx}",
                    parent_id=boot["baseline_node_id"],
                    novelty_threshold=None,
                )
            pruned = db.prune_stale_queued_nodes(max_explore_per_island=3)
            self.assertGreaterEqual(pruned, 2)

            claimed_exp = db.claim_next_queued_node(preferred_island=0, boost_explore=True)
            self.assertEqual(claimed_exp["role"], "EXPLORE")
            self.assertEqual(claimed_exp["title"], "Explore wave #4")  # freshest frontier EXPLORE!

            # 4. Once an EXPLORE is active (boost_explore=False), novel researcher IMPLEMENT beats auto_replenish!
            claimed_imp = db.claim_next_queued_node(preferred_island=0, boost_explore=False)
            self.assertEqual(claimed_imp["node_id"], novel_id)
            self.assertNotEqual(claimed_imp["node_id"], auto_id)


if __name__ == "__main__":
    unittest.main()
