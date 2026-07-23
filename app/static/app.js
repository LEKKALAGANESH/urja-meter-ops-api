/**
 * Urja Meter Ops Console.
 *
 * Hash-routed views over the REST API. Deliberately framework-free — five views and a
 * drawer do not justify a runtime, a build step and a dependency tree.
 *
 * Conventions enforced throughout:
 *  - Every async view renders loading -> (data | empty | error) explicitly. There is no
 *    state where the operator sees a blank panel and cannot tell why.
 *  - Anything clickable is a real <button> or <a>, so keyboard and screen readers work
 *    without ARIA patches.
 *  - Repeated structure is rendered from data, never copy-pasted.
 */

import { api, ApiError, freshness } from './api.js';
import { barChart, formatNumber, lineChart, scatterMap } from './charts.js';

const viewRoot = document.getElementById('view');
const drawer = document.getElementById('drawer');
const drawerBody = document.getElementById('drawer-body');
const drawerTitle = document.getElementById('drawer-title');
const toast = document.getElementById('toast');

const STATUS_TONE = { installed: 'ok', faulty: 'warn', decommissioned: 'idle' };
const PAGE_SIZE = 25;

/** Meters-view state, mirrored into the URL hash so views are shareable and reloadable. */
const metersState = {
  page: 1, search: '', make: '', install_status: '', phase_type: '', build: '',
  sort: 'meter_id', order: 'asc',
};

// ---------------------------------------------------------------- helpers

function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    // Assign through CSSOM rather than setAttribute('style'): a strict `style-src 'self'`
    // blocks the style *attribute*, but permits CSSOM property assignment. This keeps the
    // policy tight instead of reaching for 'unsafe-inline'.
    else if (key === 'style') {
      for (const rule of String(value).split(';')) {
        const [prop, ...rest] = rule.split(':');
        if (prop?.trim() && rest.length) node.style.setProperty(prop.trim(), rest.join(':').trim());
      }
    }
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child?.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const card = (title, hint, body) =>
  h('section', { class: 'card' }, [
    title && h('div', { class: 'card__head' }, [
      h('h3', { class: 'card__title', text: title }),
      hint && h('p', { class: 'card__hint', text: hint }),
    ]),
    body,
  ]);

const stateBlock = ({ icon, title, text, action, variant = '' }) =>
  h('div', { class: `state ${variant}` }, [
    h('div', { class: 'state__icon', text: icon, 'aria-hidden': 'true' }),
    h('p', { class: 'state__title', text: title }),
    text && h('p', { class: 'state__text', text }),
    action,
  ]);

const skeleton = (rows = 5) =>
  h('div', { class: 'skeleton-stack', 'aria-hidden': 'true' },
    Array.from({ length: rows }, (_, i) =>
      h('div', { class: 'skeleton', style: `width:${100 - i * 9}%` })));

const badge = (label, tone) => h('span', { class: 'badge', dataset: { tone }, text: label });

const titleCase = (value) =>
  typeof value === 'string' && value
    ? value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
    : '—';

function showToast(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 4000);
}

/** Render a failed load as something the operator can act on, not a dead panel. */
function errorState(error, retry) {
  const isApi = error instanceof ApiError;
  return stateBlock({
    icon: '⚠', variant: 'state--error',
    title: isApi ? error.message : 'Something went wrong',
    text: [isApi && error.hint, isApi && error.requestId && `Request ID ${error.requestId}`]
      .filter(Boolean).join(' · ') || String(error),
    action: retry && h('button', { class: 'btn btn--primary', text: 'Try again', onclick: retry }),
  });
}

/** Run an async view: swap in a loader, then data / empty / error. Never leaves a blank. */
async function renderAsync(container, loader, render, { rows = 5 } = {}) {
  container.replaceChildren(skeleton(rows));
  try {
    const data = await loader();
    container.replaceChildren(render(data));
  } catch (error) {
    container.replaceChildren(errorState(error, () => renderAsync(container, loader, render, { rows })));
  }
}

// ---------------------------------------------------------------- freshness

