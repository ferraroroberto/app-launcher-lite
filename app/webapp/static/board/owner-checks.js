/* Owner checks on the Board: issues that wait on the owner.
 *
 * Two kinds, from fleet-config's owner_check labels (GET /api/board's
 * `owner_checks` section): shipped work to validate as a user
 * (`validate-after`) and a step owed before work can start (`before-start`).
 * They sit in Your turn under the live sessions in three disjoint groups, one
 * fold each and one issue in one group: Before you start (an open
 * `before-start`), Validate to close (an open `validate-after`, naming the plan
 * rows it unblocks) and Try what shipped (a closed issue, only a check). The
 * first two are the Backlog's Waiting on you; the three are the badge. A row's
 * tap unfolds its own detail in place: why it needs you, what to try, and the
 * links to the issue and its proof. The detail's Answer, like its group's
 * own button, opens that group's owner-check answer sheet (board-answers.js), whose
 * blocks owner-answer.js draws.
 *
 * The list is a cached read with its age. Before one has landed it is
 * unknown, never "none"; repos the list could not read make it a lower bound,
 * and the group says so. An item answered Pending, Pause, Close or Problem keeps
 * its row, marked, and leaves the "needs you" count.
 */

import { els } from '../state.js';
import { chip } from '../glance.js';
import { actionRow } from '../action-rows.js';
import { el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { boardRepoFilter, matchesRepoFilter } from './board-dispatch.js';
import { foldGroup } from './chief-plan.js';
import { agoText, calendarDaysFromToday, newestDateFirst, plural, shortDate } from '../shared/format.js';
import { renderBoard } from '../board.js';
import {
  BEFORE_START_GROUP, TRY_SHIPPED, VALIDATE_TO_CLOSE, ownerGroup, ownerItemWaiting, ownerNeedsCount,
  ownerSection,
} from './owner-needs.js';
import { bucketName } from './plan-buckets.js';

// Before start blocks work from starting, so it leads; Try what shipped is
// only a check, so it trails.
const GROUPS = [
  { key: BEFORE_START_GROUP, heading: 'Before you start', label: 'Before start' },
  { key: VALIDATE_TO_CLOSE, heading: 'Validate to close', label: 'Validate to close' },
  { key: TRY_SHIPPED, heading: 'Try what shipped', label: 'Try shipped' },
];

// The ref whose detail is open, across the 5 s poll's rebuilds.
let expandedRef = null;

const section = ownerSection;
const waiting = ownerItemWaiting;

// Re-exported: the Board's head line and the nav badge share one rule.
export { ownerNeedsCount };

// Newest first: `since` descending, the higher (later filed) issue number
// breaking a tie, and an item with no readable `since` after the dated ones.
function newestFirst(a, b) {
  const byDate = newestDateFirst(a.since, b.since);
  if (byDate) return byDate;
  return (Number(b.number) || 0) - (Number(a.number) || 0);
}

// The items the repo filter shows, the waiting ones first, each half newest
// first (each group of the card filters this list, so it inherits the order).
// `groupKey`, when given, keeps one group's items: the sheet a group opens.
export function shownOwnerItems(body, groupKey) {
  const sec = section(body);
  if (!sec) return [];
  const filter = boardRepoFilter();
  const items = (sec.items || []).filter(function (it) {
    return matchesRepoFilter(it, filter) && (!groupKey || ownerGroup(it) === groupKey);
  });
  return items.filter(waiting).sort(newestFirst)
    .concat(items.filter(function (it) { return !waiting(it); }).sort(newestFirst));
}

export function groupLabel(key) {
  const g = GROUPS.find(function (x) { return x.key === key; });
  return g ? g.label : key;
}

// "3 Nov · waiting 3 d" from the block's `since` date; '' when there is none.
function sinceText(since) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(since || ''));
  if (!m) return '';
  const days = Math.max(0, calendarDaysFromToday(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))));
  return shortDate(since) + (days ? ' · waiting ' + days + ' d' : ' · since today');
}

