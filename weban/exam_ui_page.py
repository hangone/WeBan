"""WeBan 交互式答题控制台单页 HTML/CSS/JS 资源（内置于模块中，无需额外打包静态文件）。"""

from __future__ import annotations

EXAM_UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>WeBan 答题</title>
<style>
  :root {
    --bg: #f5f6f8;
    --surface: #ffffff;
    --surface-2: #f8f9fb;
    --surface-3: #eff1f5;
    --border: #e2e6ed;
    --border-strong: #cbd2de;
    --text: #111827;
    --text-2: #4b5563;
    --text-3: #6b7280;
    --accent: #2563eb;
    --accent-soft: #eff6ff;
    --accent-border: #bfdbfe;
    --green: #059669;
    --green-soft: #ecfdf5;
    --green-border: #a7f3d0;
    --amber: #d97706;
    --amber-soft: #fffbeb;
    --amber-border: #fde68a;
    --teal: #0d9488;
    --teal-soft: #f0fdfa;
    --teal-border: #99f6e4;
    --rose: #dc2626;
    --rose-soft: #fef2f2;
    --rose-border: #fecaca;
    --indigo: #4f46e5;
    --indigo-soft: #eef2ff;
    --indigo-border: #c7d2fe;
    --shadow-lg: 0 12px 32px rgba(15, 23, 42, 0.14);
    --radius: 8px;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Cascadia Code", monospace;
  }

  [data-theme="dark"] {
    --bg: #0f141e;
    --surface: #161c28;
    --surface-2: #1c2433;
    --surface-3: #232d40;
    --border: #2a3548;
    --border-strong: #3b4a64;
    --text: #f3f4f6;
    --text-2: #cbd5e1;
    --text-3: #94a3b8;
    --accent: #60a5fa;
    --accent-soft: rgba(37, 99, 235, 0.18);
    --accent-border: rgba(96, 165, 250, 0.42);
    --green: #34d399;
    --green-soft: rgba(5, 150, 105, 0.16);
    --green-border: rgba(52, 211, 153, 0.38);
    --amber: #fbbf24;
    --amber-soft: rgba(217, 119, 6, 0.18);
    --amber-border: rgba(251, 191, 36, 0.4);
    --teal: #2dd4bf;
    --teal-soft: rgba(13, 148, 136, 0.18);
    --teal-border: rgba(45, 212, 191, 0.4);
    --rose: #f87171;
    --rose-soft: rgba(220, 38, 38, 0.18);
    --rose-border: rgba(248, 113, 113, 0.4);
    --indigo: #818cf8;
    --indigo-soft: rgba(79, 70, 229, 0.22);
    --indigo-border: rgba(129, 140, 248, 0.42);
    --shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.55);
  }

  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-sans);
    font-size: 14px;
    line-height: 1.55;
    overflow: hidden;
  }

  .app {
    display: flex;
    flex-direction: column;
    height: 100vh;
  }

  /* ── Top Bar ── */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    z-index: 10;
    flex-wrap: wrap;
  }
  .topbar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .exam-title {
    font-weight: 700;
    font-size: 15px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 300px;
  }
  .user-label {
    color: var(--text-3);
    font-size: 13px;
    white-space: nowrap;
  }
  .model-pill {
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    white-space: nowrap;
    max-width: 340px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 9px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    white-space: nowrap;
  }
  .pill.running { background: var(--indigo-soft); border-color: var(--indigo-border); color: var(--indigo); }
  .pill.paused { background: var(--amber-soft); border-color: var(--amber-border); color: var(--amber); }
  .pill.ready { background: var(--teal-soft); border-color: var(--teal-border); color: var(--teal); }
  .pill.submitted { background: var(--green-soft); border-color: var(--green-border); color: var(--green); }
  .pill.aborted { background: var(--rose-soft); border-color: var(--rose-border); color: var(--rose); }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
  }

  .timer {
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 600;
    padding: 4px 9px;
    border-radius: 6px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text-2);
  }
  .timer.warn { color: var(--rose); border-color: var(--rose-border); background: var(--rose-soft); }

  /* ── Buttons & Keycaps ── */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 5px 11px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
    user-select: none;
    white-space: nowrap;
  }
  .btn:hover:not(:disabled) { background: var(--surface-2); border-color: var(--border-strong); }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }
  .btn-primary:hover:not(:disabled) { filter: brightness(1.06); background: var(--accent); border-color: var(--accent); }
  .btn-success {
    background: var(--green);
    border-color: var(--green);
    color: #fff;
    font-weight: 600;
  }
  .btn-success:hover:not(:disabled) { filter: brightness(1.05); background: var(--green); border-color: var(--green); }
  .btn-danger-ghost {
    color: var(--rose);
    border-color: var(--rose-border);
    background: var(--rose-soft);
  }
  .btn-active {
    background: var(--accent-soft);
    border-color: var(--accent-border);
    color: var(--accent);
  }
  .btn-sm { padding: 4px 8px; font-size: 12px; }

  kbd {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 4px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
    line-height: 1;
    border-radius: 4px;
    border: 1px solid var(--border-strong);
    background: var(--surface-2);
    color: var(--text-3);
  }
  .btn-primary kbd, .btn-success kbd {
    background: rgba(255, 255, 255, 0.2);
    border-color: rgba(255, 255, 255, 0.35);
    color: #fff;
  }

  /* ── Workspace Grid ── */
  .workspace {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: 248px minmax(0, 1.3fr) minmax(330px, 1fr);
    gap: 12px;
    padding: 12px 16px;
  }
  @media (max-width: 1120px) {
    .workspace {
      grid-template-columns: 208px minmax(0, 1fr) 320px;
      gap: 10px;
      padding: 10px;
    }
  }
  @media (max-width: 860px) {
    html, body { overflow: auto; }
    .app { height: auto; min-height: 100vh; }
    .workspace { grid-template-columns: 1fr; }
  }

  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 9px 13px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
    font-weight: 600;
    font-size: 13px;
  }
  .panel-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 12px;
  }

  /* ── Left Navigator ── */
  .progress-wrap {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
  }
  .progress-bar {
    height: 6px;
    border-radius: 999px;
    background: var(--surface-3);
    overflow: hidden;
    display: flex;
  }
  .prog-seg { height: 100%; transition: width 0.2s ease; }
  .prog-bank { background: var(--green); }
  .prog-manual { background: var(--teal); }
  .prog-ai { background: var(--amber); }

  .stat-chips {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 6px;
    margin-top: 9px;
  }
  .stat-chip {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px;
    border-radius: 6px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    font-size: 12px;
    color: var(--text-2);
  }
  .stat-chip strong { color: var(--text); font-family: var(--font-mono); }

  .filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    padding: 9px 12px;
    border-bottom: 1px solid var(--border);
  }
  .filter-chip {
    padding: 2px 8px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-2);
    font-size: 12px;
    cursor: pointer;
  }
  .filter-chip:hover { border-color: var(--border-strong); color: var(--text); }
  .filter-chip.active {
    background: var(--accent-soft);
    border-color: var(--accent-border);
    color: var(--accent);
    font-weight: 600;
  }

  .q-grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 6px;
  }
  .q-tile {
    position: relative;
    height: 34px;
    border-radius: 6px;
    border: 1.5px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .q-tile:hover { border-color: var(--accent); }
  .q-tile.st-bank { background: var(--green-soft); border-color: var(--green-border); color: var(--green); }
  .q-tile.st-ai-thinking {
    background: var(--indigo-soft);
    border-color: var(--indigo);
    color: var(--indigo);
    animation: pulse 1.2s infinite;
  }
  .q-tile.st-ai-pending-review { background: var(--amber-soft); border-color: var(--amber-border); color: var(--amber); }
  .q-tile.st-reviewed { background: var(--teal-soft); border-color: var(--teal-border); color: var(--teal); }
  .q-tile.st-failed { background: var(--rose-soft); border-color: var(--rose-border); color: var(--rose); }
  .q-tile.selected {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
    z-index: 2;
  }
  .q-tile .dot-flag {
    position: absolute;
    top: 3px;
    right: 3px;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--rose);
  }
  .q-tile .dot-unsynced {
    position: absolute;
    bottom: 3px;
    right: 3px;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--amber);
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 10px;
    padding: 8px 12px;
    border-top: 1px solid var(--border);
    font-size: 11px;
    color: var(--text-3);
    background: var(--surface-2);
  }
  .legend-item { display: inline-flex; align-items: center; gap: 4px; }
  .legend-sw { width: 8px; height: 8px; border-radius: 2px; border: 1px solid transparent; }

  /* ── Center Question Stage ── */
  .q-stage-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }
  .q-badges {
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
  }
  .q-num {
    font-family: var(--font-mono);
    font-weight: 700;
    font-size: 14px;
    color: var(--text);
  }
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    border-radius: 5px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-2);
  }
  .badge-type { background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent); }
  .badge-bank { background: var(--green-soft); border-color: var(--green-border); color: var(--green); }
  .badge-ai { background: var(--amber-soft); border-color: var(--amber-border); color: var(--amber); }
  .badge-manual { background: var(--teal-soft); border-color: var(--teal-border); color: var(--teal); }
  .badge-thinking { background: var(--indigo-soft); border-color: var(--indigo-border); color: var(--indigo); }
  .badge-error { background: var(--rose-soft); border-color: var(--rose-border); color: var(--rose); }

  .q-stage-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 18px 20px;
  }
  .follow-banner {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 7px 11px;
    margin-bottom: 14px;
    border-radius: 7px;
    background: var(--indigo-soft);
    border: 1px solid var(--indigo-border);
    color: var(--indigo);
    font-size: 12.5px;
  }
  .follow-banner.show { display: flex; }

  .q-title {
    font-size: 16.5px;
    font-weight: 600;
    line-height: 1.65;
    color: var(--text);
    margin: 0 0 16px 0;
    white-space: pre-wrap;
  }

  .options-list {
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .option-card {
    display: flex;
    align-items: flex-start;
    gap: 11px;
    padding: 12px 14px;
    border-radius: 8px;
    border: 1.5px solid var(--border);
    background: var(--surface);
    cursor: pointer;
    transition: border-color 0.12s, background 0.12s;
    text-align: left;
    width: 100%;
    color: var(--text);
    font-size: 14.5px;
    line-height: 1.55;
  }
  .option-card:hover:not(:disabled) {
    border-color: var(--accent);
    background: var(--surface-2);
  }
  .option-card.chosen {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .option-card.chosen.manual-chosen {
    border-color: var(--teal);
    background: var(--teal-soft);
  }
  .option-card.chosen.bank-chosen {
    border-color: var(--green);
    background: var(--green-soft);
  }
  .opt-key {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    border: 1.5px solid var(--border-strong);
    background: var(--surface-2);
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: 12.5px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-top: 1px;
  }
  .opt-key.single { border-radius: 50%; }
  .option-card.chosen .opt-key {
    border-color: var(--accent);
    background: var(--accent);
    color: #fff;
  }
  .option-card.chosen.manual-chosen .opt-key {
    border-color: var(--teal);
    background: var(--teal);
    color: #fff;
  }
  .option-card.chosen.bank-chosen .opt-key {
    border-color: var(--green);
    background: var(--green);
    color: #fff;
  }
  .opt-text {
    flex: 1;
    min-width: 0;
    word-break: break-word;
  }
  .opt-ai-tag {
    flex-shrink: 0;
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 4px;
    background: var(--amber-soft);
    border: 1px solid var(--amber-border);
    color: var(--amber);
    font-weight: 600;
    margin-top: 2px;
  }

  .q-notice {
    margin-top: 12px;
    padding: 9px 12px;
    border-radius: 7px;
    font-size: 12.5px;
    font-family: var(--font-mono);
    border: 1px solid var(--rose-border);
    background: var(--rose-soft);
    color: var(--rose);
    display: none;
    word-break: break-all;
  }
  .q-notice.show { display: block; }

  .q-stage-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 16px;
    border-top: 1px solid var(--border);
    background: var(--surface-2);
    flex-wrap: wrap;
  }

  /* ── Right Agent & Telemetry Panel ── */
  .ai-controls {
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .select-effort {
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text);
    font-size: 12px;
    font-family: var(--font-mono);
  }

  .telemetry-strip {
    padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }
  .telemetry-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 6px;
  }
  .metric-cell {
    padding: 5px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .metric-label {
    font-size: 11px;
    color: var(--text-3);
  }
  .metric-val {
    font-family: var(--font-mono);
    font-size: 12.5px;
    font-weight: 700;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .token-line {
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: 11.5px;
    color: var(--text-2);
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .token-cached { color: var(--green); font-weight: 600; }

  .section-box {
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
    margin-bottom: 10px;
    overflow: hidden;
  }
  .section-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-2);
    border-bottom: 1px solid var(--border);
    background: var(--surface-3);
  }
  .section-content {
    padding: 9px 11px;
    font-size: 13px;
    color: var(--text);
  }
  .tool-list {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-bottom: 7px;
  }
  .tool-item {
    font-family: var(--font-mono);
    font-size: 11.5px;
    padding: 4px 7px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-2);
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }
  .tool-item-q {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tool-item-meta {
    flex-shrink: 0;
    color: var(--text-3);
  }
  .kb-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }
  .kb-chip {
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    border-radius: 5px;
    background: var(--surface);
    border: 1px solid var(--border);
    font-size: 12px;
    font-family: var(--font-mono);
    color: var(--text-2);
  }
  .thinking-box {
    font-family: var(--font-mono);
    font-size: 12.5px;
    line-height: 1.6;
    color: var(--text-2);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 300px;
    overflow-y: auto;
    padding: 9px 11px;
  }
  .stream-cursor {
    display: inline-block;
    width: 6px;
    height: 13px;
    background: var(--indigo);
    vertical-align: -2px;
    margin-left: 2px;
    animation: blink 0.9s steps(2) infinite;
  }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

  .reason-box {
    font-size: 13.5px;
    line-height: 1.65;
    color: var(--text);
    word-break: break-word;
  }
  .reason-box p { margin: 0 0 6px 0; }
  .reason-box p:last-child { margin-bottom: 0; }
  .reason-box blockquote {
    margin: 6px 0;
    padding: 5px 10px;
    border-left: 3px solid var(--accent);
    background: var(--surface);
    color: var(--text-2);
    border-radius: 0 5px 5px 0;
  }
  .reason-box ul { margin: 4px 0 6px 18px; padding: 0; }
  .reason-box li { margin-bottom: 3px; }
  .reason-box code {
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 1px 4px;
    border-radius: 4px;
    background: var(--surface-3);
  }
  .empty-hint {
    color: var(--text-3);
    font-size: 12.5px;
  }

  /* ── Shortcut Footer ── */
  .shortcut-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 16px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    font-size: 12px;
    color: var(--text-3);
    overflow-x: auto;
    white-space: nowrap;
  }
  .shortcut-list { display: flex; align-items: center; gap: 14px; }
  .shortcut-item { display: inline-flex; align-items: center; gap: 4px; }

  /* ── Modals & Toast ── */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(15, 23, 42, 0.5);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 16px;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    width: 100%;
    max-width: 440px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    box-shadow: var(--shadow-lg);
    overflow: hidden;
  }
  .modal-head {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    font-weight: 700;
    font-size: 14.5px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .modal-body { padding: 14px 16px; font-size: 13.5px; color: var(--text-2); }
  .modal-foot {
    padding: 10px 16px;
    border-top: 1px solid var(--border);
    background: var(--surface-2);
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    margin: 4px 0 8px 0;
  }
  .summary-cell {
    padding: 8px 10px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .summary-cell strong { font-size: 14.5px; font-family: var(--font-mono); color: var(--text); }
  .warn-box {
    padding: 8px 11px;
    border-radius: 7px;
    background: var(--amber-soft);
    border: 1px solid var(--amber-border);
    color: var(--amber);
    font-size: 12.5px;
    margin-top: 8px;
  }
  .score-hero {
    text-align: center;
    padding: 14px 8px;
  }
  .score-number {
    font-size: 42px;
    font-weight: 800;
    font-family: var(--font-mono);
    color: var(--green);
    line-height: 1.1;
  }

  .toast-stack {
    position: fixed;
    top: 56px;
    right: 16px;
    display: flex;
    flex-direction: column;
    gap: 7px;
    z-index: 120;
    pointer-events: none;
  }
  .toast {
    padding: 8px 13px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    box-shadow: var(--shadow-lg);
    font-size: 13px;
    max-width: 340px;
  }
  .toast.info { border-left: 3px solid var(--accent); }
  .toast.success { border-left: 3px solid var(--green); }
  .toast.warning { border-left: 3px solid var(--amber); }
  .toast.error { border-left: 3px solid var(--rose); }

  .shortcut-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .shortcut-table td {
    padding: 6px 4px;
    border-bottom: 1px solid var(--border);
  }
  .shortcut-table td:first-child { width: 130px; }
</style>
</head>
<body>
<div class="app">
  <!-- Top Bar -->
  <header class="topbar">
    <div class="topbar-left">
      <span class="exam-title" id="examTitle">加载中...</span>
      <span class="user-label" id="userLabel"></span>
      <span class="model-pill" id="modelEndpointPill" title="当前接入的模型与接口地址">未配置模型</span>
      <span class="pill running" id="phasePill">连接中</span>
    </div>
    <div class="topbar-right">
      <div class="timer" id="examTimer">--:--</div>
      <button class="btn" id="btnPause"><span id="pauseLabel">暂停</span> <kbd>P</kbd></button>
      <button class="btn" id="btnFollow">跟随 <kbd>G</kbd></button>
      <button class="btn" id="btnTheme">主题</button>
      <button class="btn" id="btnHelp"><kbd>?</kbd></button>
      <button class="btn btn-danger-ghost" id="btnAbort">放弃</button>
      <button class="btn btn-success" id="btnSubmit">交卷 <kbd>Ctrl+↵</kbd></button>
    </div>
  </header>

  <!-- Main 3-Column Workbench -->
  <main class="workspace">
    <!-- Left: Question Navigator -->
    <aside class="panel">
      <div class="panel-header">
        <span>答题卡</span>
        <span id="navCount" style="font-family:var(--font-mono);color:var(--text-3)">0/0</span>
      </div>
      <div class="progress-wrap">
        <div class="progress-bar">
          <div class="prog-seg prog-bank" id="progBank" style="width:0%"></div>
          <div class="prog-seg prog-manual" id="progManual" style="width:0%"></div>
          <div class="prog-seg prog-ai" id="progAi" style="width:0%"></div>
        </div>
        <div class="stat-chips">
          <div class="stat-chip"><span>题库</span><strong id="stBank">0</strong></div>
          <div class="stat-chip"><span>AI未核</span><strong id="stAiUnrev" style="color:var(--amber)">0</strong></div>
          <div class="stat-chip"><span>已核</span><strong id="stReviewed" style="color:var(--teal)">0</strong></div>
          <div class="stat-chip"><span>未答</span><strong id="stUnanswered" style="color:var(--rose)">0</strong></div>
        </div>
      </div>
      <div class="filter-bar" id="filterBar">
        <button class="filter-chip active" data-filter="all">全部</button>
        <button class="filter-chip" data-filter="ai">AI</button>
        <button class="filter-chip" data-filter="unreviewed">未核</button>
        <button class="filter-chip" data-filter="manual">已改</button>
        <button class="filter-chip" data-filter="flagged">标星</button>
        <button class="filter-chip" data-filter="unanswered">未答</button>
      </div>
      <div class="panel-body">
        <div class="q-grid" id="qGrid"></div>
      </div>
      <div class="legend">
        <span class="legend-item"><span class="legend-sw" style="background:var(--green-soft);border-color:var(--green-border)"></span>题库</span>
        <span class="legend-item"><span class="legend-sw" style="background:var(--indigo-soft);border-color:var(--indigo)"></span>思考</span>
        <span class="legend-item"><span class="legend-sw" style="background:var(--amber-soft);border-color:var(--amber-border)"></span>AI未核</span>
        <span class="legend-item"><span class="legend-sw" style="background:var(--teal-soft);border-color:var(--teal-border)"></span>已核</span>
        <span class="legend-item"><span class="legend-sw" style="background:var(--rose-soft);border-color:var(--rose-border)"></span>未答</span>
      </div>
    </aside>

    <!-- Center: Question & Options -->
    <section class="panel">
      <div class="q-stage-head">
        <div class="q-badges">
          <span class="q-num" id="qNumber">#1 / 1</span>
          <span class="badge badge-type" id="qType">单选题</span>
          <span class="badge" id="qSourceBadge">等待</span>
          <span class="badge" id="qSyncBadge">未同步</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="btn btn-sm" id="btnReview"><span id="reviewText">核对</span> <kbd>V</kbd></button>
          <button class="btn btn-sm" id="btnFlag"><span id="flagText">标星</span> <kbd>F</kbd></button>
        </div>
      </div>

      <div class="q-stage-body">
        <div class="follow-banner" id="followBanner">
          <span id="followBannerText">正在执行第 1 题</span>
          <button class="btn btn-sm btn-active" id="btnJumpActive">跳转 <kbd>G</kbd></button>
        </div>

        <h2 class="q-title" id="qTitle">加载中...</h2>
        <div class="options-list" id="optionsList"></div>
        <div class="q-notice" id="qNotice"></div>
      </div>

      <div class="q-stage-foot">
        <button class="btn" id="btnPrev"><kbd>←</kbd> 上一题</button>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-active" id="btnNextReview">下一未核 <kbd>N</kbd></button>
          <button class="btn" id="btnRevertAi" style="display:none">恢复 AI</button>
        </div>
        <button class="btn" id="btnNext">下一题 <kbd>→</kbd></button>
      </div>
    </section>

    <!-- Right: Agent Telemetry, Tools, Thinking & Reason -->
    <aside class="panel">
      <div class="panel-header">
        <span>Agent 遥测与推理</span>
        <span id="aiRoundTag" style="font-family:var(--font-mono);font-size:11.5px;color:var(--text-3)"></span>
      </div>
      <div class="ai-controls">
        <label for="effortSelect" style="font-size:12px;color:var(--text-2)">思考强度</label>
        <select id="effortSelect" class="select-effort">
          <option value="">默认</option>
          <option value="none">none</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
          <option value="xhigh">xhigh</option>
        </select>
        <button class="btn btn-sm btn-primary" id="btnRerunAi" style="margin-left:auto">重跑 <kbd>R</kbd></button>
      </div>

      <!-- Live LLM Metrics -->
      <div class="telemetry-strip">
        <div class="telemetry-grid">
          <div class="metric-cell">
            <span class="metric-label">TTFT（本题 / 均值）</span>
            <span class="metric-val" id="mTtft">—</span>
          </div>
          <div class="metric-cell">
            <span class="metric-label">TPS（本题 / 均值）</span>
            <span class="metric-val" id="mTps">—</span>
          </div>
          <div class="metric-cell">
            <span class="metric-label">上下文长度</span>
            <span class="metric-val" id="mContext">—</span>
          </div>
          <div class="metric-cell">
            <span class="metric-label">总 Token（本题 / 累计）</span>
            <span class="metric-val" id="mTokens">—</span>
          </div>
        </div>
        <div class="token-line" id="mTokenDetail">
          <span>入 0</span>
          <span>出 0</span>
          <span class="token-cached" id="mCached">缓存 0</span>
        </div>
      </div>

      <div class="panel-body">
        <div class="section-box">
          <div class="section-title">
            <span>知识库协作</span>
            <span id="kbModeLabel" style="font-family:var(--font-mono);font-weight:500">无</span>
          </div>
          <div class="section-content" id="kbSourcesBox">
            <span class="empty-hint">—</span>
          </div>
        </div>

        <div class="section-box">
          <div class="section-title">
            <span>思考过程</span>
            <span id="thinkLenLabel" style="font-family:var(--font-mono);font-weight:500">0 字</span>
          </div>
          <div class="thinking-box" id="thinkingBox">
            <span class="empty-hint">—</span>
          </div>
        </div>

        <div class="section-box">
          <div class="section-title">
            <span>作答理由</span>
          </div>
          <div class="section-content reason-box" id="reasonBox">
            <span class="empty-hint">—</span>
          </div>
        </div>
      </div>
    </aside>
  </main>

  <!-- Shortcut Footer -->
  <footer class="shortcut-bar">
    <div class="shortcut-list">
      <span class="shortcut-item"><kbd>←</kbd><kbd>→</kbd> 切题</span>
      <span class="shortcut-item"><kbd>1</kbd>-<kbd>9</kbd> / <kbd>A</kbd>-<kbd>I</kbd> 选答案</span>
      <span class="shortcut-item"><kbd>N</kbd> 下一未核</span>
      <span class="shortcut-item"><kbd>V</kbd> 核对</span>
      <span class="shortcut-item"><kbd>F</kbd> 标星</span>
      <span class="shortcut-item"><kbd>P</kbd> 暂停</span>
      <span class="shortcut-item"><kbd>R</kbd> 重跑 AI</span>
      <span class="shortcut-item"><kbd>Ctrl+↵</kbd> 交卷</span>
    </div>
    <span id="connStatus">连接中</span>
  </footer>
</div>

<div class="toast-stack" id="toastStack"></div>

<!-- Submit Confirm Modal -->
<div class="modal-backdrop" id="submitModal">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <span>确认交卷</span>
      <button class="btn btn-sm" onclick="closeModal('submitModal')">✕</button>
    </div>
    <div class="modal-body">
      <div class="summary-grid" id="submitSummary"></div>
      <div class="warn-box" id="submitWarn" style="display:none"></div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal('submitModal')">取消</button>
      <button class="btn btn-success" id="btnConfirmSubmit">确认交卷</button>
    </div>
  </div>
</div>

<!-- Abort Confirm Modal -->
<div class="modal-backdrop" id="abortModal">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <span>放弃交卷</span>
      <button class="btn btn-sm" onclick="closeModal('abortModal')">✕</button>
    </div>
    <div class="modal-body">停止答题并退出，不提交试卷。</div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal('abortModal')">取消</button>
      <button class="btn btn-danger-ghost" id="btnConfirmAbort">确认放弃</button>
    </div>
  </div>
</div>

<!-- Result Modal -->
<div class="modal-backdrop" id="resultModal">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <span>已交卷</span>
    </div>
    <div class="modal-body" id="resultBody"></div>
    <div class="modal-foot">
      <button class="btn btn-primary" onclick="closeModal('resultModal')">关闭</button>
    </div>
  </div>
</div>

<!-- Help Modal -->
<div class="modal-backdrop" id="helpModal">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <span>快捷键</span>
      <button class="btn btn-sm" onclick="closeModal('helpModal')">✕</button>
    </div>
    <div class="modal-body">
      <table class="shortcut-table">
        <tr><td><kbd>←</kbd> / <kbd>→</kbd> 或 <kbd>K</kbd> / <kbd>J</kbd></td><td>上一题 / 下一题</td></tr>
        <tr><td><kbd>1</kbd>–<kbd>9</kbd> 或 <kbd>A</kbd>–<kbd>I</kbd></td><td>选择或切换选项</td></tr>
        <tr><td><kbd>N</kbd></td><td>跳到下一未核对或未答题</td></tr>
        <tr><td><kbd>V</kbd></td><td>标记/取消已核对</td></tr>
        <tr><td><kbd>F</kbd></td><td>标星/取消标星</td></tr>
        <tr><td><kbd>P</kbd></td><td>暂停/继续自动答题</td></tr>
        <tr><td><kbd>R</kbd></td><td>重跑当前题 AI</td></tr>
        <tr><td><kbd>G</kbd></td><td>跳回并跟随当前执行题</td></tr>
        <tr><td><kbd>Ctrl</kbd> + <kbd>Enter</kbd></td><td>弹出交卷确认</td></tr>
        <tr><td><kbd>Esc</kbd></td><td>关闭弹窗</td></tr>
      </table>
    </div>
    <div class="modal-foot">
      <button class="btn btn-primary" onclick="closeModal('helpModal')">关闭</button>
    </div>
  </div>
</div>

<script>
(() => {
  const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  const state = {
    meta: {},
    phase: "running",
    paused: false,
    activeIndex: null,
    questions: [],
    stats: {},
    telemetry: {},
    submitResult: null,
    currentIndex: 0,
    filter: "all",
    followAuto: true,
    startedAtMs: Date.now(),
    answerTimeSec: 0,
  };

  const $ = (id) => document.getElementById(id);

  function fmtNum(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 10000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  }
  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
    return `${Math.round(ms)}ms`;
  }
  function fmtTps(tps) {
    if (tps === null || tps === undefined) return "—";
    return `${Number(tps).toFixed(1)} t/s`;
  }

  const savedTheme = localStorage.getItem("weban_ui_theme");
  if (savedTheme === "dark") document.documentElement.setAttribute("data-theme", "dark");
  $("btnTheme").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("weban_ui_theme", next);
  });

  function showToast(message, level = "info") {
    const el = document.createElement("div");
    el.className = `toast ${level}`;
    el.textContent = message;
    $("toastStack").appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  window.closeModal = (id) => $(id).classList.remove("open");
  function openModal(id) { $(id).classList.add("open"); }

  async function postJson(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `请求失败 (${resp.status})`);
    }
    return data;
  }

  function isLocked() {
    return state.phase === "submitted" || state.phase === "aborted";
  }

  function matchesFilter(q) {
    switch (state.filter) {
      case "ai": return q.source === "ai" || (q.ai_suggested_ids && q.ai_suggested_ids.length > 0) || q.status === "ai_thinking";
      case "unreviewed": return !q.reviewed && q.source !== "bank";
      case "manual": return q.source === "manual";
      case "flagged": return !!q.flagged;
      case "unanswered": return !q.selected_ids || q.selected_ids.length === 0 || q.status === "ai_failed";
      default: return true;
    }
  }

  function tileClass(q) {
    if (q.status === "ai_thinking") return "st-ai-thinking";
    if (q.status === "ai_failed" || (!q.selected_ids || q.selected_ids.length === 0)) {
      return q.status === "pending" ? "" : "st-failed";
    }
    if (q.source === "bank") return "st-bank";
    if (q.source === "manual" || q.reviewed) return "st-reviewed";
    if (q.source === "ai") return "st-ai-pending-review";
    return "";
  }

  function renderTopbar() {
    const m = state.meta;
    const prefix = m.demo_mode ? (m.mock_mode ? "[Mock] " : "[Demo] ") : "";
    $("examTitle").textContent = `${prefix}${m.plan_name || "考试"}`;
    $("userLabel").textContent = m.user_label || "";

    const mp = $("modelEndpointPill");
    if (m.ai_enabled) {
      const modelTxt = m.ai_model || "未命名模型";
      const hostTxt = m.ai_endpoint_host ? ` @ ${m.ai_endpoint_host}` : "";
      mp.textContent = `${modelTxt}${hostTxt}`;
      mp.title = `模型: ${modelTxt}\n接口: ${m.ai_endpoint_host || "-"}\n思考强度: ${m.default_reasoning_effort || "默认"}`;
    } else {
      mp.textContent = "AI 未启用";
    }

    const pill = $("phasePill");
    pill.className = "pill";
    if (state.phase === "submitted") {
      pill.classList.add("submitted");
      const score = state.submitResult && state.submitResult.score !== undefined ? ` ${state.submitResult.score}分` : "";
      pill.textContent = `已交卷${score}`;
    } else if (state.phase === "aborted") {
      pill.classList.add("aborted");
      pill.textContent = "已放弃";
    } else if (state.paused) {
      pill.classList.add("paused");
      pill.textContent = "已暂停";
    } else if (state.phase === "ready_for_review") {
      pill.classList.add("ready");
      pill.textContent = "待交卷";
    } else {
      pill.classList.add("running");
      pill.textContent = "执行中";
    }

    $("pauseLabel").textContent = state.paused ? "继续" : "暂停";
    $("btnPause").classList.toggle("btn-active", state.paused);
    $("btnPause").disabled = isLocked() || state.phase === "ready_for_review";
    $("btnFollow").classList.toggle("btn-active", state.followAuto);
    $("btnSubmit").disabled = isLocked();
    $("btnAbort").disabled = isLocked();
  }

  function renderStatsAndGrid() {
    const total = state.questions.length;
    const st = state.stats || {};
    const answered = st.answered || 0;
    const bankCount = st.bank || 0;
    const manualCount = st.manual || 0;
    const aiOnlyCount = Math.max(0, answered - bankCount - manualCount);

    $("navCount").textContent = `${answered}/${total}`;
    $("progBank").style.width = total ? `${(bankCount / total) * 100}%` : "0%";
    $("progManual").style.width = total ? `${(manualCount / total) * 100}%` : "0%";
    $("progAi").style.width = total ? `${(aiOnlyCount / total) * 100}%` : "0%";

    $("stBank").textContent = bankCount;
    $("stAiUnrev").textContent = st.ai_unreviewed || 0;
    $("stReviewed").textContent = st.reviewed_non_bank || 0;
    $("stUnanswered").textContent = st.unanswered || 0;

    const grid = $("qGrid");
    grid.innerHTML = "";
    state.questions.forEach((q) => {
      if (!matchesFilter(q)) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `q-tile ${tileClass(q)} ${q.index === state.currentIndex ? "selected" : ""}`;
      btn.textContent = String(q.index + 1);
      btn.title = `#${q.index + 1} ${q.title.slice(0, 40)}`;
      if (q.flagged) {
        const d = document.createElement("span");
        d.className = "dot-flag";
        btn.appendChild(d);
      }
      if (q.selected_ids && q.selected_ids.length > 0 && !q.synced) {
        const d = document.createElement("span");
        d.className = "dot-unsynced";
        btn.appendChild(d);
      }
      btn.addEventListener("click", () => selectQuestion(q.index, true));
      grid.appendChild(btn);
    });
  }

  function renderCurrentQuestion() {
    const total = state.questions.length;
    const q = state.questions[state.currentIndex];
    if (!q) return;

    $("qNumber").textContent = `#${q.index + 1} / ${total}`;
    $("qType").textContent = q.type_label || (q.type === 2 ? "多选题" : "单选题");

    const srcBadge = $("qSourceBadge");
    srcBadge.className = "badge";
    if (q.status === "ai_thinking") {
      srcBadge.classList.add("badge-thinking");
      srcBadge.textContent = "AI 思考中";
    } else if (q.source === "bank") {
      srcBadge.classList.add("badge-bank");
      srcBadge.textContent = "题库";
    } else if (q.source === "manual") {
      srcBadge.classList.add("badge-manual");
      srcBadge.textContent = "人工已改";
    } else if (q.source === "ai") {
      srcBadge.classList.add(q.reviewed ? "badge-manual" : "badge-ai");
      srcBadge.textContent = q.reviewed ? "AI 已核" : "AI 未核";
    } else if (q.status === "ai_failed") {
      srcBadge.classList.add("badge-error");
      srcBadge.textContent = "AI 失败";
    } else {
      srcBadge.textContent = "等待";
    }

    const syncBadge = $("qSyncBadge");
    syncBadge.className = "badge";
    if (q.sync_error) {
      syncBadge.classList.add("badge-error");
      syncBadge.textContent = "同步失败";
      syncBadge.title = q.sync_error;
    } else if (q.synced) {
      syncBadge.classList.add("badge-bank");
      syncBadge.textContent = "已同步";
      syncBadge.title = "";
    } else if (q.selected_ids && q.selected_ids.length > 0) {
      syncBadge.classList.add("badge-ai");
      syncBadge.textContent = "同步中";
      syncBadge.title = "";
    } else {
      syncBadge.textContent = "未答";
      syncBadge.title = "";
    }

    $("btnReview").classList.toggle("btn-active", !!q.reviewed);
    $("reviewText").textContent = q.reviewed ? "已核 ✓" : "核对";
    $("btnFlag").classList.toggle("btn-danger-ghost", !!q.flagged);
    $("flagText").textContent = q.flagged ? "已标星 ★" : "标星";

    const banner = $("followBanner");
    if (state.activeIndex !== null && state.activeIndex !== state.currentIndex && state.phase === "running") {
      $("followBannerText").textContent = `正在执行第 ${state.activeIndex + 1} 题`;
      banner.classList.add("show");
    } else {
      banner.classList.remove("show");
    }

    $("qTitle").textContent = q.title;

    const isMulti = q.type === 2;
    const chosenSet = new Set(q.selected_ids || []);
    const aiSet = new Set(q.ai_suggested_ids || []);
    const list = $("optionsList");
    list.innerHTML = "";

    q.options.forEach((opt, idx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const isChosen = chosenSet.has(opt.id);
      let cls = "option-card";
      if (isChosen) {
        cls += " chosen";
        if (q.source === "manual") cls += " manual-chosen";
        else if (q.source === "bank") cls += " bank-chosen";
      }
      btn.className = cls;
      btn.disabled = isLocked();

      const keyLabel = LETTERS[idx] || String(idx + 1);
      const aiTagHtml = aiSet.has(opt.id) ? `<span class="opt-ai-tag">AI</span>` : "";

      btn.innerHTML = `
        <span class="opt-key ${isMulti ? "" : "single"}">${keyLabel}</span>
        <span class="opt-text"></span>
        ${aiTagHtml}
      `;
      btn.querySelector(".opt-text").textContent = opt.content;
      btn.addEventListener("click", () => toggleOption(q.index, opt.id));
      list.appendChild(btn);
    });

    const notice = $("qNotice");
    if (q.error || q.sync_error) {
      notice.className = "q-notice show";
      notice.textContent = q.error || q.sync_error;
    } else {
      notice.className = "q-notice";
      notice.textContent = "";
    }

    const canRevert = !isLocked()
      && q.source === "manual"
      && q.ai_suggested_ids
      && q.ai_suggested_ids.length > 0;
    $("btnRevertAi").style.display = canRevert ? "inline-flex" : "none";

    $("btnPrev").disabled = state.currentIndex <= 0;
    $("btnNext").disabled = state.currentIndex >= total - 1;

    renderAiPanel(q);
  }

  function renderTelemetry(q) {
    const qm = q.metrics || {};
    const sm = state.telemetry || {};
    const estPrefix = (qm.usage_estimated || sm.usage_estimated) ? "~" : "";

    const qTtft = fmtMs(qm.ttft_ms);
    const sTtft = fmtMs(sm.avg_ttft_ms);
    $("mTtft").textContent = `${qTtft} / ${sTtft}`;

    const qTps = fmtTps(qm.tps);
    const sTps = fmtTps(sm.avg_tps);
    $("mTps").textContent = `${qTps} / ${sTps}`;

    const ctxTok = qm.context_tokens || sm.last_context_tokens || 0;
    const ctxChars = qm.context_chars || sm.last_context_chars || 0;
    $("mContext").textContent = ctxTok ? `${estPrefix}${fmtNum(ctxTok)} tok (${fmtNum(ctxChars)}字)` : "—";

    const qTok = qm.total_tokens || 0;
    const sTok = sm.total_tokens || 0;
    $("mTokens").textContent = (qTok || sTok) ? `${estPrefix}${fmtNum(qTok)} / ${fmtNum(sTok)}` : "—";

    const pTot = sm.prompt_tokens || 0;
    const cTot = sm.completion_tokens || 0;
    const cacheTot = sm.cached_tokens || 0;
    const reasonTot = sm.reasoning_tokens || 0;
    const cachePct = pTot > 0 && cacheTot > 0 ? ` (${Math.round((cacheTot / pTot) * 100)}%)` : "";
    $("mTokenDetail").innerHTML = `
      <span>累计 入 ${estPrefix}${fmtNum(pTot)}</span>
      <span>出 ${estPrefix}${fmtNum(cTot)}</span>
      <span>推理 ${fmtNum(reasonTot)}</span>
      <span class="token-cached">缓存 ${fmtNum(cacheTot)}${cachePct}</span>
    `;

    const rounds = qm.rounds || 0;
    const elapsed = qm.elapsed_ms ? fmtMs(qm.elapsed_ms) : "";
    const proto = qm.api_protocol || sm.api_protocol || "";
    const protoTag = proto ? `[${proto}] ` : "";
    $("aiRoundTag").textContent = rounds ? `${protoTag}${rounds} 轮 ${elapsed}` : (proto ? `[${proto}]` : "");
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderInlineMd(escaped) {
    return escaped
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function renderSafeMarkdown(text) {
    const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let inList = false;
    let quoteBuf = [];

    function flushQuote() {
      if (quoteBuf.length) {
        out.push(`<blockquote>${quoteBuf.join("<br>")}</blockquote>`);
        quoteBuf = [];
      }
    }
    function flushList() {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
    }

    for (const rawLine of lines) {
      const trimmed = rawLine.trim();
      if (!trimmed) {
        flushQuote();
        flushList();
        continue;
      }
      if (trimmed.startsWith(">")) {
        flushList();
        const body = trimmed.replace(/^>\s?/, "");
        quoteBuf.push(renderInlineMd(escapeHtml(body)));
        continue;
      }
      flushQuote();
      const heading = trimmed.match(/^#{1,4}\s+(.+)$/);
      if (heading) {
        flushList();
        out.push(`<p><strong>${renderInlineMd(escapeHtml(heading[1]))}</strong></p>`);
        continue;
      }
      const bullet = trimmed.match(/^[-*]\s+(.+)$/) || trimmed.match(/^\d+\.\s+(.+)$/);
      if (bullet) {
        if (!inList) {
          out.push("<ul>");
          inList = true;
        }
        out.push(`<li>${renderInlineMd(escapeHtml(bullet[1]))}</li>`);
        continue;
      }
      flushList();
      out.push(`<p>${renderInlineMd(escapeHtml(trimmed))}</p>`);
    }
    flushQuote();
    flushList();
    return out.join("");
  }

  function renderAiPanel(q) {
    renderTelemetry(q);

    const kbLabel = $("kbModeLabel");
    const kbBox = $("kbSourcesBox");
    const modeMap = {
      full_cached: "KV缓存全文",
      agent_tools: "工具按需检索",
      bm25: "BM25 预召回",
      bm25_empty: "BM25 未命中",
      empty: q.source === "bank" ? "题库" : "无",
    };
    kbLabel.textContent = modeMap[q.kb_mode] || (q.source === "bank" ? "题库" : "无");

    const hasTools = q.tool_events && q.tool_events.length > 0;
    const hasSources = q.kb_sources && q.kb_sources.length > 0;
    if (hasTools || hasSources) {
      kbBox.innerHTML = "";
      if (hasTools) {
        const tl = document.createElement("div");
        tl.className = "tool-list";
        q.tool_events.forEach((ev) => {
          const row = document.createElement("div");
          row.className = "tool-item";
          const qSpan = document.createElement("span");
          qSpan.className = "tool-item-q";
          qSpan.textContent = `${ev.name}(${ev.query || ""})`;
          qSpan.title = qSpan.textContent;
          const mSpan = document.createElement("span");
          mSpan.className = "tool-item-meta";
          mSpan.textContent = `${ev.hits || 0}段 ${ev.duration_ms || 0}ms`;
          row.appendChild(qSpan);
          row.appendChild(mSpan);
          tl.appendChild(row);
        });
        kbBox.appendChild(tl);
      }
      if (hasSources) {
        const wrap = document.createElement("div");
        wrap.className = "kb-chips";
        q.kb_sources.forEach((s) => {
          const chip = document.createElement("span");
          chip.className = "kb-chip";
          chip.textContent = s;
          wrap.appendChild(chip);
        });
        kbBox.appendChild(wrap);
      }
    } else {
      kbBox.innerHTML = `<span class="empty-hint">—</span>`;
    }

    const thinkBox = $("thinkingBox");
    const thinkingText = q.thinking || "";
    const qReasonTok = (q.metrics && q.metrics.reasoning_tokens) || 0;
    if (thinkingText) {
      $("thinkLenLabel").textContent = qReasonTok ? `${thinkingText.length} 字 (${qReasonTok} 推理tok)` : `${thinkingText.length} 字`;
      thinkBox.textContent = thinkingText;
      if (q.status === "ai_thinking") {
        const cur = document.createElement("span");
        cur.className = "stream-cursor";
        thinkBox.appendChild(cur);
        thinkBox.scrollTop = thinkBox.scrollHeight;
      }
    } else if (q.status === "ai_thinking") {
      $("thinkLenLabel").textContent = "思考中";
      thinkBox.innerHTML = `<span class="stream-cursor"></span>`;
    } else if (q.metrics && q.metrics.rounds > 0) {
      $("thinkLenLabel").textContent = `${qReasonTok} 推理tok`;
      thinkBox.innerHTML = qReasonTok > 0
        ? `<span class="empty-hint">模型使用了 ${qReasonTok} 推理 token，未输出可见摘要</span>`
        : `<span class="empty-hint">模型本题 0 推理 token（直接作答）</span>`;
    } else {
      $("thinkLenLabel").textContent = "0 字";
      thinkBox.innerHTML = `<span class="empty-hint">—</span>`;
    }

    const reasonBox = $("reasonBox");
    if (q.reason) {
      reasonBox.innerHTML = renderSafeMarkdown(q.reason);
    } else {
      reasonBox.innerHTML = `<span class="empty-hint">—</span>`;
    }

    $("btnRerunAi").disabled = isLocked() || !state.meta.ai_enabled || q.status === "ai_thinking";
  }

  function selectQuestion(index, manual = false) {
    if (index < 0 || index >= state.questions.length) return;
    state.currentIndex = index;
    if (manual) state.followAuto = false;
    renderTopbar();
    renderStatsAndGrid();
    renderCurrentQuestion();
  }

  function jumpNextReview() {
    const n = state.questions.length;
    if (!n) return;
    for (let offset = 1; offset <= n; offset++) {
      const idx = (state.currentIndex + offset) % n;
      const q = state.questions[idx];
      if ((!q.reviewed && q.source !== "bank") || !q.selected_ids || q.selected_ids.length === 0 || q.flagged) {
        selectQuestion(idx, true);
        return;
      }
    }
    showToast("全部已核对", "success");
  }

  async function toggleOption(index, optId) {
    if (isLocked()) return;
    const q = state.questions[index];
    if (!q) return;
    const isMulti = q.type === 2;
    let nextIds;
    if (isMulti) {
      const s = new Set(q.selected_ids || []);
      if (s.has(optId)) s.delete(optId);
      else s.add(optId);
      nextIds = q.options.map((o) => o.id).filter((id) => s.has(id));
    } else {
      nextIds = [optId];
    }

    q.selected_ids = nextIds;
    q.source = "manual";
    q.status = nextIds.length ? "manual" : "pending";
    q.reviewed = nextIds.length > 0;
    q.synced = false;
    renderStatsAndGrid();
    renderCurrentQuestion();

    try {
      await postJson("/api/select", { index, option_ids: nextIds });
    } catch (err) {
      showToast(`保存失败：${err.message}`, "error");
    }
  }

  async function toggleReview() {
    const q = state.questions[state.currentIndex];
    if (!q) return;
    try {
      await postJson("/api/review", { index: q.index, reviewed: !q.reviewed });
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function toggleFlag() {
    const q = state.questions[state.currentIndex];
    if (!q) return;
    try {
      await postJson("/api/flag", { index: q.index, flagged: !q.flagged });
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function togglePause() {
    if (isLocked()) return;
    try {
      await postJson("/api/pause", { paused: !state.paused });
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function rerunAiCurrent() {
    if (isLocked()) return;
    const q = state.questions[state.currentIndex];
    if (!q) return;
    const effort = $("effortSelect").value;
    try {
      await postJson("/api/rerun-ai", {
        index: q.index,
        reasoning_effort: effort || null,
      });
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function revertAiCurrent() {
    const q = state.questions[state.currentIndex];
    if (!q) return;
    try {
      await postJson("/api/apply-ai", { index: q.index });
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  function openSubmitConfirm() {
    if (isLocked()) return;
    const st = state.stats || {};
    const total = state.questions.length;
    const cells = [
      ["总题数", total],
      ["题库", st.bank || 0],
      ["AI", st.ai || 0],
      ["人工修改", st.manual || 0],
      ["AI 未核", st.ai_unreviewed || 0],
      ["未答", st.unanswered || 0],
    ];
    $("submitSummary").innerHTML = cells
      .map(([k, v]) => `<div class="summary-cell"><span>${k}</span><strong>${v}</strong></div>`)
      .join("");

    const warns = [];
    if ((st.unanswered || 0) > 0) warns.push(`${st.unanswered} 题未答`);
    if ((st.ai_unreviewed || 0) > 0) warns.push(`${st.ai_unreviewed} 题 AI 未核对`);
    if ((st.flagged || 0) > 0) warns.push(`${st.flagged} 题已标星`);
    if (state.phase === "running") warns.push("后台仍在答题中");

    const warnEl = $("submitWarn");
    if (warns.length) {
      warnEl.textContent = warns.join("，");
      warnEl.style.display = "block";
    } else {
      warnEl.style.display = "none";
    }
    openModal("submitModal");
  }

  async function doConfirmSubmit() {
    const btn = $("btnConfirmSubmit");
    btn.disabled = true;
    btn.textContent = "提交中...";
    try {
      const res = await postJson("/api/submit", { confirm: true });
      closeModal("submitModal");
      showSubmitResult(res.result || {});
    } catch (err) {
      showToast(`交卷失败：${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "确认交卷";
    }
  }

  function showSubmitResult(result) {
    const score = result.score !== undefined ? result.score : "--";
    $("resultBody").innerHTML = `
      <div class="score-hero">
        <div class="score-number">${score} <span style="font-size:18px;font-weight:600">分</span></div>
      </div>
    `;
    openModal("resultModal");
  }

  async function doConfirmAbort() {
    try {
      await postJson("/api/abort", { confirm: true });
      closeModal("abortModal");
      showToast("已放弃交卷", "warning");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  $("filterBar").addEventListener("click", (e) => {
    const btn = e.target.closest(".filter-chip");
    if (!btn) return;
    state.filter = btn.dataset.filter;
    document.querySelectorAll(".filter-chip").forEach((c) => c.classList.toggle("active", c === btn));
    renderStatsAndGrid();
  });

  $("btnPause").addEventListener("click", togglePause);
  $("btnFollow").addEventListener("click", () => {
    state.followAuto = !state.followAuto;
    if (state.followAuto && state.activeIndex !== null) state.currentIndex = state.activeIndex;
    renderTopbar();
    renderStatsAndGrid();
    renderCurrentQuestion();
  });
  $("btnJumpActive").addEventListener("click", () => {
    state.followAuto = true;
    if (state.activeIndex !== null) state.currentIndex = state.activeIndex;
    renderTopbar();
    renderStatsAndGrid();
    renderCurrentQuestion();
  });
  $("btnReview").addEventListener("click", toggleReview);
  $("btnFlag").addEventListener("click", toggleFlag);
  $("btnPrev").addEventListener("click", () => selectQuestion(state.currentIndex - 1, true));
  $("btnNext").addEventListener("click", () => selectQuestion(state.currentIndex + 1, true));
  $("btnNextReview").addEventListener("click", jumpNextReview);
  $("btnRerunAi").addEventListener("click", rerunAiCurrent);
  $("btnRevertAi").addEventListener("click", revertAiCurrent);
  $("btnSubmit").addEventListener("click", openSubmitConfirm);
  $("btnConfirmSubmit").addEventListener("click", doConfirmSubmit);
  $("btnAbort").addEventListener("click", () => openModal("abortModal"));
  $("btnConfirmAbort").addEventListener("click", doConfirmAbort);
  $("btnHelp").addEventListener("click", () => openModal("helpModal"));

  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) ? e.target.tagName.toUpperCase() : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      if (e.key === "Escape") e.target.blur();
      return;
    }

    const anyModalOpen = !!document.querySelector(".modal-backdrop.open");
    if (e.key === "Escape" && anyModalOpen) {
      document.querySelectorAll(".modal-backdrop.open").forEach((m) => m.classList.remove("open"));
      return;
    }
    if (anyModalOpen) return;

    if (e.ctrlKey && e.key === "Enter") {
      e.preventDefault();
      openSubmitConfirm();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const key = e.key;
    const low = key.toLowerCase();

    if (key === "ArrowLeft" || low === "k") {
      e.preventDefault();
      selectQuestion(state.currentIndex - 1, true);
    } else if (key === "ArrowRight" || low === "j") {
      e.preventDefault();
      selectQuestion(state.currentIndex + 1, true);
    } else if (low === "n") {
      e.preventDefault();
      jumpNextReview();
    } else if (low === "v") {
      e.preventDefault();
      toggleReview();
    } else if (low === "f") {
      e.preventDefault();
      toggleFlag();
    } else if (low === "p") {
      e.preventDefault();
      togglePause();
    } else if (low === "r") {
      e.preventDefault();
      rerunAiCurrent();
    } else if (low === "g") {
      e.preventDefault();
      state.followAuto = true;
      if (state.activeIndex !== null) state.currentIndex = state.activeIndex;
      renderTopbar();
      renderStatsAndGrid();
      renderCurrentQuestion();
    } else if (key === "?" || (e.shiftKey && key === "/")) {
      e.preventDefault();
      openModal("helpModal");
    } else if (/^[1-9]$/.test(key)) {
      const idx = parseInt(key, 10) - 1;
      const q = state.questions[state.currentIndex];
      if (q && q.options[idx]) {
        e.preventDefault();
        toggleOption(q.index, q.options[idx].id);
      }
    } else if (/^[a-i]$/.test(low)) {
      const idx = low.charCodeAt(0) - 97;
      const q = state.questions[state.currentIndex];
      if (q && q.options[idx]) {
        e.preventDefault();
        toggleOption(q.index, q.options[idx].id);
      }
    }
  });

  setInterval(() => {
    if (!state.answerTimeSec || isLocked()) return;
    const elapsed = Math.floor((Date.now() - state.startedAtMs) / 1000);
    const remain = Math.max(0, state.answerTimeSec - elapsed);
    const mm = String(Math.floor(remain / 60)).padStart(2, "0");
    const ss = String(remain % 60).padStart(2, "0");
    const el = $("examTimer");
    el.textContent = `${mm}:${ss}`;
    el.classList.toggle("warn", remain <= 180);
  }, 1000);

  function connectSSE() {
    const es = new EventSource("/api/events");
    es.onopen = () => { $("connStatus").textContent = "已连接"; };
    es.onerror = () => { $("connStatus").textContent = "重连中"; };

    es.addEventListener("init", (ev) => {
      const payload = JSON.parse(ev.data);
      state.meta = payload.meta || {};
      state.phase = payload.phase || "running";
      state.paused = !!payload.paused;
      state.activeIndex = payload.active_index;
      state.questions = payload.questions || [];
      state.stats = payload.stats || {};
      state.telemetry = payload.telemetry || {};
      state.submitResult = payload.submit_result || null;
      state.startedAtMs = (payload.meta && payload.meta.started_at_ms) || Date.now();
      state.answerTimeSec = ((payload.meta && payload.meta.answer_time_minutes) || 0) * 60;

      if (state.meta.default_reasoning_effort) {
        $("effortSelect").value = state.meta.default_reasoning_effort;
      }
      if (state.followAuto && state.activeIndex !== null && state.activeIndex !== undefined) {
        state.currentIndex = state.activeIndex;
      } else if (state.currentIndex >= state.questions.length) {
        state.currentIndex = 0;
      }

      renderTopbar();
      renderStatsAndGrid();
      renderCurrentQuestion();
    });

    es.addEventListener("question", (ev) => {
      const payload = JSON.parse(ev.data);
      const q = payload.question;
      if (q && q.index >= 0 && q.index < state.questions.length) {
        state.questions[q.index] = q;
      }
      if (payload.stats) state.stats = payload.stats;
      if (payload.telemetry) state.telemetry = payload.telemetry;
      renderStatsAndGrid();
      if (q && q.index === state.currentIndex) {
        renderCurrentQuestion();
      } else if (state.questions[state.currentIndex]) {
        renderTelemetry(state.questions[state.currentIndex]);
      }
    });

    es.addEventListener("thinking_delta", (ev) => {
      const payload = JSON.parse(ev.data);
      const q = state.questions[payload.index];
      if (!q) return;
      q.thinking = (q.thinking || "") + (payload.delta || "");
      if (payload.index === state.currentIndex) {
        const thinkBox = $("thinkingBox");
        $("thinkLenLabel").textContent = `${q.thinking.length} 字`;
        thinkBox.textContent = q.thinking;
        if (q.status === "ai_thinking") {
          const cur = document.createElement("span");
          cur.className = "stream-cursor";
          thinkBox.appendChild(cur);
        }
        thinkBox.scrollTop = thinkBox.scrollHeight;
      }
    });

    es.addEventListener("status", (ev) => {
      const payload = JSON.parse(ev.data);
      if (payload.phase !== undefined) state.phase = payload.phase;
      if (payload.paused !== undefined) state.paused = payload.paused;
      if (payload.active_index !== undefined) state.activeIndex = payload.active_index;
      if (payload.stats) state.stats = payload.stats;
      if (payload.telemetry) state.telemetry = payload.telemetry;
      if (payload.submit_result !== undefined) state.submitResult = payload.submit_result;

      if (state.followAuto && state.activeIndex !== null && state.activeIndex !== undefined && state.phase === "running") {
        state.currentIndex = state.activeIndex;
      }
      renderTopbar();
      renderStatsAndGrid();
      renderCurrentQuestion();
    });

    es.addEventListener("toast", (ev) => {
      const payload = JSON.parse(ev.data);
      showToast(payload.message || "", payload.level || "info");
    });
  }

  connectSSE();
})();
</script>
</body>
</html>
"""
