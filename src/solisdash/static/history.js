// Solisdash history page: metric tabs across the top, a From/To date
// range below, one Chart.js chart underneath. The server's range
// endpoint picks resolution (5-min samples / daily / monthly / yearly)
// based on (metric, span); the JS just plots whatever comes back.

(function () {
  const form = document.getElementById("history-form");
  if (!form) return; // No stations yet — the page rendered the redirect to /data.

  const stationSelect = document.getElementById("station-select");
  const startInput = document.getElementById("range-start");
  const endInput = document.getElementById("range-end");
  const resolutionSelect = document.getElementById("resolution-select");
  const titleEl = document.getElementById("chart-title");
  const unitEl = document.getElementById("chart-unit");
  const resolutionEl = document.getElementById("chart-resolution");
  const statusEl = document.getElementById("chart-status");
  const canvas = document.getElementById("history-chart");
  const csvLink = document.getElementById("csv-download");
  const tabBar = document.getElementById("metric-tabs");

  const METRIC_COLOURS = {
    power: "rgb(11, 113, 159)",
    energy: "rgb(11, 159, 90)",
    total_output: "rgb(15, 130, 70)",
    battery: "rgb(159, 113, 11)",
    battery_power: "rgb(190, 140, 30)",
    battery_charge: "rgb(40, 140, 100)",
    battery_discharge: "rgb(200, 110, 50)",
    consumption: "rgb(120, 80, 160)",
    import_energy: "rgb(170, 80, 80)",
    export_energy: "rgb(60, 140, 60)",
    net: "rgb(40, 100, 180)",
    cashflow: "rgb(80, 130, 60)",
    money: "rgb(159, 60, 90)",
    alarms: "rgb(176, 35, 35)",
  };

  let metric = tabBar.dataset.initialMetric || "energy";
  let chart = null;

  // Which (metric, resolution) combos the server accepts. Mirrors
  // `HistoryService._explicit_resolution` so we can grey out impossible
  // options instead of letting the user trigger a 400.
  const SAMPLE_METRICS = new Set(["power", "battery", "alarms", "battery_power"]);
  function resolutionSupported(m, res) {
    // After the auto-thresholds were aligned, every metric supports
    // every aggregation: sample-only metrics gain monthly / yearly
    // averages via `_range_samples_aggregated`. The one combo that
    // still doesn't work is rollup-metric + samples, since
    // station_daily has no sub-day data.
    if (res === "auto") return true;
    if (res === "samples") return SAMPLE_METRICS.has(m) || m === "energy";
    return true;
  }

  function applyResolutionOptionsState() {
    if (!resolutionSelect) return;
    for (const opt of resolutionSelect.options) {
      opt.disabled = !resolutionSupported(metric, opt.value);
    }
    if (!resolutionSupported(metric, resolutionSelect.value)) {
      resolutionSelect.value = "auto";
    }
  }

  function applyTabState() {
    for (const btn of tabBar.querySelectorAll("[data-metric]")) {
      btn.classList.toggle("active", btn.dataset.metric === metric);
      btn.setAttribute("aria-selected", btn.dataset.metric === metric);
    }
  }

  function todayISO() {
    return new Date().toISOString().slice(0, 10);
  }

  function isoDaysAgo(n) {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - n);
    return d.toISOString().slice(0, 10);
  }

  function isoFirstOfMonth() {
    const d = new Date();
    return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1))
      .toISOString().slice(0, 10);
  }

  function isoFirstOfYear() {
    const d = new Date();
    return new Date(Date.UTC(d.getUTCFullYear(), 0, 1))
      .toISOString().slice(0, 10);
  }

  function applyPreset(name) {
    const today = todayISO();
    endInput.value = today;
    if (name === "today") {
      startInput.value = today;
    } else if (name === "month") {
      startInput.value = isoFirstOfMonth();
    } else if (name === "year") {
      startInput.value = isoFirstOfYear();
    } else if (name === "all") {
      startInput.value = "2000-01-01";
    }
    refresh();
  }

  function rangeUrl(ext = "json") {
    const params = new URLSearchParams({
      station_id: stationSelect.value,
      metric,
      start: startInput.value,
      end: endInput.value,
      resolution: resolutionSelect ? resolutionSelect.value : "auto",
    });
    return `/history/range.${ext}?${params}`;
  }

  function buildChart(payload) {
    if (chart) chart.destroy();
    titleEl.textContent = payload.label || "";
    unitEl.textContent = payload.unit ? `(${payload.unit})` : "";
    resolutionEl.textContent = payload.resolution ? `· ${payload.resolution}` : "";

    const colour = METRIC_COLOURS[metric] || "rgb(11, 113, 159)";
    // Sample-resolution payloads have numeric `t` (ms since epoch); the
    // aggregates have string `t` ("YYYY", "YYYY-MM", or "YYYY-MM-DD").
    // Time axis for numeric, category axis for strings. The default
    // chart shape is line for numeric / bar for category, but the
    // cumulative-output metric overrides that — it's a category axis
    // with a climbing line so the shape of the growth is visible.
    const isSamples = payload.points.length > 0 && typeof payload.points[0].t === "number";
    const forceLineOnCategory = metric === "total_output";

    if (!isSamples && forceLineOnCategory) {
      chart = new Chart(canvas, {
        type: "line",
        data: {
          labels: payload.points.map((p) => p.t),
          datasets: [{
            label: payload.label,
            data: payload.points.map((p) => p.v),
            borderColor: colour,
            backgroundColor: colour.replace("rgb", "rgba").replace(")", ", 0.15)"),
            tension: 0.2,
            pointRadius: 0,
            fill: true,
          }],
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
    } else if (isSamples) {
      const data = payload.points
        .filter((p) => p.v !== null)        // hide gaps from null battery_soc etc.
        .map((p) => ({ x: p.t, y: p.v }));
      // Hide the per-point dot only when the series is dense (>120 points
      // across the chart — roughly every 4 minutes for a 7-day window).
      // Single-sample / handful-of-samples ranges otherwise render as an
      // invisible line and look like "nothing's there".
      const pointRadius = data.length <= 120 ? 3 : 0;
      // Dense sample series (a full day at 1-min cadence is 1440 points)
      // gets drawn as a thin solid line, no area fill — the fill makes
      // a packed curve look like a single coloured block. Sparse series
      // (<= 120 points) keep a gentle fill so the shape is obvious.
      const dense = data.length > 120;
      chart = new Chart(canvas, {
        type: "line",
        data: {
          datasets: [{
            label: payload.label,
            data,
            borderColor: colour,
            backgroundColor: colour.replace("rgb", "rgba").replace(")", ", 0.12)"),
            tension: 0.15,
            borderWidth: dense ? 1.25 : 2,
            pointRadius,
            pointHoverRadius: pointRadius + 2,
            spanGaps: true,
            fill: !dense,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: { type: "time", time: { unit: "hour" } },
            y: { beginAtZero: true, title: { display: true, text: payload.unit } },
          },
          plugins: { legend: { display: false } },
        },
      });
    } else {
      const labels = payload.points.map((p) => p.t);
      const values = payload.points.map((p) => p.v);
      chart = new Chart(canvas, {
        type: "bar",
        data: {
          labels,
          datasets: [{
            label: payload.label,
            data: values,
            backgroundColor: colour.replace("rgb", "rgba").replace(")", ", 0.7)"),
          }],
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
    if (csvLink) csvLink.href = rangeUrl("csv");
    statusEl.textContent = "Loading…";
    try {
      const res = await fetch(rangeUrl("json"), {
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
        resolutionEl.textContent = "";
        statusEl.textContent = "No data for that range.";
        return;
      }
      buildChart(payload);
      // When Auto is selected, surface what the server actually picked
      // by relabelling the Auto option to e.g. "Auto (monthly totals)".
      // The chart subtitle shows it too, but having it in the dropdown
      // makes the resolution visible without scanning to the subtitle.
      if (resolutionSelect) {
        const autoOpt = resolutionSelect.options[0];
        if (autoOpt && autoOpt.value === "auto") {
          autoOpt.text =
            resolutionSelect.value === "auto" && payload.resolution
              ? `Auto (${payload.resolution})`
              : "Auto";
        }
      }
      let footnote = `${payload.points.length} points · ${payload.resolution}`;
      if (
        payload.effective_start && payload.effective_end &&
        (payload.effective_start !== startInput.value ||
         payload.effective_end !== endInput.value)
      ) {
        footnote += ` · showing ${payload.effective_start} → ${payload.effective_end}`;
      }
      statusEl.textContent = footnote;
    } catch (err) {
      statusEl.textContent = `Error: ${err}`;
    }
  }

  tabBar.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-metric]");
    if (!btn) return;
    metric = btn.dataset.metric;
    applyTabState();
    applyResolutionOptionsState();
    refresh();
  });

  for (const btn of document.querySelectorAll("[data-preset]")) {
    btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
  }

  // Standard "change" plus a debounced "input" listener so keyboard
  // typing in the date pickers (where `change` only fires on blur or
  // picker-dismiss) updates the chart as soon as a complete date is
  // entered, not when the user accidentally clicks somewhere else.
  let refreshDebounce = null;
  function refreshSoon() {
    if (refreshDebounce !== null) clearTimeout(refreshDebounce);
    refreshDebounce = setTimeout(() => {
      refreshDebounce = null;
      refresh();
    }, 350);
  }
  for (const el of [stationSelect, startInput, endInput, resolutionSelect]) {
    if (!el) continue;
    el.addEventListener("change", refresh);
    el.addEventListener("blur", refresh);
    el.addEventListener("input", refreshSoon);
  }

  // Auto-refresh the chart every 60s so new scheduler samples show up
  // without the user hitting reload. Paused when the document is hidden
  // — there's no point hammering Mongo for a background tab. Browser
  // fires `visibilitychange` when the user comes back, at which point we
  // refresh immediately and resume the interval.
  const AUTO_REFRESH_MS = 60_000;
  let autoRefreshTimer = null;
  function startAutoRefresh() {
    stopAutoRefresh();
    autoRefreshTimer = setInterval(refresh, AUTO_REFRESH_MS);
  }
  function stopAutoRefresh() {
    if (autoRefreshTimer !== null) {
      clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopAutoRefresh();
    } else {
      refresh();
      startAutoRefresh();
    }
  });

  applyTabState();
  applyResolutionOptionsState();
  refresh();
  if (!document.hidden) startAutoRefresh();
})();
