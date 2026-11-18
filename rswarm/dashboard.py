"""Radial Outward-Branching Research Diary Web Dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .db import SwarmDB
from .governor import ResourceGovernor
from .models import SwarmConfig
from .tmux_pool import TmuxPool

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>rswarm · Radial Research Diary</title>
<style>
  :root {
    --bg: #06080d;
    --panel: rgba(13, 18, 28, 0.88);
    --panel-solid: #0d121c;
    --border: rgba(148, 163, 184, 0.14);
    --border-bright: rgba(56, 189, 248, 0.38);
    --text: #e2e8f0;
    --muted: #94a3b8;
    --dim: #64748b;
    --accent: #38bdf8;
    --win: #34d399;
    --gold: #fbbf24;
    --danger: #fb7185;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%; height: 100%; overflow: hidden;
    background: radial-gradient(circle at 50% 50%, #0d1424 0%, #06080d 72%);
    color: var(--text);
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
    font-variant-numeric: tabular-nums;
  }
  .app {
    display: grid;
    grid-template-rows: auto 1fr;
    grid-template-columns: minmax(260px, 320px) minmax(420px, 1fr) minmax(260px, 360px);
    width: 100vw; height: 100vh;
  }
  .mobile-tabs { display: none; }
  @media (max-width: 1120px) {
    .app {
      grid-template-rows: auto 40px 1fr;
      grid-template-columns: 1fr;
    }
    header { padding: 8px 12px; overflow-x: auto; gap: 12px; }
    .mobile-tabs {
      display: flex; gap: 6px; align-items: center; padding: 0 12px;
      background: rgba(9, 13, 22, 0.95); border-bottom: 1px solid var(--border);
    }
    .sidebar-left, .sidebar-right, .viewport {
      grid-row: 3; grid-column: 1; width: 100%; height: 100%;
    }
    .app[data-view="graph"] .sidebar-left,
    .app[data-view="graph"] .sidebar-right { display: none; }
    .app[data-view="diary"] .viewport,
    .app[data-view="diary"] .sidebar-right { display: none; }
    .app[data-view="inspector"] .viewport,
    .app[data-view="inspector"] .sidebar-left { display: none; }
  }
  header {
    grid-column: 1 / -1;
    min-height: 58px;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 6px 14px;
    padding: 6px 16px;
    background: rgba(9, 13, 22, 0.92);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(12px);
    z-index: 20;
  }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .badge-live {
    display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0;
    padding: 4px 10px; border-radius: 999px;
    background: rgba(52, 211, 153, 0.12);
    border: 1px solid rgba(52, 211, 153, 0.35);
    color: var(--win); font-size: 11px; font-weight: 700; letter-spacing: 0.05em;
  }
  .dot-pulse {
    width: 7px; height: 7px; border-radius: 50%; background: var(--win);
    box-shadow: 0 0 8px var(--win);
    animation: pulse 1.8s infinite;
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.45); opacity: 0.55; }
  }
  .title-main { font-size: 14.5px; font-weight: 700; letter-spacing: -0.01em; white-space: nowrap; }
  .subtitle { font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 460px; }
  .kpis { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
  .kpi {
    display: flex; flex-direction: column; align-items: flex-end; flex-shrink: 0;
    padding: 4px 8px; border-right: 1px solid var(--border);
    border-radius: 6px; cursor: help; transition: background 0.14s ease;
  }
  .kpi:hover { background: rgba(56, 189, 248, 0.08); }
  .kpi:last-child { border-right: none; }
  .kpi-label {
    display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;
    font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--dim);
  }
  .help-q {
    display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
    width: 13px; height: 13px; border-radius: 50%;
    background: rgba(56, 189, 248, 0.18); border: 1px solid rgba(56, 189, 248, 0.48);
    color: var(--accent); font-size: 9px; font-weight: 800; line-height: 1;
  }
  .kpi-val { font-size: 13.5px; font-weight: 700; color: var(--text); white-space: nowrap; margin-top: 2px; }
  .kpi-val.win { color: var(--win); }
  .kpi-val.gold { color: var(--gold); }
  .kpi-val.cyan { color: var(--accent); }

  /* Floating Rich Hover Tooltip (KPIs & Graph Nodes) */
  .hover-tooltip {
    position: fixed; top: 0; left: 0; z-index: 9999;
    max-width: 370px; min-width: 240px;
    padding: 11px 13px; border-radius: 10px;
    background: rgba(9, 13, 22, 0.96);
    border: 1px solid rgba(56, 189, 248, 0.42);
    box-shadow: 0 14px 34px rgba(0, 0, 0, 0.65), 0 0 0 1px rgba(255, 255, 255, 0.04);
    backdrop-filter: blur(14px);
    color: var(--text); font-size: 12px; line-height: 1.45;
    pointer-events: none; opacity: 0; transform: translateY(4px);
    transition: opacity 0.12s ease, transform 0.12s ease;
  }
  .hover-tooltip.visible { opacity: 1; transform: translateY(0); }
  .ht-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 5px; font-size: 10.5px; color: var(--muted); }
  .ht-badge { padding: 2px 6px; border-radius: 4px; font-weight: 700; font-size: 10px; background: rgba(56,189,248,0.15); color: var(--accent); }
  .ht-badge.BREAKTHROUGH { background: rgba(52,211,153,0.16); color: var(--win); }
  .ht-badge.RUNNING { background: rgba(251,191,36,0.18); color: var(--gold); }
  .ht-title { font-size: 13px; font-weight: 700; color: #f8fafc; margin-bottom: 6px; line-height: 1.3; }
  .ht-metrics {
    display: flex; flex-wrap: wrap; gap: 6px 10px;
    padding: 6px 8px; margin-bottom: 7px; border-radius: 6px;
    background: rgba(255,255,255,0.04); font-size: 11px;
  }
  .ht-desc { font-size: 11.5px; color: #cbd5e1; line-height: 1.45; }
  .ht-foot { margin-top: 6px; font-size: 10px; color: var(--dim); }
  .node-hit { cursor: pointer; }

  /* Left Sidebar: Chronological Research Diary & Bulletin */
  .sidebar-left, .sidebar-right {
    background: var(--panel);
    backdrop-filter: blur(14px);
    display: flex; flex-direction: column;
    min-width: 0; min-height: 0;
    overflow: hidden; z-index: 10;
  }
  .sidebar-left { border-right: 1px solid var(--border); }
  .sidebar-right { border-left: 1px solid var(--border); }
  .pane-hdr {
    padding: 12px 14px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .pane-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .filter-bar {
    display: flex; gap: 5px; padding: 8px 12px;
    border-bottom: 1px solid var(--border); flex-wrap: wrap;
  }
  .pill {
    padding: 4px 9px; border-radius: 6px; font-size: 11px; font-weight: 600;
    background: rgba(255,255,255,0.04); border: 1px solid transparent;
    color: var(--muted); cursor: pointer; transition: all 0.15s ease;
  }
  .pill:hover { color: var(--text); background: rgba(255,255,255,0.08); }
  .pill.active {
    background: rgba(56, 189, 248, 0.16); border-color: rgba(56, 189, 248, 0.45);
    color: var(--accent);
  }
  .diary-list {
    flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 8px;
  }
  .diary-card {
    padding: 10px 12px; border-radius: 8px;
    background: rgba(15, 23, 42, 0.65);
    border: 1px solid var(--border);
    cursor: pointer; transition: transform 0.12s ease, border-color 0.12s ease;
  }
  .diary-card:hover { border-color: var(--border-bright); transform: translateY(-1px); }
  .diary-card.selected { border-color: var(--accent); box-shadow: 0 0 0 1px rgba(56,189,248,0.3); }
  .diary-card.BREAKTHROUGH { border-left: 3px solid var(--win); }
  .diary-card.HYPOTHESIS { border-left: 3px solid var(--accent); }
  .diary-card.OBSTRUCTION { border-left: 3px solid #64748b; }
  .diary-card.HOSTED_SUBMIT { border-left: 3px solid var(--gold); }
  .dc-top { display: flex; justify-content: space-between; font-size: 10px; color: var(--dim); margin-bottom: 4px; }
  .dc-type { font-weight: 700; letter-spacing: 0.04em; }
  .dc-type.BREAKTHROUGH { color: var(--win); }
  .dc-type.HYPOTHESIS { color: var(--accent); }
  .dc-title { font-size: 12.5px; font-weight: 600; color: var(--text); margin-bottom: 4px; line-height: 1.35; }
  .dc-body { font-size: 11.5px; color: var(--muted); line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }

  /* Center Radial Viewport */
  .viewport {
    position: relative; width: 100%; height: 100%; overflow: hidden;
    min-width: 0; min-height: 0;
    user-select: none;
  }
  #radialSvg { display: block; width: 100%; height: 100%; cursor: grab; }
  #radialSvg:active { cursor: grabbing; }
  .hud-controls {
    position: absolute; top: 14px; left: 14px;
    display: flex; gap: 6px; align-items: center;
    background: rgba(9, 13, 22, 0.85); border: 1px solid var(--border);
    padding: 6px 8px; border-radius: 8px; backdrop-filter: blur(8px);
  }
  .hud-legend {
    position: absolute; bottom: 14px; left: 14px;
    display: flex; gap: 12px; flex-wrap: wrap;
    background: rgba(9, 13, 22, 0.85); border: 1px solid var(--border);
    padding: 8px 12px; border-radius: 8px; font-size: 11px; color: var(--muted);
  }
  .leg-item { display: inline-flex; align-items: center; gap: 6px; }
  .leg-swatch { width: 9px; height: 9px; border-radius: 50%; }

  /* Right Inspector Drawer */
  .inspector-body {
    flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 14px;
  }
  .sec-box {
    background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border);
    border-radius: 8px; padding: 11px 12px;
  }
  .sec-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--dim); margin-bottom: 6px; }
  .kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .kv-cell { background: rgba(255,255,255,0.025); padding: 7px 9px; border-radius: 6px; }
  .kv-k { font-size: 10px; color: var(--dim); }
  .kv-v { font-size: 13px; font-weight: 700; color: var(--text); margin-top: 2px; }
  pre.code-block {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px; line-height: 1.45; color: #cbd5e1;
    white-space: pre-wrap; word-break: break-word;
    max-height: 280px; overflow-y: auto;
    background: rgba(2, 6, 14, 0.75); padding: 9px; border-radius: 6px;
    border: 1px solid rgba(148,163,184,0.1);
  }
  .ray-edge { fill: none; transition: opacity 0.2s ease, stroke-width 0.2s ease; }
  .node-circle { cursor: pointer; transition: r 0.15s ease; }
  .node-running-halo {
    fill: none; stroke: #fbbf24; stroke-width: 2;
    animation: ringPulse 1.4s ease-out infinite;
  }
  @keyframes ringPulse {
    0% { r: 6px; opacity: 0.95; }
    100% { r: 18px; opacity: 0; }
  }
</style>
</head>
<body>
<div class="app" id="appRoot" data-view="graph">
  <header>
    <div class="brand">
      <span class="badge-live"><span class="dot-pulse"></span>LIVE SWARM</span>
      <div>
        <div class="title-main" id="hdrName">rswarm · Autonomous Research Diary</div>
        <div class="subtitle" id="hdrSub">Center = Baseline (1.00x) · Outward Ray Length = Verified Speedup & Goal Progress</div>
      </div>
    </div>
    <div class="kpis" id="kpiBar">
      <div class="kpi" data-kpi="baseline"><span class="kpi-label">Baseline</span><span class="kpi-val" id="kpiBase">—</span></div>
      <div class="kpi" data-kpi="best"><span class="kpi-label">Global Best</span><span class="kpi-val win" id="kpiBest">—</span></div>
      <div class="kpi" data-kpi="speedup"><span class="kpi-label">Speedup</span><span class="kpi-val win" id="kpiSpeedup">1.0000x</span></div>
      <div class="kpi" data-kpi="goal"><span class="kpi-label">Goal <span class="help-q">?</span></span><span class="kpi-val gold" id="kpiGoal">0.0%</span></div>
      <div class="kpi" data-kpi="lb1" id="kpiBoxLbOne"><span class="kpi-label">Leaderboard #1 <span class="help-q">?</span></span><span class="kpi-val" style="color:#e879f9" id="kpiLbOne">—</span></div>
      <div class="kpi" data-kpi="created"><span class="kpi-label">Hypotheses Created <span class="help-q">?</span></span><span class="kpi-val cyan" id="kpiHypCreated">0</span></div>
      <div class="kpi" data-kpi="tested" id="kpiBoxTested"><span class="kpi-label">Tested (Eval Runs) <span class="help-q">?</span></span><span class="kpi-val win" id="kpiHypTested">0</span></div>
      <div class="kpi" data-kpi="testing"><span class="kpi-label">Testing Now <span class="help-q">?</span></span><span class="kpi-val gold" id="kpiHypTesting">0</span></div>
      <div class="kpi" data-kpi="avgeval"><span class="kpi-label">Avg Eval Suite <span class="help-q">?</span></span><span class="kpi-val cyan" id="kpiAvgEval">—</span></div>
      <div class="kpi" data-kpi="ram"><span class="kpi-label">Slice RAM</span><span class="kpi-val" id="kpiRam">—</span></div>
    </div>
  </header>

  <nav class="mobile-tabs" id="mobileTabs">
    <button class="pill active" data-tab="graph">Radial Graph</button>
    <button class="pill" data-tab="diary">Research Diary</button>
    <button class="pill" data-tab="inspector">Node Inspector</button>
  </nav>

  <aside class="sidebar-left">
    <div class="pane-hdr">
      <span class="pane-title">Research Diary & Bulletin</span>
      <span class="subtitle" id="diaryCount">0 entries</span>
    </div>
    <div class="filter-bar" id="diaryFilters">
      <button class="pill active" data-filter="ALL">All</button>
      <button class="pill" data-filter="BREAKTHROUGH">Breakthroughs</button>
      <button class="pill" data-filter="HYPOTHESIS">Hypotheses</button>
      <button class="pill" data-filter="OBSTRUCTION">Obstructions</button>
      <button class="pill" data-filter="HOSTED_SUBMIT">Hosted</button>
    </div>
    <div class="diary-list" id="diaryList"></div>
  </aside>

  <main class="viewport" id="viewport">
    <div class="hud-controls">
      <span style="font-size:11px;color:var(--muted);padding:0 4px;">Radial Length:</span>
      <button class="pill active" data-scale="hybrid">Goal + Speedup</button>
      <button class="pill" data-scale="speedup">Pure Speedup (Nx)</button>
      <button class="pill" data-scale="depth">Tree Depth</button>
      <button class="pill" id="btnResetView">Reset Center</button>
    </div>
    <svg id="radialSvg" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <filter id="glowWin" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="3.2" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>
      <g id="panZoomGroup"></g>
    </svg>
    <div class="hud-legend" id="islandLegend"></div>
  </main>

  <aside class="sidebar-right">
    <div class="pane-hdr">
      <span class="pane-title">Lineage & Node Inspector</span>
      <span class="subtitle" id="inspRole">Click any ray or node</span>
    </div>
    <div class="inspector-body" id="inspectorBody">
      <div class="sec-box">
        <div class="sec-label">How to Read This Graph</div>
        <p style="font-size:12px;color:var(--muted);line-height:1.5;">
          The <b>center hub</b> is your verified Phase-0 Baseline (<b>1.00x</b>). Each angular sector is an independent <b>Research Direction (Island)</b>. As agents spawn child hypotheses and achieve breakthroughs, rays branch outward—the <b>radial length of every line</b> shows the quantitative speedup and progress achieved toward (and beyond!) the Leaderboard Target ring.
        </p>
      </div>
    </div>
  </aside>
</div>
<div class="hover-tooltip" id="hoverTooltip"></div>

<script>
const API_BASE = window.location.pathname.endsWith('/')
  ? window.location.pathname
  : window.location.pathname + '/';

const state = {
  data: /*__RSWARM_INITIAL_STATE__*/ null,
  selectedNodeId: null,
  diaryFilter: 'ALL',
  scaleMode: 'hybrid',
  panX: 0,
  panY: 0,
  zoom: 1.0,
  dragging: false,
  dragStartX: 0,
  dragStartY: 0,
};

const svg = document.getElementById('radialSvg');
const gRoot = document.getElementById('panZoomGroup');

function polarToXY(angleDeg, radiusPx) {
  const rad = (angleDeg - 90) * Math.PI / 180.0;
  return [radiusPx * Math.cos(rad), radiusPx * Math.sin(rad)];
}

function computeNodeRadiusPx(node, nodeById, maxR, maxSpeedup, maxDepth) {
  if (node.role === 'BASELINE') return 0;
  const parent = node.parent_id ? nodeById.get(node.parent_id) : null;
  const pR = parent && parent._rPx !== undefined ? parent._rPx : 0;

  if (state.scaleMode === 'depth') {
    return Math.min(maxR * 1.25, ((node.depth || 1) / Math.max(4, maxDepth)) * maxR);
  }
  if (state.scaleMode === 'speedup') {
    const sp = Number(node.speedup || 0);
    if (node.valid && sp > 1.000001) {
      const denom = Math.max(0.04, Math.log(Math.max(1.05, maxSpeedup)));
      const r = maxR * (0.12 + 0.88 * (Math.log(sp) / denom));
      return Math.max(pR + 16, r);
    }
    return Math.min(maxR * 1.15, Math.max(26, pR + 22));
  }
  // Default 'hybrid': uses server-computed progress+speedup radius_norm (1.0 = Leaderboard Target Ring!)
  const rn = Number(node.radius_norm || 0.08);
  return Math.max(pR + 14, rn * maxR);
}

function getAncestors(nodeId, nodeById) {
  const set = new Set();
  let cur = nodeId;
  let guard = 0;
  while (cur && nodeById.has(cur) && guard++ < 200) {
    set.add(cur);
    cur = nodeById.get(cur).parent_id;
  }
  return set;
}

function shortModelTag(modelId, effortLvl) {
  if (!modelId) return '';
  const m = String(modelId)
    .replace(/^anthropic-morph-/, '')
    .replace(/^anthropic-local-/, 'local-')
    .replace(/^anthropic-/, '');
  const e = effortLvl ? `·${effortLvl === 'minimal' ? 'min' : effortLvl}` : '';
  return `${m}${e}`;
}

function renderAll() {
  const d = state.data;
  if (!d) return;
  const st = d.stats || {};
  const meta = d.meta || {};
  const gov = d.governor || {};
  const rr = meta.role_routing || {};

  document.getElementById('hdrName').textContent = `rswarm · ${meta.name || 'Research Swarm'}`;
  const rTag = rr.researcher ? shortModelTag(rr.researcher.model, rr.researcher.effort) : shortModelTag(meta.researcher_model, 'max');
  const aTag = rr.deepen ? shortModelTag(rr.deepen.model, rr.deepen.effort) : rTag;
  const iTag = rr.implementer ? shortModelTag(rr.implementer.model, rr.implementer.effort) : shortModelTag(meta.implementer_model, 'minimal');
  const dTag = rr.debugger ? shortModelTag(rr.debugger.model, rr.debugger.effort) : iTag;
  document.getElementById('hdrSub').textContent =
    `R: ${rTag} · Deepen: ${aTag} · I: ${iTag} · D: ${dTag} · Cap: ${meta.concurrency || '—'}`;
  document.getElementById('kpiBase').textContent = st.baseline_score !== null && st.baseline_score !== undefined ? Number(st.baseline_score).toPrecision(6) : '—';
  document.getElementById('kpiBest').textContent = st.best_score !== null && st.best_score !== undefined ? Number(st.best_score).toPrecision(6) : '—';
  document.getElementById('kpiSpeedup').textContent = `${Number(st.best_speedup || 1).toFixed(4)}x`;
  const tgtVal = meta.leaderboard_target ?? st.leaderboard_target;
  const gpPct = `${(Number(st.best_goal_progress || 0) * 100).toFixed(1)}%`;
  document.getElementById('kpiGoal').textContent = tgtVal !== null && tgtVal !== undefined ? `${Number(tgtVal).toPrecision(4)} (${gpPct})` : gpPct;
  const lbOne = meta.leaderboard_number_one ?? st.leaderboard_number_one;
  document.getElementById('kpiLbOne').textContent = lbOne !== null && lbOne !== undefined ? Number(lbOne).toFixed(4) : '—';
  document.getElementById('kpiHypCreated').textContent = `${st.hypotheses_created ?? 0}`;
  document.getElementById('kpiHypTested').textContent = `${st.hypotheses_tested ?? 0} (${st.eval_suite_runs ?? 0})`;
  document.getElementById('kpiHypTesting').textContent = `${st.hypotheses_testing_now ?? 0} / ${meta.concurrency || st.running || 0}`;
  const avgMs = Number(st.avg_eval_ms || 0);
  document.getElementById('kpiAvgEval').textContent = avgMs >= 1000 ? `${(avgMs / 1000).toFixed(2)}s` : (avgMs > 0 ? `${avgMs.toFixed(0)}ms` : '—');
  document.getElementById('kpiRam').textContent = gov.slice_current_gib !== undefined ? `${gov.slice_current_gib} GiB` : '—';

  // Legend
  const islands = d.islands || [];
  const islMap = new Map(islands.map(i => [Number(i.island_id), i]));
  const legEl = document.getElementById('islandLegend');
  legEl.innerHTML = islands.map(i =>
    `<span class="leg-item"><span class="leg-swatch" style="background:${i.color}"></span>` +
    `<b>${i.name}</b> (${Number(i.best_speedup || 1).toFixed(3)}x)</span>`
  ).join('') + `<span class="leg-item" style="margin-left:8px;color:#fbbf24;">⚡ <b>Pulsing Gold</b> = Live Running Agent</span>`;

  renderRadialSvg(d, islMap);
  renderDiaryList(d);
}

function renderRadialSvg(d, islMap) {
  const rect = svg.getBoundingClientRect();
  const cx = rect.width / 2 + state.panX;
  const cy = rect.height / 2 + state.panY;
  const maxR = Math.max(180, Math.min(rect.width, rect.height) * 0.40);

  gRoot.setAttribute('transform', `translate(${cx}, ${cy}) scale(${state.zoom})`);

  const nodes = d.nodes || [];
  const nodeById = new Map(nodes.map(n => [n.node_id, n]));
  const maxSpeedup = Math.max(1.05, ...nodes.filter(n => n.valid).map(n => Number(n.speedup || 1)));
  const maxDepth = Math.max(3, ...nodes.map(n => Number(n.depth || 0)));

  // Topological order ensures parents get _rPx before children
  const sorted = [...nodes].sort((a, b) => (a.depth || 0) - (b.depth || 0));
  for (const n of sorted) {
    n._rPx = computeNodeRadiusPx(n, nodeById, maxR, maxSpeedup, maxDepth);
    const [x, y] = polarToXY(Number(n.angle_deg || 0), n._rPx);
    n._x = x;
    n._y = y;
  }

  const lineageSet = state.selectedNodeId ? getAncestors(state.selectedNodeId, nodeById) : null;
  let html = '';

  // 1. Concentric Progress & Speedup Rings
  const rings = [
    { frac: 0.25, label: '25% Goal', stroke: 'rgba(148,163,184,0.12)', dash: '3,5' },
    { frac: 0.50, label: '50% Goal', stroke: 'rgba(148,163,184,0.16)', dash: '3,5' },
    { frac: 0.75, label: '75% Goal', stroke: 'rgba(148,163,184,0.20)', dash: '4,4' },
    { frac: 1.00, label: d.meta?.leaderboard_target != null ? `GOAL (${d.meta.leaderboard_target})` : 'Frontier 1.0 Ring', stroke: 'rgba(251,191,36,0.45)', dash: '6,4' },
    { frac: 1.22, label: d.meta?.leaderboard_number_one != null ? `LEADERBOARD #1 (${Number(d.meta.leaderboard_number_one).toFixed(4)})` : 'BEYOND LEADERBOARD SOTA', stroke: 'rgba(232,121,249,0.36)', dash: '2,6' },
  ];
  for (const rg of rings) {
    const rr = maxR * rg.frac;
    html += `<circle cx="0" cy="0" r="${rr.toFixed(1)}" fill="none" stroke="${rg.stroke}" stroke-width="1.2" stroke-dasharray="${rg.dash}" />`;
    html += `<text x="6" y="${(-rr + 13).toFixed(1)}" fill="rgba(148,163,184,0.55)" font-size="10" font-weight="600">${rg.label}</text>`;
  }

  // 2. Angular Sector Spokes & Island Perimeter Titles
  for (const isl of (d.islands || [])) {
    const [sx, sy] = polarToXY(isl.angle_start_deg, maxR * 1.24);
    html += `<line x1="0" y1="0" x2="${sx.toFixed(1)}" y2="${sy.toFixed(1)}" stroke="rgba(148,163,184,0.08)" stroke-width="1" />`;
    const midDeg = 0.5 * (Number(isl.angle_start_deg) + Number(isl.angle_end_deg));
    const [lx, ly] = polarToXY(midDeg, maxR * 1.13);
    html += `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="middle" fill="${isl.color}" font-size="11" font-weight="700" opacity="0.88">${isl.name.toUpperCase()}</text>`;
  }

  // 3. Outward Branching Research Rays (Bezier curves from Parent -> Child)
  for (const n of sorted) {
    if (!n.parent_id || !nodeById.has(n.parent_id)) continue;
    const p = nodeById.get(n.parent_id);
    const isl = islMap.get(Number(n.island_id)) || { color: '#38bdf8' };
    const isWin = n.status === 'BREAKTHROUGH';
    const inLineage = lineageSet ? (lineageSet.has(n.node_id) && lineageSet.has(p.node_id)) : false;
    const dimmed = lineageSet && !inLineage;

    // Control point bends radially outward smoothly from parent's radius
    const midR = 0.55 * p._rPx + 0.45 * n._rPx;
    const [cx1, cy1] = polarToXY(Number(n.angle_deg || 0), midR);
    const width = inLineage ? 3.4 : (isWin ? 2.5 : (n.status === 'RUNNING' ? 2.0 : 1.15));
    const opacity = dimmed ? 0.14 : (isWin ? 0.95 : (n.status === 'RUNNING' ? 0.85 : 0.42));
    const filter = (isWin || inLineage) ? 'filter="url(#glowWin)"' : '';

    html += `<path class="ray-edge" d="M ${p._x.toFixed(1)} ${p._y.toFixed(1)} Q ${cx1.toFixed(1)} ${cy1.toFixed(1)} ${n._x.toFixed(1)} ${n._y.toFixed(1)}" ` +
            `stroke="${isl.color}" stroke-width="${width}" opacity="${opacity}" ${filter} data-nid="${n.node_id}" />`;

    // Cross-island secondary parent dashed arc for CROSSOVER nodes
    if (n.secondary_parent_id && nodeById.has(n.secondary_parent_id)) {
      const sp = nodeById.get(n.secondary_parent_id);
      html += `<line x1="${sp._x.toFixed(1)}" y1="${sp._y.toFixed(1)}" x2="${n._x.toFixed(1)}" y2="${n._y.toFixed(1)}" ` +
              `stroke="#fbbf24" stroke-width="1.2" stroke-dasharray="4,4" opacity="${dimmed ? 0.1 : 0.55}" />`;
    }
  }

  // 4. Nodes + Center Baseline Hub + Speedup Callouts
  for (const n of sorted) {
    const isl = islMap.get(Number(n.island_id)) || { color: '#38bdf8' };
    const isBase = n.role === 'BASELINE';
    const isWin = n.status === 'BREAKTHROUGH';
    const isRun = n.status === 'RUNNING';
    const isSel = state.selectedNodeId === n.node_id;
    const dimmed = lineageSet && !lineageSet.has(n.node_id);

    if (isRun) {
      html += `<circle class="node-running-halo" cx="${n._x.toFixed(1)}" cy="${n._y.toFixed(1)}" r="8" />`;
    }

    const r = isBase ? 13 : (isSel ? 8.5 : (isWin ? 6.5 : (isRun ? 5.5 : 3.8)));
    const fill = isBase ? '#f8fafc' : (isWin ? isl.color : (isRun ? '#fbbf24' : (n.status === 'FAILED' ? '#fb7185' : '#1e293b')));
    const stroke = isSel ? '#ffffff' : isl.color;
    const sw = isSel ? 2.8 : (isBase ? 3.5 : 1.5);

    html += `<circle class="node-circle" data-nid="${n.node_id}" cx="${n._x.toFixed(1)}" cy="${n._y.toFixed(1)}" r="${r}" ` +
            `fill="${fill}" stroke="${stroke}" stroke-width="${sw}" opacity="${dimmed ? 0.2 : 1.0}" />`;
    html += `<circle class="node-hit" data-nid="${n.node_id}" cx="${n._x.toFixed(1)}" cy="${n._y.toFixed(1)}" r="${Math.max(11, r + 5)}" fill="transparent" />`;

    if (isBase) {
      html += `<text x="0" y="27" text-anchor="middle" fill="#f8fafc" font-size="11" font-weight="700">ORIGIN 1.00x</text>`;
    } else if ((isWin || isRun || isSel) && !dimmed) {
      const mTag = shortModelTag(n.model_used, n.effort_used);
      const mBracket = mTag ? ` [${mTag}]` : '';
      const spTxt = n.valid && n.speedup ? `${Number(n.speedup).toFixed(3)}x` : n.role;
      const lbl = isRun && n.tmux_window
        ? `⚡ ${n.tmux_window}${mBracket}`
        : `${spTxt}${mBracket} · ${(n.title || '').slice(0, 20)}`;
      html += `<text x="${(n._x + 9).toFixed(1)}" y="${(n._y + 3).toFixed(1)}" fill="#e2e8f0" font-size="10.5" font-weight="600" pointer-events="none">${escapeHtml(lbl)}</text>`;
    }
  }

  gRoot.innerHTML = html;
}

function renderDiaryList(d) {
  const nodeMap = new Map((d.nodes || []).map(n => [n.node_id, n]));
  const entries = (d.diary || []).filter(e => state.diaryFilter === 'ALL' || e.entry_type === state.diaryFilter);
  document.getElementById('diaryCount').textContent = `${entries.length} entries`;
  const listEl = document.getElementById('diaryList');
  listEl.innerHTML = entries.map(e => {
    const sel = e.node_id && e.node_id === state.selectedNodeId ? 'selected' : '';
    const nd = e.node_id ? nodeMap.get(e.node_id) : null;
    const mTag = nd ? shortModelTag(nd.model_used, nd.effort_used) : '';
    const spBadge = e.speedup ? `${Number(e.speedup).toFixed(4)}x` : '';
    const ts = new Date(e.created_at * 1000).toLocaleTimeString();
    const rightMeta = [mTag ? `[${mTag}]` : '', spBadge, ts].filter(Boolean).join(' · ');
    return `<div class="diary-card ${e.entry_type} ${sel}" data-nid="${e.node_id || ''}">
      <div class="dc-top">
        <span class="dc-type ${e.entry_type}">${e.entry_type}</span>
        <span>${escapeHtml(rightMeta)}</span>
      </div>
      <div class="dc-title">${escapeHtml(e.title)}</div>
      <div class="dc-body">${escapeHtml(e.body)}</div>
    </div>`;
  }).join('');
}

async function selectNode(nodeId) {
  if (!nodeId) return;
  state.selectedNodeId = nodeId;
  const appEl = document.getElementById('appRoot');
  if (window.innerWidth <= 1120 && appEl) {
    appEl.dataset.view = 'inspector';
    document.querySelectorAll('#mobileTabs .pill').forEach(b => b.classList.toggle('active', b.dataset.tab === 'inspector'));
  }
  renderAll();
  try {
    const resp = await fetch(`${API_BASE}api/node/${encodeURIComponent(nodeId)}`);
    const detail = await resp.json();
    if (!detail.ok) return;
    const n = detail.node;
    document.getElementById('inspRole').textContent = `${n.role} · ${n.status}`;
    const body = document.getElementById('inspectorBody');
    body.innerHTML = `
      <div class="sec-box">
        <div class="sec-label">Node & Lineage Metrics</div>
        <div style="font-size:14px;font-weight:700;margin-bottom:8px;">${escapeHtml(n.title)}</div>
        <div class="kv-grid">
          <div class="kv-cell"><div class="kv-k">Node ID</div><div class="kv-v">${escapeHtml(n.node_id)}</div></div>
          <div class="kv-cell"><div class="kv-k">Parent ID</div><div class="kv-v">${escapeHtml(n.parent_id || 'ROOT')}</div></div>
          <div class="kv-cell"><div class="kv-k">Verified Score</div><div class="kv-v" style="color:var(--win)">${n.score !== null ? n.score : '—'}</div></div>
          <div class="kv-cell"><div class="kv-k">Speedup vs Origin</div><div class="kv-v" style="color:var(--win)">${Number(n.speedup || 0).toFixed(4)}x</div></div>
          <div class="kv-cell"><div class="kv-k">Model Used</div><div class="kv-v" style="color:#c084fc;font-size:11.5px">${escapeHtml(n.model_used || '—')}</div></div>
          <div class="kv-cell"><div class="kv-k">Reasoning Effort</div><div class="kv-v" style="color:var(--gold);font-size:11.5px">${escapeHtml(n.effort_used || '—')}</div></div>
          <div class="kv-cell"><div class="kv-k">tmux Window</div><div class="kv-v" style="color:var(--accent);font-size:11.5px">${escapeHtml(n.tmux_window || '—')}</div></div>
          <div class="kv-cell"><div class="kv-k">Harness Mode</div><div class="kv-v" style="font-size:11px">${escapeHtml(n.harness_used || '—')}</div></div>
        </div>
      </div>
      <div class="sec-box">
        <div class="sec-label">Hypothesis & Mathematical Rationale</div>
        <pre class="code-block">${escapeHtml((n.hypothesis || '') + (n.rationale ? '\n\nRationale:\n' + n.rationale : ''))}</pre>
      </div>
      <div class="sec-box">
        <div class="sec-label">Implementation & Eval Diagnostics</div>
        <pre class="code-block">${escapeHtml((n.code_summary || '') + (n.diagnostics ? '\n\nDiagnostics:\n' + n.diagnostics : ''))}</pre>
      </div>
      <div class="sec-box">
        <div class="sec-label">Agent Execution Log Tail (${escapeHtml(n.tmux_window || n.node_id)})</div>
        <pre class="code-block">${escapeHtml(detail.log_tail || '(no log output yet)')}</pre>
      </div>
    `;
  } catch (err) {
    console.error(err);
  }
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Hover Tooltip Logic (KPI Help Boxes & Radial Graph Nodes)
const tooltipEl = document.getElementById('hoverTooltip');

function placeTooltip(clientX, clientY, htmlContent) {
  tooltipEl.innerHTML = htmlContent;
  tooltipEl.classList.add('visible');
  const rect = tooltipEl.getBoundingClientRect();
  let x = clientX + 14;
  let y = clientY + 14;
  if (x + rect.width > window.innerWidth - 12) x = Math.max(12, clientX - rect.width - 14);
  if (y + rect.height > window.innerHeight - 12) y = Math.max(12, clientY - rect.height - 14);
  tooltipEl.style.left = `${x}px`;
  tooltipEl.style.top = `${y}px`;
}

function hideTooltip() {
  tooltipEl.classList.remove('visible');
}

function buildKpiTooltipHtml(kpiKey) {
  const st = state.data?.stats || {};
  const meta = state.data?.meta || {};
  const gov = state.data?.governor || {};
  const tested = st.hypotheses_tested ?? 0;
  const runs = st.eval_suite_runs ?? 0;
  const created = st.hypotheses_created ?? 0;
  const queued = st.hypotheses_queued ?? 0;
  const testing = st.hypotheses_testing_now ?? 0;
  const avgMs = Number(st.avg_eval_ms || 0);

  const docs = {
    baseline: {
      title: 'Origin Baseline Score (1.0000x)',
      body: `Verified Phase-0 starting score (${st.baseline_score ?? '—'}) plotted at the center hub of the radial graph.`,
    },
    best: {
      title: 'Global Best Verified Score',
      body: `Best score (${st.best_score ?? '—'}) achieved across all islands and verified by the read-only local evaluation suite (${meta.direction || 'minimize'}).`,
    },
    speedup: {
      title: 'Speedup / Improvement Multiplier',
      body: `Ratio of Baseline to Global Best (${Number(st.best_speedup || 1).toFixed(4)}x). Outward ray lengths scale with this verified gain.`,
    },
    goal: {
      title: 'Milestone Goal & Gap Closed',
      body: `Current milestone target is <b>${meta.leaderboard_target ?? st.leaderboard_target ?? 'open'}</b> (${(Number(st.best_goal_progress || 0) * 100).toFixed(1)}% of gap closed from Baseline ${st.baseline_score ?? '—'}).`,
    },
    lb1: {
      title: 'Public Leaderboard #1 & Top-5 Submit Gate',
      body: `<div style="margin-bottom:5px;">• <b>Leaderboard #1 Score:</b> <b>${meta.leaderboard_number_one ?? st.leaderboard_number_one ?? '—'}</b> (${meta.direction || 'minimize'}).</div>` +
            `<div>• <b>Auto-Submit Gate (Top 5):</b> <b>${meta.leaderboard_submit_threshold ?? st.leaderboard_submit_threshold ?? '—'}</b>. Hosted leaderboard submissions stay gated until our verified global best enters the Top 5 best results, then submit automatically.</div>`,
    },
    created: {
      title: 'Total Hypotheses Created',
      body: `<b>${created}</b> experiment nodes (<code>IMPLEMENT</code>, <code>DEEPEN</code>, <code>CROSSOVER</code>, <code>DEBUG</code>) generated by Researchers so far (<b>${queued}</b> currently waiting in queue).`,
    },
    tested: {
      title: 'Tested Hypotheses vs. Total Eval Runs',
      body: `<div style="margin-bottom:5px;">• <b>First number (${tested}) — Tested Hypotheses:</b> Unique hypothesis nodes built and evaluated to completion.</div>` +
            `<div>• <b>In parentheses (${runs}) — Total Eval Suite Runs:</b> Total times <code>local_eval.sh</code> ran across all agents. Higher than ${tested} because builders iteratively call <code>swarm-eval</code> multiple times while tuning parameters before calling <code>swarm-submit</code>.</div>`,
    },
    testing: {
      title: 'Hypotheses Being Tested Right Now',
      body: `<b>${testing}</b> active builder/debugger agents currently coding and benchmarking in live <code>tmux</code> windows (out of concurrency cap <b>${meta.concurrency || '—'}</b>).`,
    },
    avgeval: {
      title: 'Average Verification & Benchmark Duration',
      body: `Mean wall-clock time (<b>${avgMs.toFixed(1)} ms</b> = <b>${(avgMs / 1000).toFixed(2)} s</b>) for one full execution of <code>eval/local_eval.sh</code> across ${runs} benchmark runs.`,
    },
    ram: {
      title: 'Sparky Non-Reclaimable User-Slice RAM',
      body: `Active process RAM in <code>user-1000.slice</code> (${gov.slice_current_gib ?? '—'} GiB used, ${gov.mem_avail_gib ?? '—'} GiB system RAM free).`,
    },
  };
  const info = docs[kpiKey];
  if (!info) return '';
  return `<div class="ht-title">${info.title}</div><div class="ht-desc">${info.body}</div>`;
}

function buildNodeTooltipHtml(nodeId) {
  const d = state.data;
  if (!d) return '';
  const n = (d.nodes || []).find(x => x.node_id === nodeId);
  if (!n) return '';
  const isl = (d.islands || []).find(i => Number(i.island_id) === Number(n.island_id)) || { name: 'origin', color: '#38bdf8' };
  const mTag = shortModelTag(n.model_used, n.effort_used) || 'baseline';
  const scoreTxt = n.score !== null && n.score !== undefined ? Number(n.score).toPrecision(6) : 'pending';
  const spTxt = n.valid && n.speedup ? `${Number(n.speedup).toFixed(4)}x` : '—';
  const rawDesc = (n.code_summary && n.code_summary.trim())
    ? `${n.hypothesis || ''}\n\nResult: ${n.code_summary}`
    : (n.hypothesis || n.rationale || 'Initial verified reference point at center origin.');
  const trimmedDesc = rawDesc.length > 290 ? rawDesc.slice(0, 290) + '…' : rawDesc;

  return `
    <div class="ht-top">
      <span style="display:inline-flex;align-items:center;gap:5px;">
        <span class="leg-swatch" style="background:${isl.color}"></span>
        <b>${escapeHtml(isl.name)}</b> · ${escapeHtml(n.node_id)}
      </span>
      <span class="ht-badge ${escapeHtml(n.status)}">${escapeHtml(n.role)} · ${escapeHtml(n.status)}</span>
    </div>
    <div class="ht-title">${escapeHtml(n.title)}</div>
    <div class="ht-metrics">
      <span>Score: <b style="color:var(--win)">${escapeHtml(scoreTxt)}</b></span>
      <span>Speedup: <b style="color:var(--win)">${escapeHtml(spTxt)}</b></span>
      <span>Model: <b style="color:#c084fc">${escapeHtml(mTag)}</b></span>
      ${n.tmux_window ? `<span>tmux: <b style="color:var(--accent)">${escapeHtml(n.tmux_window)}</b></span>` : ''}
    </div>
    <div class="ht-desc">${escapeHtml(trimmedDesc)}</div>
    <div class="ht-foot">Click node to pin full rationale, diagnostics & agent logs</div>
  `;
}

document.getElementById('kpiBar').addEventListener('mousemove', e => {
  const box = e.target.closest('.kpi[data-kpi]');
  if (!box) { hideTooltip(); return; }
  const html = buildKpiTooltipHtml(box.dataset.kpi);
  if (html) placeTooltip(e.clientX, e.clientY, html);
});
document.getElementById('kpiBar').addEventListener('mouseleave', hideTooltip);

svg.addEventListener('mousemove', e => {
  if (state.dragging) { hideTooltip(); return; }
  const nid = e.target?.getAttribute?.('data-nid');
  if (!nid) { hideTooltip(); return; }
  const html = buildNodeTooltipHtml(nid);
  if (html) placeTooltip(e.clientX, e.clientY, html);
});
svg.addEventListener('mouseleave', hideTooltip);

// Event Listeners
svg.addEventListener('click', e => {
  const nid = e.target?.getAttribute?.('data-nid');
  if (nid) selectNode(nid);
});
document.getElementById('diaryList').addEventListener('click', e => {
  const card = e.target.closest('.diary-card');
  if (card && card.dataset.nid) selectNode(card.dataset.nid);
});
document.getElementById('diaryFilters').addEventListener('click', e => {
  const btn = e.target.closest('.pill');
  if (!btn) return;
  document.querySelectorAll('#diaryFilters .pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.diaryFilter = btn.dataset.filter;
  renderAll();
});
document.querySelectorAll('.hud-controls .pill[data-scale]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.hud-controls .pill[data-scale]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.scaleMode = btn.dataset.scale;
    renderAll();
  });
});
document.getElementById('btnResetView').addEventListener('click', () => {
  state.panX = 0; state.panY = 0; state.zoom = 1.0; state.selectedNodeId = null;
  renderAll();
});
document.getElementById('mobileTabs')?.addEventListener('click', e => {
  const btn = e.target.closest('.pill');
  if (!btn) return;
  document.querySelectorAll('#mobileTabs .pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('appRoot').dataset.view = btn.dataset.tab;
  requestAnimationFrame(() => renderAll());
});

// Pan & Zoom
svg.addEventListener('mousedown', e => {
  if (e.target.getAttribute('data-nid')) return;
  state.dragging = true;
  state.dragStartX = e.clientX - state.panX;
  state.dragStartY = e.clientY - state.panY;
});
window.addEventListener('mousemove', e => {
  if (!state.dragging) return;
  state.panX = e.clientX - state.dragStartX;
  state.panY = e.clientY - state.dragStartY;
  renderAll();
});
window.addEventListener('mouseup', () => { state.dragging = false; });
svg.addEventListener('wheel', e => {
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.12 : 0.89;
  state.zoom = Math.max(0.35, Math.min(4.5, state.zoom * factor));
  renderAll();
}, { passive: false });
window.addEventListener('resize', () => renderAll());

async function poll() {
  try {
    const r = await fetch(`${API_BASE}api/state`);
    if (!r.ok) return;
    state.data = await r.json();
    renderAll();
  } catch (err) {
    console.warn('poll failed', err);
  }
}
if (state.data) {
  requestAnimationFrame(() => renderAll());
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


class DashboardServer:
    """Zero-dependency asyncio HTTP server serving the Radial Research Diary."""

    def __init__(
        self,
        *,
        cfg: SwarmConfig,
        db: SwarmDB,
        governor: ResourceGovernor,
        tmux_pool: TmuxPool,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.governor = governor
        self.tmux_pool = tmux_pool
        self.ws = Path(cfg.workspace_dir).resolve()
        self._server: asyncio.AbstractServer | None = None
        self.bound_host: str = cfg.dashboard_host
        self.bound_port: int = cfg.dashboard_port

    async def start(self, write_registry: bool = True) -> str:
        bind_host = "0.0.0.0" if self.cfg.dashboard_host not in ("127.0.0.1", "localhost") else "127.0.0.1"
        display_host = self.cfg.dashboard_host if self.cfg.dashboard_host != "0.0.0.0" else "127.0.0.1"
        for offset in range(25):
            port = self.cfg.dashboard_port + offset
            try:
                self._server = await asyncio.start_server(
                    self._handle_http, host=bind_host, port=port
                )
                self.bound_host = display_host
                self.bound_port = port
                tailscale_url = f"http://{display_host}:{port}"
                if write_registry and bind_host == "0.0.0.0":
                    self._write_registry(port, tailscale_url)
                return tailscale_url
            except OSError:
                continue
        raise RuntimeError("Could not bind dashboard HTTP server on any port.")

    def _write_registry(self, port: int, tailscale_url: str) -> None:
        reg_dir = Path.home() / ".tangram-dev"
        if not reg_dir.is_dir():
            return
        reg_path = reg_dir / "rswarm-dashboard.json"
        doc = {
            "pid": os.getpid(),
            "url": f"http://127.0.0.1:{port}",
            "tailscale_url": tailscale_url,
            "workspace": str(self.ws),
            "name": self.cfg.name,
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        fd, tmp = tempfile.mkstemp(dir=str(reg_dir), prefix=".rswarm-dash.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, indent=2)
            os.replace(tmp, reg_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        reg_path = Path.home() / ".tangram-dev" / "rswarm-dashboard.json"
        if reg_path.is_file():
            try:
                data = json.loads(reg_path.read_text())
                if int(data.get("pid", -1)) == os.getpid():
                    reg_path.unlink()
            except Exception:
                pass

    def _build_state_payload(self) -> dict[str, Any]:
        payload = self.db.export_dashboard_state()
        cfg_file = self.ws / "swarm.json"
        if cfg_file.is_file():
            try:
                live_cfg = SwarmConfig.from_dict(json.loads(cfg_file.read_text()))
                payload["meta"]["concurrency"] = live_cfg.concurrency
                payload["meta"]["role_routing"] = live_cfg.role_routing_dict()
                payload["meta"]["leaderboard_target"] = live_cfg.leaderboard_target
                payload["meta"]["leaderboard_number_one"] = live_cfg.leaderboard_number_one
                payload["meta"]["leaderboard_submit_threshold"] = live_cfg.leaderboard_submit_threshold
            except Exception:
                pass
        payload["governor"] = self.governor.sample().to_dict()
        payload["tmux_session"] = self.tmux_pool.session_name
        payload["tmux_windows"] = self.tmux_pool.list_live_window_names()
        return payload

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line:
                return
            # Consume headers
            while True:
                hline = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not hline or hline in (b"\r\n", b"\n"):
                    break

            parts = req_line.decode("utf-8", errors="replace").split()
            path = urllib.parse.unquote(parts[1].split("?", 1)[0]) if len(parts) >= 2 else "/"

            if path in ("/", "/index.html"):
                init_json = json.dumps(self._build_state_payload()).replace("</", "<\\/")
                rendered = DASHBOARD_HTML.replace("/*__RSWARM_INITIAL_STATE__*/ null", init_json, 1)
                body = rendered.encode("utf-8")
                self._write_resp(writer, 200, "text/html; charset=utf-8", body)
            elif path == "/api/state":
                body = json.dumps(self._build_state_payload()).encode("utf-8")
                self._write_resp(writer, 200, "application/json", body)
            elif path.startswith("/api/node/"):
                nid = path[len("/api/node/") :].strip("/")
                node = self.db.get_node(nid)
                if not node:
                    self._write_resp(writer, 404, "application/json", b'{"ok":false,"error":"not found"}')
                else:
                    log_file = self.ws / "logs" / f"{nid}.log"
                    log_tail = log_file.read_text(errors="replace")[-16000:] if log_file.is_file() else ""
                    body = json.dumps({"ok": True, "node": node, "log_tail": log_tail}).encode("utf-8")
                    self._write_resp(writer, 200, "application/json", body)
            else:
                self._write_resp(writer, 404, "text/plain", b"Not Found")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    def _write_resp(self, writer: asyncio.StreamWriter, code: int, ctype: str, body: bytes) -> None:
        reason = "OK" if code == 200 else "Not Found"
        hdr = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(hdr + body)
