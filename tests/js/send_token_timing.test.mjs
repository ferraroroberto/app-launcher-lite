// Pin for a send's terminal-token step (static/webauthn.js terminalJsonApi with
// `timeToken`): a cached token goes straight to the send with source `cached`;
// a missing one runs the passkey ceremony first and reports `passkey`; a
// ceremony that fails sends nothing at all. Invoked by
// tests/unit/sessions/test_send_stages.py.

import './_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { state } = await import('../../app/webapp/static/state.js');
const { terminalJsonApi, writeTerminalToken, clearTerminalToken } =
  await import('../../app/webapp/static/webauthn.js');

const SEND = '/api/claude-code/sessions/s1/input';
let calls = [];
let credentials = null;
Object.defineProperty(globalThis.navigator, 'credentials', {
  configurable: true, get: () => credentials,
});

function reply(body, status = 200) {
  return { status, ok: status < 400, json: async () => body };
}

globalThis.fetch = async (path, opts) => {
  calls.push({ path, headers: opts.headers });
  if (path === '/api/webauthn/auth/begin') return reply({ challenge: 'AAAA', allowCredentials: [] });
  if (path === '/api/webauthn/auth/finish') return reply({ terminal_token: 'fresh', ttl_seconds: 60 });
  return reply({ reason: 'ok' });
};

const buf = new Uint8Array([1, 2]).buffer;
const goodCredentials = {
  get: async () => ({
    id: 'c', rawId: buf, type: 'public-key',
    response: { authenticatorData: buf, clientDataJSON: buf, signature: buf, userHandle: null },
  }),
};

function send() {
  return terminalJsonApi(SEND, { method: 'POST', body: { data: 'hi', submit: true }, timeToken: true });
}

state.webauthn = { configured: true };
state.status = { terminal: { reason: 'tailscale' } };

// Cached: no ceremony, the send carries the cached token and says so.
calls = [];
writeTerminalToken('cached-tok', 600);
await send();
assert.deepEqual(calls.map((c) => c.path), [SEND]);
assert.equal(calls[0].headers.get('X-Terminal-Token'), 'cached-tok');
assert.equal(calls[0].headers.get('X-Send-Token-Source'), 'cached');
assert.match(calls[0].headers.get('X-Send-Token-Ms'), /^\d+$/);

// Missing: the ceremony runs first, then the send carries the fresh token.
calls = [];
clearTerminalToken();
credentials = goodCredentials;
await send();
assert.deepEqual(
  calls.map((c) => c.path),
  ['/api/webauthn/auth/begin', '/api/webauthn/auth/finish', SEND],
);
assert.equal(calls[2].headers.get('X-Terminal-Token'), 'fresh');
assert.equal(calls[2].headers.get('X-Send-Token-Source'), 'passkey');

// Missing and the ceremony fails: nothing is sent.
calls = [];
clearTerminalToken();
credentials = { get: async () => { throw new Error('NotAllowedError'); } };
await assert.rejects(send, /NotAllowedError/);
assert.ok(!calls.some((c) => c.path === SEND), 'a send went out without a token');

// No gate (loopback): no ceremony, source `none`.
calls = [];
state.status = { terminal: { reason: 'loopback' } };
await send();
assert.deepEqual(calls.map((c) => c.path), [SEND]);
assert.equal(calls[0].headers.get('X-Send-Token-Source'), 'none');
assert.equal(calls[0].headers.get('X-Terminal-Token'), null);

// A plain terminal call does not report the token step.
calls = [];
await terminalJsonApi('/api/x');
assert.equal(calls[0].headers.get('X-Send-Token-Source'), null);

console.log('OK');
