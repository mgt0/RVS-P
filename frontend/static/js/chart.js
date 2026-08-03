/* =============================================================
   경량 라인 차트 — 외부 라이브러리 없이 canvas 2D 로 직접 그린다.
   Chart.js 를 안 쓴 이유: 곡선 2개짜리 학습 그래프에 200KB 라이브러리는
   과하고, 디자인 토큰(흰색/선셋) 색을 정확히 맞추기가 오히려 번거롭다.
   ============================================================= */

const Chart = {
  /* CSS 변수를 읽어 캔버스 색을 토큰과 일치시킨다. */
  _color(varName, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    return v || fallback;
  },

  /**
   * @param canvas  대상 <canvas>
   * @param series  [{ points: [{x,y}], color, dashed }]
   * @param opts    { yLabel }
   */
  drawLines(canvas, series, opts = {}) {
    const ctx = canvas.getContext('2d');
    // 레티나 대응: CSS 크기 × devicePixelRatio 로 실제 픽셀을 잡아야 선이 또렷하다.
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 600;
    const cssH = canvas.clientHeight || 240;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    const ink = this._color('--ink', '#ffffff');
    const hairline = this._color('--hairline', '#212327');
    const muted = this._color('--muted', '#7d8187');

    const pad = { l: 44, r: 12, t: 10, b: 24 };
    const plotW = cssW - pad.l - pad.r;
    const plotH = cssH - pad.t - pad.b;

    // 모든 시리즈를 아우르는 범위
    const all = series.flatMap((s) => s.points);
    if (all.length === 0) return;
    const xs = all.map((p) => p.x);
    const ys = all.map((p) => p.y);
    const xMin = Math.min(...xs), xMax = Math.max(...xs) || 1;
    let yMin = Math.min(...ys), yMax = Math.max(...ys);
    if (yMin === yMax) { yMax = yMin + 1; }
    const yPad = (yMax - yMin) * 0.08;
    yMin -= yPad; yMax += yPad;

    const sx = (x) => pad.l + ((x - xMin) / (xMax - xMin || 1)) * plotW;
    const sy = (y) => pad.t + (1 - (y - yMin) / (yMax - yMin || 1)) * plotH;

    // 격자 + y축 눈금 (4단계)
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillStyle = muted;
    ctx.strokeStyle = hairline;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yVal = yMin + ((yMax - yMin) * i) / 4;
      const py = sy(yVal);
      ctx.beginPath();
      ctx.moveTo(pad.l, py);
      ctx.lineTo(cssW - pad.r, py);
      ctx.stroke();
      ctx.fillText(this._fmt(yVal), 4, py + 3);
    }

    // 각 시리즈
    series.forEach((s) => {
      if (s.points.length < 1) return;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(s.dashed ? [4, 3] : []);
      ctx.beginPath();
      s.points.forEach((p, i) => {
        const px = sx(p.x), py = sy(p.y);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    });

    // x축 양끝 라벨
    ctx.fillStyle = muted;
    ctx.fillText(String(Math.round(xMin)), pad.l, cssH - 6);
    ctx.textAlign = 'right';
    ctx.fillText(String(Math.round(xMax)), cssW - pad.r, cssH - 6);
    ctx.textAlign = 'left';
  },

  _fmt(v) {
    const a = Math.abs(v);
    if (a >= 1000) return v.toFixed(0);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  },
};