function paintFreshness(info) {
  const dot = document.querySelector('.freshness__dot');
  const text = document.getElementById('freshness-text');
  if (!info) {
    dot.dataset.state = 'error';
    text.textContent = 'Data unavailable';
    return;
  }
  const age = Math.round(info.age_seconds ?? freshness.ageSeconds ?? 0);
  const stale = info.is_stale ?? age > (info.ttl_seconds ?? 300);
  dot.dataset.state = stale ? 'stale' : 'fresh';
  const when = age < 60 ? `${age}s ago` : `${Math.round(age / 60)}m ago`;
  text.textContent = `${info.meter_count ?? '—'} meters · updated ${when}`;
  document.getElementById('footer-meta').textContent =
    `Snapshot source: ${info.source ?? 'unknown'}`;
}

async function refreshFreshness() {
  try { paintFreshness(await api.snapshot()); } catch { paintFreshness(null); }
}

// ---------------------------------------------------------------- overview

function overviewView() {
  const root = h('div', { class: 'view' }, [
    h('div', { class: 'view__head' }, [
      h('div', {}, [
        h('h2', { class: 'view__title', text: 'Estate overview' }),
        h('p', { class: 'view__sub', text: 'Health and composition of the metering estate.' }),
      ]),
    ]),
  ]);
  const body = h('div', { class: 'view' });
  root.append(body);

  renderAsync(body, () => Promise.all([api.stats(), api.dataQuality()]), ([stats, quality]) => {
    const status = stats.by_install_status ?? {};
    const total = stats.meter_count || 0;
    const pct = (n) => (total ? `${Math.round((n / total) * 100)}% of estate` : '—');

    const tiles = [
      { label: 'Total meters', value: total, meta: `${stats.transformer_count} transformers`, tone: null },
      { label: 'Installed', value: status.installed ?? 0, meta: pct(status.installed ?? 0), tone: 'ok' },
      { label: 'Faulty', value: status.faulty ?? 0, meta: pct(status.faulty ?? 0), tone: 'warn' },
      { label: 'Decommissioned', value: status.decommissioned ?? 0, meta: pct(status.decommissioned ?? 0), tone: 'idle' },
      { label: 'Records with issues', value: stats.with_data_quality_issues ?? 0, meta: `${quality.issue_count} distinct issues`, tone: null },
      { label: 'Network nodes', value: stats.hierarchy_nodes ?? 0, meta: 'across 7 levels', tone: null },
    ];

    const kpis = h('div', { class: 'grid grid--kpi' },
      tiles.map((t) => h('article', { class: 'card stat' }, [
        h('h3', { class: 'stat__label', text: t.label }),
        h('p', { class: 'stat__value', dataset: t.tone ? { tone: t.tone } : {}, text: formatNumber(t.value, 0) }),
        h('p', { class: 'stat__meta', text: t.meta }),
      ])));

    const distribution = (title, hint, data, tone) => {
      const entries = Object.entries(data ?? {}).sort((a, b) => b[1] - a[1]);
      const max = Math.max(1, ...entries.map(([, v]) => v));
      return card(title, hint,
        entries.length
          ? h('div', { class: 'card__body stack stack--sm' },
              entries.map(([label, value]) => h('div', { class: 'bar-row' }, [
                h('span', { text: titleCase(label) }),
                h('div', { class: 'bar-row__track' }, [
                  h('div', {
                    class: 'bar-row__fill',
                    style: `width:${(value / max) * 100}%${tone ? `;background:var(--${tone})` : ''}`,
                  }),
                ]),
                h('span', { class: 'bar-row__value', text: formatNumber(value, 0) }),
              ])))
          : stateBlock({ icon: '∅', title: 'No data' }));
    };

    const topIssues = (quality.issues ?? []).slice(0, 6);
    const issuesCard = card('Data quality', 'Inconsistencies found in the upstream portal data',
      topIssues.length
        ? h('div', { class: 'table-wrap' }, [
            h('table', { class: 'table' }, [
              h('thead', {}, h('tr', {}, [
                h('th', { scope: 'col', text: 'Issue' }),
                h('th', { scope: 'col', text: 'Severity' }),
                h('th', { scope: 'col', class: 'td--num', text: 'Affected' }),
              ])),
              h('tbody', {}, topIssues.map((issue) => h('tr', {}, [
                h('td', {}, [h('span', { title: issue.message, text: titleCase(issue.code) })]),
                h('td', {}, [badge(issue.level, issue.level === 'error' ? 'danger' : 'warn')]),
                h('td', { class: 'td--num', text: formatNumber(issue.affected_count, 0) }),
              ]))),
            ]),
          ])
        : stateBlock({ icon: '✓', title: 'No issues detected', text: 'Every record parsed cleanly.' }));

    return h('div', { class: 'view' }, [
      kpis,
      h('div', { class: 'grid grid--split' }, [
        distribution('By manufacturer', 'Meter makes across the estate', stats.by_make),
        distribution('By phase type', 'Single vs three phase', stats.by_phase_type),
      ]),
      h('div', { class: 'grid grid--split' }, [
        distribution('By data model', 'Legacy vs v2 records upstream', stats.by_build, 'accent'),
        issuesCard,
      ]),
    ]);
  }, { rows: 6 });

  return root;
}

