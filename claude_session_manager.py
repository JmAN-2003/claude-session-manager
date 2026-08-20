#!/usr/bin/env python3
"""
Claude Session Manager — GUI tool for browsing, previewing, exporting,
migrating, and deleting Claude Code sessions across all projects.

Start:  python claude-session-manager.py
Then open http://localhost:8899 in your browser.
"""
import os, sys, json, glob, shutil, re, webbrowser, html as html_mod, argparse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

PORT = 8899
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
CLAUDE_DIR = os.path.expanduser("~/.claude")


# ──────────────────────────────────────────────────────────────────────────
#  BACKEND — session scanning & operations
# ──────────────────────────────────────────────────────────────────────────

def _short_path(cwd):
    if not cwd:
        return "?"
    if len(cwd) > 60:
        return "..." + cwd[-57:]
    return cwd


def _read_first_lines(filepath, max_bytes=16384):
    """Read first max_bytes of a file as text."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def scan_sessions():
    """Yield session dicts from all project directories."""
    for proj_name in sorted(os.listdir(PROJECTS_DIR)):
        proj_path = os.path.join(PROJECTS_DIR, proj_name)
        if not os.path.isdir(proj_path):
            continue

        for fpath in glob.glob(os.path.join(proj_path, "*.jsonl")):
            fname = os.path.basename(fpath).replace(".jsonl", "")
            if len(fname) != 36 or fname.count("-") != 4:
                continue

            try:
                stat = os.stat(fpath)
                size_kb = round(stat.st_size / 1024, 1)
                last_time = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

                cwd = first_time = first_msg = ""

                head = _read_first_lines(fpath, 32768)
                for line in head.split("\n"):
                    try:
                        obj = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue

                    if not cwd and "cwd" in obj:
                        cwd = obj.get("cwd", "")
                    if not first_time and "timestamp" in obj:
                        ts = obj.get("timestamp", "")
                        try:
                            first_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            first_time = ts[:16].replace("T", " ")
                    if not first_msg and obj.get("type") == "user":
                        origin = obj.get("origin", {})
                        if isinstance(origin, dict) and origin.get("kind") == "human":
                            content = obj.get("message", {}).get("content", "")
                            if isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        content = part.get("text", "")
                                        break
                                else:
                                    content = ""
                            if isinstance(content, str) and content.strip():
                                if not content.startswith("<local-command") and not content.startswith("<command-name>"):
                                    first_msg = content[:120].replace("\n", " ").replace("\r", "")
                    if cwd and first_time and first_msg:
                        break

                yield {
                    "full_id": fname,
                    "short_id": fname[:8],
                    "filepath": fpath,
                    "project_dir": proj_path,
                    "project_name": proj_name,
                    "size_kb": size_kb,
                    "first_time": first_time or "?",
                    "last_time": last_time or "?",
                    "cwd": cwd or "?",
                    "first_msg": first_msg or "(no message)",
                }
            except Exception:
                pass


def get_session_by_id(full_id):
    """Find a session by its full UUID."""
    for s in scan_sessions():
        if s["full_id"] == full_id:
            return s
    return None


def extract_messages(filepath, max_messages=200):
    """Extract human + assistant messages from a session JSONL."""
    messages = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(messages) >= max_messages:
                    break
                try:
                    obj = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                msg = None
                if obj.get("type") == "user":
                    origin = obj.get("origin", {})
                    if isinstance(origin, dict) and origin.get("kind") == "human":
                        content = obj.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            texts = []
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    texts.append(part.get("text", ""))
                            content = " ".join(texts)
                        if isinstance(content, str) and content.strip():
                            if not content.startswith("<local-command") and not content.startswith("<command-name>"):
                                ts = obj.get("timestamp", "")
                                try:
                                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
                                except Exception:
                                    ts = ts[:16]
                                msg = {"role": "user", "time": ts, "content": content.strip()}

                elif obj.get("type") == "assistant":
                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        texts = []
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    texts.append(part.get("text", ""))
                                elif part.get("type") == "tool_use":
                                    texts.append(f"[tool: {part.get('name', '?')}]")
                        content = "\n".join(texts)
                    if isinstance(content, str) and content.strip():
                        ts = obj.get("timestamp", "")
                        try:
                            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
                        except Exception:
                            ts = ts[:16]
                        msg = {"role": "assistant", "time": ts, "content": content.strip()}

                if msg:
                    messages.append(msg)
    except Exception:
        pass
    return messages


def format_export(messages, session_info):
    """Format messages as a readable text export."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"Claude Code Session Export")
    lines.append(f"Session ID : {session_info['full_id']}")
    lines.append(f"Project    : {session_info['cwd']}")
    lines.append(f"Started    : {session_info['first_time']}")
    lines.append(f"Last active: {session_info['last_time']}")
    lines.append(f"Size       : {session_info['size_kb']:.1f} KB")
    lines.append("=" * 60)
    lines.append("")

    for m in messages:
        role_label = "YOU" if m["role"] == "user" else "CLAUDE"
        lines.append(f"[{m['time']}] {role_label}")
        lines.append(m["content"])
        lines.append("")

    lines.append("=" * 60)
    lines.append("Exported by Claude Session Manager")
    return "\n".join(lines)


