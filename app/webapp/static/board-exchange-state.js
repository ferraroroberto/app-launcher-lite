/* Board drawer: the last-exchange preview's display state, declared once.
 *
 * The pane starts LOADING and settles exactly once, to READY (an exchange was
 * returned), EMPTY (the session has none yet) or ERROR (anything else). The
 * state rides on `data-state` so board.css can mute the three non-ready ones.
 * deriveExchangeState() is the only code that maps the API answer to a state,
 * and setExchangeState() is the only writer of the attribute: it refuses a
 * move the table does not allow, so a late response can never reopen a pane
 * that already settled. Table and rationale: docs/board.md.
 */

export const ExchangeState = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  EMPTY: 'empty',
  ERROR: 'error',
});

const SETTLED = Object.freeze([ExchangeState.READY, ExchangeState.EMPTY, ExchangeState.ERROR]);

// Reason code GET /api/board/sessions/<sid>/exchange returns for a session with no
// exchange yet; every other unavailable reason is a failure to show.
const NO_EXCHANGE = 'no_exchange';

export function deriveExchangeState(body) {
  if (body.available) return ExchangeState.READY;
  return body.reason === NO_EXCHANGE ? ExchangeState.EMPTY : ExchangeState.ERROR;
}

export function canMoveExchange(from, to) {
  if (from === undefined || from === '') return to === ExchangeState.LOADING;
  return from === ExchangeState.LOADING && SETTLED.includes(to);
}

export function setExchangeState(el, to) {
  const from = el.dataset.state;
  if (!canMoveExchange(from, to)) {
    throw new Error('illegal exchange state move: ' + (from || 'none') + ' -> ' + to);
  }
  el.dataset.state = to;
}
