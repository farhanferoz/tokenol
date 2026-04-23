/* global uPlot */

export const Y_FMTRS = {
  percent: v => Number.isFinite(v) ? `${v.toFixed(1)}%`                                     : '',
  usd:     v => Number.isFinite(v) ? `$${v.toFixed(2)}`                                     : '',
  ratio:   v => Number.isFinite(v) ? `${v >= 100 ? Math.round(v) : v.toFixed(1)}:1`         : '',
  tokens:  v => Number.isFinite(v) ? (v >= 1e6 ? `${(v/1e6).toFixed(1)}M` : `${(v/1e3).toFixed(0)}k`) : '',
};

const _PAL = ['#a66408', '#2a6389', '#2f7a4a', '#b8301b', '#6b3fa0', '#888', '#aa7a2c', '#4a78a0', '#c45050'];
const _Y_AXIS_SIZE = { usd: 62, tokens: 52, ratio: 56, percent: 52 };

// Custom stepped path (align=1, leading) that explicitly breaks on NaN.
// When a valid point is followed by NaN, extend the horizontal to the NaN's x so
// isolated data points render as short bars instead of bare dots. uPlot's built-in
// stepped doesn't always honour spanGaps:false for typed-array NaN.
function _steppedGapAware(u, seriesIdx, idx0, idx1) {
  const scaleX = u.series[0].scale ?? 'x';
  const scaleY = u.series[seriesIdx].scale;
  const dataX  = u.data[0];
  const dataY  = u.data[seriesIdx];
  const stroke = new Path2D();
  let prevY = 0, hasPrev = false;
  for (let i = idx0; i <= idx1; i++) {
    const yv = dataY[i];
    const x  = u.valToPos(dataX[i], scaleX, true);
    if (!Number.isFinite(yv)) {
      if (hasPrev) { stroke.lineTo(x, prevY); hasPrev = false; }
      continue;
    }
    const y = u.valToPos(yv, scaleY, true);
    if (!hasPrev) { stroke.moveTo(x, y); hasPrev = true; }
    else          { stroke.lineTo(x, prevY); stroke.lineTo(x, y); }
    prevY = y;
  }
  return { stroke };
}

// Draw faint dotted stepped connectors across NaN gaps for ratio/percent charts,
// mirroring _steppedGapAware: the connector starts at the solid's end point
// (first-NaN x, prev y) and steps horizontally then vertically to the next value.
function _gapConnectorPlugin() {
  return {
    hooks: {
      draw(u) {
        const dataX = u.data[0];
        const ctx = u.ctx;
        ctx.save();
        ctx.setLineDash([2, 3]);
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.4;
        for (let s = 1; s < u.series.length; s++) {
          const ser = u.series[s];
          if (ser.show === false) continue;
          const dataY = u.data[s];
          const scaleY = ser.scale;
          ctx.strokeStyle = ser.stroke || '#888';
          ctx.beginPath();
          let prev = null;
          for (let i = 0; i < dataX.length; i++) {
            const yv = dataY[i];
            if (!Number.isFinite(yv)) continue;
            if (prev && prev.i < i - 1) {
              const xStart = u.valToPos(dataX[prev.i + 1], 'x', true);
              const yStart = u.valToPos(prev.y, scaleY, true);
              const xEnd   = u.valToPos(dataX[i],          'x', true);
              const yEnd   = u.valToPos(yv,                scaleY, true);
              ctx.moveTo(xStart, yStart);
              ctx.lineTo(xEnd,   yStart);
              ctx.lineTo(xEnd,   yEnd);
            }
            prev = { i, y: yv };
          }
          ctx.stroke();
        }
        ctx.restore();
      },
    },
  };
}

function _tooltipPlugin(fmt, xFmtFn, turnsAt) {
  let el;
  return {
    hooks: {
      init(u) {
        el = document.createElement('div');
        el.className = 'u-tooltip';
        el.style.display = 'none';
        u.over.appendChild(el);
        u.over.addEventListener('mouseleave', () => { el.style.display = 'none'; });
      },
      setCursor(u) {
        const { idx, left, top } = u.cursor;
        if (idx == null) { el.style.display = 'none'; return; }
        const parts = u.series.slice(1).reduce((acc, s, i) => {
          const v = u.data[i + 1]?.[idx];
          if (Number.isFinite(v)) {
            acc.push(`<span class="tt-lbl">${s.label}</span> <span class="tt-val">${fmt(v)}</span>`);
          }
          return acc;
        }, []);
        if (!parts.length) { el.style.display = 'none'; return; }
        const x = u.data[0][idx];
        const turns = turnsAt?.(x);
        const turnsLine = Number.isFinite(turns)
          ? `<div class="tt-sample">${turns} turn${turns === 1 ? '' : 's'}</div>`
          : '';
        el.innerHTML = `<div class="tt-time">${xFmtFn(x)}</div>${parts.join('<br>')}${turnsLine}`;
        el.style.display = '';
        el.style.left = `${Math.min(left + 14, u.over.clientWidth - (el.offsetWidth || 100) - 4)}px`;
        el.style.top  = `${Math.max(top - (el.offsetHeight || 40) - 8, 4)}px`;
      },
    },
  };
}

