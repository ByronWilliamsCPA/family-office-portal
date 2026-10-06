// SPDX-FileCopyrightText: 2026 Byron Williams
// SPDX-License-Identifier: MIT
//
// Draws the daily total as a line once the page has loaded. The same numbers
// are always in a table on the page, so nothing depends on this script.
(function () {
  "use strict";
  var holder = document.querySelector("[data-trend-chart]");
  if (!holder || typeof Chart === "undefined") {
    return;
  }
  var canvas = holder.querySelector("canvas");
  var points;
  try {
    points = JSON.parse(holder.getAttribute("data-points") || "[]");
  } catch (err) {
    return;
  }
  if (!canvas || points.length < 2) {
    return;
  }
  holder.hidden = false;
  new Chart(canvas, {
    type: "line",
    data: {
      labels: points.map(function (p) { return p.date; }),
      datasets: [{
        data: points.map(function (p) { return Number(p.total); }),
        borderColor: "#1e293b",
        backgroundColor: "#1e293b",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.2
      }]
    },
    options: {
      animation: false,
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        y: {
          ticks: {
            callback: function (v) { return "$" + Number(v).toLocaleString("en-US"); }
          }
        }
      }
    }
  });
})();
