/* The charting seam.
 *
 * `renderSeries` is the whole of this module's contract: it takes the shape
 * `GET /api/v1/charts/series` already returns and draws it. Swapping in Chart.js,
 * Observable Plot or D3 — or growing the full metric/grain/group_by explorer — is a
 * change to this one function and to nothing that calls it.
 *
 * Inline SVG rather than a library: two series over a dozen monthly buckets does not
 * repay a vendored bundle, and this keeps the repository dependency-free.
 *
 * Grouped columns, not stacked. Spent and remaining do sum to the allocated total,
 * which argues for a stack — but `discretionary_remaining_minor` goes negative the
 * moment a period is overspent, and a stacked segment cannot render a negative part of
 * a whole. Grouped columns off a zero baseline show the overspend as a bar below the
 * line, which is the case the reader most needs to see.
 *
 * Palette: slots 1 and 2 of the validated categorical order, stepped per mode. Verified
 * with the dataviz validator against this page's own surfaces (#ffffff / #1e2124) —
 * all five checks pass in both modes: worst-pair CVD ΔE 24.7 light / 26.8 dark against
 * a target of 8, normal-vision ΔE 33.6 / 31.8 against a floor of 15, both slots ≥ 3:1
 * against the surface. Do not hand-edit these hexes; re-run the validator if they change.
 */

const STYLE_ID = "bt-chart-style";

const STYLE = `
.bt-chart {
  --bt-surface: #ffffff;
  --bt-grid:    #e3e0da;
  --bt-ink:     #1c1b19;
  --bt-muted:   #6d6a64;
  --bt-s1:      #2a78d6;
  --bt-s2:      #eb6834;
  position: relative;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  .bt-chart {
    --bt-surface: #1e2124;
    --bt-grid:    #32363b;
    --bt-ink:     #e7e5e1;
    --bt-muted:   #9b9791;
    --bt-s1:      #3987e5;
    --bt-s2:      #d95926;
  }
}
.bt-chart svg { display: block; }
.bt-chart .bt-axis    { fill: var(--bt-muted); font-size: 11px; }
.bt-chart .bt-grid    { stroke: var(--bt-grid); stroke-width: 1; }
.bt-chart .bt-base    { stroke: var(--bt-muted); stroke-width: 1; }
.bt-chart .bt-legend  { fill: var(--bt-ink); font-size: 12px; }
.bt-chart .bt-hit     { fill: transparent; cursor: default; }
.bt-chart .bt-bar     { transition: opacity 120ms ease; }
.bt-chart .bt-bar.dim { opacity: 0.45; }
.bt-chart .bt-empty   { color: var(--bt-muted); font-size: 0.85rem; padding: 1.5rem 0; text-align: center; }
.bt-chart .bt-tip {
  background: var(--bt-surface);
  border: 1px solid var(--bt-grid);
  border-radius: 6px;
  box-shadow: 0 2px 10px rgb(0 0 0 / 0.12);
  font-size: 12px;
  padding: 0.45rem 0.6rem;
  pointer-events: none;
  position: absolute;
  transform: translate(-50%, -100%);
  white-space: nowrap;
  z-index: 5;
}
.bt-chart .bt-tip[hidden] { display: none; }
.bt-chart .bt-tip-head { color: var(--bt-muted); margin-bottom: 0.2rem; }
.bt-chart .bt-tip-row  { align-items: center; display: flex; gap: 0.4rem; }
.bt-chart .bt-tip-key  { border-radius: 1px; height: 2px; width: 10px; }
.bt-chart .bt-tip-val  { color: var(--bt-ink); font-weight: 600;
                         font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.bt-chart .bt-tip-name { color: var(--bt-muted); }
`;

const SVG_NS = "http://www.w3.org/2000/svg";

