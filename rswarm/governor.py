"""Sparky RAM & CPU Admission Governor for 100s of parallel coding agents."""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path


@dataclasses.dataclass
class ResourceSnapshot:
    timestamp: float
    mem_total_gib: float
    mem_avail_gib: float
    slice_current_gib: float
    slice_anon_gib: float
    slice_high_gib: float
    slice_max_gib: float
    load_1m: float
    load_5m: float
    cpu_count: int

    def to_dict(self) -> dict[str, float | int]:
        return dataclasses.asdict(self)


class ResourceGovernor:
    """Monitors Linux /proc/meminfo, user-1000.slice cgroup v2, and CPU loadavg.

    Keeps Sparky well below user-1000.slice MemoryHigh (78 GiB) and prevents CPU
    stampedes while allowing 100-300+ network-bound DeepSeek agents to run concurrently.
    """

    def __init__(
        self,
        max_slice_mem_gib: float = 60.0,
        min_avail_mem_gib: float = 20.0,
        max_heavy_mux_procs: int = 24,
        max_load_factor: float = 1.65,
    ) -> None:
        self.max_slice_mem_gib = max_slice_mem_gib
        self.min_avail_mem_gib = min_avail_mem_gib
        self.max_heavy_mux_procs = max_heavy_mux_procs
        self.max_load_factor = max_load_factor
        self.uid = os.getuid()
        self.cgroup_dir = Path(f"/sys/fs/cgroup/user.slice/user-{self.uid}.slice")
        self._last_snap: ResourceSnapshot | None = None
        self._last_ts: float = 0.0

    def _read_cgroup_bytes(self, filename: str, fallback_gib: float) -> float:
        p = self.cgroup_dir / filename
        try:
            raw = p.read_text().strip()
            if raw == "max" or not raw:
                return fallback_gib
            return int(raw) / (1024.0**3)
        except OSError:
            return fallback_gib
        except ValueError:
            return fallback_gib

    def sample(self, max_age_sec: float = 0.75) -> ResourceSnapshot:
        now = time.monotonic()
        if self._last_snap is not None and (now - self._last_ts) < max_age_sec:
            return self._last_snap

        mem_total_kb = 128 * 1024 * 1024
        mem_avail_kb = 64 * 1024 * 1024
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail_kb = int(line.split()[1])
        except OSError:
            pass

        mem_total_gib = mem_total_kb / (1024.0**2)
        mem_avail_gib = mem_avail_kb / (1024.0**2)

        raw_slice_gib = self._read_cgroup_bytes(
            "memory.current", max(0.0, mem_total_gib - mem_avail_gib)
        )
        # Subtract reclaimable `inactive_file` page cache from cgroup v2 `memory.current`
        # so clean file cache does not masquerade as RSS bloat!
        inactive_file_gib = 0.0
        anon_gib = 0.0
        try:
            for sline in (self.cgroup_dir / "memory.stat").read_text().splitlines():
                if sline.startswith("inactive_file "):
                    inactive_file_gib = int(sline.split()[1]) / (1024.0**3)
                elif sline.startswith("anon "):
                    anon_gib = int(sline.split()[1]) / (1024.0**3)
        except OSError:
            pass

        slice_current_gib = max(anon_gib, raw_slice_gib - inactive_file_gib)
        slice_high_gib = self._read_cgroup_bytes("memory.high", 106.0)
        slice_max_gib = self._read_cgroup_bytes("memory.max", 116.0)

        try:
            load_1m, load_5m, _ = os.getloadavg()
        except OSError:
            load_1m, load_5m = 0.0, 0.0

        cpus = os.cpu_count() or 20
        snap = ResourceSnapshot(
            timestamp=time.time(),
            mem_total_gib=round(mem_total_gib, 2),
            mem_avail_gib=round(mem_avail_gib, 2),
            slice_current_gib=round(slice_current_gib, 2),
            slice_anon_gib=round(anon_gib, 2),
            slice_high_gib=round(slice_high_gib, 2),
            slice_max_gib=round(slice_max_gib, 2),
            load_1m=round(load_1m, 2),
            load_5m=round(load_5m, 2),
            cpu_count=cpus,
        )
        self._last_snap = snap
        self._last_ts = now
        return snap

    def admit(
        self,
        configured_harness: str,
        active_heavy_procs: int,
        active_lite_workers: int,
    ) -> tuple[str, str, ResourceSnapshot]:
        """Decide how (or whether) to admit the next coding agent right now.

        Returns: (resolved_harness_or_WAIT, reason, snapshot)
        """
        snap = self.sample()

        # Hard RAM ceiling across all modes.
        if snap.slice_current_gib >= self.max_slice_mem_gib:
            return (
                "WAIT",
                f"user-{self.uid}.slice={snap.slice_current_gib:.1f}GiB >= cap {self.max_slice_mem_gib:.1f}GiB",
                snap,
            )
        if snap.mem_avail_gib <= self.min_avail_mem_gib:
            return (
                "WAIT",
                f"MemAvailable={snap.mem_avail_gib:.1f}GiB <= floor {self.min_avail_mem_gib:.1f}GiB",
                snap,
            )

        cpu_ceiling = snap.cpu_count * self.max_load_factor

        if configured_harness == "claude-mux-lite":
            return ("claude-mux-lite", "high-density mux-lite admitted", snap)

        if configured_harness == "custom":
            if snap.load_1m > cpu_ceiling and active_heavy_procs > 2:
                return ("WAIT", f"CPU load1m={snap.load_1m:.1f} > {cpu_ceiling:.1f}", snap)
            return ("custom", "custom harness admitted", snap)

        # Heavy claude -p process costs ~265 MiB RSS each.
        heavy_headroom_ok = (
            active_heavy_procs < self.max_heavy_mux_procs
            and (snap.slice_current_gib + 0.35) < self.max_slice_mem_gib
            and (snap.mem_avail_gib - 0.35) > self.min_avail_mem_gib
            and snap.load_1m <= cpu_ceiling
        )

        if configured_harness == "claude-mux":
            if heavy_headroom_ok:
                return ("claude-mux", "headless claude-mux CLI admitted", snap)
            return (
                "WAIT",
                f"heavy claude-mux cap ({active_heavy_procs}/{self.max_heavy_mux_procs}, load={snap.load_1m:.1f})",
                snap,
            )

        # claude-mux-scale: use full claude-mux CLI up to heavy budget, burst 100s+ via claude-mux-lite!
        if heavy_headroom_ok:
            return ("claude-mux", "scale-tier1: headless claude-mux CLI", snap)
        return (
            "claude-mux-lite",
            f"scale-tier2: burst onto 2MiB claude-mux-lite (heavy={active_heavy_procs}/{self.max_heavy_mux_procs})",
            snap,
        )