def delete_session_files(filepath, project_dir, full_id):
    """Delete a session's JSONL file and all companion files/dirs."""
    deleted = []
    # Main file
    try:
        os.remove(filepath)
        deleted.append(filepath)
    except OSError as e:
        raise OSError(f"Cannot delete {filepath}: {e}")

    # Companion files (same UUID prefix in project dir)
    for name in os.listdir(project_dir):
        if name.startswith(full_id):
            companion = os.path.join(project_dir, name)
            try:
                if os.path.isdir(companion):
                    shutil.rmtree(companion)
                else:
                    os.remove(companion)
                deleted.append(companion)
            except OSError:
                pass

    # Also clean up file-history and tasks dirs for this session
    for subdir in ["file-history", "tasks"]:
        for name in os.listdir(os.path.join(CLAUDE_DIR, subdir)):
            if name.startswith(full_id):
                companion = os.path.join(CLAUDE_DIR, subdir, name)
                try:
                    if os.path.isdir(companion):
                        shutil.rmtree(companion)
                    else:
                        os.remove(companion)
                    deleted.append(companion)
                except OSError:
                    pass

    return deleted


def migrate_session(filepath, full_id, from_project_dir, to_project_name):
    """Move a session from one project directory to another."""
    to_project_dir = os.path.join(PROJECTS_DIR, to_project_name)
    if not os.path.isdir(to_project_dir):
        raise ValueError(f"Target project directory does not exist: {to_project_dir}")
    if from_project_dir == to_project_dir:
        raise ValueError("Source and target projects are the same")

    moved = []
    # Move main file
    dest = os.path.join(to_project_dir, os.path.basename(filepath))
    shutil.move(filepath, dest)
    moved.append(dest)

    # Move companion files
    for name in os.listdir(from_project_dir):
        if name.startswith(full_id):
            src = os.path.join(from_project_dir, name)
            dst = os.path.join(to_project_dir, name)
            shutil.move(src, dst)
            moved.append(dst)

    return moved


def encode_project_name(path):
    """Encode a filesystem path to a project directory name."""
    # Claude Code encodes project paths like this:
    #   C:\Users\user -> C--Users-user
    # Replace \ and / with - (cross-platform)
    return path.replace("\\", "-").replace("/", "-").replace(":", "")


def list_projects():
    """List all project directories with session counts."""
    projects = []
    for name in sorted(os.listdir(PROJECTS_DIR)):
        proj_path = os.path.join(PROJECTS_DIR, name)
        if not os.path.isdir(proj_path):
            continue
        count = len([f for f in os.listdir(proj_path)
                     if f.endswith(".jsonl")
                     and len(os.path.splitext(f)[0]) == 36
                     and os.path.splitext(f)[0].count("-") == 4])
        # Try to get real path from a session file
        real_path = name  # fallback
        for f in os.listdir(proj_path):
            if f.endswith(".jsonl"):
                head = _read_first_lines(os.path.join(proj_path, f), 4096)
                m = re.search(r'"cwd"\s*:\s*"([^"]+)"', head)
                if m:
                    real_path = m.group(1).replace("\\\\", "\\")
                    break
        projects.append({
            "name": name,
            "real_path": real_path,
            "count": count,
        })
    return projects


