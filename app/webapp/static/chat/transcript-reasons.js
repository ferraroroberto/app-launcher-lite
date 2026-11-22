// The Chat pane's line for each reason the transcript route refuses, kept
// beside session-transcript.js, which shows them.

// One line per server-side reason, so "nothing there" never reads like
// "couldn't read it" (and vice versa).
const REASON_COPY = {
  session_not_found: 'This session is no longer running',
  // Not an ending: the session list could not be read, so nothing is
  // known about the session; the live tick keeps asking, backed off.
  session_host_unreachable: 'Can’t reach the session host — retrying',
  unsupported_agent: 'Transcript not supported for this agent yet',
  no_transcript: 'No transcript found for this session',
  read_failed: 'Couldn’t read the transcript',
};

// Which link failed when `no_transcript` comes with a `missing` field, so a
// live session that should have a log says where the trail broke.
const MISSING_COPY = {
  recorded_file_gone: 'No transcript found: the recorded log file is gone',
  no_path_recorded: 'No transcript found: no log is linked to this session yet',
  no_hook_row: 'No transcript found: no log matches this session’s folder and start time',
};

export function reasonCopy(reason, missing) {
  return (reason === 'no_transcript' && MISSING_COPY[missing])
    || REASON_COPY[reason] || 'Transcript unavailable';
}
