/* =============================================================
   경량 라인 차트 — 외부 라이브러리 없이 canvas 2D 로 직접 그린다.
   Chart.js 를 안 쓴 이유: 곡선 몇 개짜리 학습 그래프에 200KB 라이브러리는
   과하고, 디자인 토큰 색을 정확히 맞추기가 오히려 번거롭다.

   부드러움은 두 겹이다:
   1) 공간: 꺾은선 대신 중점 이차 베지어 — 데이터를 지나가되 모서리가 없다.
   2) 시간: 에폭이 갱신될 때 이전 화면값에서 목표값으로 이징 — SSE 가 한
      에폭씩 밀어줄 때 그래프가 '뚝' 바뀌지 않고 미끄러진다.
   ============================================================= */

const Chart = {
  /* canvas → { display: 직전 프레임에 실제로 그려진 y값들, raf } */
  _state: new WeakMap(),

  _color(varName, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    return v || fallback;
  },

  _reducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  },

  /**
   * @param canvas  대상 <canvas>
   * @param series  [{ points: [{x,y}], color, dashed, fill }]
   */
  drawLines(canvas, series) {
    const st = this._state.get(canvas) || { display: null, raf: 0 };
    cancelAnimationFrame(st.raf);

    // 모션 축소 설정이거나 첫 렌더면 즉시 목표값을 그린다.
    if (this._reducedMotion() || !st.display) {
      this._render(canvas, series);
      st.display = series.map((s) => s.points.map((p) => p.y));
      this._state.set(canvas, st);
      return;
    }

    // 이전 '화면값'에서 목표값으로 이징. 애니메이션 도중 새 에폭이 와도
    // 현재 화면값에서 다시 출발하므로 튀지 않는다.
    const from = st.display;
    const t0 = performance.now();
    const DUR = 320;
    const easeOut = (t) => 1 - Math.pow(1 - t, 3);

    const step = (now) => {
      const t = easeOut(Math.min(1, (now - t0) / DUR));
      const frame = series.map((s, i) =>
        s.points.map((p, j) => {
          // 새로 생긴 점(from 에 없음)은 목표값에서 시작 — 바닥에서 솟는 잔상 방지
          const fy = from[i]?.[j];
          return fy === undefined ? p.y : fy + (p.y - fy) * t;
        })
      );
      this._render(
        canvas,
        series.map((s, i) => ({
          ...s,
          points: s.points.map((p, j) => ({ x: p.x, y: frame[i][j] })),
        }))
      );
      st.display = frame;
      if (t < 1) st.raf = requestAnimationFrame(step);
    };
    st.raf = requestAnimationFrame(step);
    this._state.set(canvas, st);
  },

  _render(canvas, series) {
    const ctx = canvas.getContext('2d');
    // 레티나 대응: CSS 크기 × devicePixelRatio 로 실제 픽셀을 잡아야 선이 또렷하다.
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 600;
    const cssH = canvas.clientHeight || 240;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    const hairline = this._color('--hairline', '#212327');
    const muted = this._color('--muted', '#9aa0a8');

    const pad = { l: 48, r: 14, t: 12, b: 26 };
    const plotW = cssW - pad.l - pad.r;
    const plotH = cssH - pad.t - pad.b;

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
    ctx.font = '11px "JetBrains Mono", monospace';
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

    series.forEach((s) => {
      if (s.points.length < 1) return;
      const pts = s.points.map((p) => ({ x: sx(p.x), y: sy(p.y) }));

      // 중점 이차 베지어: 각 데이터점을 제어점으로, 이웃 중점을 통과점으로.
      // Catmull-Rom 보다 단순하면서 오버슈트(가짜 봉우리)가 없다.
      const trace = () => {
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < pts.length - 1; i++) {
          const mx = (pts[i].x + pts[i + 1].x) / 2;
          const my = (pts[i].y + pts[i + 1].y) / 2;
          ctx.quadraticCurveTo(pts[i].x, pts[i].y, mx, my);
        }
        const lastPt = pts[pts.length - 1];
        ctx.lineTo(lastPt.x, lastPt.y);
      };

      // 선 아래 은은한 면 채움 — 곡선이 배경과 분리되어 읽힌다.
      if (s.fill !== false && pts.length > 1) {
        trace();
        ctx.lineTo(pts[pts.length - 1].x, pad.t + plotH);
        ctx.lineTo(pts[0].x, pad.t + plotH);
        ctx.closePath();
        ctx.globalAlpha = 0.09;
        ctx.fillStyle = s.color;
        ctx.fill();
        ctx.globalAlpha = 1;
      }

      trace();
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.setLineDash(s.dashed ? [5, 4] : []);
      ctx.stroke();
      ctx.setLineDash([]);

      // 마지막 값 강조점 — 지금 어디인지가 한눈에 보인다.
      const tip = pts[pts.length - 1];
      ctx.beginPath();
      ctx.arc(tip.x, tip.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = s.color;
      ctx.fill();
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
