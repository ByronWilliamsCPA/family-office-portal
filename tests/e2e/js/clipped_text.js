// SPDX-FileCopyrightText: 2026 Byron Williams
// SPDX-License-Identifier: MIT
() => {
  const bad = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.textContent.trim()) continue;
    const el = node.parentElement;
    if (!el || !el.checkVisibility() || el.closest('.sr-only, [hidden], script, style')) continue;
    let box = el;
    while (box && getComputedStyle(box).display === 'inline') box = box.parentElement;
    if (!box) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const t = range.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (t.width === 0 || b.width <= 1) continue;
    const spills = t.left < b.left - 1 || t.right > b.right + 1;
    const cut = box.scrollWidth > box.clientWidth + 1 &&
      getComputedStyle(box).overflowX !== 'visible';
    const wide = box.scrollWidth > box.clientWidth + 1 && box.clientWidth > 0 &&
      getComputedStyle(box).display !== 'table-cell' && getComputedStyle(box).display !== 'table';
    if (spills || cut || wide) {
      bad.push(node.textContent.trim().slice(0, 60) + ' [' + box.tagName.toLowerCase() + ']');
    }
  }
  return bad;
}