const PAD = { top: 30, right: 14, bottom: 34, left: 68 };
const BAR_MAX = 24; // cap: never fill the slot
const BAR_GAP = 2; // the surface gap that separates touching marks
const CORNER = 4; // rounded data-end
const MIN_BAND = 62;

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const tag = document.createElement("style");
  tag.id = STYLE_ID;
  tag.textContent = STYLE;
  document.head.append(tag);
}

/** A scale over clean round numbers, always including zero. */
function niceScale(min, max, target = 4) {
  let lo = Math.min(0, min);
  let hi = Math.max(0, max);
  if (lo === hi) hi = lo + 100;

  const rough = (hi - lo) / target;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = Math.max(1, Math.round(multiplier * magnitude));

  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;

  const ticks = [];
  for (let value = lo; value <= hi + step / 2; value += step) ticks.push(Math.round(value));
  return { lo, hi, ticks };
}

/**
 * A column with its data-end rounded and its baseline end square.
 * `up` is false for a negative value, which rounds the bottom instead.
 */
function columnPath(x, y, width, height, up) {
  const r = Math.min(CORNER, width / 2, height);
  if (height <= 0) return "";
  return up
    ? `M${x},${y + height} L${x},${y + r} Q${x},${y} ${x + r},${y} ` +
      `L${x + width - r},${y} Q${x + width},${y} ${x + width},${y + r} ` +
      `L${x + width},${y + height} Z`
    : `M${x},${y} L${x},${y + height - r} Q${x},${y + height} ${x + r},${y + height} ` +
      `L${x + width - r},${y + height} Q${x + width},${y + height} ${x + width},${y + height - r} ` +
      `L${x + width},${y} Z`;
}

/**
 * Draw grouped columns.
 *
 * @param {Element} root       container; its contents are replaced
 * @param {Array}   series     [{label, points: [{bucket, series, value_minor}]}]
 * @param {Object}  opts       {format} — minor units to a display string
 */
