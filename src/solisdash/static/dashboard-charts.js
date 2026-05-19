// Lightweight Chart.js panels for the home dashboard. Each <canvas> with
// `data-dashboard-chart` declares its metric and a short range keyword.
// We translate that into a /history/range.json call, render the chart,
// and re-fetch every 60s (paused while the document is hidden).

(function () {
  const canvases = Array.from(
    document.querySelectorAll("canvas[data-dashboard-chart]")
  );
  if (!canvases.length) return;

  function isoToday() {
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

  function rangeForKeyword(keyword) {
    const today = isoToday();
    if (keyword === "today")  return { start: today, end: today };
    if (keyword === "week")   return { start: isoDaysAgo(6), end: today };
    if (keyword === "month")  return { start: isoFirstOfMonth(), end: today };
    return { start: today, end: today };
  }

  function rgba(rgb, alpha) {
    return rgb.replace("rgb", "rgba").replace(")", `, ${alpha})`);
  }

  async function fetchAndRender(canvas, chartRef) {
    const { metric } = canvas.dataset;
    const { start, end } = rangeForKeyword(canvas.dataset.range);
    const colour = canvas.dataset.color || "rgb(11, 113, 159)";
    let payload;
    try {
      const res = await fetch(
        `/history/range.json?metric=${metric}&start=${start}&end=${end}`,
        { headers: { Accept: "application/json" } }
      );
      if (!res.ok) return;
      payload = await res.json();
    } catch {
      return;
    }
    if (!payload.points || payload.points.length === 0) {
      if (chartRef.chart) {
        chartRef.chart.destroy();
        chartRef.chart = null;
      }
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.font = "0.85rem system-ui, sans-serif";
      ctx.fillStyle = "#888";
      ctx.fillText("No data yet.", 10, 20);
      return;
    }

    const isSamples =
      typeof payload.points[0].t === "number";

    if (chartRef.chart) chartRef.chart.destroy();

    if (isSamples) {
      chartRef.chart = new Chart(canvas, {
        type: "line",
        data: {
          datasets: [{
            label: payload.label,
            data: payload.points
              .filter((p) => p.v !== null)
              .map((p) => ({ x: p.t, y: p.v })),
            borderColor: colour,
            backgroundColor: rgba(colour, 0.15),
            tension: 0.2,
            pointRadius: 0,
            fill: true,
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
      chartRef.chart = new Chart(canvas, {
        type: "bar",
        data: {
          labels: payload.points.map((p) => p.t),
          datasets: [{
            label: payload.label,
            data: payload.points.map((p) => p.v),
            backgroundColor: rgba(colour, 0.7),
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

  const REFRESH_MS = 60_000;
  const refs = canvases.map((canvas) => ({ canvas, chart: null }));

  async function refreshAll() {
    await Promise.all(refs.map((r) => fetchAndRender(r.canvas, r)));
  }

  let timer = null;
  function start() {
    stop();
    timer = setInterval(refreshAll, REFRESH_MS);
  }
  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
    } else {
      refreshAll();
      start();
    }
  });

  refreshAll();
  if (!document.hidden) start();
})();
