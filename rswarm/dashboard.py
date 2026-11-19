"""Radial Outward-Branching Research Diary Web Dashboard (High-Fidelity & Touch-Ready)."""

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
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />
<title>rswarm · Radial Research Observatory</title>
<style>
  :root {
    --bg: #050811;
    --panel: rgba(11, 16, 27, 0.90);
    --panel-elev: rgba(16, 23, 38, 0.94);
    --border: rgba(148, 163, 184, 0.13);
    --border-bright: rgba(56, 189, 248, 0.40);
    --shadow-card: 0 1px 0 rgba(255, 255, 255, 0.05) inset, 0 12px 32px rgba(0, 0, 0, 0.48);
    --shadow-float: 0 1px 0 rgba(255, 255, 255, 0.07) inset, 0 18px 42px rgba(0, 0, 0, 0.68);
    --text: #f1f5f9;
    --muted: #94a3b8;
    --dim: #64748b;
    --accent: #38bdf8;
    --win: #34d399;
    --gold: #fbbf24;
    -- orchid: #e879f9;
    --danger: #fb7185;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html, body {
    width: 100%; height: 100%; overflow: hidden;
    background: radial-gradient(circle at 50% 46%, #0d1629 0%, #050811 74%);
    color: var(--text);
    font-family: Inter, -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    font-variant-numeric: tabular-nums;
  }

  .app {
    display: grid;
    grid-template-rows: auto 1fr;
    grid-template-columns: minmax(265px, 315px) minmax(420px, 1fr) minmax(270px, 355px);
    width: 100vw; max-width: 100vw; height: 100dvh; overflow: hidden;
  }
  .mobile-tabs { display: none; }

  @media (max-width: 1180px) {
    .app {
      grid-template-rows: auto 48px 1fr;
      grid-template-columns: minmax(0, 1fr);
    }
    .mobile-tabs {
      display: flex; gap: 6px; align-items: center; padding: 4px 12px;
      min-width: 0; max-width: 100vw;
      background: rgba(8, 12, 21, 0.96);
      border-bottom: 1px solid var(--border);
      overflow-x: auto; scrollbar-width: none;
    }
    .mobile-tabs::-webkit-scrollbar { display: none; }
    .sidebar-left, .sidebar-right, .viewport {
      grid-row: 3; grid-column: 1; width: 100%; max-width: 100vw; height: 100%;
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
    min-height: 58px; min-width: 0; max-width: 100vw;
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px 12px;
    padding: 6px 14px;
    background: rgba(8, 12, 21, 0.94);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(16px);
    z-index: 30;
  }
  .brand { display: flex; align-items: center; gap: 9px; min-width: 0; flex: 0 1 auto; }
  .badge-live {
    display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0;
    padding: 5px 9px; border-radius: 999px;
    background: rgba(52, 211, 153, 0.12);
    box-shadow: 0 0 0 1px rgba(52, 211, 153, 0.34) inset;
    color: var(--win); font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
  }
  .dot-pulse {
    width: 7px; height: 7px; border-radius: 50%; background: var(--win);
    box-shadow: 0 0 8px var(--win);
    animation: pulse 1.8s infinite;
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.4); opacity: 0.55; }
  }
  .title-main { font-size: 13.5px; font-weight: 700; letter-spacing: -0.015em; white-space: nowrap; }
  .subtitle { font-size: 10px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 215px; }

  .kpis {
    display: flex; align-items: center; gap: 0px;
    margin-left: auto; min-width: 0; justify-content: flex-start;
    overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
  }
  @media (max-width: 1180px) {
    .kpis { margin-left: 0; width: 100%; }
    header { flex-wrap: wrap; padding: 8px 12px; gap: 6px; }
    .brand { width: 100%; }
    .subtitle { max-width: 68vw; }
    .kpi { align-items: flex-start; padding: 3px 10px 3px 4px; }
  }
  .kpis::-webkit-scrollbar { display: none; }
  .kpi {
    display: flex; flex-direction: column; align-items: flex-end; flex-shrink: 0;
    min-height: 40px; justify-content: center;
    padding: 3px 6px; border-right: 1px solid var(--border);
    border-radius: 8px; cursor: help;
    transition: background-color 0.14s ease, transform 0.14s ease;
  }
  .kpi:hover { background-color: rgba(56, 189, 248, 0.08); }
  .kpi:active { transform: scale(0.96); }
  .kpi:last-child { border-right: none; }
  .kpi-label {
    display: inline-flex; align-items: center; gap: 3px; white-space: nowrap;
    font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--dim);
  }
  .help-q {
    display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
    width: 13px; height: 13px; border-radius: 50%;
    background: rgba(56, 189, 248, 0.16);
    box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.45) inset;
    color: var(--accent); font-size: 8.5px; font-weight: 800; line-height: 1;
  }
  .kpi-val { font-size: 12.5px; font-weight: 700; color: var(--text); white-space: nowrap; margin-top: 2px; }
  .kpi-val.win { color: var(--win); }
  .kpi-val.gold { color: var(--gold); }
  .kpi-val.cyan { color: var(--accent); }
  .kpi-val.orchid { color: #e879f9; }

  @media (max-width: 760px) {
    header { flex-direction: column; align-items: stretch; padding: 8px 12px; gap: 6px; }
    .subtitle { max-width: 82vw; }
    .kpi { align-items: flex-start; padding: 4px 10px 4px 0; margin-right: 6px; }
  }

  /* Tactile Controls: Concentric Radii (outer 12px = inner 8px + 4px padding) & 40px Touch Hit Area */
  .pill {
    min-height: 36px; padding: 6px 11px; border-radius: 8px;
    font-size: 11.5px; font-weight: 600; white-space: nowrap;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid transparent;
    color: var(--muted); cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 5px;
    transition: background-color 0.14s ease, color 0.14s ease, transform 0.12s ease, border-color 0.14s ease;
  }
  @media (pointer: coarse) {
    .pill { min-height: 40px; padding: 8px 13px; font-size: 12px; }
  }
  .pill:hover { color: var(--text); background-color: rgba(255, 255, 255, 0.08); }
  .pill:active { transform: scale(0.96); }
  .pill.active {
    background-color: rgba(56, 189, 248, 0.16);
    border-color: rgba(56, 189, 248, 0.42);
    color: var(--accent);
  }

  /* Sidebars */
  .sidebar-left, .sidebar-right {
    background: var(--panel);
    backdrop-filter: blur(16px);
    display: flex; flex-direction: column;
    min-width: 0; min-height: 0;
    overflow: hidden; z-index: 15;
  }
  .sidebar-left { border-right: 1px solid var(--border); }
  .sidebar-right { border-left: 1px solid var(--border); }
  .pane-hdr {
    padding: 11px 14px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }
  .pane-title { font-size: 11.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .filter-bar {
    display: flex; gap: 5px; padding: 7px 10px;
    border-bottom: 1px solid var(--border);
    overflow-x: auto; scrollbar-width: none;
  }
  .filter-bar::-webkit-scrollbar { display: none; }
  .diary-list {
    flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 10px; display: flex; flex-direction: column; gap: 8px;
  }
  .diary-card {
    padding: 11px 12px; border-radius: 10px;
    background: rgba(15, 23, 42, 0.62);
    box-shadow: var(--shadow-card);
    border: 1px solid var(--border);
    cursor: pointer;
    transition: transform 0.14s ease, border-color 0.14s ease, background-color 0.14s ease;
  }
  .diary-card:hover { border-color: var(--border-bright); background-color: rgba(19, 29, 51, 0.78); }
  .diary-card:active { transform: scale(0.98); }
  .diary-card.selected { border-color: var(--accent); box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.35), var(--shadow-card); }
  .diary-card.BREAKTHROUGH { border-left: 3px solid var(--win); }
  .diary-card.HYPOTHESIS { border-left: 3px solid var(--accent); }
  .diary-card.OBSTRUCTION { border-left: 3px solid #64748b; }
  .diary-card.STRATEGY { border-left: 3px solid var(--gold); }
  .diary-card.HOSTED_SUBMIT { border-left: 3px solid var(--gold); }
  .dc-top { display: flex; justify-content: space-between; gap: 6px; font-size: 10px; color: var(--dim); margin-bottom: 4px; }
  .dc-type { font-weight: 700; letter-spacing: 0.04em; }
  .dc-type.BREAKTHROUGH { color: var(--win); }
  .dc-type.HYPOTHESIS { color: var(--accent); }
  .dc-type.STRATEGY { color: var(--gold); }
  .dc-title { font-size: 12.5px; font-weight: 600; color: var(--text); margin-bottom: 4px; line-height: 1.35; text-wrap: balance; }
  .dc-body { font-size: 11.5px; color: var(--muted); line-height: 1.42; text-wrap: pretty; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }

  /* Center Radial Observatory Viewport */
  .viewport {
    position: relative; width: 100%; height: 100%; overflow: hidden;
    min-width: 0; min-height: 0;
    user-select: none; -webkit-user-select: none;
  }
  #radialSvg {
    display: block; width: 100%; height: 100%;
    touch-action: none; /* Enables custom 1-finger pan & 2-finger pinch-zoom on iPad/mobile */
    cursor: grab;
  }
  #radialSvg.dragging { cursor: grabbing; }

  .hud-controls {
    position: absolute; top: 12px; left: 12px; right: 12px;
    pointer-events: none;
    display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; flex-wrap: wrap;
    z-index: 10;
  }
  .hud-bar {
    pointer-events: auto;
    display: inline-flex; gap: 4px; align-items: center; flex-wrap: wrap;
    background: rgba(9, 13, 23, 0.86);
    box-shadow: var(--shadow-card);
    border: 1px solid var(--border);
    padding: 4px; border-radius: 12px;
    backdrop-filter: blur(12px);
    max-width: 100%; overflow-x: auto; scrollbar-width: none;
  }
  .hud-bar::-webkit-scrollbar { display: none; }
  .hud-lbl { font-size: 10.5px; font-weight: 600; color: var(--dim); padding: 0 6px; white-space: nowrap; }

  /* Floating Zoom & Fit Touch Dock */
  .zoom-dock {
    position: absolute; right: 14px; bottom: 58px; z-index: 12;
    display: flex; flex-direction: column; gap: 4px;
    padding: 4px; border-radius: 12px;
    background: rgba(9, 13, 23, 0.88);
    border: 1px solid var(--border);
    box-shadow: var(--shadow-card);
    backdrop-filter: blur(12px);
  }
  .zoom-btn {
    width: 40px; height: 40px; border-radius: 8px;
    border: 1px solid transparent;
    background: rgba(255, 255, 255, 0.04);
    color: var(--text); font-size: 16px; font-weight: 700;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    transition: background-color 0.14s ease, transform 0.12s ease;
  }
  .zoom-btn:hover { background-color: rgba(56, 189, 248, 0.16); color: var(--accent); }
  .zoom-btn:active { transform: scale(0.95); }
  .zoom-readout { font-size: 10px; font-weight: 700; color: var(--muted); text-align: center; padding: 2px 0; }

  .hud-legend {
    position: absolute; bottom: 12px; left: 12px; right: 12px; z-index: 10;
    pointer-events: auto;
    display: flex; gap: 10px 14px; align-items: center;
    overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
    background: rgba(9, 13, 23, 0.86);
    border: 1px solid var(--border);
    box-shadow: var(--shadow-card);
    padding: 7px 12px; border-radius: 10px;
    font-size: 11px; color: var(--muted); white-space: nowrap;
  }
  .hud-legend::-webkit-scrollbar { display: none; }
  .leg-item { display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .leg-swatch { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }

  /* Mobile / iPad Bottom Quick-Inspect Sheet (keeps Graph visible on tap!) */
  .mobile-sheet {
    display: none;
    position: absolute; left: 12px; right: 64px; bottom: 54px; z-index: 25;
    padding: 13px 14px; border-radius: 14px;
    background: #0d1526;
    border: 1px solid rgba(56, 189, 248, 0.45);
    box-shadow: var(--shadow-float);
  }
  .mobile-sheet.open { display: block; }
  @media (min-width: 1181px) {
    .mobile-sheet, .mobile-sheet.open { display: none; }
  }

  /* Inspector Drawer */
  .inspector-body {
    flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 14px; display: flex; flex-direction: column; gap: 12px;
  }
  .sec-box {
    background: rgba(15, 23, 42, 0.62); border: 1px solid var(--border);
    box-shadow: var(--shadow-card);
    border-radius: 12px; padding: 12px;
  }
  .sec-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--dim); margin-bottom: 6px; }
  .kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .kv-cell { background: rgba(255, 255, 255, 0.03); padding: 8px 10px; border-radius: 8px; }
  .kv-k { font-size: 10px; color: var(--dim); }
  .kv-v { font-size: 13px; font-weight: 700; color: var(--text); margin-top: 2px; word-break: break-word; }
  pre.code-block {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px; line-height: 1.48; color: #cbd5e1;
    white-space: pre-wrap; word-break: break-word;
    max-height: 300px; overflow-y: auto; -webkit-overflow-scrolling: touch;
    background: rgba(3, 7, 16, 0.78); padding: 10px; border-radius: 8px;
    border: 1px solid rgba(148, 163, 184, 0.12);
  }

  /* Floating Desktop Hover Tooltip */
  .hover-tooltip {
    position: fixed; top: 0; left: 0; z-index: 9999;
    max-width: 375px; min-width: 245px;
    padding: 12px 14px; border-radius: 12px;
    background: rgba(9, 13, 23, 0.96);
    border: 1px solid rgba(56, 189, 248, 0.42);
    box-shadow: var(--shadow-float);
    backdrop-filter: blur(16px);
    color: var(--text); font-size: 12px; line-height: 1.45;
    pointer-events: none; opacity: 0; transform: translateY(4px);
    transition: opacity 0.12s ease, transform 0.12s ease;
  }
  .hover-tooltip.visible { opacity: 1; transform: translateY(0); }
  .ht-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 5px; font-size: 10.5px; color: var(--muted); }
  .ht-badge { padding: 2px 6px; border-radius: 5px; font-weight: 700; font-size: 10px; background: rgba(56, 189, 248, 0.15); color: var(--accent); }
  .ht-badge.BREAKTHROUGH { background: rgba(52, 211, 153, 0.16); color: var(--win); }
  .ht-badge.RUNNING { background: rgba(251, 191, 36, 0.18); color: var(--gold); }
  .ht-title { font-size: 13px; font-weight: 700; color: #f8fafc; margin-bottom: 6px; line-height: 1.3; text-wrap: balance; }
  .ht-metrics {
    display: flex; flex-wrap: wrap; gap: 6px 10px;
    padding: 6px 8px; margin-bottom: 7px; border-radius: 8px;
    background: rgba(255, 255, 255, 0.04); font-size: 11px;
  }
  .ht-desc { font-size: 11.5px; color: #cbd5e1; line-height: 1.45; text-wrap: pretty; }
  .ht-foot { margin-top: 6px; font-size: 10px; color: var(--dim); }

  .ray-edge { fill: none; }
  .node-running-halo {
    fill: none; stroke: #fbbf24; stroke-width: 2;
    animation: ringPulse 1.5s ease-out infinite;
  }
  @keyframes ringPulse {
    0% { stroke-opacity: 0.95; }
    100% { stroke-opacity: 0.12; }
  }
</style>
</head>
<body>
<div class="app" id="appRoot" data-view="graph">
  <header>
    <div class="brand">
      <span class="badge-live"><span class="dot-pulse"></span>LIVE SWARM</span>
      <div style="min-width:0;">
        <div class="title-main" id="hdrName">rswarm · Autonomous Research Observatory</div>
        <div class="subtitle" id="hdrSub">Center = Baseline (1.00x) · Outward Ray = Verified Progress to Leaderboard #1</div>
      </div>
    </div>
    <div class="kpis" id="kpiBar">
      <div class="kpi" data-kpi="baseline"><span class="kpi-label">Baseline</span><span class="kpi-val" id="kpiBase">—</span></div>
      <div class="kpi" data-kpi="best"><span class="kpi-label">Global Best</span><span class="kpi-val win" id="kpiBest">—</span></div>
      <div class="kpi" data-kpi="speedup"><span class="kpi-label">Speedup</span><span class="kpi-val win" id="kpiSpeedup">1.0000x</span></div>
      <div class="kpi" data-kpi="goal"><span class="kpi-label">Goal <span class="help-q">?</span></span><span class="kpi-val gold" id="kpiGoal">0.0%</span></div>
      <div class="kpi" data-kpi="lb1" id="kpiBoxLbOne"><span class="kpi-label">Leaderboard #1 <span class="help-q">?</span></span><span class="kpi-val orchid" id="kpiLbOne">—</span></div>
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
      <button class="pill" data-filter="STRATEGY">Strategy</button>
      <button class="pill" data-filter="BREAKTHROUGH">Breakthroughs</button>
      <button class="pill" data-filter="HYPOTHESIS">Hypotheses</button>
      <button class="pill" data-filter="OBSTRUCTION">Obstructions</button>
      <button class="pill" data-filter="HOSTED_SUBMIT">Hosted</button>
    </div>
    <div class="diary-list" id="diaryList"></div>
  </aside>

  <main class="viewport" id="viewport">
    <div class="hud-controls">
      <div class="hud-bar" id="scaleBar">
        <span class="hud-lbl">Scale</span>
        <button class="pill active" data-scale="hybrid">Goal + Speedup</button>
        <button class="pill" data-scale="speedup">Pure Speedup</button>
        <button class="pill" data-scale="depth">Tree Depth</button>
      </div>
      <div class="hud-bar" id="densityBar">
        <span class="hud-lbl">Nodes</span>
        <button class="pill active" data-density="all">All</button>
        <button class="pill" data-density="frontier">Wins + Live</button>
      </div>
    </div>

    <svg id="radialSvg" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <filter id="glowWin" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="2.8" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
        <radialGradient id="hubGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="rgba(56,189,248,0.28)" />
          <stop offset="100%" stop-color="rgba(56,189,248,0)" />
        </radialGradient>
      </defs>
      <g id="panZoomGroup"></g>
      <g id="screenOverlayGroup"></g>
    </svg>

    <div class="zoom-dock" aria-label="Zoom and view controls">
      <button class="zoom-btn" id="btnZoomIn" title="Zoom in">+</button>
      <div class="zoom-readout" id="zoomReadout">1.0×</div>
      <button class="zoom-btn" id="btnZoomOut" title="Zoom out">−</button>
      <button class="zoom-btn" id="btnResetView" title="Fit & reset center" style="font-size:11px;">Fit</button>
    </div>

    <div class="mobile-sheet" id="mobileSheet"></div>
    <div class="hud-legend" id="islandLegend"></div>
  </main>

  <aside class="sidebar-right">
    <div class="pane-hdr">
      <span class="pane-title">Lineage & Node Inspector</span>
      <button class="pill" id="btnBackToGraph" style="display:none;min-height:32px;padding:4px 9px;">← Graph</button>
      <span class="subtitle" id="inspRole">Tap or click any node</span>
    </div>
    <div class="inspector-body" id="inspectorBody">
      <div class="sec-box">
        <div class="sec-label">How to Read This Observatory</div>
        <p style="font-size:12px;color:var(--muted);line-height:1.55;text-wrap:pretty;">
          The <b>center hub</b> is the verified Phase-0 Baseline (<b>1.00x</b>). Each sector is an independent <b>Research Island</b>. Outward ray length measures verified progress toward the <b>100% Goal / Leaderboard #1 Ring</b>. Pinch or scroll to zoom in—the label solver progressively reveals additional callouts with zero overlapping text.
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
  hoveredNodeId: null,
  diaryFilter: 'ALL',
  scaleMode: 'hybrid',
  nodeDensity: 'all',
  panX: 0,
  panY: 0,
  zoom: 1.0,
  screenNodes: [],
};

const svg = document.getElementById('radialSvg');
const gRoot = document.getElementById('panZoomGroup');
const gOverlay = document.getElementById('screenOverlayGroup');
const mobileSheetEl = document.getElementById('mobileSheet');

function polarToXY(angleDeg, radiusPx) {
  const rad = (angleDeg - 90) * Math.PI / 180.0;
  return [radiusPx * Math.cos(rad), radiusPx * Math.sin(rad)];
}

function sectorArcPath(rInner, rOuter, startDeg, endDeg) {
  const [x1, y1] = polarToXY(startDeg, rOuter);
  const [x2, y2] = polarToXY(endDeg, rOuter);
  const [x3, y3] = polarToXY(endDeg, rInner);
  const [x4, y4] = polarToXY(startDeg, rInner);
  const large = (endDeg - startDeg) > 180 ? 1 : 0;
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${rOuter.toFixed(1)} ${rOuter.toFixed(1)} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} ` +
         `L ${x3.toFixed(1)} ${y3.toFixed(1)} A ${rInner.toFixed(1)} ${rInner.toFixed(1)} 0 ${large} 0 ${x4.toFixed(1)} ${y4.toFixed(1)} Z`;
}

function computeNodeRadiusPx(node, nodeById, maxR, maxSpeedup, maxDepth) {
  if (node.role === 'BASELINE') return 0;
  const parent = node.parent_id ? nodeById.get(node.parent_id) : null;
  const pR = parent && parent._rPx !== undefined ? parent._rPx : 0;

  if (state.scaleMode === 'depth') {
    return Math.min(maxR * 1.24, Math.max(28, ((node.depth || 1) / Math.max(4, maxDepth)) * maxR));
  }
  if (state.scaleMode === 'speedup') {
    const sp = Number(node.speedup || 0);
    if (node.valid && sp > 1.000001) {
      const denom = Math.max(0.04, Math.log(Math.max(1.05, maxSpeedup)));
      const r = maxR * (0.14 + 0.86 * (Math.log(sp) / denom));
      return Math.max(pR + 18, r);
    }
    return Math.min(maxR * 1.12, Math.max(30, pR + 20));
  }
  const rn = Number(node.radius_norm || 0.10);
  const gp = Number(node.goal_progress || 0);
  const rawPx = Math.max(pR + 5, rn * maxR);
  // Never let a sub-goal node visually cross the 100% Goal Ring on Goal + Speedup scale!
  if (state.data?.meta?.leaderboard_target != null && gp < 1.0) {
    return Math.min(maxR * 0.965, rawPx);
  }
  return rawPx;
}

function getAncestors(nodeId, nodeById) {
  const set = new Set();
  let cur = nodeId;
  let guard = 0;
  while (cur && nodeById.has(cur) && guard++ < 250) {
    set.add(cur);
    cur = nodeById.get(cur).parent_id;
  }
  return set;
}

function shortModelTag(modelId, effortLvl) {
  if (!modelId) return '';
  const m = String(modelId)
    .replace(/^anthropic-morph-/, '')
    .replace(/^claude-fable-/, 'fable-')
    .replace(/^gemini-4-/, 'g4-')
    .replace(/^gemini-/, 'gem-')
    .replace(/^anthropic-local-/, 'local-')
    .replace(/^anthropic-/, '');
  const hasTier = /-(low|medium|high|xhigh)$/i.test(m);
  const e = (effortLvl && !hasTier) ? `·${effortLvl === 'minimal' ? 'min' : effortLvl}` : '';
  return `${m}${e}`;
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function boxesOverlap(a, b, pad = 4) {
  return !(
    a.x2 + pad < b.x1 ||
    a.x1 - pad > b.x2 ||
    a.y2 + pad < b.y1 ||
    a.y1 - pad > b.y2
  );
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

  document.getElementById('kpiBase').textContent = st.baseline_score != null ? Number(st.baseline_score).toPrecision(6) : '—';
  document.getElementById('kpiBest').textContent = st.best_score != null ? Number(st.best_score).toPrecision(6) : '—';
  document.getElementById('kpiSpeedup').textContent = `${Number(st.best_speedup || 1).toFixed(4)}x`;
  const tgtVal = meta.leaderboard_target ?? st.leaderboard_target;
  const gpPct = `${(Number(st.best_goal_progress || 0) * 100).toFixed(1)}%`;
  document.getElementById('kpiGoal').textContent = tgtVal != null ? `${Number(tgtVal).toFixed(4)} (${gpPct})` : gpPct;
  const lbOne = meta.leaderboard_number_one ?? st.leaderboard_number_one;
  document.getElementById('kpiLbOne').textContent = lbOne != null ? Number(lbOne).toFixed(4) : '—';
  document.getElementById('kpiHypCreated').textContent = `${st.hypotheses_created ?? 0}`;
  document.getElementById('kpiHypTested').textContent = `${st.hypotheses_tested ?? 0} (${st.eval_suite_runs ?? 0})`;
  document.getElementById('kpiHypTesting').textContent = `${st.hypotheses_testing_now ?? 0} / ${meta.concurrency || st.running || 0}`;
  const avgMs = Number(st.avg_eval_ms || 0);
  document.getElementById('kpiAvgEval').textContent = avgMs >= 1000 ? `${(avgMs / 1000).toFixed(2)}s` : (avgMs > 0 ? `${avgMs.toFixed(0)}ms` : '—');
  document.getElementById('kpiRam').textContent = gov.slice_current_gib !== undefined ? `${gov.slice_current_gib} GiB` : '—';
  document.getElementById('zoomReadout').textContent = `${state.zoom.toFixed(1)}×`;

  const isMobileLayout = window.innerWidth <= 1180;
  document.getElementById('btnBackToGraph').style.display = isMobileLayout ? 'inline-flex' : 'none';

  const islands = d.islands || [];
  const islMap = new Map(islands.map(i => [Number(i.island_id), i]));
  document.getElementById('islandLegend').innerHTML = islands.map(i =>
    `<span class="leg-item"><span class="leg-swatch" style="background:${i.color}"></span>` +
    `<b>${escapeHtml(i.name)}</b> (${Number(i.best_speedup || 1).toFixed(3)}x)</span>`
  ).join('') + `<span class="leg-item" style="color:#fbbf24;">⚡ <b>Gold</b> = Running</span>` +
  `<span class="leg-item" style="color:#34d399;">★ <b>Glow</b> = Breakthrough</span>`;

  renderRadialSvg(d, islMap);
  renderDiaryList(d);
}

function renderRadialSvg(d, islMap) {
  const rect = svg.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) return;

  const cx = rect.width / 2 + state.panX;
  const cy = rect.height / 2 + state.panY;
  const maxR = Math.max(150, Math.min(rect.width, rect.height) * 0.39);

  gRoot.setAttribute('transform', `translate(${cx.toFixed(1)}, ${cy.toFixed(1)}) scale(${state.zoom.toFixed(4)})`);

  const allNodes = d.nodes || [];
  const nodeById = new Map(allNodes.map(n => [n.node_id, n]));
  const maxSpeedup = Math.max(1.05, ...allNodes.filter(n => n.valid).map(n => Number(n.speedup || 1)));
  const maxDepth = Math.max(3, ...allNodes.map(n => Number(n.depth || 0)));

  const sorted = [...allNodes].sort((a, b) => (a.depth || 0) - (b.depth || 0));
  for (const n of sorted) {
    n._rPx = computeNodeRadiusPx(n, nodeById, maxR, maxSpeedup, maxDepth);
    const [x, y] = polarToXY(Number(n.angle_deg || 0), n._rPx);
    n._x = x;
    n._y = y;
    n._sx = cx + x * state.zoom;
    n._sy = cy + y * state.zoom;
  }

  const activeFocusId = state.selectedNodeId || state.hoveredNodeId;
  const lineageSet = activeFocusId ? getAncestors(activeFocusId, nodeById) : null;
  const frontierOnly = state.nodeDensity === 'frontier';
  const islandElites = new Set((d.islands || []).map(i => i.best_node_id).filter(Boolean));
  const bestNodeId = d.stats?.best_node_id || d.meta?.best_node_id;

  let worldHtml = '';
  let overlayHtml = '';
  const occupiedBoxes = [];

  // 1. Alternating subtle radar sector wedges & spokes (world space)
  const islands = d.islands || [];
  islands.forEach((isl, idx) => {
    const a0 = Number(isl.angle_start_deg || 0);
    const a1 = Number(isl.angle_end_deg || 0);
    if (idx % 2 === 0 && a1 > a0) {
      worldHtml += `<path d="${sectorArcPath(18, maxR * 1.18, a0, a1)}" fill="rgba(56,189,248,0.016)" />`;
    }
    const [sx, sy] = polarToXY(a0, maxR * 1.20);
    worldHtml += `<line x1="0" y1="0" x2="${sx.toFixed(1)}" y2="${sy.toFixed(1)}" stroke="rgba(148,163,184,0.09)" stroke-width="${(1 / state.zoom).toFixed(2)}" />`;
  });

  // 2. Concentric Progress & Leaderboard #1 Goal Ring (Goal Ring rendered FIRST at top-center so it is ALWAYS prominent!)
  const tgt = d.meta?.leaderboard_target;
  const subGate = d.meta?.leaderboard_submit_threshold;
  const baseSc = d.stats?.baseline_score;
  const rings = [
    {
      frac: 1.00,
      deg: 4,
      label: tgt != null ? `🎯 GOAL · LEADERBOARD #1 (${Number(tgt).toFixed(4)})` : '100% GOAL RING',
      stroke: 'rgba(251, 191, 36, 0.72)',
      sw: 1.8,
      dash: '7,4',
      color: '#fde68a',
      bg: 'rgba(24, 18, 6, 0.92)',
    },
    { frac: 0.75, deg: 4, label: '75% Goal', stroke: 'rgba(148,163,184,0.22)', sw: 1.15, dash: '4,4', color: '#cbd5e1', bg: 'rgba(5,8,17,0.82)' },
    { frac: 0.50, deg: 4, label: '50% Goal', stroke: 'rgba(148,163,184,0.17)', sw: 1.1, dash: '3,5', color: '#94a3b8', bg: 'rgba(5,8,17,0.82)' },
    { frac: 0.25, deg: 4, label: '25% Goal', stroke: 'rgba(148,163,184,0.13)', sw: 1.05, dash: '3,5', color: '#94a3b8', bg: 'rgba(5,8,17,0.82)' },
  ];
  if (subGate != null && tgt != null && baseSc != null && Math.abs(subGate - tgt) > 1e-5) {
    const gateGp = Math.max(0.1, Math.min(0.992, (baseSc - subGate) / (baseSc - tgt)));
    const gateFrac = 0.10 + 0.88 * Math.pow(gateGp, 0.78);
    rings.splice(1, 0, {
      frac: gateFrac,
      deg: 184,
      label: `TOP-5 AUTO-SUBMIT (${Number(subGate).toFixed(4)})`,
      stroke: 'rgba(56,189,248,0.42)',
      sw: 1.25,
      dash: '4,4',
      color: '#38bdf8',
      bg: 'rgba(6,15,28,0.90)',
    });
  }

  const invZ = 1 / state.zoom;
  for (const rg of rings) {
    const rr = maxR * rg.frac;
    worldHtml += `<circle cx="0" cy="0" r="${rr.toFixed(1)}" fill="none" stroke="${rg.stroke}" stroke-width="${(rg.sw * invZ).toFixed(2)}" stroke-dasharray="${rg.dash}" />`;
    const [rx, ry] = polarToXY(rg.deg, rr * state.zoom);
    const sx = cx + rx;
    const sy = cy + ry;
    if (sx > 30 && sx < rect.width - 30 && sy > 52 && sy < rect.height - 48) {
      const w = rg.label.length * 5.85 + 14;
      const box = { x1: sx - w / 2, y1: sy - 9.5, x2: sx + w / 2, y2: sy + 9.5 };
      if (!occupiedBoxes.some(b => boxesOverlap(box, b, 3))) {
        occupiedBoxes.push(box);
        overlayHtml += `<g pointer-events="none">` +
          `<rect x="${box.x1.toFixed(1)}" y="${box.y1.toFixed(1)}" width="${w.toFixed(1)}" height="19" rx="5" fill="${rg.bg}" stroke="${rg.stroke}" stroke-width="1" />` +
          `<text x="${sx.toFixed(1)}" y="${(sy + 3.4).toFixed(1)}" text-anchor="middle" fill="${rg.color}" font-size="9.5" font-weight="700" letter-spacing="0.03em">${escapeHtml(rg.label)}</text>` +
          `</g>`;
      }
    }
  }

  // 3. Crisp Screen-Space Island Perimeter Headers (collision-registered!)
  for (const isl of islands) {
    const midDeg = 0.5 * (Number(isl.angle_start_deg) + Number(isl.angle_end_deg));
    const [lx, ly] = polarToXY(midDeg, maxR * 1.11 * state.zoom);
    const sx = cx + lx;
    const sy = cy + ly;
    if (sx > 30 && sx < rect.width - 30 && sy > 48 && sy < rect.height - 44) {
      const txt = isl.name.toUpperCase();
      const w = txt.length * 6.8 + 16;
      const box = { x1: sx - w / 2, y1: sy - 11, x2: sx + w / 2, y2: sy + 11 };
      if (!occupiedBoxes.some(b => boxesOverlap(box, b, 4))) {
        occupiedBoxes.push(box);
        overlayHtml += `<g pointer-events="none">` +
          `<rect x="${box.x1.toFixed(1)}" y="${box.y1.toFixed(1)}" width="${w.toFixed(1)}" height="21" rx="6" fill="rgba(8,12,22,0.88)" stroke="${isl.color}" stroke-opacity="0.4" stroke-width="1" />` +
          `<text x="${sx.toFixed(1)}" y="${(sy + 3.5).toFixed(1)}" text-anchor="middle" fill="${isl.color}" font-size="10.5" font-weight="800" letter-spacing="0.05em">${escapeHtml(txt)}</text>` +
          `</g>`;
      }
    }
  }

  // 4. Outward Branching Research Rays (Bezier curves)
  const visibleNodes = [];
  for (const n of sorted) {
    const isBase = n.role === 'BASELINE';
    const isWin = n.status === 'BREAKTHROUGH';
    const isRun = n.status === 'RUNNING';
    const isFocus = n.node_id === state.selectedNodeId || n.node_id === state.hoveredNodeId;
    if (frontierOnly && !isBase && !isWin && !isRun && !isFocus && !(lineageSet && lineageSet.has(n.node_id))) {
      continue;
    }
    visibleNodes.push(n);

    if (!n.parent_id || !nodeById.has(n.parent_id)) continue;
    const p = nodeById.get(n.parent_id);
    const isl = islMap.get(Number(n.island_id)) || { color: '#38bdf8' };
    const inLineage = lineageSet ? (lineageSet.has(n.node_id) && lineageSet.has(p.node_id)) : false;
    const dimmed = lineageSet && !inLineage;

    const midR = 0.52 * p._rPx + 0.48 * n._rPx;
    const [cx1, cy1] = polarToXY(Number(n.angle_deg || 0), midR);
    const baseW = inLineage ? 3.2 : (isWin ? 2.1 : (isRun ? 1.9 : 0.9));
    const sw = Math.max(0.5, baseW * Math.pow(invZ, 0.55));
    const opacity = dimmed ? 0.09 : (inLineage ? 1.0 : (isWin ? 0.88 : (isRun ? 0.82 : 0.24)));
    const filter = (inLineage || (isWin && islandElites.has(n.node_id))) ? 'filter="url(#glowWin)"' : '';

    worldHtml += `<path class="ray-edge" d="M ${p._x.toFixed(1)} ${p._y.toFixed(1)} Q ${cx1.toFixed(1)} ${cy1.toFixed(1)} ${n._x.toFixed(1)} ${n._y.toFixed(1)}" ` +
                 `stroke="${isl.color}" stroke-width="${sw.toFixed(2)}" opacity="${opacity}" ${filter} />`;

    if (n.secondary_parent_id && nodeById.has(n.secondary_parent_id)) {
      const sp = nodeById.get(n.secondary_parent_id);
      worldHtml += `<line x1="${sp._x.toFixed(1)}" y1="${sp._y.toFixed(1)}" x2="${n._x.toFixed(1)}" y2="${n._y.toFixed(1)}" ` +
                   `stroke="#fbbf24" stroke-width="${(1.2 * invZ).toFixed(2)}" stroke-dasharray="4,4" opacity="${dimmed ? 0.08 : 0.55}" />`;
    }
  }

  // 5. Center Hub Glow + Nodes (world space, crisp zoom-compensated radii)
  worldHtml += `<circle cx="0" cy="0" r="${(34 * invZ).toFixed(1)}" fill="url(#hubGlow)" pointer-events="none" />`;

  const labelCandidates = [];
  for (const n of visibleNodes) {
    const isl = islMap.get(Number(n.island_id)) || { color: '#38bdf8' };
    const isBase = n.role === 'BASELINE';
    const isWin = n.status === 'BREAKTHROUGH';
    const isRun = n.status === 'RUNNING';
    const isSel = state.selectedNodeId === n.node_id;
    const isHov = state.hoveredNodeId === n.node_id;
    const isGlobalBest = n.node_id === bestNodeId;
    const isIslandElite = islandElites.has(n.node_id);
    const dimmed = lineageSet && !lineageSet.has(n.node_id);

    const rScreen = isBase ? 11.5 : (isSel || isHov ? 8.0 : (isGlobalBest ? 7.2 : (isWin ? 5.2 : (isRun ? 5.2 : 2.7))));
    const rWorld = rScreen * Math.pow(invZ, 0.72);

    if (isRun) {
      worldHtml += `<circle class="node-running-halo" cx="${n._x.toFixed(1)}" cy="${n._y.toFixed(1)}" r="${(rWorld * 2.1).toFixed(1)}" stroke-width="${(1.8 * invZ).toFixed(2)}" />`;
    }
    if (isGlobalBest || isSel || isHov) {
      const haloCol = isSel || isHov ? '#ffffff' : '#34d399';
      worldHtml += `<circle cx="${n._x.toFixed(1)}" cy="${n._y.toFixed(1)}" r="${(rWorld * 1.85).toFixed(1)}" fill="none" stroke="${haloCol}" stroke-opacity="0.55" stroke-width="${(1.5 * invZ).toFixed(2)}" />`;
    }

    const fill = isBase ? '#f8fafc' : (isWin ? isl.color : (isRun ? '#fbbf24' : (n.status === 'FAILED' ? '#fb7185' : '#1e293b')));
    const stroke = (isSel || isHov) ? '#ffffff' : (isBase ? '#38bdf8' : isl.color);
    const sw = ((isSel || isHov) ? 2.4 : (isBase ? 3.0 : (isWin ? 1.4 : 1.0))) * Math.pow(invZ, 0.72);
    const op = dimmed ? 0.16 : (isWin || isRun || isBase || isSel ? 1.0 : 0.55);

    worldHtml += `<circle cx="${n._x.toFixed(1)}" cy="${n._y.toFixed(1)}" r="${rWorld.toFixed(2)}" fill="${fill}" stroke="${stroke}" stroke-width="${sw.toFixed(2)}" opacity="${op}" />`;

    // Reserve node dot footprint slightly in screen space
    if (isBase) {
      occupiedBoxes.push({ x1: n._sx - 44, y1: n._sy - 16, x2: n._sx + 44, y2: n._sy + 32 });
      overlayHtml += `<g pointer-events="none">` +
        `<rect x="${(n._sx - 42).toFixed(1)}" y="${(n._sy + 14).toFixed(1)}" width="84" height="18" rx="5" fill="rgba(8,12,22,0.90)" stroke="rgba(56,189,248,0.35)" stroke-width="0.9" />` +
        `<text x="${n._sx.toFixed(1)}" y="${(n._sy + 26.5).toFixed(1)}" text-anchor="middle" fill="#f8fafc" font-size="10" font-weight="700">ORIGIN 1.00x</text>` +
        `</g>`;
    } else if (!dimmed && (isSel || isHov || isGlobalBest || isRun || isIslandElite || (isWin && state.zoom >= 1.35))) {
      let prio = 5;
      if (isSel || isHov) prio = 0;
      else if (isGlobalBest) prio = 1;
      else if (isRun) prio = 2;
      else if (isIslandElite) prio = 3;
      else prio = 4;

      const mTag = shortModelTag(n.model_used, n.effort_used);
      const shortM = mTag ? ` · ${mTag}` : '';
      let lbl = '';
      if (isGlobalBest) {
        lbl = `★ BEST ${Number(n.score).toFixed(4)} (${Number(n.speedup).toFixed(3)}x)`;
      } else if (isRun) {
        const cleanTitle = (n.title || n.role).slice(0, 22);
        lbl = `⚡ ${cleanTitle}${shortM}`;
      } else if (n.valid && n.score != null) {
        lbl = `${Number(n.score).toFixed(3)} (${Number(n.speedup).toFixed(3)}x) · ${(n.title || '').slice(0, 18)}`;
      } else {
        lbl = `${n.role} · ${(n.title || '').slice(0, 20)}`;
      }
      labelCandidates.push({ node: n, isl, prio, lbl, isHighlight: isSel || isHov || isGlobalBest, isRun });
    }
  }

  // 6. Zero-Overlap Greedy Multi-Slot Label Solver (100% non-overlapping pills + leader lines!)
  labelCandidates.sort((a, b) => a.prio - b.prio || (Number(b.node.speedup || 0) - Number(a.node.speedup || 0)));

  // Keep HUD top bar & bottom legend clear of labels
  const minY = 62;
  const maxY = rect.height - 54;
  const minX = 12;
  const maxX = rect.width - 62;

  for (const cand of labelCandidates) {
    const nx = cand.node._sx;
    const ny = cand.node._sy;
    if (nx < minX || nx > maxX || ny < minY || ny > maxY) continue;

    const deg = ((Number(cand.node.angle_deg || 0) % 360) + 360) % 360;
    const onLeft = deg > 185 && deg < 355;
    const w = Math.min(255, cand.lbl.length * 6.05 + 16);
    const h = 21;

    // Candidate displacement slots radiating cleanly away from center
    const offsets = onLeft
      ? [[-14, 0], [-16, -20], [-16, 20], [-22, -38], [-22, 38], [14, -22]]
      : [[14, 0], [16, -20], [16, 20], [22, -38], [22, 38], [-14, -22]];

    let placed = null;
    for (const [dx, dy] of offsets) {
      const goLeft = dx < 0;
      const x1 = goLeft ? (nx + dx - w) : (nx + dx);
      const x2 = x1 + w;
      const y1 = ny + dy - h / 2;
      const y2 = y1 + h;
      if (x1 < minX || x2 > maxX || y1 < minY || y2 > maxY) continue;
      const box = { x1, y1, x2, y2 };
      if (!occupiedBoxes.some(b => boxesOverlap(box, b, 5))) {
        placed = { box, goLeft, anchorX: goLeft ? x2 : x1, cy: ny + dy, displaced: Math.abs(dy) > 4 };
        break;
      }
    }
    if (!placed) continue;

    occupiedBoxes.push(placed.box);
    const borderCol = cand.isHighlight ? '#38bdf8' : (cand.isRun ? 'rgba(251,191,36,0.55)' : cand.isl.color);
    const bgFill = cand.isHighlight ? 'rgba(12, 22, 39, 0.95)' : 'rgba(7, 11, 20, 0.88)';
    const txtCol = cand.isRun ? '#fde68a' : '#f1f5f9';

    overlayHtml += `<g class="lbl-pill" data-nid="${cand.node.node_id}" style="cursor:pointer;">`;
    if (placed.displaced) {
      overlayHtml += `<line x1="${nx.toFixed(1)}" y1="${ny.toFixed(1)}" x2="${placed.anchorX.toFixed(1)}" y2="${placed.cy.toFixed(1)}" stroke="${borderCol}" stroke-opacity="0.5" stroke-width="1" />`;
    }
    overlayHtml += `<rect x="${placed.box.x1.toFixed(1)}" y="${placed.box.y1.toFixed(1)}" width="${w.toFixed(1)}" height="${h}" rx="6" fill="${bgFill}" stroke="${borderCol}" stroke-opacity="${cand.isHighlight ? 0.9 : 0.42}" stroke-width="${cand.isHighlight ? 1.3 : 0.9}" />` +
                   `<text x="${(placed.box.x1 + 8).toFixed(1)}" y="${(placed.cy + 3.8).toFixed(1)}" fill="${txtCol}" font-size="10.5" font-weight="600">${escapeHtml(cand.lbl)}</text>` +
                   `</g>`;
  }

  state.screenNodes = visibleNodes;
  gRoot.innerHTML = worldHtml;
  gOverlay.innerHTML = overlayHtml;
}

