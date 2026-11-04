// SPDX-FileCopyrightText: 2026 Byron Williams
// SPDX-License-Identifier: MIT
() => [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
  .filter(h => h.checkVisibility() || h.closest('.sr-only'))
  .map(h => ({level: Number(h.tagName[1]), text: h.innerText.trim()}))
