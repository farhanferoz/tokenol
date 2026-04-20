const NS = 'http://www.w3.org/2000/svg';
const R = 80, CX = 100, CY = 100;

function gaugePt(frac) {
  const a = Math.PI * (1 - frac);
  return [CX + R * Math.cos(a), CY - R * Math.sin(a)];
}

function arcPath(f1, f2) {
  if (f2 - f1 < 0.001) return 'M 0 0';
  const [x1, y1] = gaugePt(f1);
  const [x2, y2] = gaugePt(f2);
  const large = (f2 - f1) > 0.5 ? 1 : 0;
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${R} ${R} 0 ${large} 0 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

class BurnGauge extends HTMLElement {
  static get observedAttributes() { return ['rate', 'projected', 'reference', 'max-rate']; }

  connectedCallback() {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 200 108');
    svg.style.cssText = 'display:block;width:100%;overflow:visible';
    this.appendChild(svg);
    this._svg = svg;

    const mk = (tag, attrs = {}) => {
      const el = document.createElementNS(NS, tag);
      Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
      svg.appendChild(el);
      return el;
    };

    this._track  = mk('path', { fill:'none', stroke:'var(--rule)',  'stroke-width':8, 'stroke-linecap':'round' });
    this._alarm  = mk('path', { fill:'none', stroke:'var(--alarm)', 'stroke-width':8, 'stroke-linecap':'round', opacity:'0.35' });
    this._fill   = mk('path', { fill:'none', stroke:'var(--amber)', 'stroke-width':8, 'stroke-linecap':'round' });
    this._proj   = mk('path', { fill:'none', stroke:'var(--cool)',  'stroke-width':4, 'stroke-linecap':'round', 'stroke-dasharray':'4 3' });
    this._tickG  = mk('g');
    this._needle = mk('line', { stroke:'var(--amber)', 'stroke-width':2.5, 'stroke-linecap':'round' });
    mk('circle', { cx:CX, cy:CY, r:4, fill:'var(--amber)' });

    this._update();
  }

  attributeChangedCallback() { if (this._svg && !this._batching) this._update(); }

  setAll(rate, projected, reference, maxRate) {
    this._batching = true;
    this.setAttribute('rate',      rate);
    this.setAttribute('projected', projected);
    this.setAttribute('reference', reference);
    this.setAttribute('max-rate',  maxRate);
    this._batching = false;
    if (this._svg) this._update();
  }

  _update() {
    const rate    = parseFloat(this.getAttribute('rate')      ?? 0);
    const proj    = parseFloat(this.getAttribute('projected') ?? 0);
    const ref     = parseFloat(this.getAttribute('reference') ?? 50);
    const maxRate = parseFloat(this.getAttribute('max-rate')  ?? 120);

    const rf = Math.min(rate / maxRate, 1);
    const pf = Math.min(proj / maxRate, 1);
    const xf = Math.min(ref  / maxRate, 1);

    this._track.setAttribute('d', arcPath(0, 1));
    this._fill .setAttribute('d', rf > 0.002 ? arcPath(0, rf) : 'M 0 0');
    this._alarm.setAttribute('d', xf < 0.998 ? arcPath(xf, 1) : 'M 0 0');
    this._proj .setAttribute('d', pf > rf + 0.015 ? arcPath(rf, Math.min(pf, 1)) : 'M 0 0');

    const [nx, ny] = gaugePt(rf);
    this._needle.setAttribute('x1', CX); this._needle.setAttribute('y1', CY);
    this._needle.setAttribute('x2', nx.toFixed(2)); this._needle.setAttribute('y2', ny.toFixed(2));

    this._tickG.innerHTML = '';
    [0, 0.25, 0.5, 0.75, 1].forEach(f => {
      const [tx, ty] = gaugePt(f);
      const dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', tx.toFixed(2)); dot.setAttribute('cy', ty.toFixed(2));
      dot.setAttribute('r', 2.5);
      dot.setAttribute('fill', f <= rf ? 'var(--amber)' : 'var(--mute)');
      this._tickG.appendChild(dot);

      const ang = Math.PI * (1 - f);
      const lx = (CX + (R + 15) * Math.cos(ang)).toFixed(1);
      const ly = (CY - (R + 15) * Math.sin(ang)).toFixed(1);
      const txt = document.createElementNS(NS, 'text');
      txt.setAttribute('x', lx); txt.setAttribute('y', ly);
      txt.setAttribute('text-anchor', 'middle');
      txt.setAttribute('dominant-baseline', 'middle');
      txt.setAttribute('font-size', '8');
      txt.setAttribute('fill', 'var(--mute)');
      txt.setAttribute('font-family', 'var(--font-mono)');
      txt.textContent = Math.round(f * maxRate);
      this._tickG.appendChild(txt);
    });
  }
}

customElements.define('burn-gauge', BurnGauge);
