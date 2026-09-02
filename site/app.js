/* home-broadband dashboard
 * Loads the JSON the Raspberry Pi publishes and renders it with uPlot.
 * No build step, no external requests — everything here is served from the
 * same GitHub Pages branch. */
(() => {
  "use strict";

  const DATA = "data";
  const SERIES_SLOTS = 8;
  // Engine ids are stored verbatim by the collector; these are display names.
  const ENGINE_LABELS = {
    ookla: "Ookla",
    "speedtest-cli": "speedtest-cli",
    cloudflare: "Cloudflare",
  };
  const engineLabel = (id) => ENGINE_LABELS[id] || id || "—";
  const RANGES = {
    "24h": { hours: 24, source: "latest" },
    "48h": { hours: 48, source: "latest" },
    "7d": { hours: 24 * 7, source: "daily" },
    "30d": { hours: 24 * 30, source: "daily" },
    all: { hours: null, source: "monthly" },
  };

  const state = {
    manifest: null,
    summary: null,
    range: "24h",
    dataset: null,
    charts: [],
    hidden: {},
    // name -> colour slot, for targets no longer in config.yaml
    retired: new Map(),
    tz: "UTC",
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // ---------------------------------------------------------------- utils
  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const seriesColour = (index) => cssVar(`--series-${(index % SERIES_SLOTS) + 1}`);

  async function fetchJSON(path) {
    const res = await fetch(`${path}?v=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }

  const fmtNum = (value, digits = 1) =>
    value === null || value === undefined || Number.isNaN(value)
      ? "—"
      : Number(value).toLocaleString(undefined, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    const hours = Math.floor(seconds / 3600);
    return `${hours}h ${Math.round((seconds % 3600) / 60)}m`;
  }

  function tsFormatter(opts) {
    return new Intl.DateTimeFormat("en-GB", { timeZone: state.tz, ...opts });
  }

  const fmtDateTime = (ts) =>
    ts
      ? tsFormatter({
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        }).format(new Date(ts * 1000))
      : "—";

  const relative = (ts) => {
    const mins = Math.round((Date.now() / 1000 - ts) / 60);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.round(mins / 60);
    return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
  };

  // ------------------------------------------------------- data assembly
  function daysInRange(hours) {
    const days = state.manifest.days || [];
    if (hours === null) return days;
    const cutoff = new Date(Date.now() - hours * 3600 * 1000);
    return days.filter((day) => new Date(`${day}T23:59:59Z`) >= cutoff);
  }

  function mergeSeries(target, incoming) {
    for (const [name, series] of Object.entries(incoming || {})) {
      const dest = (target[name] = target[name] || {});
      for (const [key, values] of Object.entries(series)) {
        dest[key] = (dest[key] || []).concat(values);
      }
    }
  }

  function mergeColumns(target, incoming) {
    for (const [key, values] of Object.entries(incoming || {})) {
      target[key] = (target[key] || []).concat(values);
    }
  }

  async function loadDataset(rangeKey) {
    const range = RANGES[rangeKey];
    // outages: null means "derive them from the raw samples"; an array means the
    // publisher already computed them at full resolution (bucketed files cannot
    // show a 90-second drop, so trusting the file beats re-deriving it here).
    const dataset = { latency: {}, speed: {}, http: {}, resolution: 60, outages: null };

    if (range.source === "latest") {
      const latest = await fetchJSON(`${DATA}/latest.json`);
      dataset.resolution = latest.resolution || 60;
      const cutoff = Math.floor(Date.now() / 1000) - range.hours * 3600;
      for (const [name, series] of Object.entries(latest.latency || {})) {
        const keep = series.t.map((t, i) => (t >= cutoff ? i : -1)).filter((i) => i >= 0);
        dataset.latency[name] = {
          t: keep.map((i) => series.t[i]),
          rtt: keep.map((i) => series.rtt[i]),
          loss: keep.map((i) => series.loss[i]),
        };
      }
      const speed = latest.speed || {};
      const keepSpeed = (speed.t || [])
        .map((t, i) => (t >= cutoff ? i : -1))
        .filter((i) => i >= 0);
      for (const key of Object.keys(speed)) {
        dataset.speed[key] = keepSpeed.map((i) => speed[key][i]);
      }
      for (const [name, series] of Object.entries(latest.http || {})) {
        const keep = series.t.map((t, i) => (t >= cutoff ? i : -1)).filter((i) => i >= 0);
        dataset.http[name] = { t: keep.map((i) => series.t[i]), ttfb: keep.map((i) => series.ttfb[i]) };
      }
      return dataset;
    }

    const files =
      range.source === "daily"
        ? daysInRange(range.hours).map((day) => `${DATA}/daily/${day}.json`)
        : (state.manifest.months || []).map((month) => `${DATA}/monthly/${month}.json`);

    const payloads = await Promise.all(
      files.map((file) => fetchJSON(file).catch(() => null))
    );
    const cutoff =
      range.hours === null ? 0 : Math.floor(Date.now() / 1000) - range.hours * 3600;

    const outages = new Map();
    for (const payload of payloads) {
      if (!payload) continue;
      dataset.resolution = payload.resolution || dataset.resolution;
      mergeSeries(dataset.latency, payload.latency);
      mergeColumns(dataset.speed, payload.speed);
      mergeSeries(dataset.http, payload.http);
      for (const outage of (payload.summary && payload.summary.outages) || []) {
        outages.set(outage.start, outage); // day and month files overlap
      }
    }
    dataset.outages = [...outages.values()].sort((a, b) => a.start - b.start);

    // trim to the requested window (day files include whole days)
    for (const series of Object.values(dataset.latency)) {
      const keep = series.t.map((t, i) => (t >= cutoff ? i : -1)).filter((i) => i >= 0);
      for (const key of Object.keys(series)) series[key] = keep.map((i) => series[key][i]);
    }
    if (dataset.speed.t) {
      const keep = dataset.speed.t.map((t, i) => (t >= cutoff ? i : -1)).filter((i) => i >= 0);
      for (const key of Object.keys(dataset.speed)) {
        dataset.speed[key] = keep.map((i) => dataset.speed[key][i]);
      }
    }
    for (const series of Object.values(dataset.http)) {
      const keep = series.t.map((t, i) => (t >= cutoff ? i : -1)).filter((i) => i >= 0);
      for (const key of Object.keys(series)) series[key] = keep.map((i) => series[key][i]);
    }
    return dataset;
  }

  /** Snap timestamps onto a common grid so multi-target series share an x axis. */
  function align(seriesMap, valueKey, resolution) {
    const names = Object.keys(seriesMap);
    const grid = new Set();
    const lookup = new Map();

    for (const name of names) {
      const series = seriesMap[name];
      const perName = new Map();
      (series.t || []).forEach((t, i) => {
        const snapped = Math.round(t / resolution) * resolution;
        grid.add(snapped);
        perName.set(snapped, series[valueKey][i]);
      });
      lookup.set(name, perName);
    }

    const xs = [...grid].sort((a, b) => a - b);
    const columns = names.map((name) => {
      const perName = lookup.get(name);
      return xs.map((x) => {
        const value = perName.get(x);
        return value === undefined ? null : value;
      });
    });
    return { names, xs, columns };
  }

  // ------------------------------------------------------------- charting
  function tooltipPlugin(formatValue, unit) {
    const node = $("#tooltip");
    return {
      hooks: {
        setCursor: (u) => {
          const { idx, left, top } = u.cursor;
          if (idx === null || idx === undefined || left < 0) {
            node.hidden = true;
            return;
          }
          const rows = [];
          for (let i = 1; i < u.series.length; i++) {
            const series = u.series[i];
            if (!series.show || series._ref) continue;
            const value = u.data[i][idx];
            if (value === null || value === undefined) continue;
            rows.push(
              `<div class="tt-row"><span class="swatch" style="background:${series.stroke()}"></span>` +
                `<span>${series.label}</span><span class="tt-val">${formatValue(value)}${unit}</span></div>`
            );
          }
          if (!rows.length) {
            node.hidden = true;
            return;
          }
          node.innerHTML =
            `<div class="tt-time">${fmtDateTime(u.data[0][idx])}</div>` + rows.join("");
          node.hidden = false;
          const rect = u.over.getBoundingClientRect();
          const box = node.getBoundingClientRect();
          let x = rect.left + left + 14;
          if (x + box.width > window.innerWidth - 8) x = rect.left + left - box.width - 14;
          let y = rect.top + top - box.height / 2;
          y = Math.max(8, Math.min(y, window.innerHeight - box.height - 8));
          node.style.left = `${x}px`;
          node.style.top = `${y}px`;
        },
      },
    };
  }

  function axisDefaults() {
    return {
      stroke: cssVar("--ink-muted"),
      grid: { stroke: cssVar("--grid"), width: 1 },
      ticks: { stroke: cssVar("--axis"), width: 1, size: 4 },
      font: `11px ${cssVar("--font") || "system-ui"}`,
    };
  }

  /** UK-style time axis: clock time within a day, date at midnight and on wide spans. */
  function timeAxisValues(u, splits) {
    const span = (u.scales.x.max || 0) - (u.scales.x.min || 0);
    const dateFmt = tsFormatter({ day: "2-digit", month: "short" });
    const clockFmt = tsFormatter({ hour: "2-digit", minute: "2-digit", hour12: false });
    return splits.map((ts, index) => {
      const when = new Date(ts * 1000);
      if (span > 6 * 86400) return dateFmt.format(when);
      const clock = clockFmt.format(when);
      return clock === "00:00" || index === 0 ? dateFmt.format(when) : clock;
    });
  }

  /** Colour follows the target, not its position in this particular chart. */
  function targetColour(name) {
    const index = (state.manifest.targets || []).findIndex((t) => t.name === name);
    if (index >= 0) return seriesColour(index);

    // A target removed from config.yaml keeps its history in the published
    // data until it ages out of the window, so it still has a series here.
    // Give it a slot after the configured targets — returning slot 0 would
    // paint it the same colour as the first configured target.
    if (!state.retired.has(name)) {
      state.retired.set(name, (state.manifest.targets || []).length + state.retired.size);
    }
    return seriesColour(state.retired.get(name));
  }

  /** Order series the way the config lists them, so charts agree with each other. */
  function orderByManifest(names) {
    const order = (state.manifest.targets || []).map((t) => t.name);
    const rank = (name) => {
      const exact = order.indexOf(name);
      if (exact >= 0) return exact;
      const prefix = order.findIndex((t) => name.startsWith(t));
      return prefix >= 0 ? prefix : order.length;
    };
    return names
      .map((name, index) => ({ name, index }))
      .sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  }

  function makeChart(containerSel, legendSel, spec) {
    const container = $(containerSel);
    container.innerHTML = "";
    const legendBox = legendSel ? $(legendSel) : null;
    if (legendBox) legendBox.innerHTML = "";

    if (!spec.xs.length) {
      container.appendChild(el("p", "muted empty", "No data in this window yet."));
      return null;
    }

    // With more configured targets than palette slots, two entities on the SAME
    // chart can land on the same hue (slot = index % 8). Identity must never be
    // ambiguous within one chart, so move any duplicate to a free slot. Charts
    // never carry more than eight series here, so a free slot always exists.
    const paletteSeen = new Set();
    const resolved = spec.series.map((entry, index) => {
      let colour = entry.colour || seriesColour(index);
      if (!entry.reference && paletteSeen.has(colour)) {
        for (let slot = 0; slot < SERIES_SLOTS; slot++) {
          const candidate = seriesColour(slot);
          if (!paletteSeen.has(candidate)) {
            colour = candidate;
            break;
          }
        }
      }
      if (!entry.reference) paletteSeen.add(colour);
      return { ...entry, colour };
    });

    const series = [{}].concat(
      resolved.map((entry, index) => ({
        label: entry.label,
        stroke: entry.colour || seriesColour(index),
        width: entry.width ?? 2,
        dash: entry.dash,
        _ref: entry.reference || false,
        spanGaps: false,
        show: state.hidden[`${spec.key}:${entry.label}`] !== true,
        points: {
          show: entry.points ?? spec.xs.length < 400,
          size: 6,
          stroke: entry.colour || seriesColour(index),
          fill: cssVar("--surface-1"),
          width: 2,
        },
        value: (_u, v) => (v == null ? "—" : fmtNum(v, spec.digits ?? 1)),
      }))
    );

    const opts = {
      width: container.clientWidth || 900,
      height: spec.height || 280,
      tzDate: (ts) => uPlot.tzDate(new Date(ts * 1000), state.tz),
      cursor: {
        y: false,
        points: { size: 7 },
        drag: { setScale: false },
      },
      legend: { show: false },
      scales: { x: { time: true }, y: { range: spec.range } },
      axes: [
        { ...axisDefaults(), space: 80, values: timeAxisValues },
        {
          ...axisDefaults(),
          label: spec.yLabel,
          labelSize: 30,
          labelFont: `500 11px ${cssVar("--font") || "system-ui"}`,
          size: 52,
          values: (_u, splits) => splits.map((v) => fmtNum(v, spec.axisDigits ?? 0)),
        },
      ],
      series,
      plugins: [tooltipPlugin((v) => fmtNum(v, spec.digits ?? 1), spec.unit || "")],
    };

    const chart = new uPlot(opts, [spec.xs].concat(spec.columns), container);

    if (legendBox) {
      resolved.forEach((entry, index) => {
        const button = el("button", "legend-item");
        button.type = "button";
        const key = `${spec.key}:${entry.label}`;
        button.setAttribute("aria-pressed", String(state.hidden[key] !== true));
        const swatch = el("span", "swatch");
        swatch.style.background = entry.colour || seriesColour(index);
        if (entry.dash) swatch.style.opacity = "0.6";
        button.append(swatch, el("span", null, entry.label));
        button.addEventListener("click", () => {
          const nowHidden = !(state.hidden[key] === true);
          state.hidden[key] = nowHidden;
          button.setAttribute("aria-pressed", String(!nowHidden));
          chart.setSeries(index + 1, { show: !nowHidden });
        });
        legendBox.appendChild(button);
      });
    }

    state.charts.push({ chart, container });
    return chart;
  }

  function resizeCharts() {
    for (const { chart, container } of state.charts) {
      chart.setSize({ width: container.clientWidth, height: chart.height });
    }
  }

  // ------------------------------------------------------------- rendering
  function tile(label, value, unit, sub, status) {
    const node = el("div", "tile");
    node.appendChild(el("div", "tile-label", label));
    const valueNode = el("div", "tile-value");
    valueNode.append(document.createTextNode(value));
    if (unit) valueNode.appendChild(el("span", "unit", unit));
    node.appendChild(valueNode);
    if (sub) node.appendChild(el("div", "tile-sub", sub));
    if (status) {
      const wrap = el("div", `tile-status s-${status.level}`);
      wrap.append(el("span", "dot"), el("span", null, status.text));
      node.appendChild(wrap);
    }
    return node;
  }

  function renderTiles() {
    const box = $("#tiles");
    box.innerHTML = "";
    const current = state.summary.current;
    const day = state.summary.windows["24h"];
    const isp = state.manifest.site.isp || {};

    if (current) {
      const guaranteed = Number(isp.guaranteed_min_down_mbps) || 0;
      // A result at the collector's own NIC ceiling says nothing about the line.
      const linkSpeed = Number((state.manifest.host || {}).link_speed_mbps) || 0;
      const nicBound =
        linkSpeed > 0 && Math.max(current.down_mbps || 0, current.up_mbps || 0) >= linkSpeed * 0.85;
      const level =
        guaranteed && current.down_mbps < guaranteed
          ? "critical"
          : guaranteed && current.down_mbps < guaranteed * 1.2
          ? "warning"
          : "good";
      box.appendChild(
        tile(
          "Download",
          fmtNum(current.down_mbps, 1),
          "Mbps",
          `${engineLabel(current.engine)} · ${relative(current.ts)}`,
          nicBound
            ? {
                level: "warning",
                text: `capped by this machine's ${linkSpeed} Mbit link`,
              }
            : guaranteed
            ? {
                level,
                text:
                  level === "critical"
                    ? `below ${guaranteed} Mbps minimum`
                    : level === "warning"
                    ? "close to minimum"
                    : "above guaranteed minimum",
              }
            : null
        )
      );
      box.appendChild(
        tile("Upload", fmtNum(current.up_mbps, 1), "Mbps", `${engineLabel(current.engine)} · ${relative(current.ts)}`)
      );
      box.appendChild(
        tile(
          "Idle latency",
          fmtNum(current.ping_ms, 1),
          "ms",
          `jitter ${fmtNum(current.jitter_ms, 1)} ms`
        )
      );
    }

    if (day && day.latency) {
      box.appendChild(
        tile(
          "Availability (24h)",
          fmtNum(day.availability_pct, 3),
          "%",
          `${day.outage_count} outage${day.outage_count === 1 ? "" : "s"}, ${fmtDuration(
            day.outage_seconds
          )} total`,
          {
            level:
              day.availability_pct >= 99.99
                ? "good"
                : day.availability_pct >= 99.5
                ? "warning"
                : "critical",
            text: day.availability_pct >= 99.99 ? "stable" : "interruptions seen",
          }
        )
      );
      box.appendChild(
        tile(
          "Latency p95 (24h)",
          fmtNum(day.latency.p95_ms, 1),
          "ms",
          `${day.latency.target} · ${day.latency.samples} probes`
        )
      );
    }
    if (day) {
      box.appendChild(
        tile("Test data used (24h)", fmtNum(day.data_used_gb, 2), "GB", `${day.tests} tests run`)
      );
    }
  }

  function renderSLA() {
    const isp = state.manifest.site.isp || {};
    const advertised = Number(isp.advertised_down_mbps) || 0;
    const guaranteed = Number(isp.guaranteed_min_down_mbps) || 0;
    if (!advertised && !guaranteed) return;

    const window30 = state.summary.windows["30d"];
    if (!window30 || !window30.tests) return;

    // If the collector's own NIC cannot carry the advertised speed, every
    // throughput figure here is a floor rather than a measurement of the line.
    // Reporting "62% of headline speed" without saying so is worse than useless:
    // it looks like an ISP problem and it is not.
    const linkSpeed = Number((state.manifest.host || {}).link_speed_mbps) || 0;
    const usableCeiling = linkSpeed ? linkSpeed * 0.94 : 0;
    const nicBound = usableCeiling > 0 && advertised > usableCeiling;

    const body = $("#sla-body");
    body.innerHTML = "";

    const banner = $("#sla-banner");
    if (nicBound) {
      banner.textContent =
        `This collector is on a ${linkSpeed} Mbit link, which caps measurements at ` +
        `about ${Math.round(usableCeiling)} Mbps — below your ${advertised} Mbps package. ` +
        `Throughput below is a floor, not a measurement of your line. Latency, jitter, ` +
        `packet loss and outages are unaffected.`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }

    const item = (label, valueHtml, sub, pct, colour) => {
      const node = el("div", "sla-item");
      node.appendChild(el("div", "tile-label", label));
      const strong = el("strong");
      strong.innerHTML = valueHtml;
      node.appendChild(strong);
      if (sub) node.appendChild(el("div", "tile-sub", sub));
      if (pct !== undefined) {
        const bar = el("div", "bar");
        const fill = el("span");
        fill.style.width = `${Math.max(2, Math.min(100, pct))}%`;
        if (colour) fill.style.background = colour;
        bar.appendChild(fill);
        node.appendChild(bar);
      }
      return node;
    };

    if (advertised) {
      // When the NIC is the binding constraint, compare against what this
      // machine can actually carry — comparing to the package is meaningless.
      const reference = nicBound ? usableCeiling : advertised;
      const pct = (window30.down.avg / reference) * 100;
      body.appendChild(
        item(
          nicBound ? "30-day average vs link ceiling" : "30-day average vs advertised",
          `${fmtNum(window30.down.avg, 1)} <span class="unit">of ${
            nicBound ? `~${Math.round(usableCeiling)} Mbps usable` : `${advertised} Mbps`
          }</span>`,
          nicBound
            ? `${fmtNum(pct, 0)}% of this machine's ${linkSpeed} Mbit ceiling`
            : `${fmtNum(pct, 0)}% of the headline speed`,
          pct,
          pct >= 90 ? cssVar("--good") : pct >= 70 ? cssVar("--warning") : cssVar("--critical")
        )
      );
    }
    if (guaranteed) {
      const below = window30.below_guaranteed_pct ?? 0;
      body.appendChild(
        item(
          "Tests below guaranteed minimum",
          `${fmtNum(below, 1)}<span class="unit">%</span>`,
          `${guaranteed} Mbps minimum · ${window30.tests} tests in 30 days`,
          below,
          below === 0 ? cssVar("--good") : below < 5 ? cssVar("--warning") : cssVar("--critical")
        )
      );
      body.appendChild(
        item(
          "Slowest 10% of tests",
          `${fmtNum(window30.down.p10, 1)} <span class="unit">Mbps</span>`,
          `worst single result ${fmtNum(window30.down.min, 1)} Mbps`
        )
      );
    }
    body.appendChild(
      item(
        "30-day availability",
        `${fmtNum(window30.availability_pct, 3)}<span class="unit">%</span>`,
        `${window30.outage_count} outages, ${fmtDuration(window30.outage_seconds)} total downtime`
      )
    );

    const heading = $("#sla-panel").querySelector("h2");
    heading.textContent = isp.name
      ? `Against what you pay for — ${isp.name}${isp.package ? ` (${isp.package})` : ""}`
      : "Against what you pay for";
    $("#sla-panel").hidden = false;
  }

  function renderSpeedChart() {
    const speed = state.dataset.speed;
    const xs = speed.t || [];
    const isp = state.manifest.site.isp || {};
    const guaranteed = Number(isp.guaranteed_min_down_mbps) || 0;

    const series = [
      { label: "Download", colour: cssVar("--series-1") },
      { label: "Upload", colour: cssVar("--series-2") },
    ];
    const columns = [speed.down || [], speed.up || []];

    if (guaranteed) {
      series.push({
        label: `Guaranteed minimum (${guaranteed} Mbps)`,
        colour: cssVar("--ink-muted"),
        width: 1.5,
        dash: [6, 4],
        points: false,
        reference: true,
      });
      columns.push(xs.map(() => guaranteed));
    }

    makeChart("#chart-speed", "#legend-speed", {
      key: "speed",
      xs,
      series,
      columns,
      yLabel: "Mbps",
      unit: " Mbps",
      digits: 1,
      range: (_u, _min, max) => [0, max * 1.1],
    });
  }

  function percentile(values, pct) {
    const clean = values.filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
    if (!clean.length) return null;
    return clean[Math.min(clean.length - 1, Math.ceil((pct / 100) * clean.length) - 1)];
  }

  function renderLatencyChart() {
    const resolution = state.dataset.resolution || 60;
    const { names, xs, columns } = align(state.dataset.latency, "rtt", resolution);
    const sorted = orderByManifest(names);

    // A handful of 200 ms spikes would otherwise squash the everyday 10 ms band
    // into a flat line, so clip the axis and say so rather than hiding the shape.
    const flat = columns.flat();
    const p995 = percentile(flat, 99.5) || 10;
    const peak = Math.max(...flat.filter((v) => v !== null), 10);
    const clipped = peak > p995 * 1.6;
    const top = clipped ? p995 * 1.3 : peak * 1.15;

    $("#latency-note").textContent = clipped
      ? `Axis clipped at ${fmtNum(top, 0)} ms — ${fmtNum(peak, 0)} ms peak. Hover for exact values.`
      : "";

    makeChart("#chart-latency", "#legend-latency", {
      key: "latency",
      xs,
      series: sorted.map(({ name }) => ({ label: name, colour: targetColour(name) })),
      columns: sorted.map(({ index }) => columns[index]),
      yLabel: "ms",
      unit: " ms",
      digits: 1,
      height: 300,
      range: [0, Math.max(10, top)],
    });
  }

  function renderLossChart() {
    const resolution = state.dataset.resolution || 60;
    const { names, xs, columns } = align(state.dataset.latency, "loss", resolution);
    const sorted = orderByManifest(names);
    makeChart("#chart-loss", "#legend-loss", {
      key: "loss",
      xs,
      series: sorted.map(({ name }) => ({ label: name, colour: targetColour(name) })),
      columns: sorted.map(({ index }) => columns[index]),
      yLabel: "% lost",
      unit: "%",
      digits: 1,
      height: 200,
      range: [0, 100],
    });
  }

  function renderTtfbChart() {
    const names = Object.keys(state.dataset.http || {});
    const panel = $("#ttfb-panel");
    if (!names.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const resolution = state.dataset.resolution || 60;
    const { names: ordered, xs, columns } = align(state.dataset.http, "ttfb", resolution);
    const sorted = orderByManifest(ordered);
    makeChart("#chart-ttfb", "#legend-ttfb", {
      key: "ttfb",
      xs,
      series: sorted.map(({ name }) => ({ label: name, colour: targetColour(name) })),
      columns: sorted.map(({ index }) => columns[index]),
      yLabel: "ms",
      unit: " ms",
      digits: 0,
      height: 220,
      range: (_u, _min, max) => [0, Math.max(100, max * 1.15)],
    });
  }

  /** Every non-LAN target unreachable at the same timestamp = a WAN outage. */
  function computeOutages() {
    if (state.dataset.outages) {
      const cutoff = RANGES[state.range].hours
        ? Math.floor(Date.now() / 1000) - RANGES[state.range].hours * 3600
        : 0;
      return state.dataset.outages.filter((o) => o.end >= cutoff);
    }

    const wan = (state.manifest.targets || [])
      .filter((t) => t.group !== "lan" && (t.checks || []).includes("icmp"))
      .map((t) => t.name);
    if (!wan.length) return [];

    const resolution = state.dataset.resolution || 60;
    const byTime = new Map();
    for (const name of wan) {
      const series = state.dataset.latency[name];
      if (!series) continue;
      series.t.forEach((t, i) => {
        const snapped = Math.round(t / resolution) * resolution;
        const entry = byTime.get(snapped) || { total: 0, dead: 0 };
        entry.total += 1;
        if (series.loss[i] >= 100) entry.dead += 1;
        byTime.set(snapped, entry);
      });
    }

    const times = [...byTime.keys()].sort((a, b) => a - b);
    const outages = [];
    let current = null;
    for (const t of times) {
      const entry = byTime.get(t);
      const down = entry.total > 0 && entry.dead === entry.total;
      if (down) {
        if (current && t - current.end <= resolution * 2) current.end = t;
        else {
          if (current) outages.push(current);
          current = { start: t, end: t };
        }
      } else if (current) {
        outages.push(current);
        current = null;
      }
    }
    if (current) outages.push(current);
    return outages.map((o) => ({ ...o, seconds: o.end - o.start + resolution }));
  }

  function renderOutages() {
    const outages = computeOutages().reverse();
    const body = $("#outages-table").querySelector("tbody");
    body.innerHTML = "";
    $("#outages-empty").hidden = outages.length > 0;
    $("#outages-table").hidden = outages.length === 0;
    for (const outage of outages.slice(0, 50)) {
      const row = el("tr");
      row.append(
        el("td", null, fmtDateTime(outage.start)),
        el("td", null, fmtDateTime(outage.end)),
        el("td", null, fmtDuration(outage.seconds))
      );
      body.appendChild(row);
    }
  }

  function renderTestsTable() {
    const speed = state.dataset.speed;
    const body = $("#tests-table").querySelector("tbody");
    body.innerHTML = "";
    const count = (speed.t || []).length;
    for (let i = count - 1; i >= Math.max(0, count - 20); i--) {
      const row = el("tr");
      const result = el("td");
      if (speed.url && speed.url[i]) {
        const link = el("a", null, "view");
        link.href = speed.url[i];
        link.rel = "noopener";
        link.target = "_blank";
        result.appendChild(link);
      } else if (speed.error && speed.error[i]) {
        result.textContent = "failed";
        result.title = speed.error[i];
      } else {
        result.textContent = "—";
      }
      row.append(
        el("td", null, fmtDateTime(speed.t[i])),
        el("td", null, engineLabel(speed.engine && speed.engine[i])),
        el("td", "num", fmtNum(speed.down ? speed.down[i] : null, 1)),
        el("td", "num", fmtNum(speed.up ? speed.up[i] : null, 1)),
        el("td", "num", fmtNum(speed.ping ? speed.ping[i] : null, 1)),
        el("td", "num", fmtNum(speed.jitter ? speed.jitter[i] : null, 1)),
        el("td", null, (speed.server && speed.server[i]) || "—"),
        result
      );
      body.appendChild(row);
    }
  }

  function renderTargets() {
    const body = $("#targets-table").querySelector("tbody");
    body.innerHTML = "";
    for (const target of state.manifest.targets || []) {
      const row = el("tr");
      row.append(
        el("td", null, target.name + (target.primary ? " ★" : "")),
        el("td", null, target.host),
        el("td", null, target.group || "internet"),
        el("td", null, (target.checks || []).join(", "))
      );
      body.appendChild(row);
    }
  }

  function renderAll() {
    for (const { chart } of state.charts) chart.destroy();
    state.charts = [];
    renderSpeedChart();
    renderLatencyChart();
    renderLossChart();
    renderTtfbChart();
    renderOutages();
    renderTestsTable();
  }

  async function setRange(rangeKey) {
    state.range = rangeKey;
    for (const button of document.querySelectorAll("#range-buttons button")) {
      button.classList.toggle("active", button.dataset.range === rangeKey);
    }
    $("#range-note").textContent = "Loading…";
    try {
      state.dataset = await loadDataset(rangeKey);
    } catch (error) {
      $("#range-note").textContent = `Could not load data: ${error.message}`;
      return;
    }
    const resolution = state.dataset.resolution;
    $("#range-note").textContent =
      resolution >= 3600
        ? `Hourly averages · ${(state.dataset.speed.t || []).length} throughput tests`
        : `${Math.round(resolution / 60)}-minute resolution · ${
            (state.dataset.speed.t || []).length
          } throughput tests`;
    renderAll();
  }

  // ----------------------------------------------------------------- theme
  function applyTheme(theme) {
    if (theme) document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
    try {
      if (theme) localStorage.setItem("bb-theme", theme);
      else localStorage.removeItem("bb-theme");
    } catch (_) {
      /* storage unavailable — theme is per-session only */
    }
  }

  function initTheme() {
    let stored = null;
    try {
      stored = localStorage.getItem("bb-theme");
    } catch (_) {
      stored = null;
    }
    if (stored) applyTheme(stored);
    $("#theme-toggle").addEventListener("click", () => {
      const isDark =
        document.documentElement.getAttribute("data-theme") === "dark" ||
        (!document.documentElement.getAttribute("data-theme") &&
          window.matchMedia("(prefers-color-scheme: dark)").matches);
      applyTheme(isDark ? "light" : "dark");
      if (state.dataset) renderAll();
    });
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    initTheme();
    try {
      const [manifest, summary] = await Promise.all([
        fetchJSON(`${DATA}/manifest.json`),
        fetchJSON(`${DATA}/summary.json`),
      ]);
      state.manifest = manifest;
      state.summary = summary;
    } catch (error) {
      $("#generated").textContent = "No data published yet.";
      $("#tiles").appendChild(
        el(
          "p",
          "muted",
          `Waiting for the collector's first publish (${error.message}).`
        )
      );
      return;
    }

    state.tz = state.manifest.site.timezone || "UTC";
    document.title = state.manifest.site.title || document.title;
    $("#site-title").textContent = state.manifest.site.title || "Home Broadband Monitor";
    $("#site-subtitle").textContent = state.manifest.site.subtitle || "";
    $("#generated").textContent = `Updated ${fmtDateTime(state.manifest.generated_at)} · ${relative(
      state.manifest.generated_at
    )}`;

    renderTiles();
    renderSLA();
    renderTargets();
    await setRange("24h");

    for (const button of document.querySelectorAll("#range-buttons button")) {
      button.addEventListener("click", () => setRange(button.dataset.range));
    }

    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(resizeCharts, 120);
    });

    // Pick up a new publish without a manual refresh.
    setInterval(async () => {
      try {
        const manifest = await fetchJSON(`${DATA}/manifest.json`);
        if (manifest.generated_at !== state.manifest.generated_at) location.reload();
      } catch (_) {
        /* offline or mid-deploy — try again next tick */
      }
    }, 5 * 60 * 1000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
