/* What kind of session a row is (chief, job run) and the
   confirm wording for stopping one. */

// The standing fleet chief (cross-tab parity): one label="chief"
// PTY session. Same label-first, name-fallback match everywhere a session
// might be the chief — a session-host that predates the label field still
// reports the chief's launch name, so the fallback keeps crown/tint/confirm
// working across that host's last pre-label run. Single source of truth for
// board.js (card rendering), sessions.js (Coding tab rows + stop guard), and
// terminal.js (overlay title) — do not re-derive this check independently.
export function isChiefSession(s) {
  return !!s && (s.label === 'chief' || (s.kind === 'pty' && s.name === 'chief'));
}
// The themed confirm's wording (confirmDialog, never a native
// confirm()). Same words as the native dialogs they replaced.
export const CHIEF_KILL_CONFIRM = { title: 'Kill the chief session?', action: 'Kill' };

// A scheduled job's own claude session: the session-state hook flags it when
// the job runner's env is present, and the Jobs card already tracks the run.
// Strict `true`, so a card without the field is an ordinary session.
export function isJobRunSession(s) {
  return !!s && s.job_run === true;
}
// A managed session has its conversation somewhere else (the Board chat bar)
// or is a job runner's own orchestrator, so it never counts as work waiting
// on the owner. Adding a managed kind is one more clause here.
export function isManagedSession(s) {
  return isChiefSession(s) || isJobRunSession(s);
}
// True for a card the Board's lanes and every waiting count must leave out:
// any managed session, the chief included (it keeps its Coding tab row).
export function isOffBoard(item) {
  return isManagedSession(item);
}
export const CHIEF_RESTART_CONFIRM = {
  title: 'Restart the chief?',
  message: 'It will stop the current one gracefully and resume the same ' +
    'conversation (falling back to a fresh one only if nothing is resumable).',
  action: 'Restart',
};
