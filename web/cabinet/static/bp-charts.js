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
          x: {
            ticks: {
              maxRotation: 0,
              autoSkipPadding: 12,
              maxTicksLimit: isNarrow ? 4 : 6,
              // «дд.мм ЧЧ:ММ» — в две строки: на 30 днях подписи не наезжают друг на друга
              callback: function (value) {
                return String(this.getLabelForValue(value)).split(" ");
              }
            }
          }
        }
      }
    });
  }

  // ── Увеличение графиков (A4, R4): модалка САД+ДАД одним графиком + пульс ──
  // Общая шкала мм рт. ст. у САД/ДАД (как в PDF §10.1: пороги 140/90
  // пунктиром), пульс — второй панелью (своя шкала, на одну ось не
  // смешивается). Дни без измерений — разрывом линии (`spanGaps: false`).

  function isNextDay(prev, next) {
    // «дд.мм» соседних суток (год в подписи не участвует) — приблизительно
    // по границам месяцев (28+ → 1).
    var p = prev.split(".");
    var n = next.split(".");
    var pd = parseInt(p[0], 10);
    var pm = parseInt(p[1], 10);
    var nd = parseInt(n[0], 10);
    var nm = parseInt(n[1], 10);
    if (pm === nm) {
      return nd === pd + 1;
    }
    return pd >= 28 && nd === 1 && (nm === pm + 1 || (pm === 12 && nm === 1));
  }

  function gappedLabels(points) {
    // Метки с вставленным «разрывом» между непоследовательными днями.
    var labels = [];
    var prevDay = null;
    points.forEach(function (point) {
      var day = String(point.t).split(" ")[0];
      if (prevDay !== null && day !== prevDay && !isNextDay(prevDay, day)) {
        labels.push("");
      }
      labels.push(point.t);
      prevDay = day;
    });
    return labels;
  }

  function gappedSeries(points, labels, field) {
    // Значения по меткам с «разрывом»: в дырку — null (линия рвётся).
    var byLabel = {};
    points.forEach(function (point) {
      byLabel[point.t] = point[field];
    });
    return labels.map(function (label) {
      return label === "" ? null : byLabel[label];
    });
  }

  function renderModal(points) {
    var labels = gappedLabels(points);
    var sys = gappedSeries(points, labels, "s");
    var dia = gappedSeries(points, labels, "d");
    var pulse = gappedSeries(points, labels, "p");
    var isNarrow = window.innerWidth < 768;
    var xTicks = {
      maxRotation: 0,
      autoSkipPadding: 12,
      maxTicksLimit: isNarrow ? 5 : 10,
      callback: function (value) {
        return String(this.getLabelForValue(value)).split(" ");
      }
    };
    new Chart(document.getElementById("bp-modal-combined"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { label: "САД", data: sys, borderColor: "#1476cc", tension: 0.2, spanGaps: false },
          { label: "ДАД", data: dia, borderColor: "#0f5ba3", tension: 0.2, spanGaps: false },
          {
            label: "Порог 140",
            data: labels.map(function () { return 140; }),
            borderColor: "#d9534f",
            borderDash: [6, 4],
            pointRadius: 0,
            borderWidth: 1
          },
          {
            label: "Порог 90",
            data: labels.map(function () { return 90; }),
            borderColor: "#d9534f",
            borderDash: [6, 4],
            pointRadius: 0,
            borderWidth: 1
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, labels: { filter: function (item) { return item.text.indexOf("Порог") !== 0; } } },
          tooltip: {
            filter: function (item) { return item.dataset.label.indexOf("Порог") !== 0; },
            callbacks: {
              label: function (ctx) {
                var unit = ctx.dataset.label === "Пульс" ? "уд/мин" : "мм рт. ст.";
                return ctx.dataset.label + ": " + ctx.parsed.y + " " + unit;
              }
            }
          }
        },
        scales: {
          y: { suggestedMin: 50, suggestedMax: 180 },
          x: xTicks
        }
      }
    });
    new Chart(document.getElementById("bp-modal-pulse"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { label: "Пульс", data: pulse, borderColor: "#9b59b6", tension: 0.2, spanGaps: false }
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
                return "Пульс: " + ctx.parsed.y + " уд/мин";
              }
            }
          }
        },
        scales: {
          y: { suggestedMin: 40, suggestedMax: 130 },
          x: xTicks
        }
      }
    });
  }

  var modalRendered = false;
  var modalReturnFocus = null;

  function openModal(trigger) {
    var modal = document.getElementById("bp-chart-modal");
    if (!modal) {
      return;
    }
    var points = readJsonIsland("bp-chart-data");
    if (!modalRendered && Array.isArray(points) && points.length > 0) {
      renderModal(points);
      modalRendered = true;
    }
    modalReturnFocus = trigger || document.activeElement;
    modal.classList.remove("is-hidden");
    var close = document.getElementById("bp-modal-close");
    if (close) {
      close.focus();
    }
  }

  function closeModal() {
    var modal = document.getElementById("bp-chart-modal");
    if (!modal) {
      return;
    }
    modal.classList.add("is-hidden");
    if (modalReturnFocus && typeof modalReturnFocus.focus === "function") {
      modalReturnFocus.focus();
    }
  }

  function initModal() {
    var modal = document.getElementById("bp-chart-modal");
    if (!modal) {
      return;
    }
    var panels = document.querySelectorAll("[data-bp-zoom]");
    for (var i = 0; i < panels.length; i += 1) {
      (function (panel) {
        panel.addEventListener("click", function () {
          openModal(panel);
        });
        panel.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
            event.preventDefault();
            openModal(panel);
          }
        });
      })(panels[i]);
    }
    var close = document.getElementById("bp-modal-close");
    if (close) {
      close.addEventListener("click", closeModal);
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.classList.contains("is-hidden")) {
        closeModal();
      }
    });
  }

  function initBpCharts() {
    if (typeof Chart === "undefined") {
      return;
    }
    initModal();
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