function markText(item) {
  if (item.mark === 'later') return 'snoozed';
  if (item.mark === 'problem') return 'problem reported, waiting on a fix';
  if (item.mark === 'paused') return 'pause asked, waiting on the chief';
  if (item.mark === 'closing') return 'close asked, waiting on the chief';
  // Open, unmarked, yet not counted: its issue sits outside Waiting on you.
  if (item.counted === false && item.bucket === 'absent') return 'not in the Backlog yet, not counted';
  if (item.counted === false) return bucketName(item.bucket).toLowerCase() + ', not counted';
  return '';
}

// A proof link's label: an MR by its number, an artifact by name, else the host.
function linkLabel(url) {
  const pr = /\/merge_requests\/(\d+)/.exec(url);
  if (pr) return 'MR !' + pr[1];
  if (/claude\.ai\/(code\/)?artifact/.test(url)) return 'Artifact';
  const issue = /\/issues\/(\d+)/.exec(url);
  if (issue) return 'Issue #' + issue[1];
  try {
    return new URL(url).hostname;
  } catch (_) {
    return 'Link';
  }
}

// A reference pill inside a 44px tap target.
function link(url, label) {
  const a = el('a', 'board-owner-link');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  const pill = el('span', 'board-owner-pill');
  pill.innerHTML = icon('link');
  pill.appendChild(document.createTextNode(label));
  a.appendChild(pill);
  return a;
}

