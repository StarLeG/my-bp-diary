/* Графики сводки: три панели с цветными зонами АД (SPEC.md, раздел 16.2).
 *
 * Общий код для личного кабинета и кабинета врача: читает JSON-острова
 * #bp-chart-data (точки [{t, s, d, p}]) и #bp-chart-zones (зоны
 * {systolic: [...], diastolic: [...]}, константы web/cabinet/chart_zones.py)
 * и рисует три графика Chart.js. Зоны — собственный плагин beforeDraw,
 * новых CDN-зависимостей нет. Панель «Пульс» — без зон.
 */
"use strict";

(function () {
  var PANELS = [
    { id: "bp-chart", key: "systolic", field: "s", label: "Систолическое", color: "#1476cc", min: 90, max: 180, unit: "мм рт. ст." },
    { id: "bp-chart-dia", key: "diastolic", field: "d", label: "Диастолическое", color: "#0f5ba3", min: 50, max: 120, unit: "мм рт. ст." },
    { id: "bp-chart-pulse", key: "pulse", field: "p", label: "Пульс", color: "#9b59b6", min: 40, max: 130, unit: "уд/мин" }
  ];

  // Плагин зон: полупрозрачные полосы до границ графика + линии порогов.
  var bpZones = {
    id: "bpZones",
    beforeDraw: function (chart, _args, pluginOptions) {
      var bands = (pluginOptions && pluginOptions.bands) || [];
      if (!bands.length) {
        return;
      }
      var area = chart.chartArea;
      var y = chart.scales.y;
      if (!area || !y) {
        return;
      }
      var ctx = chart.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
      ctx.clip();
      var prevTo = null;
      for (var i = 0; i < bands.length; i += 1) {
        var band = bands[i];
        var yTop = band.to === null ? area.top : y.getPixelForValue(band.to);
        var yBottom = prevTo === null ? area.bottom : y.getPixelForValue(prevTo);
        ctx.fillStyle = band.fill;
        ctx.fillRect(area.left, yTop, area.right - area.left, yBottom - yTop);
        if (band.to !== null && band.line) {
          ctx.strokeStyle = band.line;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(area.left, yTop);
          ctx.lineTo(area.right, yTop);
          ctx.stroke();
        }
        prevTo = band.to;
      }
      ctx.restore();
    }
  };

  function readJsonIsland(id) {
    var el = document.getElementById(id);
    if (!el) {
      return null;
    }
    try {
      return JSON.parse(el.textContent);
    } catch (_err) {
      return null;
    }
  }

  function renderPanel(panel, points, zones) {
    var canvas = document.getElementById(panel.id);
    if (!canvas) {
      return;
    }
    var isNarrow = window.innerWidth < 768;
    new Chart(canvas, {
      type: "line",
      plugins: [bpZones],
      data: {
        labels: points.map(function (point) {
          return point.t;
        }),
        datasets: [
          {
            label: panel.label,
            data: points.map(function (point) {
              return point[panel.field];
            }),
            borderColor: panel.color,
            tension: 0.2
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                return panel.label + ": " + ctx.parsed.y + " " + panel.unit;
              }
            }
          },
          bpZones: { bands: zones[panel.key] || [] }
        },
        scales: {
          y: { suggestedMin: panel.min, suggestedMax: panel.max },
          x: { ticks: { maxRotation: 0, maxTicksLimit: isNarrow ? 4 : 6 } }
        }
      }
    });
  }

  function initBpCharts() {
    if (typeof Chart === "undefined") {
      return;
    }
    var points = readJsonIsland("bp-chart-data");
    if (!Array.isArray(points) || points.length === 0) {
      return;
    }
    var zones = readJsonIsland("bp-chart-zones") || {};
    PANELS.forEach(function (panel) {
      renderPanel(panel, points, zones);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initBpCharts);
  } else {
    initBpCharts();
  }
})();