export function renderSeries(root, series, { format = String } = {}) {
  ensureStyle();
  root.classList.add("bt-chart");

  // Buckets are the union across series, so a metric missing a period still lines up.
  const buckets = [...new Set(series.flatMap((s) => s.points.map((p) => p.bucket)))].sort();

  if (buckets.length === 0) {
    root.replaceChildren(
      Object.assign(document.createElement("p"), {
        className: "bt-empty",
        textContent: "No periods to chart yet — record some income and spending first.",
      }),
    );
    return;
  }

  const valueAt = (s, bucket) => {
    const point = s.points.find((p) => p.bucket === bucket);
    return point ? Number(point.value_minor) : 0;
  };

  const values = series.flatMap((s) => buckets.map((b) => valueAt(s, b)));
  const scale = niceScale(Math.min(...values), Math.max(...values));

  const available = root.clientWidth || 720;
  const width = Math.max(available, buckets.length * MIN_BAND + PAD.left + PAD.right);
  const height = 260;
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  const y = (value) => PAD.top + plotH - ((value - scale.lo) / (scale.hi - scale.lo)) * plotH;
  const band = plotW / buckets.length;
  const barW = Math.min(BAR_MAX, (band * 0.68 - BAR_GAP * (series.length - 1)) / series.length);
  const groupW = barW * series.length + BAR_GAP * (series.length - 1);

  const frame = svg("svg", {
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${series.map((s) => s.label).join(" and ")} by period`,
  });

  // --- gridlines and y ticks. Hairline, solid, recessive.
  for (const tick of scale.ticks) {
    const ty = y(tick);
    frame.append(
      svg("line", { class: "bt-grid", x1: PAD.left, x2: width - PAD.right, y1: ty, y2: ty }),
    );
    const label = svg("text", {
      class: "bt-axis",
      x: PAD.left - 8,
      y: ty + 3.5,
      "text-anchor": "end",
    });
    label.textContent = format(tick);
    frame.append(label);
  }

  // The zero baseline is emphasised over the other gridlines: with a negative
  // remaining it is the line the reader is actually comparing against.
  frame.append(
    svg("line", {
      class: "bt-base",
      x1: PAD.left,
      x2: width - PAD.right,
      y1: y(0),
      y2: y(0),
    }),
  );

  // --- columns
  const skip = Math.ceil((buckets.length * 52) / plotW); // thin x labels rather than collide
  const bars = [];

  buckets.forEach((bucket, index) => {
    const bandStart = PAD.left + index * band;
    const groupStart = bandStart + (band - groupW) / 2;

    series.forEach((s, slot) => {
      const value = valueAt(s, bucket);
      const x = groupStart + slot * (barW + BAR_GAP);
      const top = Math.min(y(value), y(0));
      const barH = Math.abs(y(value) - y(0));

      if (barH > 0) {
        const bar = svg("path", {
          class: "bt-bar",
          d: columnPath(x, top, barW, barH, value >= 0),
          style: `fill: var(--bt-s${slot + 1})`,
        });
        frame.append(bar);
        bars.push(bar);
      } else {
        // A zero-height bar still takes its slot, so `bars` stays index-aligned with
        // (bucket, series) and the hover highlight cannot drift onto a neighbour.
        bars.push(null);
      }
    });

    if (index % skip === 0) {
      const label = svg("text", {
        class: "bt-axis",
        x: bandStart + band / 2,
        y: height - PAD.bottom + 18,
        "text-anchor": "middle",
      });
      // Bucket ids come from the API — inserted as text, never as markup.
      label.textContent = bucket;
      frame.append(label);
    }
  });

  // --- legend. Always present for two or more series; identity never color-alone.
  let legendX = PAD.left;
  series.forEach((s, slot) => {
    frame.append(
      svg("rect", {
        x: legendX,
        y: 10,
        width: 10,
        height: 10,
        rx: 2,
        style: `fill: var(--bt-s${slot + 1})`,
      }),
    );
    const text = svg("text", { class: "bt-legend", x: legendX + 15, y: 19 });
    text.textContent = s.label;
    frame.append(text);
    legendX += 15 + s.label.length * 7 + 18;
  });

  // --- hover. One tooltip listing every series at that bucket, so the pointer never
  // has to find a specific bar. The hit target is the whole band, not the painted mark.
  const tip = document.createElement("div");
  tip.className = "bt-tip";
  tip.hidden = true;

  buckets.forEach((bucket, index) => {
    const bandStart = PAD.left + index * band;
    const hit = svg("rect", {
      class: "bt-hit",
      x: bandStart,
      y: PAD.top,
      width: band,
      height: plotH,
      tabindex: "0",
    });

    const show = () => {
      tip.replaceChildren(
        Object.assign(document.createElement("div"), {
          className: "bt-tip-head",
          textContent: bucket,
        }),
        ...series.map((s, slot) => {
          const row = document.createElement("div");
          row.className = "bt-tip-row";
          const key = document.createElement("span");
          key.className = "bt-tip-key";
          key.style.background = `var(--bt-s${slot + 1})`;
          const value = document.createElement("span");
          value.className = "bt-tip-val";
          value.textContent = format(valueAt(s, bucket));
          const name = document.createElement("span");
          name.className = "bt-tip-name";
          name.textContent = s.label;
          row.append(key, value, name);
          return row;
        }),
      );
      tip.hidden = false;
      tip.style.left = `${bandStart + band / 2}px`;
      tip.style.top = `${PAD.top - 6}px`;
      for (const bar of bars) bar.classList.add("dim");
      for (let slot = 0; slot < series.length; slot += 1) {
        const bar = bars[index * series.length + slot];
        if (bar) bar.classList.remove("dim");
      }
    };

    const hide = () => {
      tip.hidden = true;
      for (const bar of bars) bar.classList.remove("dim");
    };

    hit.addEventListener("pointerenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("blur", hide);
    frame.append(hit);
  });

  root.replaceChildren(frame, tip);
}
