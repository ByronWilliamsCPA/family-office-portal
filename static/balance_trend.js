// SPDX-FileCopyrightText: 2026 Byron Williams
// SPDX-License-Identifier: MIT
//
// Draws the daily total as a line. The page loads this script with "defer",
// so it runs once the page has been parsed, after the vendored Chart.js. The
// same numbers are always in a table on the page, so nothing depends on it.
(function () {
  "use strict";
  var holder = document.querySelector("[data-trend-chart]");
  if (!holder) {
    return;
  }
  if (typeof Chart === "undefined") {
    console.warn("balance trend: Chart.js did not load; showing the table only");
    return;
  }
  var canvas = holder.querySelector("canvas");
  var points;
  try {
    points = JSON.parse(holder.getAttribute("data-points") || "[]");
  } catch (err) {
    console.warn("balance trend: chart data could not be read", err);
    return;
  }
  if (!canvas || points.length < 2) {
    return;
  }

  // Whole dollars with the sign before the symbol, for example "-$5,000",
  // matching the money format of the server-rendered table.
  function dollars(value) {
    var amount = Number(value);
    var sign = amount < 0 ? "-" : "";
    return sign + "$" + Math.abs(amount).toLocaleString("en-US", {
      maximumFractionDigits: 0
    });
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
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (ctx) { return dollars(ctx.parsed.y); }
          }
        }
      },
      scales: {
        y: {
          ticks: {
            callback: function (v) { return dollars(v); }
          }
        }
      }
    }
  });
})();
