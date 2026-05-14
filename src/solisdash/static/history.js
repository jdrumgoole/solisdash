// Solisdash history page: pulls JSON from the /history/*.json endpoints
// and renders one Chart.js line chart per view.

(function () {
  const form = document.getElementById("history-form");
  if (!form) return; // no stations yet — nothing to wire up

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

  let chart = null;

  function showField(view) {
    document.querySelectorAll("[data-view-field]").forEach((el) => {
      el.hidden = el.dataset.viewField !== view;
    });
  }

  function paramsForView(view, stationId) {
    const params = new URLSearchParams({ station_id: stationId });
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

    if (view === "day") {
      const data = pointsToChartData(view, payload.points);
      chart = new Chart(canvas, {
        type: "line",
        data: {
          datasets: [
            {
              label: payload.label,
              data,
              borderColor: "rgb(11, 113, 159)",
              backgroundColor: "rgba(11, 113, 159, 0.15)",
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
            x: { type: "time", time: { unit: "hour" } },
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
              backgroundColor: "rgba(11, 113, 159, 0.7)",
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

  for (const el of [stationSelect, viewSelect, dateInput, monthInput, yearInput]) {
    el.addEventListener("change", refresh);
  }

  // Chart.js + date-fns adapter both load via deferred <script> tags in
  // history.html, so they're ready by the time this script executes.
  refresh();
})();
