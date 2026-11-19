// Pin for the Settings sheet's login-state module (static/settings/login-state.js):
// only a status whose state is "running" keeps the login button disabled and the
// sheet polling, and a status outside the closed set is never a state.
// Prints the declared values for the Python wrapper. Invoked by
// tests/unit/core/test_login_state_ui.py.

import assert from 'node:assert/strict';

const { LoginState, loginState, isOpen } = await import('../../app/webapp/static/settings/login-state.js');

assert.ok(Object.isFrozen(LoginState));

for (const state of Object.values(LoginState)) {
  assert.equal(loginState({ state }), state, state);
  assert.equal(isOpen({ state }), state === LoginState.RUNNING, state);
}
for (const bad of [undefined, null, '', 0, {}, { state: 'Running' }, { state: 'open' }, { state: null }, { state: 7 }]) {
  assert.equal(loginState(bad), null, JSON.stringify(bad));
  assert.equal(isOpen(bad), false, JSON.stringify(bad));
}

console.log('VALUES ' + JSON.stringify(Object.values(LoginState).sort()));
console.log('OK');
