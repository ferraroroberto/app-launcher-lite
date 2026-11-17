/* The launch toolbar: the controls that shape the next agent launch. Code ›
 * Projects mounts it.
 *
 *   Detached (switch)   Resume (switch)           [trailing]
 *
 *   Detached  launch in a console window on the PC, listed and killable here
 *             but with no phone terminal ('remote' mode).
 *   Resume    reopen the agent's own session picker, orthogonal to
 *             Detached: both on renders the picker in the console.
 *
 * There is no model picker: Copilot takes no per-launch model, its model is
 * set in Settings. The two toggles are the vendored switch (accent when on,
 * design.md), each with its word beside it. The word is the switch's
 * <label>, so a tap on either toggles it. They are client-side state read at
 * launch time, never saved: a launch mode that survives a reload would
 * surprise the next tap. An optional trailing control of the caller's is
 * pinned right on the same row (Code puts its git refresh there).
 */

import { setSwitch, switchEl } from './_vendored/switch/switch.js';
import { el } from './dom-utils.js';

// One labelled switch: the visible word and the vendored track in one
// <label>, so the word is part of the tap target.
function labelledSwitch(spec) {
  const wrap = el('label', 'launch-switch');
  wrap.appendChild(el('span', 'launch-switch-label', spec.text));
  const sw = switchEl(false, {
    label: spec.label,
    onToggle: function (next, btn) { setSwitch(btn, next); },
  });
  sw.id = spec.id;
  sw.title = spec.title;
  // A tap on the switch is handled by the switch alone. Its click handler
  // rebuilds its insides, so a tap that landed on the knob (where the knob
  // sits once it is on) leaves a detached target, and WebKit's <label> then
  // reads the click as outside its control and clicks the switch again:
  // two toggles, no change. Cancelling the click stops the label's
  // re-dispatch; a tap on the word still reaches the switch through it.
  sw.addEventListener('click', function (ev) { ev.preventDefault(); });
  wrap.appendChild(sw);
  return { wrap: wrap, sw: sw };
}

// opts:
//   ids        { detached, resume } — element ids, so the surface keeps its
//              own stable hooks
//   detachedTitle / resumeTitle — each switch's hover hint
//   trailing   optional element pinned right on the switches' row
// Returns { root, isDetached(), isResume() }.
export function mountLaunchToolbar(host, opts) {
  if (!host) return null;
  const ids = opts.ids;

  const root = el('div', 'launch-toolbar');
  const switches = el('div', 'launch-toolbar-switches');
  const detached = labelledSwitch({
    id: ids.detached, text: 'Detached', label: 'Launch detached', title: opts.detachedTitle,
  });
  const resume = labelledSwitch({
    id: ids.resume, text: 'Resume', label: 'Resume session', title: opts.resumeTitle,
  });
  switches.appendChild(detached.wrap);
  switches.appendChild(resume.wrap);
  if (opts.trailing) {
    opts.trailing.classList.add('launch-toolbar-trailing');
    switches.appendChild(opts.trailing);
  }
  root.appendChild(switches);
  host.replaceChildren(root);

  function on(sw) { return sw.getAttribute('aria-checked') === 'true'; }
  return {
    root: root,
    isDetached: function () { return on(detached.sw); },
    isResume: function () { return on(resume.sw); },
  };
}
