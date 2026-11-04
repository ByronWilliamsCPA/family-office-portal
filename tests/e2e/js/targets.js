// SPDX-FileCopyrightText: 2026 Byron Williams
// SPDX-License-Identifier: MIT
() => {
  const out = [];
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary';
  for (const el of document.querySelectorAll(sel)) {
    if (!el.checkVisibility() || el.closest('.sr-only')) continue;
    // WCAG 2.5.8 exempts a link inside a sentence: five or more other words
    // of plain text around it in the same parent.
    const around = [...el.parentElement.childNodes]
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent.trim()).join(' ').split(/\s+/).filter(Boolean);
    if (around.length >= 5) continue;
    const r = el.getBoundingClientRect();
    const label = (el.innerText || el.getAttribute('aria-label') || el.name || '').trim().slice(0, 40);
    out.push({what: el.tagName.toLowerCase() + ' "' + label + '"',
              width: Math.round(r.width), height: Math.round(r.height)});
  }
  return out;
}