// ---------------------------------------------------------------- meters

const SORTABLE = [
  { key: 'meter_id', label: 'Meter' },
  { key: 'serial_no', label: 'Serial' },
  { key: 'make', label: 'Make' },
  { key: 'install_status', label: 'Status' },
  { key: 'dt_code', label: 'Transformer' },
];

const FILTERS = [
  { key: 'install_status', label: 'Status', options: ['installed', 'faulty', 'decommissioned'] },
  { key: 'make', label: 'Manufacturer', options: ['HPL', 'L&T', 'Genus', 'Allied', 'Secure'] },
  { key: 'phase_type', label: 'Phase', options: ['single', 'three'] },
  { key: 'build', label: 'Data model', options: ['legacy', 'v2'] },
];

function metersView() {
  const results = h('div', { class: 'card' });

  const searchInput = h('input', {
    class: 'input', type: 'search', id: 'meter-search',
    placeholder: 'Meter ID or serial…', value: metersState.search,
    'aria-describedby': 'search-hint',
  });

  let debounce;
  searchInput.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      metersState.search = searchInput.value.trim();
      metersState.page = 1;
      loadMeters();
    }, 250);
  });

  const selects = FILTERS.map((filter) => {
    const select = h('select', { class: 'select', id: `filter-${filter.key}` }, [
      h('option', { value: '', text: `All ${filter.label.toLowerCase()}` }),
      ...filter.options.map((option) =>
        h('option', { value: option, text: titleCase(option), selected: metersState[filter.key] === option })),
    ]);
    select.addEventListener('change', () => {
      metersState[filter.key] = select.value;
      metersState.page = 1;
      loadMeters();
    });
    return h('div', { class: 'field' }, [
      h('label', { class: 'field__label', for: `filter-${filter.key}`, text: filter.label }),
      select,
    ]);
  });

  const reset = h('button', {
    class: 'btn', text: 'Clear filters',
    onclick: () => {
      Object.assign(metersState, { search: '', make: '', install_status: '', phase_type: '', build: '', page: 1 });
      searchInput.value = '';
      for (const select of selects) select.querySelector('select').value = '';
      loadMeters();
    },
  });

  const controls = card(null, null, h('div', { class: 'card__body' }, [
    h('div', { class: 'filters' }, [
      h('div', { class: 'field' }, [
        h('label', { class: 'field__label', for: 'meter-search', text: 'Search' }),
        searchInput,
      ]),
      ...selects,
      h('div', { class: 'field' }, [
        h('span', { class: 'field__label', 'aria-hidden': 'true', text: ' ' }),
        h('div', { class: 'filters__actions' }, [reset]),
      ]),
    ]),
    h('p', {
      class: 'card__hint card__hint--spaced', id: 'search-hint',
      text: 'Search matches meter ID and serial as literal text — % and _ are not wildcards here.',
    }),
  ]));

  function sortHeader(column) {
    const active = metersState.sort === column.key;
    const ariaSort = active ? (metersState.order === 'asc' ? 'ascending' : 'descending') : 'none';
    const button = h('button', {
      class: 'sort-btn', type: 'button', 'aria-sort': ariaSort,
      'aria-label': `Sort by ${column.label}${active ? `, currently ${ariaSort}` : ''}`,
      onclick: () => {
        metersState.order = active && metersState.order === 'asc' ? 'desc' : 'asc';
        metersState.sort = column.key;
        metersState.page = 1;
        loadMeters();
      },
    }, [column.label, h('span', { class: 'sort-btn__icon', 'aria-hidden': 'true', text: active ? (metersState.order === 'asc' ? '▲' : '▼') : '↕' })]);
    return h('th', { scope: 'col', 'aria-sort': ariaSort }, [button]);
  }

  function loadMeters() {
    renderAsync(results, () => api.meters({
      page: metersState.page, page_size: PAGE_SIZE, search: metersState.search,
      make: metersState.make, install_status: metersState.install_status,
      phase_type: metersState.phase_type, build: metersState.build,
      sort: metersState.sort, order: metersState.order,
    }), (page) => {
      if (!page.items.length) {
        return stateBlock({
          icon: '∅', title: 'No meters match these filters',
          text: 'Try clearing a filter or broadening the search term.',
          action: h('button', { class: 'btn', text: 'Clear filters', onclick: () => reset.click() }),
        });
      }

      const rows = page.items.map((meter) => h('tr', {}, [
        h('td', {}, [h('button', {
          class: 'row-btn table__id', text: meter.meter_id,
          'aria-label': `Open details for meter ${meter.meter_id}`,
          onclick: () => openMeter(meter.meter_id),
        })]),
        h('td', { class: 'table__id', text: meter.serial_no ?? '—' }),
        h('td', { text: meter.make ?? '—' }),
        h('td', {}, [meter.install_status
          ? badge(titleCase(meter.install_status), STATUS_TONE[meter.install_status])
          : '—']),
        h('td', { class: 'table__id', text: meter.dt_code ?? '—' }),
        h('td', { text: titleCase(meter.phase_type) }),
      ]));

      const { meta } = page;
      const pager = h('div', { class: 'pager' }, [
        h('p', { text: `Page ${meta.page} of ${meta.total_pages} · ${formatNumber(meta.total_items, 0)} meters` }),
        h('div', { class: 'pager__group' }, [
          h('button', {
            class: 'btn', text: 'Previous', disabled: !meta.has_previous,
            onclick: () => { metersState.page -= 1; loadMeters(); },
          }),
          h('button', {
            class: 'btn', text: 'Next', disabled: !meta.has_next,
            onclick: () => { metersState.page += 1; loadMeters(); },
          }),
        ]),
      ]);

      return h('div', {}, [
        h('div', { class: 'table-wrap' }, [
          h('table', { class: 'table' }, [
            h('caption', { class: 'visually-hidden', text: 'Meters matching the current filters' }),
            h('thead', {}, h('tr', {}, [
              ...SORTABLE.map(sortHeader),
              h('th', { scope: 'col', text: 'Phase' }),
            ])),
            h('tbody', {}, rows),
          ]),
        ]),
        pager,
      ]);
    }, { rows: 8 });
  }

  loadMeters();

  return h('div', { class: 'view' }, [
    h('div', { class: 'view__head' }, [
      h('div', {}, [
        h('h2', { class: 'view__title', text: 'Meters' }),
        h('p', { class: 'view__sub', text: 'Filter and sort the estate — queries the portal itself cannot answer.' }),
      ]),
    ]),
    controls,
    results,
  ]);
}

