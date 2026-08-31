/* Downstream — the one set of page helpers.

   Pure formatters only. Anything carrying page-specific UX copy (each page's
   api() error prose, the lane labels) stays on its page — that copy is
   behaviour. These are `const`, so a page must DELETE its local copies in the
   same commit that links this file, or the redeclaration throws at parse
   time and blanks the page.

   esc escapes `"` as well as &<> — attribute contexts count. eur returns an
   em-dash for null (a missing figure is an absence, not zero). */

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const eur = n => n == null ? '—'
  : (n < 0 ? '−' : '') + 'S$' + Math.abs(Math.round(n)).toLocaleString();
const at = w => w ? w.d : '—';
const hhmm = w => w ? w.t : '—';
const span = (a, b) => !b ? at(a)
  : at(a) + ' → ' + (a && a.iso.slice(0, 10) === b.iso.slice(0, 10) ? b.t : b.d);
const late_ = m => m >= 60
  ? Math.floor(m / 60) + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '')
  : m + 'm';
const DELAYS = [30, 90, 180, 360, 720];

/* Anything wired with .onclick on a div or table row is invisible to the
   keyboard until it gets a tab stop and an Enter/Space handler. Call this
   on every pickable element right where it is wired. */
const pressable = el => {
  el.tabIndex = 0;
  if (el.tagName === 'DIV') el.setAttribute('role', 'button');
  if (el.tagName === 'TR')
    el.setAttribute('aria-selected', String(el.classList.contains('sel')));
  el.onkeydown = e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click(); }
  };
};
