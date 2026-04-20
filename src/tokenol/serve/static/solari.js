class SolariNumber extends HTMLElement {
  static get observedAttributes() { return ['value', 'format']; }

  connectedCallback() {
    this.style.display = 'inline-block';
    this.style.fontVariantNumeric = 'tabular-nums';
    this._render(parseFloat(this.getAttribute('value') || '0'));
  }

  attributeChangedCallback(name, _, newVal) {
    if (!this.isConnected) return;
    if (name === 'value') this._animate(parseFloat(newVal || '0'));
    if (name === 'format') this._render(parseFloat(this.getAttribute('value') || '0'));
  }

  _fmt(n) {
    const f = this.getAttribute('format') || '%.2f';
    return f
      .replace(/%%/g, '\0')
      .replace(/%\.(\d+)f/g, (_, d) => (+n).toFixed(+d))
      .replace(/%d/g, () => String(Math.round(+n)))
      .replace(/%s/g, () => String(n))
      .replace(/\0/g, '%');
  }

  _render(n) {
    const str = this._fmt(n);
    const spans = this.querySelectorAll('span');
    if (spans.length !== str.length) {
      this.innerHTML = [...str]
        .map(ch => `<span style="display:inline-block">${ch}</span>`)
        .join('');
    } else {
      spans.forEach((s, i) => { s.textContent = str[i]; });
    }
  }

  _animate(to) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      this._render(to);
      return;
    }
    const str = this._fmt(to);
    const spans = [...this.querySelectorAll('span')];
    if (spans.length !== str.length) { this._render(to); return; }

    spans.forEach((span, i) => {
      const target = str[i];
      if (!/\d/.test(target) || span.textContent === target) {
        span.textContent = target;
        return;
      }
      clearInterval(span._iv);
      let step = 0;
      span._iv = setInterval(() => {
        step++;
        if (step < 5) {
          span.textContent = Math.floor(Math.random() * 10);
        } else {
          span.textContent = target;
          clearInterval(span._iv);
          span._iv = null;
        }
      }, 28);
    });
  }
}

customElements.define('solari-number', SolariNumber);