// ---------------------------------------------------------------- meter drawer

let lastFocused = null;

async function openMeter(meterId) {
  lastFocused = document.activeElement;
  drawerTitle.textContent = meterId;
  drawer.hidden = false;
  document.body.style.overflow = 'hidden';
  // The close *button*, not `[data-close]` — that also matches the scrim <div>, which is
  // not focusable, so focus would silently never enter the dialog.
  drawer.querySelector('button[data-close]').focus();

  drawerBody.replaceChildren(skeleton(6));
  try {
    const [meter, consumption] = await Promise.all([
      api.meter(meterId),
      api.consumption(meterId, { granularity: 'daily' }).catch(() => null),
    ]);
    drawerBody.replaceChildren(meterDetail(meter, consumption));
  } catch (error) {
    drawerBody.replaceChildren(errorState(error, () => openMeter(meterId)));
  }
}

function closeDrawer() {
  drawer.hidden = true;
  document.body.style.overflow = '';
  lastFocused?.focus();
}

const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

/** Keep Tab inside the dialog while it is open, as a modal is required to do. */
function trapFocus(event) {
  if (event.key !== 'Tab' || drawer.hidden) return;
  const items = [...drawer.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
  if (!items.length) return;
  const [first, last] = [items[0], items[items.length - 1]];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function meterDetail(meter, consumption) {
  const facts = [
    ['Serial', meter.serial_no ?? '—'],
    ['Manufacturer', meter.make ?? '—'],
    ['Status', meter.install_status ? titleCase(meter.install_status) : '—'],
    ['Phase', titleCase(meter.phase_type)],
    ['Install type', titleCase(meter.install_type)],
    ['Data model', meter.build ? meter.build : '—'],
    ['Transformer', meter.dt_code ?? '—'],
    ['Location', meter.location
      ? `${meter.location.latitude.toFixed(5)}, ${meter.location.longitude.toFixed(5)}`
      : 'Not recorded'],
  ];

  const details = card('Details', null, h('div', { class: 'card__body' }, [
    h('dl', { class: 'def-list' }, facts.flatMap(([term, value]) => [
      h('dt', { text: term }), h('dd', { text: value }),
    ])),
  ]));

  const path = card('Network path', 'Zone through to distribution transformer',
    meter.hierarchy?.length
      ? h('div', { class: 'card__body' }, [
          h('ol', { class: 'breadcrumb' }, meter.hierarchy.map((ref) =>
            h('li', {}, [
              h('span', { text: ref.name ?? '—' }),
              ref.code && h('span', { class: 'tree__code', text: ` (${ref.code})` }),
            ]))),
        ])
      : stateBlock({ icon: '∅', title: 'No hierarchy recorded' }));

  let usage;
  if (!consumption) {
    usage = card('Consumption', null, stateBlock({
      icon: '⚠', title: 'Consumption unavailable',
      text: 'The portal did not return a reading series for this meter.',
    }));
  } else if (!consumption.intervals.length) {
    usage = card('Consumption', null, stateBlock({
      icon: '∅', title: 'No readings in the available window',
    }));
  } else {
    const s = consumption.summary;
    const points = consumption.intervals.map((i) => ({
      label: new Date(i.start).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }),
      value: i.kwh,
    }));
    usage = card('Consumption', `Daily totals · ${s.reading_count} readings at ${s.interval_minutes ?? '—'} min intervals`,
      h('div', {}, [
        h('div', { class: 'card__body stack' }, [
          h('div', { class: 'grid grid--kpi' }, [
            ['Total', `${formatNumber(s.total_kwh)} kWh`],
            ['Peak day', `${formatNumber(s.peak_interval_kwh)} kWh`],
            ['Power factor', s.average_power_factor ? formatNumber(s.average_power_factor, 3) : '—'],
            ['Voltage range', `${formatNumber(s.min_voltage_r, 0)}–${formatNumber(s.max_voltage_r, 0)} V`],
          ].map(([label, value]) => h('div', {}, [
            h('p', { class: 'stat__label', text: label }),
            h('p', { class: 'stat__value stat__value--sm', text: value }),
          ]))),
          barChart(points, { height: 200, yLabel: 'kWh per day', valueFormatter: (v) => formatNumber(v, 1) }),
        ]),
      ]));
  }

  const quality = meter.data_quality?.length
    ? card('Data quality flags', 'Detected in the upstream record',
        h('div', { class: 'card__body chip-row' },
          meter.data_quality.map((flag) => badge(titleCase(flag), 'warn'))))
    : null;

  return h('div', { class: 'stack stack--lg' }, [details, usage, path, quality]);
}

