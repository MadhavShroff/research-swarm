"""End-to-end automated unit & integration tests for `rswarm`."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from rswarm.bootstrap import bootstrap_workspace
from rswarm.dashboard import DashboardServer
from rswarm.db import SwarmDB
from rswarm.governor import ResourceGovernor
from rswarm.models import (
    SwarmConfig,
    compute_speedup_and_progress,
    make_tmux_window_name,
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
        with self.assertRaises(ValueError):
            validate_harness_choice("agy")
        self.assertEqual(validate_harness_choice("claude-mux-scale"), "claude-mux-scale")

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


if __name__ == "__main__":
    unittest.main()
