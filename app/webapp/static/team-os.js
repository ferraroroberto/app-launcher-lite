/* Team OS tab (issue #102): skill tiles + one-tap launch + a read-only
 * private-content browser.
 *
 * ~80% a clone of the Coding tab. A tile launches a GitHub Copilot CLI
 * session in the team-os repo that auto-invokes the bare /<skill>
 * slash-command; the
 * ☁️ Detached + model combo live in the Team OS Skills summary (same UX as
 * the Coding-options Detached toggle). The 📖 Browse button opens an
 * overlay that reads each skill's files — public SKILL.md/description.md
 * plus the private context/memory/examples/conversations + shared
 * identity. Those content endpoints are Tailscale + passkey gated
 * server-side, so a fetch may 403; we surface the reason rather than a
 * blank pane.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { toggleAriaChecked, wireModelCombo } from './dom-utils.js';

// The Skills-summary launch-model dropdown controller ({setValue, getValue}),
// created in the tab's wiring once the DOM exists (#540). Read at launch time;
// no server round-trip — it's per-launch, like the Board dispatch combo.
// '' is the Default option: let Copilot pick its own (auto) model.
let teamOsModelCombo = null;
function teamOsModel() {
  return (teamOsModelCombo && teamOsModelCombo.getValue()) || '';
}

// ----------------------------------------------------------- skills list
export async function fetchSkills() {
  try {
    const body = await jsonApi('/api/team-os/skills');
    state.teamOsSkills = body.skills || [];
    renderSkills();
  } catch (exc) {
    logPollFailure('team-os skills fetch failed', exc);
  }
}

export function renderSkills() {
  const host = els.teamOsList;
  if (!host) return;
  host.innerHTML = '';
  const skills = state.teamOsSkills;
  els.teamOsEmpty.hidden = skills.length !== 0;

  skills.forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'app-item coding-item teamos-item';
    li.dataset.id = s.id;

    const main = document.createElement('div');
    main.className = 'app-main';
    const name = document.createElement('div');
    name.className = 'coding-name';
    name.textContent = s.name;   // name only — one line per tile
    main.appendChild(name);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions agent-actions';

    // 📖 Browse — open the read-only content browser for this skill.
    const browseBtn = document.createElement('button');
    browseBtn.type = 'button';
    browseBtn.className = 'icon-btn agent-btn';
    browseBtn.innerHTML = icon('book-open');
    browseBtn.title = 'Browse what this skill knows';
    browseBtn.setAttribute('aria-label', 'Browse ' + s.name);
    browseBtn.addEventListener('click', function () { openBrowser(s); });
    actions.appendChild(browseBtn);

    // Launch — fires a fresh Copilot session that auto-invokes /<skill>.
    const launchBtn = document.createElement('button');
    launchBtn.type = 'button';
    launchBtn.className = 'icon-btn agent-btn teamos-launch';
    launchBtn.innerHTML = icon('rocket');
    launchBtn.title = 'Launch ' + s.name;
    launchBtn.setAttribute('aria-label', 'Launch ' + s.name);
    launchBtn.addEventListener('click', function () { launchSkill(s); });
    actions.appendChild(launchBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
}

// ------------------------------------------------- weekly recap (issue #167)
// A pinned tile above the skills list: a staleness badge driven by the recap
// ledger's mtime, and a 🚀 that launches /weekly-recap (the interactive
// review). The drafting half runs headless on a schedule, so this is
// review-only. Fetched on every Team OS tab open (cheap stat + glob server-side).
export async function fetchRecapStatus() {
  try {
    state.teamOsRecap = await jsonApi('/api/team-os/recap-status');
    renderRecap();
  } catch (exc) {
    logPollFailure('team-os recap-status fetch failed', exc);
  }
}

function renderRecap() {
  const host = els.teamOsRecap;
  if (!host) return;
  const r = state.teamOsRecap;
  // Hide the tile when team-os isn't checked out — same as the skills list.
  if (!r || !r.available) { host.hidden = true; return; }
  host.hidden = false;

  const badge = els.teamOsRecapBadge;
  const status = r.staleness || 'never';
  badge.className = 'teamos-recap-badge ' + status;
  let label;
  if (status === 'never') {
    label = 'never run';
  } else {
    const d = Math.round(r.age_days || 0);
    const ago = d <= 0 ? 'today' : (d + 'd ago');
    const tag = status === 'due' ? ' · due'
      : status === 'overdue' ? ' · overdue' : '';
    label = ago + tag;
  }
  if (r.proposal_pending) label += ' · draft ready';
  badge.textContent = label;
}

// Toast suffix for the launch model: silent on the Default (Copilot-auto)
// case, " (<model id>)" otherwise — mirroring the old tag's terseness.
function modelTag(model) {
  if (!model) return '';
  return ' (' + model + ')';
}

async function launchRecap() {
  // Reuse the Skills summary controls: ☁️ Detached → remote, the model combo
  // → the launch model ('' = Default, Copilot auto).
  const mode = (els.teamOsDetached && els.teamOsDetached.getAttribute('aria-checked') === 'true')
    ? 'remote' : 'pty';
  const model = teamOsModel();
  const payload = { mode: mode, model: model };
  // A desktop browser launch gets a dedicated PC Edge --app window (issue
  // #241); the phone carries its real terminal size so the PTY spawns at
  // the width the overlay will fit() to (issue #374, #126). Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi('/api/team-os/recap/launch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast(
      'Launched weekly recap' + modelTag(model) +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: 'sprout' }
    );
    // A desktop browser gets its terminal in a dedicated PC Edge window,
    // not in-page (issue #241) — so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Recap launch failed', exc);
  }
}

async function launchSkill(s) {
  // Resume (issue #151) reopens Copilot's session picker, dropping the
  // /<skill> prompt. Detached and Resume are orthogonal (issue #157,
  // matching the Coding tab): Detached → 'remote' independent of Resume, so
  // a Detached+Resume launch renders the picker in the detached console
  // while Resume alone streams it to the phone over a PTY.
  const resume = !!(els.teamOsResume && els.teamOsResume.getAttribute('aria-checked') === 'true');
  const mode = (els.teamOsDetached && els.teamOsDetached.getAttribute('aria-checked') === 'true')
    ? 'remote' : 'pty';
  const model = teamOsModel();
  const payload = { mode: mode, model: model, resume: resume };
  // Same size contract as launchRecap (issue #374, #126, #241). Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi(
      '/api/team-os/skills/' + encodeURIComponent(s.id) + '/launch',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }
    );
    toast(
      (resume ? 'Resumed ' : 'Launched ') + s.name +
        modelTag(model) + (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: resume ? 'rotate-ccw' : 'sprout' }
    );
    // Full-control sessions drop straight into the terminal; detached
    // ones only appear in the Coding tab's running-sessions list. A
    // desktop browser gets its terminal in a dedicated PC Edge window
    // instead of in-page (issue #241), so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Launch failed', exc);
  }
}

// --------------------------------------------------- content browser
// The file currently shown in the doc view — drives the toolbar 🗑️ (which
// deletes conversation logs only). Null while we're on the file list.
let openDocFile = null;

async function openBrowser(s) {
  state.teamOsBrowser = { skillId: s.id, name: s.name, files: [] };
  els.teamOsBrowserTitle.textContent = s.name;
  closeDoc();                       // start on the full-screen file list
  els.teamOsBrowser.hidden = false;
  await loadFileList();
}

// (Re)load the current skill's file list — runs every time the browser
// overlay opens, so a conversation log added on the PC shows up on reopen.
async function loadFileList() {
  const b = state.teamOsBrowser;
  if (!b) return;
  els.teamOsFileList.innerHTML = '<li class="muted small">Loading…</li>';
  try {
    const body = await jsonApi(
      '/api/team-os/skills/' + encodeURIComponent(b.skillId) + '/files'
    );
    b.files = body.files || [];
    renderFileList(b.files);
  } catch (exc) {
    // The content endpoints are Tailscale + passkey gated — a 403 here
    // means this connection can't reach them. Say so plainly, in the
    // (full-screen) list area.
    const msg = (exc && exc.status === 403)
      ? 'The content browser is Tailscale-only (and passkey-gated). Open the ' +
        'launcher over your Tailscale URL on an enrolled device.'
      : 'Could not load files: ' + (exc.message || exc);
    els.teamOsFileList.innerHTML = '';
    const li = document.createElement('li');
    li.className = 'muted small';
    li.textContent = msg;
    els.teamOsFileList.appendChild(li);
  }
}

function renderFileList(files) {
  const host = els.teamOsFileList;
  host.innerHTML = '';
  if (!files.length) {
    const p = document.createElement('li');
    p.className = 'muted small';
    p.textContent = 'No readable files.';
    host.appendChild(p);
    return;
  }
  let lastCat = null;
  files.forEach(function (f) {
    if (f.category !== lastCat) {
      const h = document.createElement('li');
      h.className = 'teamos-file-cat';
      h.textContent = f.category;
      host.appendChild(h);
      lastCat = f.category;
    }
    const li = document.createElement('li');
    li.className = 'teamos-file-row';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'teamos-file-btn';
    btn.textContent = f.name;
    btn.title = f.path;
    btn.addEventListener('click', function () {
      Array.prototype.forEach.call(
        host.querySelectorAll('.teamos-file-btn.active'),
        function (b) { b.classList.remove('active'); }
      );
      btn.classList.add('active');
      loadFile(f);
    });
    li.appendChild(btn);
    // No delete control in the list — the list is navigation only. The 🗑️
    // for a disposable conversation log lives in the document toolbar and
    // appears once the log is open (see openDoc / loadFile below).
    host.appendChild(li);
  });
}

async function deleteFile(f) {
  if (!confirm(
    'Delete this conversation log?\n\n' + f.name +
    '\n\nThe file is removed from disk — this cannot be undone.'
  )) return;
  try {
    await jsonApi(
      '/api/team-os/file?path=' + encodeURIComponent(f.path),
      { method: 'DELETE' }
    );
    toast('Deleted ' + f.name, 'good', { icon: 'trash-2' });
    closeDoc();             // in case the deleted file was the open one
    await loadFileList();
  } catch (exc) {
    apiFailToast('Delete failed', exc);
  }
}

// Lower-case, spaces (and any other punctuation) → single dashes, trimmed —
// the same shape the capture hook's slugs already have.
function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

async function renameFile(f) {
  const proposed = window.prompt(
    'Rename this conversation log.\n\n' +
    'The date keeps unchanged — type the new name (spaces become dashes, ' +
    'lower-cased):',
    ''
  );
  if (proposed === null) return;            // cancelled
  const slug = slugify(proposed);
  if (!slug) { toast('Name cannot be empty', 'error'); return; }
  try {
    const body = await jsonApi('/api/team-os/file/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path, slug: slug }),
    });
    toast('Renamed to ' + (body.name || slug), 'good', { icon: 'pencil' });
    closeDoc();             // name (and path) changed — back to the list
    await loadFileList();
  } catch (exc) {
    apiFailToast('Rename failed', exc);
  }
}

async function loadFile(f) {
  // The file view is a full-screen layer over the list; the ✕ close-doc
  // button in the bar appears only while it's open.
  openDocFile = f;
  openDoc(f);
  els.teamOsFileContent.innerHTML = '<p class="muted small">Loading…</p>';
  try {
    const body = await jsonApi(
      '/api/team-os/file?path=' + encodeURIComponent(f.path)
    );
    els.teamOsFileContent.innerHTML = renderMarkdown(body.content || '');
    if (body.truncated) {
      const note = document.createElement('p');
      note.className = 'muted small';
      note.textContent = '… (truncated)';
      els.teamOsFileContent.appendChild(note);
    }
    els.teamOsFileContent.scrollTop = 0;
  } catch (exc) {
    els.teamOsFileContent.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'Could not load: ' + (exc.message || exc);
    els.teamOsFileContent.appendChild(p);
  }
}

// A conversation log the toolbar may act on — any file under a skill's
// conversations/ EXCEPT the .gitkeep placeholder that keeps the (otherwise
// empty) dir tracked in git. Deleting/renaming that would untrack the dir,
// so it stays off-limits (the server refuses it too — defence in depth).
function isEditableLog(f) {
  return !!f && f.category === 'conversations' &&
    !/(^|\/)\.gitkeep$/.test(f.name || '');
}

// Reveal the full-screen file view (overlaying the list) + the ✕ button.
// The 🗑️ delete and ✏️ rename show only for a conversation log — disposable
// run transcripts, editable while you read them. Every other category (and
// the .gitkeep placeholder) keeps both hidden.
function openDoc(f) {
  els.teamOsFileContent.hidden = false;
  if (els.teamOsDocClose) els.teamOsDocClose.hidden = false;
  const editable = isEditableLog(f);
  if (els.teamOsDocDelete) els.teamOsDocDelete.hidden = !editable;
  if (els.teamOsDocRename) els.teamOsDocRename.hidden = !editable;
}

// Close the open file → back to the full-screen file list.
function closeDoc() {
  openDocFile = null;
  els.teamOsFileContent.hidden = true;
  els.teamOsFileContent.innerHTML = '';
  if (els.teamOsDocClose) els.teamOsDocClose.hidden = true;
  if (els.teamOsDocDelete) els.teamOsDocDelete.hidden = true;
  if (els.teamOsDocRename) els.teamOsDocRename.hidden = true;
  Array.prototype.forEach.call(
    els.teamOsFileList.querySelectorAll('.teamos-file-btn.active'),
    function (b) { b.classList.remove('active'); }
  );
}

// Close the whole browser → back to the skill tiles.
function closeBrowser() {
  state.teamOsBrowser = null;
  closeDoc();
  els.teamOsBrowser.hidden = true;
}

// ------------------------------------------------ minimal markdown render
// Escape-first, then apply a small, safe subset (headings, bold, italic,
// inline code, fenced code, links, unordered lists, paragraphs). Content
// comes from the user's own private files over a passkey-gated tailnet
// link, but we still escape every byte before formatting so a stray
// `<script>` in a note can never execute.
function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

export function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let inCode = false;
  let inList = false;
  let para = [];

  function flushPara() {
    if (para.length) {
      out.push('<p>' + inlineMd(para.join(' ')) + '</p>');
      para = [];
    }
  }
  function flushList() {
    if (inList) { out.push('</ul>'); inList = false; }
  }

  lines.forEach(function (line) {
    if (line.trim().startsWith('```')) {
      flushPara(); flushList();
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { out.push('<pre class="md-code"><code>'); inCode = true; }
      return;
    }
    if (inCode) { out.push(line); return; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length;
      out.push('<h' + level + '>' + inlineMd(h[2]) + '</h' + level + '>');
      return;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      flushPara();
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + inlineMd(li[1]) + '</li>');
      return;
    }
    if (!line.trim()) { flushPara(); flushList(); return; }
    para.push(line.trim());
  });
  if (inCode) out.push('</code></pre>');
  flushPara(); flushList();
  return out.join('\n');
}

// --------------------------------------------------------------- wire
// (Re)build the launch-model menu from server config: a "Default" option
// (Copilot auto, value "") plus the config-driven copilot_models list —
// the Team OS combo mirrors how copilot-options.js reads state.config,
// instead of hardcoding model ids in index.html. Idempotent: called at
// wire time (config may not be loaded yet — boot() awaits fetchConfig
// before the tab is usable) and again on every tab open, preserving the
// current selection when it survives the rebuild.
function populateModelMenu() {
  const menu = document.getElementById('teamOsModelMenu');
  const comboRoot = document.getElementById('teamOsModelCombo');
  if (!menu || !comboRoot) return;
  const cp = state.config && state.config.copilot;
  const models = (cp && cp.models_available) || [];
  const current = comboRoot.dataset.value || '';
  menu.innerHTML = '';
  [''].concat(models).forEach(function (m) {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'option');
    b.dataset.value = m;
    b.setAttribute('aria-selected', String(m === current));
    b.textContent = m || 'Default';
    menu.appendChild(b);
  });
  // The previous selection vanished from the list — fall back to Default.
  if (current && models.indexOf(current) === -1 && teamOsModelCombo) {
    teamOsModelCombo.setValue('');
  }
}

export function wireTeamOs() {
  if (els.teamOsBrowserBack) {
    els.teamOsBrowserBack.addEventListener('click', closeBrowser);
  }
  if (els.teamOsDocClose) {
    els.teamOsDocClose.addEventListener('click', closeDoc);
  }
  if (els.teamOsDocDelete) {
    // Delete the open conversation log → confirm, DELETE, back to the list
    // (deleteFile closeDoc()s, exactly like ✕).
    els.teamOsDocDelete.addEventListener('click', function () {
      if (openDocFile) deleteFile(openDocFile);
    });
  }
  if (els.teamOsDocRename) {
    // Rename the open conversation log → prompt, POST, back to the list.
    els.teamOsDocRename.addEventListener('click', function () {
      if (openDocFile) renameFile(openDocFile);
    });
  }
  if (els.teamOsRecapLaunch) {
    els.teamOsRecapLaunch.addEventListener('click', launchRecap);
  }
  // Detached/Resume are plain client-side switches (issue #355) — no server
  // config, just read at launch time above. They live in the Skills card's
  // <summary> (#496 round 2, mirroring the Coding tab's Projects card), so
  // stopPropagation keeps a tap from also collapsing the panel.
  [els.teamOsDetached, els.teamOsResume].forEach(function (btn) {
    if (!btn) return;
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleAriaChecked(btn);
    });
  });
  // The model dropdown (#540) shares that summary; wireModelCombo owns its
  // open/close + the summary-tap guard. Read at launch time via teamOsModel().
  populateModelMenu();
  teamOsModelCombo = wireModelCombo(
    document.getElementById('teamOsModelCombo'), null
  );
  // Refresh skills + recap staleness the moment the tab opens (cheap: a live
  // directory scan + a single ledger stat), and rebuild the model menu from
  // the (by now loaded) server config.
  if (els.tabTeamOS) {
    els.tabTeamOS.addEventListener('click', function () {
      populateModelMenu();
      fetchSkills().catch(function () {});
      fetchRecapStatus().catch(function () {});
    });
  }
}
