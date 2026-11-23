/* Owner checks in the answer sheet: each item's block and its answer.
 *
 * The Board's Your turn lists owner checks (owner-checks.js); their answer
 * sheet (board-answers.js) draws one block per item here, with the options
 * and Other row a chief question has (answer-item.js) and the item's files
 * (answer-files.js), then reads the answer back and posts it. It loads with
 * the sheet, not with the Board, so the Board's first draw never waits for it.
 */

import { el } from '../dom-utils.js';
import { terminalJsonApi } from '../webauthn.js';
import { ownerGroup } from './owner-needs.js';
import { details, groupLabel, links } from './owner-checks.js';
import { attachedLines, emptyFiles } from './answer-files.js';
import { optionList, otherRow, paintOptions } from './answer-item.js';

export const VALIDATE_AFTER = 'validate-after';
export const BEFORE_START = 'before-start';

const OPTIONS = {
  [VALIDATE_AFTER]: [
    { answer: 'validated', label: 'Validated', description: 'It works: removes the label and notes it on the issue' },
    { answer: 'problem', label: 'Problem found', description: 'Describe it in Other: the chief files a fix' },
  ],
  [BEFORE_START]: [
    { answer: 'done', label: 'Done', description: 'The step is done: removes the label so the work can start' },
    { answer: 'pending', label: 'Pending', description: 'Not yet: asks again tomorrow' },
    { answer: 'pause', label: 'Pause', description: 'Hold the work: the chief pauses it' },
    { answer: 'close', label: "Close (won't do)", description: 'Drop it: the chief closes the issue' },
  ],
};

const OPTION_LABEL = Object.fromEntries(
  Object.values(OPTIONS).flat().map(function (o) { return [o.answer, o.label]; }));

export function emptyOwnerDraft() {
  // `other`: the Other field's text. `error`: why the last Done could not apply
  // this answer, shown in its block. `files`: [{ name, path }] stored on the
  // chief's session; never posted to GitLab.
  return { pick: null, other: '', error: '', files: emptyFiles() };
}

export function ownerSheetKey(item) {
  return 'owner:' + item.ref;
}

// Words that only say an item works. Text made of these alone (with at least one
// AFFIRMS word), typed with no option picked on an item to validate, validates
// it; anything else, a negation included, is a problem report.
const AFFIRMS = ['validated', 'validate', 'works', 'working', 'worked', 'ok', 'okay', 'fine', 'good',
  'great', 'perfect', 'lgtm', 'confirmed', 'verified', 'tested', 'yes', 'yep', 'done', 'correct'];
const FILLER = ['it', 'all', 'looks', 'everything', 'now', 'well', 'and', 'as', 'expected',
  'correctly', 'here', 'on', 'phone', 'desktop', 'both', 'really', 'very', 'seems', 'is'];

export function affirmsItWorks(text) {
  const words = String(text || '').toLowerCase().split(/[^a-z]+/).filter(Boolean);
  return words.some(function (w) { return AFFIRMS.indexOf(w) !== -1; })
    && words.every(function (w) { return AFFIRMS.indexOf(w) !== -1 || FILLER.indexOf(w) !== -1; });
}

function otherText(draft) {
  return String((draft && draft.other) || '').split(/\s+/).filter(Boolean).join(' ');
}

// One item's answer, or null. A picked option is the answer and the Other text
// rides with it as its note; a Problem found needs that text, so without it
// there is no answer yet. With no pick, the text alone answers: an item to
// validate reads it (works, or a problem), a before-start step takes it as a
// Pending note. `attachments`, set only when files ride with it, is how many: a
// number, the only thing about them that goes to GitLab. Files with no answer
// make no answer (the draft keeps them).
export function ownerAnswerOf(item, draft) {
  const text = otherText(draft);
  const pick = draft && draft.pick;
  let answer = pick;
  if (!pick && text) {
    answer = item.kind === BEFORE_START ? 'pending' : (affirmsItWorks(text) ? 'validated' : 'problem');
  }
  if (!answer || (answer === 'problem' && !text)) return null;
  const ans = { ref: item.ref, kind: item.kind, answer: answer };
  if (text) ans.text = text;
  const count = ((draft && draft.files) || []).length;
  if (count) ans.attachments = count;
  return ans;
}

// An answer the chief has to act on, so it must be running: a problem to file, a
// pause or close to carry out, or files to store. Validated, Done and Pending
// are complete on the launcher's side.
export function ownerNeedsChief(ans, files) {
  return ans.answer === 'problem' || ans.answer === 'pause' || ans.answer === 'close' || files.length > 0;
}

// What Done will send for the item, said under its Other field; '' when there is
// nothing to say yet.
export function ownerMeaning(item, draft) {
  const text = otherText(draft);
  const ans = ownerAnswerOf(item, draft);
  if (!ans) return draft && draft.pick === 'problem' ? 'Problem found needs a description in Other.' : '';
  if (!text) return '';
  if (!draft.pick && item.kind === VALIDATE_AFTER) {
    return ans.answer === 'validated' ? 'Sends Validated, with this as a note.'
      : 'Sends a problem report: the chief files a fix.';
  }
  if (ans.answer === 'problem') return 'Sends a problem report: the chief files a fix.';
  return 'Sends ' + OPTION_LABEL[ans.answer] + ', with this as a note.';
}