// ---------------------------------------------------------------- network

function networkView() {
  const body = h('div', { class: 'card' });

  renderAsync(body, () => api.hierarchy(7), (tree) => {
    if (!tree.children?.length) {
      return stateBlock({ icon: '∅', title: 'No hierarchy available' });
    }

    const buildNode = (node, depth) => {
      const hasChildren = node.children?.length > 0;
      const childList = hasChildren
        ? h('ul', { role: 'group', hidden: depth >= 2 },
            node.children.map((child) => buildNode(child, depth + 1)))
        : null;

      const toggle = hasChildren
        ? h('button', {
            class: 'tree__toggle', type: 'button',
            'aria-expanded': String(depth < 2),
            'aria-label': `${depth < 2 ? 'Collapse' : 'Expand'} ${node.name ?? node.code ?? 'node'}`,
            onclick: (event) => {
              const button = event.currentTarget;
              const open = button.getAttribute('aria-expanded') === 'true';
              button.setAttribute('aria-expanded', String(!open));
              childList.hidden = open;
            },
          }, [h('span', { class: 'tree__chevron', 'aria-hidden': 'true', text: '▸' })])
        : h('span', { class: 'tree__spacer' });

      return h('li', { role: 'treeitem' }, [
        h('div', { class: 'tree__row' }, [
          toggle,
          h('span', { class: 'tree__label', text: node.name ?? '(unnamed)' }),
          node.code && h('span', { class: 'tree__code', text: node.code }),
          node.level && h('span', { class: 'tree__level', text: node.level }),
          badge(`${formatNumber(node.meter_count, 0)} meters`, 'accent'),
        ]),
        childList,
      ]);
    };

    return h('div', { class: 'card__body' }, [
      h('ul', { class: 'tree', role: 'tree', 'aria-label': 'Network hierarchy' },
        tree.children.map((child) => buildNode(child, 1))),
    ]);
  }, { rows: 8 });

  return h('div', { class: 'view' }, [
    h('div', { class: 'view__head' }, [
      h('div', {}, [
        h('h2', { class: 'view__title', text: 'Network hierarchy' }),
        h('p', {
          class: 'view__sub',
          text: 'Reconstructed from every meter’s path. Nodes are identified by full ancestor path, because codes repeat across branches.',
        }),
      ]),
    ]),
    body,
  ]);
}

