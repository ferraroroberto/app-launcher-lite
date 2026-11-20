/* A red GitLab pipeline row in MRs and jobs: one per project whose latest
 * finished default-branch pipeline failed, and one "unknown" row per project
 * whose pipelines could not be read (never drawn as green). The server builds
 * the cards from the forge refresh (src/board/ci_runs.py); a later green
 * pipeline drops the row. A row has no dismiss: a red branch is a live problem
 * that leaves when the branch goes green.
 */

import { actionRow } from '../action-rows.js';
import { chip } from '../glance.js';
import { fmtDuration } from '../shared/format.js';
import { AttentionState } from './attention-state.js';

// A red pipeline is danger, like a failed Job; an unread project wants a look.
const CI_CHIPS = {
  failed: ['CI failed', 'danger'],
  unknown: ['CI unknown', 'attention'],
};

export function renderCiCard(card) {
  const c = CI_CHIPS[card.state] || CI_CHIPS.unknown;
  const failed = card.state === AttentionState.FAILED;
  const meta = [
    failed ? card.branch || '' : '',
    failed && card.age_seconds != null ? 'red ' + fmtDuration(card.age_seconds) : '',
    failed ? '' : 'pipelines not read',
  ].filter(Boolean).join(' · ');
  const url = failed ? card.pipeline_url : null;
  const row = actionRow({
    className: 'board-item board-item-ci is-' + card.state,
    title: card.repo || 'project',
    meta: meta,
    chips: [chip(c[0], c[1], 'board-ci-chip')],
    label: url ? 'Open the failed pipeline of ' + card.repo + ' on GitLab' : '',
    onMain: url ? function () { window.open(url, '_blank', 'noopener'); } : null,
    inert: !url,
  });
  if (url) row.main.dataset.track = 'board.ci-pipeline';
  if (card.error) row.main.title = card.error;
  return row.li;
}
