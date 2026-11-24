/* The Usage card's menu: Usage details (the card's figures written out) and
 * History (the progress sheet). A tap anywhere on the card opens it, the ⋮
 * included; the menu is the vendored row-menu, mounted on <body> while open.
 *
 * The two sheets load on demand, so the menu is the only thing the first page
 * load carries: the caller passes the History opener (the lazy view) and the
 * details sheet comes from copilot-usage.js, which main.js loads after the
 * session list has drawn.
 */

import { createRowMenu } from '../_vendored/row-menu/row-menu.js';

export function wireUsageMenu(openHistory) {
  const kebab = document.getElementById('codingUsageKebab');
  const card = document.getElementById('codingUsageCard');
  if (!kebab || !card) return;
  const menu = createRowMenu({ className: 'usage-menu' });
  menu.attach('usage', kebab, [
    {
      dataset: { item: 'usage-details-btn', track: 'coding.usage-details' },
      glyph: 'gauge', label: 'Usage details',
      onTap: function () {
        import('../copilot-usage.js').then(function (m) { m.openUsageSheet(); })
          .catch(function (exc) { console.warn('usage details did not load', exc); });
      },
    },
    {
      dataset: { item: 'usage-history-btn', track: 'coding.usage-history' },
      glyph: 'clock', label: 'History', onTap: openHistory,
    },
  ]).dataset.surface = 'code.usage-menu';
  // The kebab opens the menu itself; a tap on the rest of the card asks it to.
  card.addEventListener('click', function (ev) {
    if (!ev.target.closest('.usage-card-kebab')) kebab.click();
  });
}