// ---------------------------------------------------------------- map

function mapView() {
  const body = h('div', { class: 'card' });

  renderAsync(body, () => api.meters({ page: 1, page_size: 200 }), (page) => {
    const located = page.items.filter((m) => m.location);
    if (!located.length) return stateBlock({ icon: '∅', title: 'No coordinates available' });

    const legend = h('div', { class: 'legend' },
      Object.entries(STATUS_TONE).map(([status, tone]) =>
        h('span', { class: 'legend__item' }, [
          h('span', { class: 'legend__swatch', style: `background:var(--${tone})`, 'aria-hidden': 'true' }),
          titleCase(status),
        ])));

    return h('div', {}, [
      legend,
      h('div', { class: 'card__body' }, [scatterMap(located, { onSelect: openMeter })]),
      h('p', {
        class: 'card__hint card__hint--footer',
        text: `Showing ${located.length} meters. Positions are true relative geography; there is no basemap because that would require an external tile provider.`,
      }),
    ]);
  }, { rows: 6 });

  return h('div', { class: 'view' }, [
    h('div', { class: 'view__head' }, [
      h('div', {}, [
        h('h2', { class: 'view__title', text: 'Geographic distribution' }),
        h('p', { class: 'view__sub', text: 'Select any meter to open its details.' }),
      ]),
    ]),
    body,
  ]);
}

// ---------------------------------------------------------------- quality

