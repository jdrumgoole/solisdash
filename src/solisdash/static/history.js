// Solisdash history page: metric tabs (Power / Energy / Battery / Money /
// Alarms) on top, range selector below, one Chart.js chart underneath.
//
// API contract: /history/{day,month,year,all}.json?station_id=…&metric=…[&when|&month|&year]

(function () {
  const form = document.getElementById("history-form");
  if (!form) return; // No stations yet — nothing to wire up.

  const stationSelect = document.getElementById("station-select");
  const viewSelect = document.getElementById("view-select");
  const dateInput = document.getElementById("date-input");
  const monthInput = document.getElementById("month-input");
  const yearInput = document.getElementById("year-input");
  const titleEl = document.getElementById("chart-title");
  const unitEl = document.getElementById("chart-unit");
  const statusEl = document.getElementById("chart-status");
  const canvas = document.getElementById("history-chart");
  const csvLink = document.getElementById("csv-download");
  const tabBar = document.getElementById("metric-tabs");

  // Mirror of solisdash.history.METRIC_SUPPORTS so the UI can disable
  // unsupported view options without round-tripping to the server.
  const METRIC_SUPPORTS = {
    power: new Set(["day"]),
    energy: new Set(["day", "month", "year", "all"]),
    battery: new Set(["day"]),
    money: new Set(["month", "year", "all"]),
    alarms: new Set(["day"]),
  };
  // Chart shape per metric. Line for time-series, bar for bucketed.
  const METRIC_CHART_TYPE = {
    power: "line",
    energy: "bar", // day view falls back to line via dayUsesLine() below
    battery: "line",
    money: "bar",
    alarms: "line",
  };

  // Metric → colour, kept in sync with the Pico palette.
  const METRIC_COLOURS = {
    power: "rgb(11, 113, 159)",
    energy: "rgb(11, 159, 90)",
    battery: "rgb(159, 113, 11)",
    money: "rgb(159, 60, 90)",
    alarms: "rgb(176, 35, 35)",
  };

  let metric = "power";
  let chart = null;

  function dayUsesLine(m) {
    return ["power", "battery", "alarms", "energy"].includes(m);
  }

  function applyTabState() {
    for (const btn of tabBar.querySelectorAll("[data-metric]")) {
      btn.classList.toggle("active", btn.dataset.metric === metric);
      btn.setAttribute("aria-selected", btn.dataset.metric === metric);
    }
  }

  function applyViewOptionState() {
    const valid = METRIC_SUPPORTS[metric];
    for (const opt of viewSelect.options) {
      opt.disabled = !valid.has(opt.value);
    }
    // If the currently-selected view isn't valid for this metric, fall
    // through to the metric's preferred default (first valid option).
    if (!valid.has(viewSelect.value)) {
      const fallback = Array.from(viewSelect.options).find((o) => valid.has(o.value));
      if (fallback) viewSelect.value = fallback.value;
    }
  }

  function showField(view) {
    document.querySelectorAll("[data-view-field]").forEach((el) => {
      el.hidden = el.dataset.viewField !== view;
    });
  }

  function paramsForView(view, stationId) {
    const params = new URLSearchParams({ station_id: stationId, metric });
    if (view === "day") params.set("when", dateInput.value);
    else if (view === "month") params.set("month", monthInput.value);
    else if (view === "year") params.set("year", yearInput.value);
    return params;
  }

  function urlForView(view, stationId, ext = "json") {
    return `/history/${view}.${ext}?${paramsForView(view, stationId)}`;
  }

  function pointsToChartData(view, points) {
    if (view === "day") {
      return points.map((p) => ({ x: p.t, y: p.v }));
    }
    return { labels: points.map((p) => p.t), values: points.map((p) => p.v) };
  }

  function buildChart(view, payload) {
    if (chart) chart.destroy();
    titleEl.textContent = payload.label || "";
    unitEl.textContent = payload.unit ? `(${payload.unit})` : "";

    const colour = METRIC_COLOURS[metric] || "rgb(11, 113, 159)";
    const useLine = view === "day" ? dayUsesLine(metric) : METRIC_CHART_TYPE[metric] === "line";

    if (useLine) {
      const data = view === "day" ? pointsToChartData(view, payload.points) : null;
      const { labels, values } = view === "day" ? { labels: null, values: null } : pointsToChartData(view, payload.points);
      chart = new Chart(canvas, {
        type: "line",
        data: view === "day"
          ? {
              datasets: [
                {
                  label: payload.label,
                  data,
                  borderColor: colour,
                  backgroundColor: colour.replace("rgb", "rgba").replace(")", ", 0.15)"),
                  tension: 0.2,
                  pointRadius: 0,
                  fill: true,
                },
              ],
            }
          : {
              labels,
              datasets: [
                {
                  label: payload.label,
                  data: values,
                  borderColor: colour,
                  backgroundColor: colour.replace("rgb", "rgba").replace(")", ", 0.15)"),
                  tension: 0.2,
                  pointRadius: 0,
                  fill: true,
                },
              ],
            },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: view === "day" ? { type: "time", time: { unit: "hour" } } : {},
            y: { beginAtZero: true, title: { display: true, text: payload.unit } },
          },
          plugins: { legend: { display: false } },
        },
      });
    } else {
      const { labels, values } = pointsToChartData(view, payload.points);
      chart = new Chart(canvas, {
        type: "bar",
        data: {
          labels,
          datasets: [
            {
              label: payload.label,
              data: values,
              backgroundColor: colour.replace("rgb", "rgba").replace(")", ", 0.7)"),
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            y: { beginAtZero: true, title: { display: true, text: payload.unit } },
          },
          plugins: { legend: { display: false } },
        },
      });
    }
  }

  async function refresh() {
    const view = viewSelect.value;
    const stationId = stationSelect.value;
    showField(view);
    if (csvLink) csvLink.href = urlForView(view, stationId, "csv");
    statusEl.textContent = "Loading…";
    try {
      const res = await fetch(urlForView(view, stationId), {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        statusEl.textContent = `Error: ${res.status} ${res.statusText}`;
        return;
      }
      const payload = await res.json();
      if (!payload.points || payload.points.length === 0) {
        if (chart) chart.destroy();
        chart = null;
        canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
        titleEl.textContent = payload.label || "";
        unitEl.textContent = payload.unit ? `(${payload.unit})` : "";
        statusEl.textContent = "No data for that range.";
        return;
      }
      buildChart(view, payload);
      statusEl.textContent = `${payload.points.length} points.`;
    } catch (err) {
      statusEl.textContent = `Error: ${err}`;
    }
  }

  // Tab clicks switch the metric and trigger a refresh.
  tabBar.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-metric]");
    if (!btn) return;
    metric = btn.dataset.metric;
    applyTabState();
    applyViewOptionState();
    refresh();
  });

  for (const el of [stationSelect, viewSelect, dateInput, monthInput, yearInput]) {
    el.addEventListener("change", refresh);
  }

  // Initial render.
  applyTabState();
  applyViewOptionState();
  refresh();
})();