// The lines the chief reads for an applied answer, its files' paths under it
// (this goes to the chief's terminal, never GitLab).
export function ownerMessageLine(item, ans, files) {
  const what = item.title ? ' ' + item.title : '';
  const note = ans.text ? ' (note: ' + ans.text + ')' : '';
  const head = '[' + item.ref + '] ';
  let line;
  if (ans.answer === 'validated') {
    line = head + 'Validated:' + what + note;
  } else if (ans.answer === 'done') {
    line = head + 'Before-start step done:' + what + note;
  } else if (ans.answer === 'problem') {
    line = head + 'Problem found validating' + what + ': ' + ans.text + ' (please file a fix)';
  } else {
    line = head + 'Before-start: ' + ans.answer + (item.title ? ' - ' + item.title : '') + note;
  }
  return [line].concat(attachedLines(files)).join('\n');
}

// One owner check in the answer sheet: its kind, repo and ref, the title as
// the question, why and what to try, its links, then the same option rows and
// Other row a chief question has (answer-item.js). `mic(input, onRender)` adds
// the field's dictation, which the sheet owns, as `files` (answer-files.js)
// adds the item's paperclip and chips.
export function ownerSheetBlock(item, draft, onChange, mic, files) {
  const block = el('section', 'tr-ask chief-answer owner-answer');
  block.dataset.key = ownerSheetKey(item);
  const head = el('div', 'tr-ask-head chief-answer-head');
  head.appendChild(el('span', 'tr-ask-header', groupLabel(ownerGroup(item))));
  head.appendChild(el('span', 'tr-ask-header chief-answer-repo', item.repo));
  const ref = el('a', 'chief-answer-ref', item.ref);
  ref.href = item.url;
  ref.target = '_blank';
  ref.rel = 'noopener';
  head.appendChild(ref);
  block.appendChild(head);
  block.appendChild(el('p', 'tr-ask-question', item.title || item.ref));
  details(item, 'tr-ask-hint owner-answer-line').forEach(function (n) { block.appendChild(n); });
  block.appendChild(links(item));

  const options = OPTIONS[item.kind];
  // What Done will send, said before Done.
  const meaning = el('p', 'tr-ask-hint owner-answer-meaning');
  meaning.setAttribute('aria-live', 'polite');
  const explain = function () {
    const text = ownerMeaning(item, draft);
    meaning.hidden = !text;
    meaning.textContent = text;
  };
  // Why the last Done could not apply this answer; any edit clears it.
  const error = el('p', 'tr-ask-hint owner-answer-error', draft.error ? 'Not saved: ' + draft.error : '');
  error.setAttribute('role', 'status');
  error.hidden = !draft.error;
  const changed = function () {
    draft.error = '';
    error.hidden = true;
    explain();
    onChange();
  };
  const other = otherRow({
    value: draft.other,
    placeholder: 'Other',
    ariaLabel: 'Other answer for ' + item.ref,
    files: files,
    mic: mic,
    onInput: function (field) {
      draft.other = field.value;
      changed();
    },
  });
  const paint = function () {
    paintOptions(block, function (i) { return options[i].answer === draft.pick; });
  };
  block.appendChild(optionList({
    groupLabel: 'Answer ' + item.ref,
    options: options.map(function (o) { return { label: o.label, description: o.description, key: o.answer }; }),
    track: 'board.owner-check-option',
    onPick: function (i) {
      draft.pick = draft.pick === options[i].answer ? null : options[i].answer;
      paint();
      changed();
    },
  }));
  block.appendChild(other.row);
  block.appendChild(other.count);
  block.appendChild(files.extra);
  block.appendChild(meaning);
  block.appendChild(error);
  explain();
  paint();
  return block;
}

// Answers per request: well under the route's own cap, so one request's
// GitLab writes (a glab call each, seconds apiece) finish within a phone's
// patience.
export const OWNER_ANSWERS_PER_REQUEST = 10;

// POST the sheet's owner-check answers (passkey-gated: GitLab writes), one
// request per OWNER_ANSWERS_PER_REQUEST, one after another. Never rejects: it
// resolves to `{results: [{ref, ok, error?}], owner_checks}` with a result per
// answer, a request that failed outright failing each of its answers with its
// reason. `owner_checks` is the last list a request returned, else null.
export async function postOwnerAnswers(answers) {
  const results = [];
  let ownerChecks = null;
  for (let at = 0; at < answers.length; at += OWNER_ANSWERS_PER_REQUEST) {
    const chunk = answers.slice(at, at + OWNER_ANSWERS_PER_REQUEST);
    try {
      const res = await terminalJsonApi('/api/board/owner-checks/answer', { method: 'POST', body: { answers: chunk } });
      const got = (res && res.results) || [];
      chunk.forEach(function (a) {
        const r = got.find(function (y) { return y.ref === a.ref; });
        results.push(r || { ref: a.ref, ok: false, error: 'no result came back' });
      });
      if (res && res.owner_checks) ownerChecks = res.owner_checks;
    } catch (exc) {
      const why = (exc && exc.message) || String(exc);
      chunk.forEach(function (a) { results.push({ ref: a.ref, ok: false, error: why }); });
    }
  }
  return { results: results, owner_checks: ownerChecks };
}