// Nearest-node spatial picker (guarantees >= 48px touch comfort with ZERO overlapping hit-box conflicts!)
function findNearestNodeAtClient(clientX, clientY, isTouch) {
  const rect = svg.getBoundingClientRect();
  const vx = clientX - rect.left;
  const vy = clientY - rect.top;
  const maxDist = isTouch ? 26 : 16;
  let best = null;
  let bestScore = Infinity;

  for (const n of state.screenNodes) {
    const d = Math.hypot(n._sx - vx, n._sy - vy);
    if (d <= maxDist) {
      // Slightly favor Breakthrough/Running nodes when dots sit very close
      const bonus = (n.status === 'BREAKTHROUGH' || n.status === 'RUNNING') ? -2.5 : 0;
      if (d + bonus < bestScore) {
        bestScore = d + bonus;
        best = n;
      }
    }
  }
  return best;
}

function renderDiaryList(d) {
  const nodeMap = new Map((d.nodes || []).map(n => [n.node_id, n]));
  const entries = (d.diary || []).filter(e => state.diaryFilter === 'ALL' || e.entry_type === state.diaryFilter);
  document.getElementById('diaryCount').textContent = `${entries.length} entries`;
  document.getElementById('diaryList').innerHTML = entries.map(e => {
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

function renderMobileSheet(node) {
  if (!node || window.innerWidth > 1180) {
    mobileSheetEl.classList.remove('open');
    return;
  }
  const d = state.data;
  const isl = (d?.islands || []).find(i => Number(i.island_id) === Number(node.island_id)) || { name: 'origin', color: '#38bdf8' };
  const mTag = shortModelTag(node.model_used, node.effort_used) || 'baseline';
  const scoreTxt = node.score != null ? Number(node.score).toPrecision(6) : 'pending';
  const spTxt = node.valid && node.speedup ? `${Number(node.speedup).toFixed(4)}x` : '—';
  const desc = (node.code_summary && node.code_summary.trim()) ? node.code_summary : (node.hypothesis || 'Initial baseline origin.');
  const trimmed = desc.length > 170 ? desc.slice(0, 170) + '…' : desc;

  mobileSheetEl.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;">
      <span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);">
        <span class="leg-swatch" style="background:${isl.color}"></span>
        <b>${escapeHtml(isl.name)}</b> · ${escapeHtml(node.node_id)}
        <span class="ht-badge ${escapeHtml(node.status)}">${escapeHtml(node.role)} · ${escapeHtml(node.status)}</span>
      </span>
      <button class="pill" id="btnCloseSheet" style="min-height:32px;padding:4px 9px;">✕</button>
    </div>
    <div class="ht-title">${escapeHtml(node.title)}</div>
    <div class="ht-metrics">
      <span>Score: <b style="color:var(--win)">${escapeHtml(scoreTxt)}</b></span>
      <span>Speedup: <b style="color:var(--win)">${escapeHtml(spTxt)}</b></span>
      <span>Model: <b style="color:#c084fc">${escapeHtml(mTag)}</b></span>
    </div>
    <div class="ht-desc" style="margin-bottom:10px;">${escapeHtml(trimmed)}</div>
    <button class="pill active" id="btnOpenFullInspector" style="width:100%;min-height:40px;">Inspect Full Rationale, Diagnostics & Logs →</button>
  `;
  mobileSheetEl.classList.add('open');
  document.getElementById('btnCloseSheet')?.addEventListener('click', () => {
    mobileSheetEl.classList.remove('open');
  });
  document.getElementById('btnOpenFullInspector')?.addEventListener('click', () => {
    mobileSheetEl.classList.remove('open');
    switchMobileTab('inspector');
  });
}

function switchMobileTab(tabName) {
  const appEl = document.getElementById('appRoot');
  if (appEl) appEl.dataset.view = tabName;
  document.querySelectorAll('#mobileTabs .pill').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  requestAnimationFrame(() => renderAll());
}

async function selectNode(nodeId, { openInspectorDrawer = false } = {}) {
  if (!nodeId) return;
  state.selectedNodeId = nodeId;
  const localNode = (state.data?.nodes || []).find(n => n.node_id === nodeId);
  if (window.innerWidth <= 1180) {
    if (openInspectorDrawer) {
      mobileSheetEl.classList.remove('open');
      switchMobileTab('inspector');
    } else if (document.getElementById('appRoot')?.dataset.view === 'graph') {
      renderMobileSheet(localNode);
    }
  }
  renderAll();

  try {
    const resp = await fetch(`${API_BASE}api/node/${encodeURIComponent(nodeId)}`);
    const detail = await resp.json();
    if (!detail.ok) return;
    const n = detail.node;
    document.getElementById('inspRole').textContent = `${n.role} · ${n.status}`;
    document.getElementById('inspectorBody').innerHTML = `
      <div class="sec-box">
        <div class="sec-label">Node & Lineage Metrics</div>
        <div style="font-size:14px;font-weight:700;margin-bottom:8px;text-wrap:balance;">${escapeHtml(n.title)}</div>
        <div class="kv-grid">
          <div class="kv-cell"><div class="kv-k">Node ID</div><div class="kv-v">${escapeHtml(n.node_id)}</div></div>
          <div class="kv-cell"><div class="kv-k">Parent ID</div><div class="kv-v">${escapeHtml(n.parent_id || 'ROOT')}</div></div>
          <div class="kv-cell"><div class="kv-k">Verified Score</div><div class="kv-v" style="color:var(--win)">${n.score !== null ? n.score : '—'}</div></div>
          <div class="kv-cell"><div class="kv-k">Speedup vs Origin</div><div class="kv-v" style="color:var(--win)">${Number(n.speedup || 0).toFixed(4)}x</div></div>
          <div class="kv-cell"><div class="kv-k">Model Used</div><div class="kv-v" style="color:#c084fc;font-size:11.5px">${escapeHtml(n.model_used || '—')}</div></div>
          <div class="kv-cell"><div class="kv-k">Reasoning Effort</div><div class="kv-v" style="color:var(--gold);font-size:11.5px">${escapeHtml(n.effort_used || '—')}</div></div>
          <div class="kv-cell"><div class="kv-k">Novelty Score</div><div class="kv-v" style="color:#38bdf8;font-size:11.5px">${n.novelty_score != null ? Number(n.novelty_score).toFixed(2) : '—'} (${escapeHtml(n.origin || 'researcher')})</div></div>
          <div class="kv-cell"><div class="kv-k">tmux Window</div><div class="kv-v" style="color:var(--accent);font-size:11.5px">${escapeHtml(n.tmux_window || '—')}</div></div>
        </div>
      </div>
      ${n.novel_insight && n.novel_insight.trim() ? `
      <div class="sec-box" style="border-color:rgba(56,189,248,0.45);background:rgba(56,189,248,0.06);">
        <div class="sec-label" style="color:#38bdf8;">Novel Frontier Insight (Beyond Known Methods)</div>
        <pre class="code-block">${escapeHtml(n.novel_insight)}</pre>
      </div>` : ''}
      <div class="sec-box">
        <div class="sec-label">Hypothesis & First-Principles Rationale</div>
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

// Tooltip Helpers
const tooltipEl = document.getElementById('hoverTooltip');
function placeTooltip(clientX, clientY, htmlContent) {
  tooltipEl.innerHTML = htmlContent;
  tooltipEl.classList.add('visible');
  const rect = tooltipEl.getBoundingClientRect();
  let x = clientX + 16;
  let y = clientY + 16;
  if (x + rect.width > window.innerWidth - 12) x = Math.max(12, clientX - rect.width - 14);
  if (y + rect.height > window.innerHeight - 12) y = Math.max(12, clientY - rect.height - 14);
  tooltipEl.style.left = `${x}px`;
  tooltipEl.style.top = `${y}px`;
}
function hideTooltip() { tooltipEl.classList.remove('visible'); }

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
      body: `Verified Phase-0 starting score (${st.baseline_score ?? '—'}) plotted at the center hub.`,
    },
    best: {
      title: 'Global Best Verified Score',
      body: `Best verified score (${st.best_score ?? '—'}) across all 1,000 puzzles (${meta.direction || 'minimize'}).`,
    },
    speedup: {
      title: 'Speedup / Improvement Multiplier',
      body: `Ratio of Baseline to Global Best (${Number(st.best_speedup || 1).toFixed(4)}x).`,
    },
    goal: {
      title: 'Progress Toward Leaderboard #1 Goal',
      body: `Gap closed (<b>${(Number(st.best_goal_progress || 0) * 100).toFixed(2)}%</b>) from Baseline (${st.baseline_score ?? '—'}) to Leaderboard #1 (${meta.leaderboard_target ?? st.leaderboard_target ?? 'open'}).`,
    },
    lb1: {
      title: 'Public Leaderboard #1 & Top-5 Submit Gate',
      body: `<div style="margin-bottom:5px;">• <b>Leaderboard #1 Score:</b> <b>${meta.leaderboard_number_one ?? st.leaderboard_number_one ?? '—'}</b></div>` +
            `<div>• <b>Auto-Submit Gate (Top 5):</b> <b>${meta.leaderboard_submit_threshold ?? st.leaderboard_submit_threshold ?? '—'}</b>. Submissions fire automatically once our verified score enters the Top 5.</div>`,
    },
    created: {
      title: 'Total Hypotheses Created',
      body: `<b>${created}</b> experiment nodes generated so far (<b>${queued}</b> queued).`,
    },
    tested: {
      title: 'Tested Hypotheses vs. Total Eval Runs',
      body: `<div style="margin-bottom:5px;">• <b>First number (${tested}) — Tested Hypotheses:</b> Unique hypothesis nodes built and evaluated to completion.</div>` +
            `<div>• <b>In parentheses (${runs}) — Total Eval Suite Runs:</b> Total executions of <code>local_eval.sh</code> across iterative <code>swarm-eval</code> and <code>swarm-submit</code> passes.</div>`,
    },
    testing: {
      title: 'Hypotheses Being Tested Right Now',
      body: `<b>${testing}</b> active agents running in live <code>tmux</code> windows (concurrency cap <b>${meta.concurrency || '—'}</b>).`,
    },
    avgeval: {
      title: 'Average Verification & Benchmark Duration',
      body: `Mean wall-clock duration (<b>${avgMs.toFixed(1)} ms</b> = <b>${(avgMs / 1000).toFixed(2)} s</b>) across ${runs} full benchmark runs.`,
    },
    ram: {
      title: 'Sparky Non-Reclaimable User-Slice RAM',
      body: `Active process RAM in <code>user-1000.slice</code> (${gov.slice_current_gib ?? '—'} GiB used, ${gov.mem_avail_gib ?? '—'} GiB system RAM free).`,
    },
  };
  const info = docs[kpiKey];
  return info ? `<div class="ht-title">${info.title}</div><div class="ht-desc">${info.body}</div>` : '';
}

