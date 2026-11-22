/* The fleet chief's lifecycle: ensure it runs, and the one shape every chief
 * Start, Resume and Restart button takes.
 *
 * The Code tab's chief row (sessions.js) is in the first load and the Board
 * is not (lazy-views.js), so these live apart from board-dispatch.js, which
 * imports board.js. Nothing here may import the Board.
 */

import { applyLaunchSizePayload } from '../shared/launch-size.js';
import { SensitiveOutcome, runSensitiveAction } from '../shared/sensitive-action.js';
import { startWorkTimer } from '../work-timer.js';
import { terminalJsonApi } from '../webauthn.js';
import { confirmDialog } from '../confirm-dialog.js';

// Exported for the Coding tab's chief row (sessions.js): its Start,
// Resume and Restart call this ensure endpoint, as the answer sheet's send
// does through board-dispatch.js's sendToChief.
// ``resume`` reattaches the most recent chief conversation instead of
// starting fresh — mutually exclusive with ``fresh`` in practice (the two
// buttons are never both pressed), but the server decides precedence.
//
// No auto-ensure race with the Resume button's !alive-gated visibility:
// every call site — the Coding tab's chief row
// Start/Resume/Restart (sessions.js) and the answer sheet's spawn-then-type
// send — fires only from an explicit user action (a click or a Done tap),
// never a background poll or timer. So
// nothing silently spawns a fresh chief out from under a still-visible
// Resume button; the only way to "miss" Resume is to deliberately send the
// answer sheet instead, which is an ordinary Start-equivalent choice, not a
// race — the resumable conversation's state row survives untouched either
// way (pruned only after 24h, per _find_resumable_chief_session_id).
//
// `restart` is the explicit intent to stop a live chief; `resume`
// alone keeps one, so a stale page's send can never replace it server-side.
export async function ensureChief(fresh, resume, restart) {
  const payload = {};
  if (fresh) payload.fresh = true;
  if (resume) payload.resume = true;
  if (restart) payload.restart = true;
  // Same size contract as every launch.
  applyLaunchSizePayload(payload);
  return terminalJsonApi('/api/board/chief/ensure', { method: 'POST', body: payload });
}

// One shape for every chief lifecycle button — the Coding tab's chief row
// Start/Resume/Restart — disable →
// ensureChief → toast → onDone → re-enable, always in a `finally`. `label`
// drives both the work-timer text (when `useTimer`) and the error toast
// ("Chief <label.toLowerCase()> failed"); `resume` drives the success
// wording ("Chief resumed" / "No resumable conversation — started fresh" vs
// "Chief started" / "Chief already running") and whether the crown icon is
// unconditional (resume/restart) or gated on an actual spawn (start).
// `confirmMessage`, when given, gates the whole action on the themed
// confirm (its { title, message, action }) before anything else runs
// (Restart's are-you-sure). `restart` is passed through to
// ensureChief (the Resume buttons' stop-a-live-chief intent).
export async function runChiefAction({
  button, label, fresh, resume, restart = false, onDone, useTimer = false,
  confirmMessage,
}) {
  if (confirmMessage && !(await confirmDialog(confirmMessage))) return;
  button.disabled = true;
  const stopTimer = useTimer ? startWorkTimer(button, label) : null;
  try {
    const verdict = await runSensitiveAction({
      key: 'chief',
      pending: 'Chief ' + label.toLowerCase() + '…',
      icon: 'crown',
      run: function () { return ensureChief(fresh, resume, restart); },
      done: function (body) {
        const text = resume
          ? (body.resumed ? 'Chief resumed' : 'No resumable conversation — started fresh')
          : (body.spawned ? 'Chief started' : 'Chief already running');
        // A saved model the list no longer offers starts on Copilot's own pick;
        // the server says so and the toast carries it.
        const warned = body.warning ? text + '. ' + body.warning : text;
        // Resume/Restart's icon is unconditional (a chat is reachable either
        // way); Start's is gated on an actual spawn.
        return { text: warned, icon: resume || body.spawned ? 'crown' : undefined };
      },
      failPrefix: 'Chief ' + label.toLowerCase() + ' failed',
      unconfirmed: 'Chief ' + label.toLowerCase() + ' not confirmed — check the session list before trying again.',
    });
    if (verdict.outcome === SensitiveOutcome.DONE && onDone) await onDone(verdict.result);
  } finally {
    if (stopTimer) stopTimer();
    button.disabled = false;
  }
}