# ──────────────────────────────────────────────────────────────────────────
#  FRONTEND — HTML page (served at /)
# ──────────────────────────────────────────────────────────────────────────

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Session Manager</title>
<style>
:root {
  --bg: #0d1117;
  --bg2: #161b22;
  --bg3: #21262d;
  --border: #30363d;
  --text: #c9d1d9;
  --text2: #8b949e;
  --accent: #58a6ff;
  --accent2: #79c0ff;
  --red: #f85149;
  --red2: #ff7b72;
  --green: #3fb950;
  --yellow: #d2991d;
  --user-bg: #1a2332;
  --asst-bg: #161b22;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  height: 100vh; display: flex; flex-direction: column;
  overflow: hidden;
}
/* header */
header {
  background: var(--bg2); border-bottom: 1px solid var(--border);
  padding: 8px 16px; display: flex; align-items: center; gap: 12px;
  flex-shrink: 0;
}
header h1 { font-size: 15px; font-weight: 600; }
header .badge { font-size: 11px; background: var(--bg3); color: var(--text2);
  padding: 2px 8px; border-radius: 10px; }
/* toolbar */
#toolbar {
  display: flex; gap: 8px; padding: 8px 12px; background: var(--bg2);
  border-bottom: 1px solid var(--border); flex-shrink: 0; align-items: center;
}
#search {
  flex:1; background: var(--bg3); border: 1px solid var(--border); color: var(--text);
  padding: 6px 10px; border-radius: 6px; font-size: 13px; outline: none;
}
#search:focus { border-color: var(--accent); }
#sort-btn {
  background: var(--bg3); border: 1px solid var(--border); color: var(--text2);
  padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
  white-space: nowrap;
}
#sort-btn:hover { border-color: var(--accent); color: var(--text); }
#refresh-btn {
  background: var(--bg3); border: 1px solid var(--border); color: var(--accent);
  padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
}
#refresh-btn:hover { background: var(--accent); color: #fff; }
#clean-empty-btn {
  background: var(--bg3); border: 1px solid var(--border); color: var(--yellow);
  padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
  white-space: nowrap;
}
#clean-empty-btn:hover { border-color: var(--yellow); background: rgba(210,153,29,0.1); }
/* main layout */
main { display: flex; flex: 1; overflow: hidden; }
#list {
  width: 380px; min-width: 300px; overflow-y: auto; overflow-x: hidden;
  border-right: 1px solid var(--border); background: var(--bg);
}
#list .card {
  padding: 10px 14px; border-bottom: 1px solid var(--border); cursor: pointer;
  transition: background .1s;
}
#list .card:hover { background: var(--bg2); }
#list .card.active { background: var(--bg3); border-left: 3px solid var(--accent); padding-left: 11px; }
#list .card .msg { font-size: 13px; color: var(--text); margin-bottom: 4px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#list .card .meta { font-size: 11px; color: var(--text2); display: flex; gap: 8px; }
#list .card .meta .path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#list .empty { padding: 40px 20px; text-align: center; color: var(--text2); font-size: 13px; }
/* detail panel */
#detail {
  flex:1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg);
}
#detail-header {
  padding: 12px 16px; background: var(--bg2); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
