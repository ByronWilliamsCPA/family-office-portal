// SPDX-FileCopyrightText: 2026 Byron Williams
// SPDX-License-Identifier: MIT
() => {
  const problems = [];
  for (const img of document.querySelectorAll('img')) {
    if (!img.hasAttribute('alt')) problems.push('image without alt: ' + img.src);
  }
  const fields = 'input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea';
  for (const el of document.querySelectorAll(fields)) {
    const named = (el.labels && el.labels.length > 0) ||
      el.getAttribute('aria-label') || el.getAttribute('aria-labelledby');
    if (!named) problems.push('field without a label: ' + (el.id || el.name || el.tagName));
  }
  for (const el of document.querySelectorAll('button, a[href], summary')) {
    const named = el.innerText.trim() || el.getAttribute('aria-label');
    if (!named) problems.push('control without a name: ' + el.outerHTML.slice(0, 60));
  }
  return {
    problems,
    mains: document.querySelectorAll('main').length,
    navs: document.querySelectorAll('nav').length,
    banners: document.querySelectorAll('body > header').length,
    lang: document.documentElement.lang,
    title: document.title.trim(),
  };
}