// The issue first, then every proof link the block carries (http(s) only).
export function links(item) {
  const wrap = el('div', 'board-owner-links');
  if (item.url) wrap.appendChild(link(item.url, 'Issue #' + item.number));
  (item.proof || []).forEach(function (url) {
    if (/^https?:\/\//.test(url)) wrap.appendChild(link(url, linkLabel(url)));
  });
  return wrap;
}

// "unblocks <repo>#<n>, ...": the plan rows parked on this issue.
function unblocksText(item) {
  const refs = item.unblocks || [];
  return refs.length ? 'unblocks ' + refs.join(', ') : '';
}

// Why it needs you and what to try; a label with no readable block says so.
export function details(item, cls) {
  const nodes = [];
  if (item.unblocks && item.unblocks.length) nodes.push(labelled(cls, 'Unblocks', item.unblocks.join(', ')));
  if (!item.block) {
    nodes.push(el('p', cls, 'Details unknown: the issue carries the label but no readable owner-check note. Open the issue to see what it needs.'));
    return nodes;
  }
  if (item.reason) nodes.push(labelled(cls, 'Why', item.reason));
  nodes.push.apply(nodes, tryNodes(item, cls));
  return nodes;
}

// What to try: the recorded Try; else lines the issue's own checklist offers,
// quoted and labelled as such; else a plain "none", never boilerplate.
function tryNodes(item, cls) {
  if (item.try) return [labelled(cls, 'Try', item.try)];
  const lines = item.try_lines || [];
  if (!lines.length) {
    return [el('p', cls + ' board-owner-try-none', 'No specific Try on record. Open the issue to see what it shipped.')];
  }
  const head = el('p', cls);
  head.appendChild(el('strong', null, 'Try'));
  head.appendChild(document.createTextNode(" (from the issue's " + (item.try_from || 'checklist').toLowerCase() + '):'));
  const list = el('ul', 'board-owner-try-list ' + cls);
  lines.forEach(function (line) { list.appendChild(el('li', null, line)); });
  return [head, list];
}

function labelled(cls, label, text) {
  const p = el('p', cls);
  p.appendChild(el('strong', null, label + ': '));
  p.appendChild(document.createTextNode(text));
  return p;
}

// ---------------------------------------------------------------- the lane

function itemRow(item, onAnswer) {
  const expanded = expandedRef === item.ref;
  const title = item.title || item.ref;
  const meta = [item.ref, unblocksText(item)].filter(Boolean).join(' · ');
  const tail = markText(item) || sinceText(item.since);
  const chips = item.block ? [] : [chip('details unknown', 'attention', 'board-owner-unknown')];
  const row = actionRow({
    className: 'board-item board-owner-row' + (waiting(item) ? '' : ' is-answered'),
    title: title,
    meta: meta,
    tail: tail,
    chips: chips,
    label: (expanded ? 'Hide' : 'Show') + ' what ' + item.ref + ' needs',
    onMain: function () {
      expandedRef = expanded ? null : item.ref;
      renderBoard();
    },
  });
  row.li.dataset.ref = item.ref;
  row.main.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (!expanded) return row.li;
  row.li.classList.add('expanded');
  const detail = el('div', 'board-owner-detail');
  detail.id = 'owner-detail-' + item.ref.replace(/[^A-Za-z0-9_-]/g, '-');
  row.main.setAttribute('aria-controls', detail.id);
  details(item, 'board-owner-line').forEach(function (n) { detail.appendChild(n); });
  detail.appendChild(links(item));
  const answer = el('button', 'button-tint board-owner-answer', 'Answer');
  answer.type = 'button';
  answer.addEventListener('click', function () { onAnswer(item.ref, ownerGroup(item)); });
  detail.appendChild(answer);
  row.li.appendChild(detail);
  return row.li;
}

// One group's fold: its rows, then its own answer button (inside the fold, so
// a folded group carries nothing but its heading and count). Open by default.
// The count is what the badge counts; a row that is shown but not counted (set
// aside, or its issue in progress) is said, never folded into the number.
function group(g, items, onAnswer) {
  const rows = items.map(function (it) { return itemRow(it, onAnswer); });
  const waitingItems = items.filter(waiting);
  const aside = items.length - waitingItems.length;
  const count = waitingItems.length + (aside ? ' · ' + aside + ' not counted' : '');
  const after = [];
  if (waitingItems.length) {
    const btn = el('button', 'button-tint board-plan-answer board-owner-answer-group');
    btn.type = 'button';
    btn.innerHTML = icon('messages-square') + ' ';
    btn.appendChild(document.createTextNode('Answer ' + g.label + ' (' + waitingItems.length + ')'));
    if (g.key === BEFORE_START_GROUP) btn.dataset.track = 'board.owner-answer-before-start';
    else if (g.key === VALIDATE_TO_CLOSE) btn.dataset.track = 'board.owner-answer-validate';
    else btn.dataset.track = 'board.owner-answer-try';
    btn.addEventListener('click', function () { onAnswer(waitingItems[0].ref, g.key); });
    after.push(btn);
  }
  const fold = { key: 'owner:' + g.key, heading: g.heading + ' (' + count + ')', rows: rows,
    after: after, cls: 'board-owner-group' };
  let wrap;
  if (g.key === BEFORE_START_GROUP) wrap = foldGroup({ ...fold, track: 'board.owner-before-start-fold' });
  else if (g.key === VALIDATE_TO_CLOSE) wrap = foldGroup({ ...fold, track: 'board.owner-validate-fold' });
  else wrap = foldGroup({ ...fold, track: 'board.owner-try-fold' });
  wrap.dataset.group = g.key;
  return wrap;
}

// Your turn's counts as the repo filter shows them. Unfiltered they are the
// server's (`backlog_buckets.your_turn`); filtered they are the same terms
// rebuilt from the shown items and the filter's Waiting on you issues, so the
// line never counts rows the list does not draw. `hidden` is what waits on you
// in other projects, like the Chief's plan card's "N more questions".
function turnCounts(body, turn) {
  const filter = boardRepoFilter();
  if (!filter) return { ...turn, hidden: 0 };
  const items = shownOwnerItems(body);
  const inGroup = function (key) {
    return items.filter(function (it) { return ownerGroup(it) === key && waiting(it); }).length;
  };
  const listed = new Set(items.filter(function (it) { return ownerGroup(it) !== TRY_SHIPPED; })
    .map(function (it) { return String(it.ref).toLowerCase(); }));
  const backlog = (body.columns && body.columns.backlog) || [];
  const unlisted = backlog.filter(function (c) {
    return c.bucket === 'waiting' && matchesRepoFilter(c, filter)
      && !listed.has((c.repo + '#' + c.number).toLowerCase());
  }).length;
  const counts = {
    before_start: inGroup(BEFORE_START_GROUP),
    validate_to_close: inGroup(VALIDATE_TO_CLOSE),
    try_shipped: inGroup(TRY_SHIPPED),
    waiting_set_aside: items.filter(function (it) {
      return it.bucket === 'waiting' && !waiting(it);
    }).length,
    unlisted: unlisted,
  };
  counts.waiting = counts.before_start + counts.validate_to_close + counts.waiting_set_aside + unlisted;
  const mine = counts.before_start + counts.validate_to_close + counts.try_shipped;
  counts.hidden = Math.max(0, (ownerNeedsCount(body) || 0) - mine);
  return counts;
}

// How the Backlog's Waiting on you adds up to Your turn's groups, in words:
// "12 waiting on you (9 before you start + 3 validate to close) · 9 to try".
// What the owner set aside, and what the list lacks, are terms of the sum.
function reconcileText(body, fleetTurn) {
  const turn = turnCounts(body, fleetTurn);
  const terms = [turn.before_start + ' before you start', turn.validate_to_close + ' validate to close'];
  if (turn.waiting_set_aside) terms.push(turn.waiting_set_aside + ' set aside');
  if (turn.unlisted) terms.push(turn.unlisted + ' not in this list yet');
  const text = turn.waiting + ' waiting on you (' + terms.join(' + ') + ') · ' + turn.try_shipped + ' to try';
  return turn.hidden ? text + ' · ' + turn.hidden + ' more in other projects' : text;
}

// The list's freshness and gaps, said every time: its age, a failed
// refresh, unreadable repos. Never a bare empty list.
function stateLine(sec, body) {
  if (!sec.available && !sec.error && !sec.refreshing) {
    return 'Owner checks not loaded yet. Refresh reads them.';
  }
  if (!sec.available) {
    return sec.refreshing ? 'Reading owner checks…' : 'Owner checks unknown: the list could not be read.';
  }
  const parts = ['Owner checks updated ' + (agoText(sec.fetched_at) || 'at an unknown time')];
  if (sec.refreshing) parts.push('refreshing');
  if (sec.error) parts.push('the last refresh failed');
  const unknown = (sec.unknown || []).length;
  if (unknown) parts.push(plural(unknown, 'repo', 'repos') + ' unreadable, so this list may be short');
  const turn = (body && body.backlog_buckets && body.backlog_buckets.your_turn) || {};
  if (Number.isInteger(turn.waiting)) parts.push(reconcileText(body, turn));
  return parts.join(' · ');
}

// Fills Your turn's owner block; returns how many waiting items it shows, for
// the lane's count. `onAnswer(ref, kind)` opens that group's owner-check sheet on that item.
export function renderOwnerChecks(body, onAnswer) {
  const host = els.boardColumns && els.boardColumns.querySelector('.board-owner');
  if (!host) return 0;
  const sec = section(body);
  if (!sec) {
    host.dataset.sig = '';
    host.replaceChildren();
    return 0;
  }
  const items = shownOwnerItems(body);
  const waitingCount = items.filter(waiting).length;
  const sig = JSON.stringify([sec && sec.available, sec && sec.fetched_at, sec && sec.error,
    sec && sec.refreshing, sec && sec.unknown, items, expandedRef,
    body && body.backlog_buckets && body.backlog_buckets.your_turn,
    agoText(sec && sec.fetched_at), boardRepoFilter()]);
  if (host.dataset.sig === sig) return waitingCount;
  host.dataset.sig = sig;
  // The state line leads, where the Chief's plan card puts its age line.
  const line = el('p', 'board-plan-line muted small board-owner-state', stateLine(sec, body));
  if (sec && sec.error) line.title = sec.error;
  const nodes = [line];
  GROUPS.forEach(function (g) {
    const mine = items.filter(function (it) { return ownerGroup(it) === g.key; });
    if (mine.length) nodes.push(group(g, mine, onAnswer));
  });
  host.replaceChildren.apply(host, nodes);
  return waitingCount;
}