#detail-header .id { font-size: 11px; color: var(--accent); font-family: monospace; margin-bottom: 4px; }
#detail-header .info { font-size: 12px; color: var(--text2); display: flex; gap: 12px; flex-wrap: wrap; }
#detail-actions {
  display: flex; gap: 8px; padding: 8px 16px; background: var(--bg2);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#detail-actions button {
  padding: 5px 14px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--bg3); color: var(--text); cursor: pointer; font-size: 12px;
}
#detail-actions button:hover { border-color: var(--accent); }
#detail-actions button.danger { color: var(--red); border-color: var(--red); }
#detail-actions button.danger:hover { background: var(--red); color: #fff; }
#detail-actions button.primary { color: var(--accent); border-color: var(--accent); }
#detail-actions button.primary:hover { background: var(--accent); color: #fff; }
#preview {
  flex:1; overflow-y: auto; padding: 12px 16px;
}
#preview .placeholder {
  display: flex; align-items: center; justify-content: center; height: 100%;
  color: var(--text2); font-size: 14px;
}
/* messages */
.msg-block { margin-bottom: 12px; }
.msg-block .role {
  font-size: 11px; font-weight: 600; margin-bottom: 3px;
  display: flex; justify-content: space-between;
}
.msg-block .role .time { font-weight: 400; color: var(--text2); font-size: 10px; }
.msg-block.user .role { color: var(--accent); }
.msg-block.assistant .role { color: var(--green); }
.msg-block .body {
  font-size: 13px; line-height: 1.55; white-space: pre-wrap;
  padding: 8px 12px; border-radius: 6px;
}
.msg-block.user .body { background: var(--user-bg); border: 1px solid rgba(88,166,255,0.15); }
.msg-block.assistant .body { background: var(--asst-bg); border: 1px solid var(--border); }
.msg-block .body code {
  background: var(--bg3); padding: 1px 4px; border-radius: 3px; font-size: 12px;
}
/* modal */
.modal-overlay {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  z-index: 100; align-items: center; justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px 24px; max-width: 480px; width: 90%; max-height: 80vh; overflow-y: auto;
}
.modal h3 { font-size: 15px; margin-bottom: 12px; }
.modal p { font-size: 13px; color: var(--text2); margin-bottom: 12px; line-height: 1.5; }
.modal select {
  width: 100%; padding: 8px; background: var(--bg3); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; font-size: 13px; margin-bottom: 12px;
}
.modal .btns { display: flex; gap: 8px; justify-content: flex-end; }
.modal .btns button {
  padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--bg3); color: var(--text); cursor: pointer; font-size: 13px;
}
.modal .btns button.confirm { background: var(--red); border-color: var(--red); color: #fff; }
.modal .btns button.confirm:hover { background: var(--red2); }
.modal .btns button.cancel:hover { border-color: var(--accent); }
/* loading */
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
/* toast */
#toast {
  position: fixed; bottom: 20px; right: 20px; z-index: 200;
  background: var(--bg3); border: 1px solid var(--border); padding: 10px 18px;
  border-radius: 8px; font-size: 13px; transition: opacity .3s; opacity: 0; pointer-events: none;
}
#toast.show { opacity: 1; }
#toast.ok { border-color: var(--green); }
#toast.err { border-color: var(--red); }
</style>
</head>
<body>

<header>
  <h1>&#x1f4cb; Claude Session Manager</h1>
  <span class="badge" id="total-badge">Loading...</span>
</header>

<div id="toolbar">
  <input id="search" placeholder="Search by content, path, or session ID..." autofocus>
  <button id="sort-btn" title="Toggle sort order">&#x1f53d; Time</button>
  <button id="clean-empty-btn" title="Delete all sessions with no messages">&#x1f9f9; Clean Empty</button>
<button id="refresh-btn" title="Refresh">&#x21bb;</button>
</div>

<main>
  <div id="list"><div class="empty">Loading sessions...</div></div>
  <div id="detail">
    <div id="detail-header" style="display:none">
      <div class="id"></div>
      <div class="info"></div>
    </div>
    <div id="detail-actions" style="display:none">
      <button class="primary" onclick="doExport()">&#x1f4e5; Export</button>
      <button onclick="showMigrateModal()">&#x1f4c1; Migrate</button>
      <button class="danger" onclick="showDeleteModal()">&#x1f5d1; Delete</button>
    </div>
    <div id="preview">
      <div class="placeholder">&#x2190; Select a session from the list</div>
    </div>
  </div>
</main>

<!-- Delete confirm modal -->
<div class="modal-overlay" id="delete-modal">
  <div class="modal">
    <h3>&#x26a0; Delete this session?</h3>
    <p id="delete-info"></p>
    <p style="color:var(--red);font-size:12px;">This cannot be undone. The session file will be permanently deleted.</p>
    <div class="btns">
      <button class="cancel" onclick="closeModal('delete-modal')">Cancel</button>
      <button class="confirm" onclick="confirmDelete()">Delete</button>
    </div>
  </div>
</div>

<!-- Migrate modal -->
<div class="modal-overlay" id="migrate-modal">
  <div class="modal">
    <h3>&#x1f4c1; Migrate session to another project</h3>
    <p>Move this session's files to a different project directory.</p>
    <select id="migrate-target"></select>
    <div class="btns">
      <button class="cancel" onclick="closeModal('migrate-modal')">Cancel</button>
      <button class="confirm" style="background:var(--accent);border-color:var(--accent);"
              onclick="confirmMigrate()">Move</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
// ── State ──
let sessions = [];
let activeId = null;
let sortDesc = true;

// ── API helpers ──
async function api(url, opts = {}) {
  try {
    const res = await fetch(url, opts);
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(txt || res.statusText);
    }
    return res;
  } catch (e) {
    toast(e.message, 'err');
    throw e;
  }
}

