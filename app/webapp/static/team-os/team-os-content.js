/* Team OS content browser: a read-only sheet of one skill's files —
 * public SKILL.md/description.md plus the private
 * context/memory/examples/conversations — and the document sheet that opens
 * one. A conversation log (disposable, unlike every other category) can be
 * renamed or deleted from here. The full app's team-os-content.js, cut to
 * what team-os offers.
 *
 * Those content endpoints are private-network + passkey gated server-side,
 * so a fetch may 403; we surface the reason rather than a blank pane.
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { nameLabel } from '../shared/format.js';
import { renderMarkdown } from '../markdown.js';
import { confirmDialog, promptDialog } from '../confirm-dialog.js';

// The file the document sheet shows, which its Rename log and Delete log
// rows act on. Null while the sheet is closed.
let openDocFile = null;

export async function openBrowser(s) {
  const d = els.teamOsBrowser;
  if (!d) return;
  state.teamOsBrowser = { skillId: s.id, name: s.name, files: [] };
  els.teamOsBrowserTitle.textContent = s.name;
  if (!d.open) d.showModal();
  await loadFileList();
}

// (Re)load the current skill's file list — runs every time the browser
// opens, so a conversation log added on the PC shows up on reopen.
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
    // The content endpoints are private-network + passkey gated — a 403 here
    // means this connection can't reach them. Say so plainly, in the list.
    const msg = (exc && exc.status === 403)
      ? 'The content browser is private-network-only (and passkey-gated). ' +
        'Open the launcher over your Tailscale/VPN URL on an enrolled device.'
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
  // The server sorts by category, so each category is one contiguous run.
  const runs = [];
  files.forEach(function (f) {
    const last = runs[runs.length - 1];
    if (last && last.category === f.category) last.files.push(f);
    else runs.push({ category: f.category, files: [f] });
  });
  runs.forEach(function (run) {
    const h = document.createElement('li');
    h.className = 'teamos-file-cat';
    h.textContent = nameLabel(run.category);
    host.appendChild(h);
    renderCategory(host, run);
  });
}

// A category's top-level files first, then one collapsed row per sub-folder
// (machine output such as a skill's generated history sits there), so the
// curated files are on the first screen. A category with no sub-folder
// renders exactly as a flat list.
function renderCategory(host, run) {
  const top = [];
  const folders = new Map();
  run.files.forEach(function (f) {
    const name = String(f.name || '');
    // The server labels a nested file with its OS path separator.
    const slash = name.search(/[\\/]/);
    if (slash <= 0) { top.push(f); return; }
    const folder = name.slice(0, slash);
    if (!folders.has(folder)) folders.set(folder, []);
    folders.get(folder).push({ file: f, label: name.slice(slash + 1) });
  });
  top.forEach(function (f) { host.appendChild(fileRow(f, f.name, false)); });
  folders.forEach(function (entries, folder) {
    const key = run.category + '/' + folder;
    const open = openFolders().has(key);
    const rows = entries.map(function (e) {
      const row = fileRow(e.file, e.label, true);
      row.hidden = !open;
      return row;
    });
    host.appendChild(folderRow(key, folder, rows, open));
    rows.forEach(function (row) { host.appendChild(row); });
  });
}

// The folders the reader has expanded, kept on the open browser so a reload
// of the list (a log added or deleted) does not fold them again.
function openFolders() {
  const b = state.teamOsBrowser;
  if (!b.openFolders) b.openFolders = new Set();
  return b.openFolders;
}

function folderRow(key, folder, rows, open) {
  const li = document.createElement('li');
  li.className = 'teamos-file-row';
  const btn = document.createElement('button');
  btn.dataset.track = 'team-os.folder-fold';
  btn.type = 'button';
  btn.className = 'teamos-folder-btn';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  const label = document.createElement('span');
  label.className = 'teamos-folder-label';
  label.textContent = folder + ' · ' + rows.length + (rows.length === 1 ? ' file' : ' files');
  const chevron = document.createElement('span');
  chevron.className = 'teamos-folder-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.innerHTML =
    '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-chevron-right"></use></svg>';
  btn.appendChild(label);
  btn.appendChild(chevron);
  btn.addEventListener('click', function () {
    const nowOpen = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
    if (nowOpen) openFolders().add(key); else openFolders().delete(key);
    rows.forEach(function (row) { row.hidden = !nowOpen; });
  });
  li.appendChild(btn);
  return li;
}

function fileRow(f, label, nested) {
  const li = document.createElement('li');
  li.className = nested ? 'teamos-file-row teamos-file-nested' : 'teamos-file-row';
  const btn = document.createElement('button');
  btn.dataset.track = 'team-os.file-open';
  btn.type = 'button';
  btn.className = 'teamos-file-btn';
  btn.textContent = label;
  btn.title = f.path;
  btn.addEventListener('click', function () { loadFile(f); });
  li.appendChild(btn);
  // No delete control in the list — the list is navigation only. A
  // disposable conversation log's Rename and Delete sit under its text
  // in the document sheet (see loadFile below).
  return li;
}

async function deleteFile(f) {
  const ok = await confirmDialog({
    title: 'Delete this conversation log?',
    message: f.name + ' is removed from disk. This cannot be undone.',
    action: 'Delete',
  });
  if (!ok) return;
  try {
    await jsonApi(
      '/api/team-os/file?path=' + encodeURIComponent(f.path),
      { method: 'DELETE' }
    );
  } catch (exc) {
    apiFailToast('Delete failed', exc);
    return;
  }
  toast('Deleted ' + f.name, 'good', { icon: 'trash-2' });
  closeDoc();
  await loadFileList();
}

// Lower-case, spaces (and any other punctuation) → single dashes, trimmed —
// the same shape the capture hook's slugs already have.
function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

// The name part of a log's file name: what follows the date-and-time prefix
// the server keeps on a rename (_DATE_PREFIX_RE in the team-os router).
function logSlug(name) {
  return String(name || '').replace(/\.md$/i, '')
    .replace(/^\d{4}-\d{2}-\d{2}-\d{4}-/, '');
}

// The rename shares the confirm sheet's field (promptDialog).
const RENAME_LOG_SHEET = {
  title: 'Rename log', action: 'Rename', required: true, label: 'New name',
  message: 'The date stays. Spaces become dashes, lower-cased.',
};

async function renameFile(f) {
  const proposed = await promptDialog(Object.assign({ tone: 'primary', value: logSlug(f.name) }, RENAME_LOG_SHEET));
  if (proposed === null) return;            // cancelled
  const slug = slugify(proposed);
  if (!slug) { toast('Name cannot be empty', 'error'); return; }
  let body;
  try {
    body = await jsonApi('/api/team-os/file/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path, slug: slug }),
    });
  } catch (exc) {
    apiFailToast('Rename failed', exc);
    return;
  }
  toast('Renamed to ' + (body.name || slug), 'good', { icon: 'pencil' });
  // Name (and path) changed: back to the list.
  closeDoc();
  await loadFileList();
}

async function loadFile(f) {
  const d = els.teamOsDoc;
  if (!d) return;
  openDocFile = f;
  els.teamOsDocTitle.textContent = f.name || 'File';
  // Rename and Delete only for a conversation log — disposable run
  // transcripts, editable while you read them. Every other category (and
  // the .gitkeep placeholder) only reads.
  els.teamOsDocLinks.hidden = !isEditableLog(f);
  els.teamOsFileContent.innerHTML = '<p class="muted small">Loading…</p>';
  if (!d.open) d.showModal();
  try {
    const body = await jsonApi(
      '/api/team-os/file?path=' + encodeURIComponent(f.path)
    );
    if (openDocFile !== f) return;          // closed or moved on meanwhile
    els.teamOsFileContent.innerHTML = renderMarkdown(body.content || '');
    if (body.truncated) {
      const note = document.createElement('p');
      note.className = 'muted small';
      note.textContent = '… (truncated)';
      els.teamOsFileContent.appendChild(note);
    }
    els.teamOsFileContent.scrollTop = 0;
  } catch (exc) {
    if (openDocFile !== f) return;
    els.teamOsFileContent.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'Could not load: ' + (exc.message || exc);
    els.teamOsFileContent.appendChild(p);
  }
}

// A conversation log the sheet may act on — any file under a skill's
// conversations/ EXCEPT the .gitkeep placeholder that keeps the (otherwise
// empty) dir tracked in git. Deleting/renaming that would untrack the dir,
// so it stays off-limits (the server refuses it too — defence in depth).
function isEditableLog(f) {
  return !!f && f.category === 'conversations' &&
    !/(^|\/)\.gitkeep$/.test(f.name || '');
}

function closeDoc() {
  if (els.teamOsDoc && els.teamOsDoc.open) els.teamOsDoc.close();
}

// A sheet that simply closes on its own buttons, a backdrop tap, or Escape.
function wireSheet(d, buttons, onClose) {
  if (!d) return;
  buttons.forEach(function (b) {
    if (b) b.addEventListener('click', function () { d.close(); });
  });
  d.addEventListener('click', function (ev) {
    if (ev.target === d) d.close();
  });
  if (onClose) d.addEventListener('close', onClose);
}

export function wireContentBrowser() {
  // Browse and its document sheet: ✕ or Done closes each, back to
  // the one under it.
  wireSheet(els.teamOsBrowser, [els.teamOsBrowserClose, els.teamOsBrowserDone],
    function () { state.teamOsBrowser = null; });
  wireSheet(els.teamOsDoc, [els.teamOsDocClose, els.teamOsDocDone], function () {
    openDocFile = null;
    els.teamOsFileContent.innerHTML = '';
  });
  if (els.teamOsDocDelete) {
    els.teamOsDocDelete.addEventListener('click', function () {
      if (openDocFile) deleteFile(openDocFile);
    });
  }
  if (els.teamOsDocRename) {
    els.teamOsDocRename.addEventListener('click', function () {
      if (openDocFile) renameFile(openDocFile);
    });
  }
}