function qualityView() {
  const body = h('div', { class: 'card' });

  renderAsync(body, () => api.dataQuality(), (report) => {
    if (!report.issues.length) {
      return stateBlock({ icon: '✓', title: 'No issues detected', text: 'Every upstream record parsed cleanly.' });
    }
    return h('div', { class: 'table-wrap' }, [
      h('table', { class: 'table' }, [
        h('caption', { class: 'visually-hidden', text: 'Upstream data quality issues' }),
        h('thead', {}, h('tr', {}, [
          h('th', { scope: 'col', text: 'Issue' }),
          h('th', { scope: 'col', text: 'Severity' }),
          h('th', { scope: 'col', class: 'td--num', text: 'Affected' }),
          h('th', { scope: 'col', text: 'Examples' }),
          h('th', { scope: 'col', text: 'Explanation' }),
        ])),
        h('tbody', {}, report.issues.map((issue) => h('tr', {}, [
          h('td', { class: 'table__id', text: issue.code }),
          h('td', {}, [badge(issue.level, issue.level === 'error' ? 'danger' : 'warn')]),
          h('td', { class: 'td--num', text: formatNumber(issue.affected_count, 0) }),
          h('td', {}, (issue.sample_meter_ids ?? []).slice(0, 3).map((id, index) =>
            h('button', {
              class: `row-btn table__id${index ? ' spaced-start' : ''}`,
              text: id, onclick: () => openMeter(id),
            }))),
          h('td', { class: 'td--wrap', text: issue.message }),
        ]))),
      ]),
    ]);
  }, { rows: 6 });

  return h('div', { class: 'view' }, [
    h('div', { class: 'view__head' }, [
      h('div', {}, [
        h('h2', { class: 'view__title', text: 'Data quality' }),
        h('p', {
          class: 'view__sub',
          text: 'Upstream inconsistencies are surfaced, not hidden — so a fault is traced to the right system.',
        }),
      ]),
    ]),
    body,
  ]);
}

// ---------------------------------------------------------------- routing

const VIEWS = {
  overview: overviewView,
  meters: metersView,
  network: networkView,
  map: mapView,
  quality: qualityView,
};

function route() {
  const name = (window.location.hash.replace('#/', '') || 'overview').split('?')[0];
  const view = VIEWS[name] ?? VIEWS.overview;

  for (const link of document.querySelectorAll('.tabs__link')) {
    const active = link.dataset.view === name;
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }

  document.title = `${name === 'overview' ? 'Overview' : titleCase(name)} · Urja Meter Ops`;
  viewRoot.replaceChildren(view());
}

// ---------------------------------------------------------------- theme

function applyTheme(theme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  const dark = theme === 'dark'
    || (theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  root.classList.toggle('dark-preferred', dark);
  const button = document.getElementById('theme-toggle');
  button.setAttribute('aria-pressed', String(dark));
  document.getElementById('theme-label').textContent =
    theme === 'auto' ? 'Auto' : theme === 'dark' ? 'Dark' : 'Light';
  try { localStorage.setItem('theme', theme); } catch { /* private mode: session-only */ }
}

// ---------------------------------------------------------------- wiring

function init() {
  let stored = 'auto';
  try { stored = localStorage.getItem('theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(stored);

  document.getElementById('theme-toggle').addEventListener('click', () => {
    const order = ['auto', 'light', 'dark'];
    applyTheme(order[(order.indexOf(document.documentElement.dataset.theme) + 1) % order.length]);
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (document.documentElement.dataset.theme === 'auto') applyTheme('auto');
  });

  const refreshButton = document.getElementById('refresh');
  refreshButton.addEventListener('click', async () => {
    refreshButton.disabled = true;
    try {
      paintFreshness(await api.refresh());
      route();
      showToast('Snapshot refreshed from the portal.');
    } catch (error) {
      showToast(error instanceof ApiError ? error.message : 'Refresh failed.');
    } finally {
      refreshButton.disabled = false;
    }
  });

  for (const target of drawer.querySelectorAll('[data-close]')) {
    target.addEventListener('click', closeDrawer);
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !drawer.hidden) closeDrawer();
    trapFocus(event);
  });

  window.addEventListener('hashchange', route);
  route();
  refreshFreshness();
  setInterval(refreshFreshness, 30_000);
}

init();
