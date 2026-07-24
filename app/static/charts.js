/**
 * Inline-SVG chart primitives.
 *
 * Hand-rolled rather than pulling a chart library: the CSP is `default-src 'self'`, the
 * app needs three chart types, and a library would cost ~50-200 KB to draw a line and a
 * few bars. Everything here is data-driven — no chart hardcodes its content.
 *
 * Accessibility: charts are `role="img"` with a generated text alternative describing the
 * data, because a screen reader cannot read an SVG path. Where the underlying numbers
 * matter, the caller also renders a table.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

function el(name, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    node.append(child?.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min || 0];
  const raw = (max - min) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? magnitude * 10;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step / 1000; v += step) ticks.push(v);
  return ticks;
}

export function formatNumber(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return Number(value.toFixed(digits)).toLocaleString();
}

/** Vertical bar chart over `{label, value}[]` points. */
export function barChart(points, { height = 220, valueFormatter = formatNumber, yLabel = '' } = {}) {
  const width = 720;
  const pad = { top: 16, right: 16, bottom: 34, left: 52 };
  if (!points.length) return emptyChart('No data for this period');

  const values = points.map((p) => p.value ?? 0);
  const max = Math.max(...values, 1);
  const ticks = niceTicks(0, max);
  const top = Math.max(max, ticks[ticks.length - 1]);
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const slot = plotW / points.length;
  const barW = Math.max(2, Math.min(48, slot * 0.62));
  const y = (v) => pad.top + plotH - (v / top) * plotH;

  const svg = el('svg', {
    class: 'chart', viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': `Bar chart of ${yLabel || 'values'} across ${points.length} periods. ` +
      `Highest ${valueFormatter(max)}.`,
  });

  for (const tick of ticks) {
    svg.append(el('line', { class: 'grid-line', x1: pad.left, x2: width - pad.right, y1: y(tick), y2: y(tick) }));
    svg.append(el('text', { x: pad.left - 8, y: y(tick) + 4, 'text-anchor': 'end' }, valueFormatter(tick)));
  }

  points.forEach((point, i) => {
    const cx = pad.left + slot * i + slot / 2;
    const value = point.value ?? 0;
    const bar = el('rect', {
      class: 'bar', x: cx - barW / 2, y: y(value),
      width: barW, height: Math.max(1, y(0) - y(value)), rx: 2,
    });
    bar.append(el('title', {}, `${point.label}: ${valueFormatter(value)}`));
    svg.append(bar);
    if (points.length <= 10 || i % Math.ceil(points.length / 8) === 0) {
      svg.append(el('text', { x: cx, y: height - 10, 'text-anchor': 'middle' }, point.label));
    }
  });

  svg.append(el('line', { class: 'axis-line', x1: pad.left, x2: width - pad.right, y1: y(0), y2: y(0) }));
  return svg;
}

/**
 * Scatter of meter coordinates in equirectangular projection.
 *
 * Not a tiled basemap: that needs an external tile provider, which this service should not
 * depend on (and the CSP forbids). Relative geography is preserved, so clustering and
 * outliers — the operationally useful signals — read correctly.
 */
export function scatterMap(meters, { height = 520, onSelect } = {}) {
  const width = 900;
  const pad = 28;
  const located = meters.filter((m) => m.location);
  if (!located.length) return emptyChart('No meters have coordinates');

  const lats = located.map((m) => m.location.latitude);
  const lngs = located.map((m) => m.location.longitude);
  const [minLat, maxLat] = [Math.min(...lats), Math.max(...lats)];
  const [minLng, maxLng] = [Math.min(...lngs), Math.max(...lngs)];
  const spanLat = maxLat - minLat || 0.01;
  const spanLng = maxLng - minLng || 0.01;

  const x = (lng) => pad + ((lng - minLng) / spanLng) * (width - pad * 2);
  // Latitude increases northward; SVG y increases downward, so invert.
  const y = (lat) => height - pad - ((lat - minLat) / spanLat) * (height - pad * 2);

  const svg = el('svg', {
    class: 'map', viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': `Geographic scatter of ${located.length} meters across ` +
      `${spanLat.toFixed(3)} degrees latitude and ${spanLng.toFixed(3)} degrees longitude.`,
  });

  const tone = { installed: 'var(--ok)', faulty: 'var(--warn)', decommissioned: 'var(--idle)' };
  for (const meter of located) {
    const dot = el('circle', {
      class: 'map__dot', cx: x(meter.location.longitude).toFixed(1), cy: y(meter.location.latitude).toFixed(1),
      r: 5, fill: tone[meter.install_status] ?? 'var(--text-subtle)',
      tabindex: onSelect ? '0' : null, role: onSelect ? 'button' : null,
      'aria-label': onSelect ? `${meter.meter_id}, ${meter.install_status ?? 'unknown status'}` : null,
    });
    dot.append(el('title', {}, `${meter.meter_id} — ${meter.install_status ?? 'unknown'} — ${meter.dt_code ?? 'no DT'}`));
    if (onSelect) {
      dot.addEventListener('click', () => onSelect(meter.meter_id));
      dot.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onSelect(meter.meter_id);
        }
      });
    }
    svg.append(dot);
  }
  return svg;
}

function emptyChart(message) {
  const wrap = document.createElement('p');
  wrap.className = 'state__text';
  wrap.style.padding = 'var(--space-6)';
  wrap.style.textAlign = 'center';
  wrap.textContent = message;
  return wrap;
}