// ── Toast ──
function toast(msg, kind = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + kind;
  setTimeout(() => el.className = '', 2500);
}

// ── Load sessions ──
async function loadSessions() {
  const list = document.getElementById('list');
  list.innerHTML = '<div class="empty"><span class="spinner"></span> Loading...</div>';
  const res = await api('/api/sessions');
  sessions = await res.json();
  renderList();
  document.getElementById('total-badge').textContent = sessions.length + ' sessions';
}

function renderList() {
  const list = document.getElementById('list');
  const q = document.getElementById('search').value.toLowerCase();
  let filtered = sessions;
  if (q) {
    filtered = sessions.filter(s =>
      s.first_msg.toLowerCase().includes(q) ||
      s.cwd.toLowerCase().includes(q) ||
      s.full_id.includes(q) ||
      s.short_id.includes(q)
    );
  }

  if (!sortDesc) filtered.reverse();

  if (filtered.length === 0) {
    list.innerHTML = '<div class="empty">No sessions found</div>';
    return;
  }

  list.innerHTML = filtered.map(s => `
    <div class="card${s.full_id === activeId ? ' active' : ''}"
         onclick="selectSession('${s.full_id}')">
      <div class="msg">${esc(s.first_msg)}</div>
      <div class="meta">
        <span>${s.first_time}</span>
        <span>${s.size_kb.toFixed(0)} KB</span>
        <span class="path" title="${esc(s.cwd)}">${esc(s.cwd)}</span>
      </div>
    </div>
  `).join('');
}

function esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Select / Preview ──
async function selectSession(fullId) {
  activeId = fullId;
  renderList();

  const s = sessions.find(x => x.full_id === fullId);
  if (!s) return;

  // Header
  const dh = document.getElementById('detail-header');
  dh.style.display = 'block';
  dh.querySelector('.id').textContent = s.full_id;
  dh.querySelector('.info').innerHTML = `
    <span>&#x1f4c1; ${esc(s.cwd)}</span>
    <span>&#x1f4c5; ${s.first_time} ~ ${s.last_time}</span>
    <span>&#x1f4e6; ${s.size_kb.toFixed(0)} KB</span>
  `;

  // Actions
  document.getElementById('detail-actions').style.display = 'flex';

  // Preview
  const preview = document.getElementById('preview');
  preview.innerHTML = '<div class="placeholder"><span class="spinner"></span> Loading preview...</div>';

  const res = await api(`/api/sessions/${encodeURIComponent(fullId)}/preview`);
  const msgs = await res.json();

  if (msgs.length === 0) {
    preview.innerHTML = '<div class="placeholder">No messages to preview</div>';
    return;
  }

  preview.innerHTML = msgs.map(m => `
    <div class="msg-block ${m.role}">
      <div class="role">
        <span>${m.role === 'user' ? '&#x1f464; You' : '&#x1f916; Claude'}</span>
        <span class="time">${m.time}</span>
      </div>
      <div class="body">${esc(m.content)}</div>
    </div>
  `).join('');

  preview.scrollTop = 0;
}