function buildNodeTooltipHtml(nodeId) {
  const d = state.data;
  if (!d) return '';
  const n = (d.nodes || []).find(x => x.node_id === nodeId);
  if (!n) return '';
  const isl = (d.islands || []).find(i => Number(i.island_id) === Number(n.island_id)) || { name: 'origin', color: '#38bdf8' };
  const mTag = shortModelTag(n.model_used, n.effort_used) || 'baseline';
  const scoreTxt = n.score != null ? Number(n.score).toPrecision(6) : 'pending';
  const spTxt = n.valid && n.speedup ? `${Number(n.speedup).toFixed(4)}x` : '—';
  const rawDesc = (n.code_summary && n.code_summary.trim())
    ? `${n.hypothesis || ''}\n\nResult: ${n.code_summary}`
    : (n.hypothesis || n.rationale || 'Initial verified reference point at center origin.');
  const trimmedDesc = rawDesc.length > 280 ? rawDesc.slice(0, 280) + '…' : rawDesc;

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
    <div class="ht-foot">Click or tap node to inspect full rationale, diagnostics & logs</div>
  `;
}

document.getElementById('kpiBar').addEventListener('pointermove', e => {
  if (e.pointerType === 'touch') return;
  const box = e.target.closest('.kpi[data-kpi]');
  if (!box) { hideTooltip(); return; }
  const html = buildKpiTooltipHtml(box.dataset.kpi);
  if (html) placeTooltip(e.clientX, e.clientY, html);
});
document.getElementById('kpiBar').addEventListener('pointerleave', hideTooltip);
document.getElementById('kpiBar').addEventListener('click', e => {
  const box = e.target.closest('.kpi[data-kpi]');
  if (!box) return;
  const html = buildKpiTooltipHtml(box.dataset.kpi);
  if (html) {
    placeTooltip(e.clientX, e.clientY, html);
    setTimeout(hideTooltip, 3800);
  }
});

// Unified Multi-Touch Pinch-Zoom + 1-Finger Pan + Spatial Tap/Hover Engine
const pointers = new Map();
let gestureStartDist = 0;
let gestureStartZoom = 1.0;
let gestureStartMid = null;
let dragMoved = false;
let pointerDownPos = { x: 0, y: 0 };
let rafScheduled = false;

function scheduleRender() {
  if (rafScheduled) return;
  rafScheduled = true;
  requestAnimationFrame(() => {
    rafScheduled = false;
    renderAll();
  });
}

function zoomAroundViewportPoint(newZoom, vx, vy) {
  const rect = svg.getBoundingClientRect();
  const clamped = Math.max(0.45, Math.min(6.0, newZoom));
  const oldCx = rect.width / 2 + state.panX;
  const oldCy = rect.height / 2 + state.panY;
  const worldX = (vx - oldCx) / state.zoom;
  const worldY = (vy - oldCy) / state.zoom;
  state.zoom = clamped;
  state.panX = vx - worldX * clamped - rect.width / 2;
  state.panY = vy - worldY * clamped - rect.height / 2;
  scheduleRender();
}

svg.addEventListener('pointerdown', e => {
  svg.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, type: e.pointerType });
  if (pointers.size === 1) {
    dragMoved = false;
    pointerDownPos = { x: e.clientX, y: e.clientY };
    svg.classList.add('dragging');
  } else if (pointers.size === 2) {
    const pts = [...pointers.values()];
    gestureStartDist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y) || 1;
    gestureStartZoom = state.zoom;
    const rect = svg.getBoundingClientRect();
    gestureStartMid = {
      vx: (pts[0].x + pts[1].x) / 2 - rect.left,
      vy: (pts[0].y + pts[1].y) / 2 - rect.top,
    };
    hideTooltip();
  }
});

svg.addEventListener('pointermove', e => {
  if (!pointers.has(e.pointerId)) {
    // Desktop hover (no button pressed)
    if (e.pointerType !== 'touch') {
      const pillEl = e.target.closest?.('.lbl-pill[data-nid]');
      const hitNode = pillEl
        ? (state.data?.nodes || []).find(x => x.node_id === pillEl.getAttribute('data-nid'))
        : findNearestNodeAtClient(e.clientX, e.clientY, false);
      const nextHov = hitNode ? hitNode.node_id : null;
      svg.style.cursor = nextHov ? 'pointer' : 'grab';
      if (nextHov !== state.hoveredNodeId) {
        state.hoveredNodeId = nextHov;
        scheduleRender();
      }
      if (nextHov) {
        const html = buildNodeTooltipHtml(nextHov);
        if (html) placeTooltip(e.clientX, e.clientY, html);
      } else {
        hideTooltip();
      }
    }
    return;
  }

  const prev = pointers.get(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, type: e.pointerType });

  if (pointers.size === 1) {
    const dx = e.clientX - prev.x;
    const dy = e.clientY - prev.y;
    if (Math.hypot(e.clientX - pointerDownPos.x, e.clientY - pointerDownPos.y) > 6) {
      dragMoved = true;
      hideTooltip();
    }
    if (dragMoved) {
      state.panX += dx;
      state.panY += dy;
      scheduleRender();
    }
  } else if (pointers.size === 2) {
    dragMoved = true;
    const pts = [...pointers.values()];
    const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y) || 1;
    const rect = svg.getBoundingClientRect();
    const midVx = (pts[0].x + pts[1].x) / 2 - rect.left;
    const midVy = (pts[0].y + pts[1].y) / 2 - rect.top;
    if (gestureStartMid) {
      state.panX += (midVx - gestureStartMid.vx);
      state.panY += (midVy - gestureStartMid.vy);
      gestureStartMid = { vx: midVx, vy: midVy };
    }
    zoomAroundViewportPoint(gestureStartZoom * (dist / gestureStartDist), midVx, midVy);
  }
});

function endPointer(e) {
  const wasSingleTap = pointers.size === 1 && !dragMoved;
  const pType = pointers.get(e.pointerId)?.type || e.pointerType;
  pointers.delete(e.pointerId);
  if (pointers.size === 0) svg.classList.remove('dragging');
  if (pointers.size < 2) gestureStartMid = null;

  if (wasSingleTap) {
    const hitDom = document.elementFromPoint(e.clientX, e.clientY);
    const pillEl = hitDom?.closest?.('.lbl-pill[data-nid]');
    const nid = pillEl
      ? pillEl.getAttribute('data-nid')
      : findNearestNodeAtClient(e.clientX, e.clientY, pType === 'touch')?.node_id;
    if (nid) {
      selectNode(nid);
    } else {
      state.selectedNodeId = null;
      mobileSheetEl.classList.remove('open');
      scheduleRender();
    }
  }
}

svg.addEventListener('pointerup', endPointer);
svg.addEventListener('pointercancel', endPointer);
svg.addEventListener('pointerleave', e => {
  if (!pointers.has(e.pointerId)) {
    state.hoveredNodeId = null;
    hideTooltip();
    scheduleRender();
  }
});

svg.addEventListener('wheel', e => {
  e.preventDefault();
  const rect = svg.getBoundingClientRect();
  const vx = e.clientX - rect.left;
  const vy = e.clientY - rect.top;
  const factor = Math.exp(-e.deltaY * 0.0016);
  zoomAroundViewportPoint(state.zoom * factor, vx, vy);
}, { passive: false });

svg.addEventListener('dblclick', e => {
  const rect = svg.getBoundingClientRect();
  zoomAroundViewportPoint(state.zoom < 2.6 ? state.zoom * 1.55 : 1.0, e.clientX - rect.left, e.clientY - rect.top);
});

// Controls & Navigation
document.getElementById('btnZoomIn').addEventListener('click', () => {
  const r = svg.getBoundingClientRect();
  zoomAroundViewportPoint(state.zoom * 1.32, r.width / 2, r.height / 2);
});
document.getElementById('btnZoomOut').addEventListener('click', () => {
  const r = svg.getBoundingClientRect();
  zoomAroundViewportPoint(state.zoom / 1.32, r.width / 2, r.height / 2);
});
document.getElementById('btnResetView').addEventListener('click', () => {
  state.panX = 0; state.panY = 0; state.zoom = 1.0; state.selectedNodeId = null;
  mobileSheetEl.classList.remove('open');
  renderAll();
});
document.getElementById('btnBackToGraph').addEventListener('click', () => switchMobileTab('graph'));

document.getElementById('diaryList').addEventListener('click', e => {
  const card = e.target.closest('.diary-card');
  if (card && card.dataset.nid) selectNode(card.dataset.nid, { openInspectorDrawer: window.innerWidth <= 1180 });
});
document.getElementById('diaryFilters').addEventListener('click', e => {
  const btn = e.target.closest('.pill');
  if (!btn) return;
  document.querySelectorAll('#diaryFilters .pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.diaryFilter = btn.dataset.filter;
  renderAll();
});
document.getElementById('scaleBar').addEventListener('click', e => {
  const btn = e.target.closest('.pill[data-scale]');
  if (!btn) return;
  document.querySelectorAll('#scaleBar .pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.scaleMode = btn.dataset.scale;
  renderAll();
});
document.getElementById('densityBar').addEventListener('click', e => {
  const btn = e.target.closest('.pill[data-density]');
  if (!btn) return;
  document.querySelectorAll('#densityBar .pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.nodeDensity = btn.dataset.density;
  renderAll();
});
document.getElementById('mobileTabs')?.addEventListener('click', e => {
  const btn = e.target.closest('.pill');
  if (!btn) return;
  switchMobileTab(btn.dataset.tab);
});

window.addEventListener('resize', () => scheduleRender());

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
            elif path == "/api/submission.csv":
                sub_f = self.ws / "vault" / "hosted_outbox" / "submission.csv"
                if not sub_f.is_file():
                    self._write_resp(writer, 404, "text/plain", b"No staged submission.csv found")
                else:
                    raw_csv = sub_f.read_bytes()
                    hdr = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/csv; charset=utf-8\r\n"
                        'Content-Disposition: attachment; filename="submission.csv"\r\n'
                        f"Content-Length: {len(raw_csv)}\r\n"
                        "Cache-Control: no-store\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                    writer.write(hdr)
                    writer.write(raw_csv)
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