const _xFmtHour = v => {
  const d = new Date(v * 1000);
  return d.getMinutes() === 0 ? `${d.getHours()}:00` : '';
};

export function drawChart(container, {
  xs, ySeries, labels, yUnit, height = 180,
  onPointClick, xFmt, dashes, stepped = false, turnsByX,
}) {
  const xFmtFn  = xFmt ?? _xFmtHour;
  const existing = container._uplot;
  if (existing && existing.series.length === ySeries.length + 1 && container._yUnit === yUnit) {
    existing.setData([xs, ...ySeries]);
    container._xs = xs;
    container._turnsByX = turnsByX;
    return;
  }

  if (existing) { existing.destroy(); container._uplot = null; }
  container.innerHTML = '';

  const fmt = Y_FMTRS[yUnit] ?? (v => Number.isFinite(v) ? String(v) : '');
  const g   = { stroke: '#d6cab0', width: 1 };
  const steppedArr = Array.isArray(stepped) ? stepped : ySeries.map(() => !!stepped);

  const series = [
    {},
    ...ySeries.map((_, i) => ({
      label: labels[i] ?? `s${i}`, stroke: _PAL[i % _PAL.length],
      width: 2, spanGaps: false, value: (_u, v) => fmt(v),
      ...(steppedArr[i] ? { paths: _steppedGapAware } : {}),
      ...(dashes?.[i] ? { dash: dashes[i] } : {}),
    })),
  ];

  // Read turns via closure so fast-path setData() picks up fresh counts.
  container._turnsByX = turnsByX;
  const turnsAt = (x) => container._turnsByX?.get(x);

  const u = new uPlot({
    width:   container.offsetWidth || 600,
    height,
    padding: [8, 4, 0, 0],
    select:  { show: false },
    legend:  { show: ySeries.length > 1 },
    cursor:  { drag: { x: false, y: false } },
    scales:  { y: { range: (_u, lo, hi) => {
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1];
      const pad = (hi - lo) * 0.05 || Math.abs(hi) * 0.05 || 1;
      return [lo - pad, hi + pad];
    }}},
    axes: [
      { stroke: '#7a7062', ticks: g, grid: g, values: (_u, vs) => vs.map(xFmtFn) },
      { stroke: '#7a7062', ticks: g, grid: g, size: _Y_AXIS_SIZE[yUnit] ?? 50, values: (_u, vs) => vs.map(fmt) },
    ],
    plugins: [
      ...(yUnit === 'percent' || yUnit === 'ratio' || yUnit === 'tokens' ? [_gapConnectorPlugin()] : []),
      _tooltipPlugin(fmt, xFmtFn, turnsAt),
    ],
    series,
  }, [xs, ...ySeries], container);

  container._uplot = u;
  container._xs    = xs;
  container._yUnit = yUnit;
  container.tabIndex = 0;

  if (onPointClick) {
    const handler = () => { const { idx } = u.cursor; if (idx != null) onPointClick(container._xs[idx]); };
    if (container._clickHandler) container.removeEventListener('click', container._clickHandler);
    container._clickHandler = handler;
    container.addEventListener('click', handler);
  }

  const keyHandler = e => {
    const cur = container._uplot;
    const pts = container._xs;
    if (!cur || !pts?.length) return;
    const last = pts.length - 1;
    let idx = cur.cursor.idx;
    if (e.key === 'ArrowLeft') {
      idx = idx == null ? last : Math.max(idx - 1, 0);
    } else if (e.key === 'ArrowRight') {
      idx = idx == null ? 0 : Math.min(idx + 1, last);
    } else if (e.key === 'Enter') {
      if (idx != null && onPointClick) { e.preventDefault(); onPointClick(pts[idx]); }
      return;
    } else {
      return;
    }
    e.preventDefault();
    cur.setCursor({ left: cur.valToPos(pts[idx], 'x'), top: cur.bbox.height / devicePixelRatio / 2 });
  };
  if (container._keyHandler) container.removeEventListener('keydown', container._keyHandler);
  container._keyHandler = keyHandler;
  container.addEventListener('keydown', keyHandler);
}