// ── Export ──
async function doExport() {
  if (!activeId) return;
  const res = await api(`/api/sessions/${encodeURIComponent(activeId)}/export`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `claude-session-${activeId.slice(0,8)}.txt`;
  a.click();
  URL.revokeObjectURL(url);
  toast('Exported!', 'ok');
}

// ── Delete ──
function showDeleteModal() {
  if (!activeId) return;
  const s = sessions.find(x => x.full_id === activeId);
  if (!s) return;
  document.getElementById('delete-info').innerHTML = `
    <b>${esc(s.first_msg)}</b><br>
    <span style="font-size:12px;color:var(--text2)">
      ID: ${s.full_id}<br>
      Path: ${esc(s.cwd)}<br>
      Size: ${s.size_kb.toFixed(0)} KB
    </span>
  `;
  document.getElementById('delete-modal').classList.add('show');
}

async function confirmDelete() {
  const res = await api(`/api/sessions/${encodeURIComponent(activeId)}`, { method: 'DELETE' });
  const data = await res.json();
  toast(`Deleted ${data.deleted.length} file(s), freed ~${data.freed_kb} KB`, 'ok');
  closeModal('delete-modal');
  activeId = null;
  document.getElementById('detail-header').style.display = 'none';
  document.getElementById('detail-actions').style.display = 'none';
  document.getElementById('preview').innerHTML = '<div class="placeholder">&#x2190; Select a session from the list</div>';
  loadSessions();
}

// ── Migrate ──
async function showMigrateModal() {
  if (!activeId) return;
  const res = await api('/api/projects');
  const projects = await res.json();
  const sel = document.getElementById('migrate-target');
  const current = sessions.find(x => x.full_id === activeId);
  sel.innerHTML = projects.map(p =>
    `<option value="${esc(p.name)}" ${p.name === (current ? current.project_name : '') ? 'disabled' : ''}>
      ${esc(p.real_path)} (${p.count} sessions)${p.name === (current ? current.project_name : '') ? ' [current]' : ''}
    </option>`
  ).join('');
  document.getElementById('migrate-modal').classList.add('show');
}

async function confirmMigrate() {
  const target = document.getElementById('migrate-target').value;
  const res = await api(`/api/sessions/${encodeURIComponent(activeId)}/migrate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_project: target })
  });
  const data = await res.json();
  toast(`Moved ${data.moved.length} file(s)`, 'ok');
  closeModal('migrate-modal');
  activeId = null;
  loadSessions();
}

function closeModal(id) {
  document.getElementById(id).classList.remove('show');
}

// ── Search / Sort ──
document.getElementById('search').addEventListener('input', renderList);
document.getElementById('sort-btn').addEventListener('click', () => {
  sortDesc = !sortDesc;
  document.getElementById('sort-btn').innerHTML = sortDesc ? '&#x1f53d; Time' : '&#x1f53c; Time';
  renderList();
});
document.getElementById('refresh-btn').addEventListener('click', () => {
  activeId = null;
  document.getElementById('detail-header').style.display = 'none';
  document.getElementById('detail-actions').style.display = 'none';
  document.getElementById('preview').innerHTML = '<div class="placeholder">&#x2190; Select a session from the list</div>';
  loadSessions();
});

// ── Clean empty sessions ──
document.getElementById('clean-empty-btn').addEventListener('click', async () => {
  if (!confirm('Delete ALL sessions that have no messages?\n\nThis cannot be undone.')) return;
  const btn = document.getElementById('clean-empty-btn');
  btn.textContent = ' Cleaning...';
  btn.disabled = true;
  const res = await api('/api/sessions/empty', { method: 'DELETE' });
  const data = await res.json();
  toast(`Deleted ${data.deleted} empty session(s), freed ~${data.freed_kb} KB`, 'ok');
  btn.textContent = '🧹 Clean Empty';
  btn.disabled = false;
  activeId = null;
  loadSessions();
});

// ── Keyboard ──
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.show').forEach(el => el.classList.remove('show'));
  }
});

// ── Init ──
loadSessions();
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────────────────────────────────
#  HTTP SERVER
# ──────────────────────────────────────────────────────────────────────────

class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logging to stderr
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, filename=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_str, status=200):
        body = html_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, msg, status=400):
        self._send_json({"error": msg}, status)

    def _parse_path(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        return path, parsed

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path, _ = self._parse_path()

        if path == "/" or path == "/index.html":
            self._send_html(PAGE_HTML)
            return

        if path == "/api/sessions":
            sessions = sorted(scan_sessions(), key=lambda s: s["last_time"], reverse=True)
            self._send_json(sessions)
            return

        if path.startswith("/api/sessions/") and path.endswith("/preview"):
            full_id = path.split("/")[3]
            s = get_session_by_id(full_id)
            if not s:
                self._send_error_json("Session not found", 404)
                return
            msgs = extract_messages(s["filepath"], max_messages=100)
            self._send_json(msgs)
            return

        if path.startswith("/api/sessions/") and path.endswith("/export"):
            full_id = path.split("/")[3]
            s = get_session_by_id(full_id)
            if not s:
                self._send_error_json("Session not found", 404)
                return
            msgs = extract_messages(s["filepath"], max_messages=5000)
            text = format_export(msgs, s)
            filename = f"claude-session-{s['short_id']}.txt"
            self._send_text(text, filename=filename)
            return

        if path == "/api/projects":
            projects = list_projects()
            self._send_json(projects)
            return

        self._send_error_json("Not found", 404)

    def do_DELETE(self):
        path, _ = self._parse_path()

        if path == "/api/sessions/empty":
            # Delete all sessions with no human messages
            deleted_ids = []
            freed = 0
            for s in scan_sessions():
                filepath = s["filepath"]
                # Check if there's any human message
                has_human = False
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if '"kind":"human"' in line:
                                has_human = True
                                break
                except Exception:
                    pass
                if not has_human:
                    try:
                        proj_dir = s["project_dir"]
                        fid = s["full_id"]
                        freed += s["size_kb"]
                        os.remove(filepath)
                        for name in os.listdir(proj_dir):
                            if name.startswith(fid):
                                companion = os.path.join(proj_dir, name)
                                if os.path.isdir(companion):
                                    shutil.rmtree(companion)
                                else:
                                    os.remove(companion)
                        deleted_ids.append(s["short_id"])
                    except OSError:
                        pass
            self._send_json({
                "deleted": len(deleted_ids),
                "ids": deleted_ids,
                "freed_kb": round(freed, 1),
                "message": f"Deleted {len(deleted_ids)} empty sessions, freed ~{freed:.0f} KB"
            })
            return

        if path.startswith("/api/sessions/") and path.count("/") == 3:
            full_id = path.split("/")[3]
            s = get_session_by_id(full_id)
            if not s:
                self._send_error_json("Session not found", 404)
                return
            try:
                deleted = delete_session_files(s["filepath"], s["project_dir"], s["full_id"])
                self._send_json({
                    "deleted": deleted,
                    "freed_kb": s["size_kb"],
                    "message": f"Deleted {len(deleted)} file(s)"
                })
            except OSError as e:
                self._send_error_json(str(e), 500)
            return

        self._send_error_json("Not found", 404)

    def do_POST(self):
        path, _ = self._parse_path()

        if path.startswith("/api/sessions/") and path.endswith("/migrate"):
            full_id = path.split("/")[3]
            s = get_session_by_id(full_id)
            if not s:
                self._send_error_json("Session not found", 404)
                return

            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len))
            target = body.get("target_project", "")

            if not target:
                self._send_error_json("Missing target_project")
                return

            # Security: must be a valid project directory name in PROJECTS_DIR
            target_dir = os.path.join(PROJECTS_DIR, target)
            if not os.path.isdir(target_dir):
                self._send_error_json("Invalid target project")
                return

            try:
                moved = migrate_session(s["filepath"], s["full_id"], s["project_dir"], target)
                self._send_json({
                    "moved": moved,
                    "message": f"Moved {len(moved)} file(s) to {target}"
                })
            except Exception as e:
                self._send_error_json(str(e), 500)
            return

        self._send_error_json("Not found", 404)


def main():
    global PORT

    parser = argparse.ArgumentParser(
        description="Claude Session Manager - browse, preview, export, migrate, "
                    "and delete Claude Code sessions across all projects."
    )
    parser.add_argument(
        "--port", type=int, default=PORT,
        help=f"Port to run the web server on (default: {PORT}). "
             "If busy, the next available port is used."
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not auto-open a browser window."
    )
    args = parser.parse_args()

    # Find an available port (try the requested one, then increment up to 5 times)
    server = None
    actual_port = None
    for attempt in range(6):
        candidate = args.port + attempt
        try:
            server = HTTPServer(("127.0.0.1", candidate), RequestHandler)
            actual_port = candidate
            break
        except OSError:
            continue

    if server is None:
        print(f"\033[31mCould not bind to any port starting at {args.port} "
              f"(ports {args.port}-{args.port + 5} are all in use).\033[0m")
        sys.exit(1)

    PORT = actual_port

    # Print startup info
    print()
    print("  \033[36m┌──────────────────────────────────────────────┐\033[0m")
    print("  \033[36m│\033[0m   \033[1mClaude Session Manager\033[0m                     \033[36m│\033[0m")
    print("  \033[36m│\033[0m                                              \033[36m│\033[0m")
    print(f"  \033[36m│\033[0m   Open: \033[33mhttp://localhost:{actual_port}\033[0m")
    if actual_port != args.port:
        print(f"  \033[36m│\033[0m   (port {args.port} was busy, using {actual_port})")
    print("  \033[36m│\033[0m   Press Ctrl+C to stop                     \033[36m│\033[0m")
    print("  \033[36m└──────────────────────────────────────────────┘\033[0m")
    print()

    # Open browser
    if not args.no_browser:
        try:
            webbrowser.open(f"http://localhost:{actual_port}")
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\033[33mShutting down...\033[0m")
        server.shutdown()


if __name__ == "__main__":
    main()
