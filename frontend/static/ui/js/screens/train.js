/* =============================================================
   학습 화면 — 라우트 train, 클래스 접두어 tr-.
   학습은 서버 밖(scripts/train_act.py)에서 돌고, 이 화면은 logs/train 을 따라 읽어 그린다.
   서버의 /api/train/{start,stop,status,stream} 와 /api/train/runs/latest 를 쓴다(레거시와 같은 API·본문).

   ■ 모드: 'lerobot'(실제 학습 로그) | 'demo'(합성 곡선). 서버 스냅샷의 params.mode 가 그 데이터의 모드다.
     토글은 보기 방식일 뿐이라, 서버가 다른 모드의 기록을 들고 있으면 이 화면은 그 기록을 보여주지 않는다.
   ■ 따라 읽기: 화면에 들어오면 서버 상태를 읽고, RUNNING 이면 SSE 에 붙는다. 서버가 쉬고 있는데 가장 최근
     실행(runs/latest)이 RUNNING 이면 자동으로 붙는다(사용자가 방금 끊은 실행은 제외). 화면이 비활성이 되면
     SSE 를 닫고, 다시 들어오면 다시 붙는다.
   ■ 지표 패널은 lerobot 버전마다 키가 달라서 '로그에 한 번이라도 온 키'만 그린다. 화면 숫자는 전부
     그리는 계열에서 계산한다(글자로 따로 적은 예시 숫자 없음).
   ■ tr-spark = 이 화면 안에서만 쓰는 SVG 선 그래프. chart.js 는 쓰지 않는다.
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});
  RVS.screens = RVS.screens || {};
  const { ref, shallowRef, reactive, computed, onActivated, onDeactivated } = Vue;

  /* ---------------- 숫자 도우미 ---------------- */
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const fmtInt = (n) => Math.round(n).toLocaleString('en-US');
  const fmtDec = (n, dp) => Number(n).toLocaleString('en-US', { maximumFractionDigits: dp });
  // lerobot 은 smpl 을 K 단위로 반올림해 찍는다(1000 이상). 그 값은 근사다.
  const approx = (n) => {
    if (n === null) return '—';
    if (n < 1000) return fmtInt(n);
    return '≈ ' + (n >= 1e6 ? fmtInt(n / 1e6) + 'M' : fmtInt(n / 1e3) + 'K');
  };
  const fmtDur = (sec) => {
    if (!Number.isFinite(sec) || sec < 0) return '—';
    if (sec < 60) return '약 ' + Math.max(1, Math.round(sec)) + '초';
    const m = Math.ceil(sec / 60);
    return m < 60 ? '약 ' + m + '분' : '약 ' + Math.floor(m / 60) + '시간 ' + (m % 60) + '분';
  };
  const rangeOf = (arrs) => {
    let lo = Infinity, hi = -Infinity;
    for (const a of arrs) for (const v of a) { if (v < lo) lo = v; if (v > hi) hi = v; }
    return [lo, hi];
  };
  /* 큰 차트의 세로축: 양 끝과 한가운데가 1·2·5 × 10^k 의 배수가 되게 바깥으로 맞춘다.
     눈금 칸 수가 6 이하인 가장 촘촘한 단위를 고른다(곡선이 차트 높이를 최대한 쓰게). */
  function niceRange(min, max) {
    if (!(max > min)) { const m = Math.abs(max) || 1; min = max - m / 2; max = max + m / 2; }
    const e0 = Math.floor(Math.log10(max - min)) - 2;
    let r = null;
    for (let e = e0; e < e0 + 6 && !(r && r.n <= 6); e++) {
      for (const m of [1, 2, 5]) {
        const u = m * Math.pow(10, e);
        const lo = Math.floor(min / u + 1e-9);
        let n = Math.ceil(max / u - 1e-9) - lo;
        if (n % 2) n++;   // 가운데 눈금도 u 의 배수가 되게
        r = { lo: lo * u, hi: (lo + n) * u, u, n };
        if (n <= 6) break;
      }
    }
    return { lo: r.lo, hi: r.hi, mid: (r.lo + r.hi) / 2, dp: Math.max(0, Math.ceil(-Math.log10(r.u) - 1e-9)) };
  }

  /* ---------------- tr-spark: 선 그래프(SVG + 눈금 글자) ---------------- */
  const SPARK_W = { sm: 270, lg: 1050 };
  const SPARK_H = { sm: 64, lg: 220 };
  const SPARK_PAD = 8;   // 위아래 여백 = 눈금 글자 높이(16)의 절반, 그래서 눈금선이 글자 가운데에 선다
  const TrSpark = {
    name: 'TrSpark',
    props: {
      lines: { type: Array, default: () => [] },     // [{ xs, ys, dash }]
      dots: { type: Array, default: () => [] },      // [{ x, y }]
      xr: { type: Array, required: true },           // [x 최소, x 최대]
      yr: { type: Array, required: true },           // [세로축 아래 끝 값, 위 끝 값]
      ylabels: { type: Array, default: () => [] },   // 위 → 아래. 한 개면 가운데(평평한 계열)
      xlabels: { type: Array, default: () => [] },
      size: { type: String, default: 'sm' },
      aria: String,
    },
    setup(props) {
      const w = computed(() => SPARK_W[props.size] || SPARK_W.sm);
      const h = computed(() => SPARK_H[props.size] || SPARK_H.sm);
      const px = (x) => (props.xr[1] > props.xr[0] ? ((x - props.xr[0]) / (props.xr[1] - props.xr[0])) * w.value : w.value / 2);
      const py = (y) => (props.yr[1] > props.yr[0]
        ? SPARK_PAD + (1 - (y - props.yr[0]) / (props.yr[1] - props.yr[0])) * (h.value - 2 * SPARK_PAD)
        : h.value / 2);
      const pt = (x, y) => px(x).toFixed(1) + ' ' + py(y).toFixed(1);
      const paths = computed(() => props.lines.map((l) => ({
        dash: !!l.dash,
        d: l.xs.map((x, i) => (i ? 'L' : 'M') + pt(x, l.ys[i])).join(' '),
      })));
      // 길이 0 선분 + 둥근 끝 = 화면 픽셀 크기가 일정한 점(viewBox 를 늘려도 타원이 되지 않는다)
      const dotD = computed(() => props.dots.map((p) => 'M' + pt(p.x, p.y) + 'h0').join(' '));
      const grid = computed(() => (props.ylabels.length < 2 ? []
        : props.ylabels.map((_, i, a) => SPARK_PAD + (i * (h.value - 2 * SPARK_PAD)) / (a.length - 1))));
      return { w, h, paths, dotD, grid };
    },
    template: `
      <div class="tr-plot" :class="'tr-plot--' + size">
        <div class="tr-plot__y" :class="{ 'tr-plot__y--mid': ylabels.length === 1 }" aria-hidden="true">
          <span v-for="(t, i) in ylabels" :key="i">{{ t }}</span>
        </div>
        <svg class="tr-plot__svg" role="img" :aria-label="aria" :viewBox="'0 0 ' + w + ' ' + h" preserveAspectRatio="none">
          <line v-for="g in grid" :key="g" class="tr-plot__grid" x1="0" :y1="g" :x2="w" :y2="g"/>
          <path v-for="(p, i) in paths" :key="i" class="tr-plot__line" :class="{ 'tr-plot__line--dash': p.dash }" :d="p.d"/>
          <path v-if="dotD" class="tr-plot__dot-bg" :d="dotD"/>
          <path v-if="dotD" class="tr-plot__dot" :d="dotD"/>
        </svg>
        <div></div>
        <div class="tr-plot__x" :class="{ 'tr-plot__x--3': xlabels.length === 3 }" aria-hidden="true">
          <span v-for="(t, i) in xlabels" :key="i">{{ t }}</span>
        </div>
      </div>`,
  };

  const CMDS = [
    'python scripts/to_lerobot.py --repo-id omx/scan',
    'python scripts/train_act.py --repo-id omx/scan --steps 20000',
  ];
  const RUN_STATUS = { RUNNING: '학습 중', DONE: '완료', FAILED: '실패' };

  RVS.screens.train = {
    name: 'TrainScreen',
    title: '학습',
    components: { 'tr-spark': TrSpark },
    template: `
      <section class="tr-screen" aria-labelledby="tr-h">
        <header class="rv-head tr-head">
          <div class="tr-head__text">
            <h1 id="tr-h" tabindex="-1">학습</h1>
            <p>학습은 GPU를 오래 쓰므로 서버 밖에서 돌고, 이 화면은 그 로그를 따라 그립니다.</p>
          </div>
          <div class="tr-head__mode">
            <rv-seg :model-value="mode" :options="modeOptions" label="보기 방식" @change="pickMode" />
            <p v-if="running" class="rv-small">진행 중에는 바꿀 수 없습니다. {{ isReal ? '먼저 따라 읽기를 중단하세요.' : '먼저 데모를 정지하세요.' }}</p>
          </div>
        </header>

        <section v-if="loading" class="rv-card"><rv-state state="loading" message="학습 상태를 불러오는 중…" /></section>
        <section v-else-if="loadError" class="rv-card"><rv-state state="error" :message="loadError" @retry="init" /></section>

        <template v-else>
          <!-- 데모 곡선 폼 -->
          <section v-if="!isReal" class="rv-card" aria-labelledby="tr-demo-h">
            <div>
              <h2 id="tr-demo-h">데모 곡선</h2>
              <p class="rv-muted">GPU·데이터 없이 차트가 제대로 그려지는지 확인하는 합성 곡선입니다. 실제 성능이 아닙니다.</p>
            </div>
            <form class="tr-form" novalidate @submit.prevent="runDemo">
              <label class="rv-field">에폭
                <input type="number" min="1" max="500" step="1" v-model.number="params.epochs" :disabled="running" :aria-invalid="formErr.field === 'epochs' ? 'true' : null">
              </label>
              <label class="rv-field">성공 임계값(°)
                <input type="number" min="0.5" max="30" step="0.5" v-model.number="params.threshold_deg" :disabled="running" :aria-invalid="formErr.field === 'threshold_deg' ? 'true' : null">
              </label>
              <label class="rv-field">학습률
                <input type="number" min="0.00001" max="0.01" step="any" v-model.number="params.lr" :disabled="running" :aria-invalid="formErr.field === 'lr' ? 'true' : null">
              </label>
              <div class="tr-form__act">
                <button v-if="!running" type="submit" class="rv-btn rv-btn--primary" :disabled="busy">데모 시작</button>
                <button v-else type="button" class="rv-btn" :disabled="busy" @click="stop">데모 정지</button>
              </div>
            </form>
            <p v-if="formErr.msg" class="rv-field__error" role="alert"><rv-status kind="warn" :label="formErr.msg" /></p>
          </section>

          <!-- 실제 학습 로그: 아직 붙지 않았을 때(최근 실행 불러오기 / 기록 없음) -->
          <section v-if="isReal && !view" class="rv-card" aria-labelledby="tr-start-h">
            <template v-if="latest">
              <div>
                <h2 id="tr-start-h">가장 최근 학습 실행</h2>
                <p class="rv-muted">
                  <code class="tr-k">logs/train/{{ latest.run_id }}</code> · {{ runStatusText(latest) }}<template v-if="latest.steps"> · 총 {{ fmtInt(latest.steps) }}스텝</template><template v-if="latest.started_at"> · 시작 {{ fmt.dateTime(latest.started_at) }}</template>
                </p>
              </div>
              <div class="tr-actions">
                <button type="button" class="rv-btn rv-btn--primary" :disabled="busy" @click="startReal()">{{ latest.status === 'RUNNING' ? '따라 읽기' : '이 실행 불러오기' }}</button>
              </div>
              <p class="rv-small rv-muted">로그를 읽기만 합니다. 학습 프로세스는 건드리지 않습니다.</p>
            </template>
            <template v-else>
              <div class="tr-empty">
                <rv-status kind="info" size="md" hide-label label="정보" />
                <div>
                  <h2 id="tr-start-h">아직 학습 기록이 없습니다</h2>
                  <p class="rv-muted">프로젝트 .venv 가 켜진 터미널에서 아래 두 줄을 실행하면 <code class="tr-k">logs/train/</code> 에 기록이 생기고, 여기서 0.5초마다 따라 읽습니다.</p>
                </div>
              </div>
              <div class="tr-cmds">
                <ol class="rv-steps">
                  <li v-for="(c, i) in cmds" :key="i">
                    <span class="rv-steps__n">{{ i + 1 }}</span>
                    <div class="rv-steps__t tr-cmd">
                      <code class="tr-cmd__text">{{ c }}</code>
                      <button type="button" class="rv-btn" :aria-label="(i + 1) + '번 명령 복사'" @click="copy(i)">복사</button>
                    </div>
                  </li>
                </ol>
                <div class="rv-small tr-copy" role="status">{{ copyMsg }}</div>
              </div>
              <p class="rv-small rv-muted">학습을 멈추려면 그 터미널에서 Ctrl+C. 이 화면의 '따라 읽기 중단'은 학습을 죽이지 않습니다.</p>
              <rv-state v-if="latestError" state="error" :message="latestError" @retry="reloadLatest" />
            </template>
          </section>

          <!-- 서버가 준 오류·안내 -->
          <section v-if="view && view.error" class="rv-card" aria-labelledby="tr-err-h">
            <h2 id="tr-err-h"><rv-status :kind="view.state === 'FAILED' ? 'warn' : 'info'" size="md" :label="view.state === 'FAILED' ? '학습 기록을 읽지 못했습니다' : '안내'" /></h2>
            <p class="tr-err">{{ view.error }}</p>
          </section>

          <!-- 지표 -->
          <section v-if="view" class="rv-card tr-run" aria-labelledby="tr-run-h">
            <div class="tr-run__head">
              <div class="tr-run__id">
                <h2 id="tr-run-h">{{ isReal ? '학습 로그' : '합성 학습 곡선' }}</h2>
                <p v-if="runId" class="rv-small rv-muted"><code class="tr-k">logs/train/{{ runId }}</code></p>
                <p v-else-if="!isReal" class="rv-small rv-muted">실제 성능이 아닙니다</p>
              </div>
              <div class="tr-run__ctl">
                <rv-status :kind="stat.kind" :label="stat.label" size="md" />
                <button v-if="isReal && running" type="button" class="rv-btn" :disabled="busy" @click="stop">따라 읽기 중단</button>
                <button v-else-if="isReal" type="button" class="rv-btn rv-btn--primary" :disabled="busy" @click="startReal()">다시 따라 읽기</button>
              </div>
            </div>

            <p v-if="!recs.length" class="rv-muted">
              <template v-if="running">첫 지표 줄을 기다리는 중입니다. 학습 로그는 <code class="tr-k">log_freq</code> 스텝마다 한 줄씩 쌓입니다.</template>
              <template v-else>이 실행에는 읽을 수 있는 지표 줄이 없습니다.</template>
            </p>

            <template v-else>
              <dl class="tr-cells">
                <div class="tr-cell">
                  <dt>{{ xUnit }} · <code class="tr-k">{{ isReal ? 'step' : 'epoch' }}</code></dt>
                  <dd>
                    <div class="tr-cell__big"><span>{{ fmtInt(last.epoch) }}</span><span v-if="total" class="tr-cell__of">/ {{ fmtInt(total) }}</span></div>
                    <div v-if="total" class="rv-meter__bar tr-bar" role="img" :aria-label="progressAria"><div class="rv-meter__fill" :style="{ width: pct + '%' }"></div></div>
                    <div class="tr-cell__note">{{ progressText }}</div>
                  </dd>
                </div>
                <div v-if="has.smpl" class="tr-cell">
                  <dt>샘플 · <code class="tr-k">smpl</code></dt>
                  <dd><div class="tr-cell__big">{{ approx(lastOf('smpl')) }}</div><div class="tr-cell__note">{{ isReal ? '스텝 × 배치 크기' + (batch ? ' ' + batch : '') + ' · 로그가 K 단위로 반올림한 근사값' : '합성 값' }}</div></dd>
                </div>
                <div v-if="has.ep" class="tr-cell">
                  <dt>에피소드 · <code class="tr-k">ep</code></dt>
                  <dd><div class="tr-cell__big">{{ fmtDec(lastOf('ep'), 2) }}</div><div class="tr-cell__note">{{ isReal ? '샘플 ÷ 에피소드 평균 프레임 수' : '합성 값' }}</div></dd>
                </div>
                <div v-if="has.epch" class="tr-cell">
                  <dt>에폭 · <code class="tr-k">epch</code></dt>
                  <dd><div class="tr-cell__big">{{ fmtDec(lastOf('epch'), 2) }}</div><div class="tr-cell__note">{{ isReal ? '샘플 ÷ 데이터셋 전체 프레임 수' : '합성 값' }}</div></dd>
                </div>
              </dl>

              <div v-if="lossPlot" class="tr-loss">
                <div class="tr-loss__head">
                  <span class="tr-loss__t">손실 <span class="rv-muted">· <code class="tr-k">loss</code></span></span>
                  <span class="tr-loss__now"><span class="tr-cell__big">{{ fmt.num(lossPlot.now) }}</span><span v-if="lossPlot.change" class="rv-small rv-muted">{{ lossPlot.change }}</span></span>
                </div>
                <tr-spark size="lg" :lines="lossPlot.lines" :dots="lossPlot.dots" :xr="xr" :yr="lossPlot.yr"
                          :ylabels="lossPlot.ylabels" :xlabels="lossPlot.xlabels" :aria="lossPlot.aria" />
                <div class="tr-legend">
                  <span class="tr-legend__i"><svg width="20" height="10" viewBox="0 0 20 10" aria-hidden="true"><path class="tr-sw--solid" d="M1 5H19"/></svg><span>학습 손실(<code class="tr-k">loss</code>)</span></span>
                  <span v-if="lossPlot.dots.length" class="tr-legend__i"><svg width="20" height="10" viewBox="0 0 20 10" aria-hidden="true"><path class="tr-sw--dot" d="M10 5h0"/></svg><span>검증 손실(<code class="tr-k">eval_loss</code>)</span></span>
                  <span v-else>검증 손실(<code class="tr-k">eval_loss</code>): 꺼짐 — 켜려면 학습 명령 끝에 <code class="tr-k">-- --eval_steps=1000 --dataset.eval_split=0.1</code></span>
                  <span>x축 = {{ xUnit }}</span>
                </div>
                <dl v-if="summary" class="tr-sum">
                  <div><dt>손실 개선율</dt><dd>{{ fmt.pct(summary.improvement) }} <span class="rv-muted">첫 기록 대비</span></dd></div>
                  <div><dt>최적 {{ xUnit }}</dt><dd>{{ summary.bestStep == null ? '—' : fmtInt(summary.bestStep) }} <span class="rv-muted">검증 {{ fmt.num(summary.bestVal) }}</span></dd></div>
                  <div>
                    <dt>일반화 갭 (검증 − 학습)</dt>
                    <dd :class="{ 'tr-warn': summary.gapRatio > 0.3 }">
                      <template v-if="summary.gapRatio > 0.3">▲ <span class="rv-sr-only">주의 </span></template>{{ fmt.num(summary.gap) }}
                      <span :class="summary.gapRatio > 0.3 ? '' : 'rv-muted'">학습 대비 {{ fmt.pct(summary.gapRatio) }}<template v-if="summary.gapRatio > 0.3"> · 크면 시연 암기(과적합)</template></span>
                    </dd>
                  </div>
                  <div><dt>학습 진단</dt><dd :class="{ 'tr-warn': summary.diag.warn }"><template v-if="summary.diag.warn">▲ <span class="rv-sr-only">주의 </span></template>{{ summary.diag.text }} <span class="rv-muted">최근 5점 추세 기준</span></dd></div>
                </dl>
              </div>

              <!-- 데모 결과(VAL MAE, SUCCESS@τ, 관절별 MAE) -->
              <div v-if="demoResult" class="tr-result">
                <h3>데모 결과</h3>
                <dl class="tr-cells">
                  <div class="tr-cell">
                    <dt>검증 MAE · <code class="tr-k">VAL MAE</code></dt>
                    <dd><div class="tr-cell__big">{{ fmt.num(demoResult.mae) }}°</div><div class="tr-cell__note">관절 각도가 평균 몇 도 틀리는가</div></dd>
                  </div>
                  <div class="tr-cell">
                    <dt>성공률 · <code class="tr-k">SUCCESS@τ</code></dt>
                    <dd><div class="tr-cell__big">{{ fmt.pct(demoResult.success) }}</div><div class="tr-cell__note">모든 관절이 {{ demoResult.thr }}° 이내인 비율</div></dd>
                  </div>
                </dl>
                <div v-if="demoResult.joints.length" class="tr-joints-wrap">
                  <h4 id="tr-joints-h">관절별 평균절대오차 (MAE, °)</h4>
                  <ul class="tr-joints" aria-labelledby="tr-joints-h">
                    <li v-for="j in demoResult.joints" :key="j.name">
                      <span class="tr-joints__n">{{ j.name }}</span>
                      <div class="rv-meter__bar" aria-hidden="true"><div class="rv-meter__fill" :class="{ 'tr-joints__fill--over': j.over }" :style="{ width: j.pct + '%' }"></div></div>
                      <span class="tr-joints__v" :class="{ 'tr-warn': j.over }"><template v-if="j.over">▲ <span class="rv-sr-only">임계값 초과 </span></template>{{ fmt.num(j.value) }}°</span>
                    </li>
                  </ul>
                </div>
              </div>

              <div v-if="panels.length" class="tr-panels">
                <article v-for="pn in panels" :key="pn.id" class="tr-panel">
                  <h3 class="tr-panel__t">{{ pn.title }} <span class="rv-muted">· <code class="tr-k">{{ pn.code }}</code></span></h3>
                  <div class="tr-panel__v" :class="{ 'tr-panel__v--multi': pn.vals.length > 1 }">
                    <span v-for="v in pn.vals" :key="v.key" class="tr-panel__val">
                      <svg v-if="v.sw" width="14" height="8" viewBox="0 0 14 8" aria-hidden="true"><path :class="'tr-sw--' + v.sw" d="M1 4H13"/></svg>
                      <span class="tr-panel__num">{{ v.text }}</span>
                      <code v-if="pn.vals.length > 1" class="tr-k tr-k--mut">{{ v.key }}</code>
                    </span>
                    <span v-if="pn.cap" class="tr-panel__cap">{{ pn.cap }}</span>
                  </div>
                  <tr-spark size="sm" :lines="pn.lines" :xr="xr" :yr="pn.yr" :ylabels="pn.ylabels" :xlabels="xl2" :aria="pn.aria" />
                  <p class="tr-panel__n"><template v-for="(p, i) in fmt.codeSpans(pn.note)" :key="i"><code v-if="p.code" class="tr-k">{{ p.text }}</code><template v-else>{{ p.text }}</template></template></p>
                </article>
              </div>

              <!-- GPU: scripts/train_act.py 가 5초마다 nvidia-smi 로 남긴 gpu.jsonl 의 최근 요약(학습 중일 때만 서버가 채움) -->
              <div v-if="gpu" class="tr-gpu">
                <div class="tr-gpu__head">
                  <h3>GPU</h3>
                  <p class="rv-small rv-muted">90% 이상 유지가 정상 · 들쭉날쭉하면 데이터 로딩 병목 — <code class="tr-k">num_workers</code> 조정</p>
                </div>
                <div class="tr-gpu__grid">
                  <div class="tr-gpu__item">
                    <span class="rv-small rv-muted">사용률 · 최근 평균</span>
                    <span class="tr-gpu__big">{{ gpu.util }}</span>
                    <div class="rv-meter__bar" role="img" :aria-label="'GPU 사용률 최근 평균 ' + gpu.util"><div class="rv-meter__fill" :style="{ width: gpu.utilPct + '%' }"></div></div>
                    <span class="rv-small rv-muted">현재 {{ gpu.utilNow }}</span>
                  </div>
                  <div class="tr-gpu__item">
                    <span class="rv-small rv-muted">전력 / 한계 (W)</span>
                    <span class="tr-gpu__big" :class="{ 'tr-warn': gpu.powerCap }">{{ gpu.power }}</span>
                    <rv-status v-if="gpu.powerCap" kind="warn" label="전력 제한 걸림 — 전원 연결·성능 모드 확인" />
                  </div>
                  <div class="tr-gpu__item">
                    <span class="rv-small rv-muted">온도</span>
                    <span class="tr-gpu__big" :class="{ 'tr-warn': gpu.thermal }">{{ gpu.temp }}</span>
                    <rv-status v-if="gpu.thermal" kind="warn" label="열 제한 걸림 — 냉각 확인" />
                  </div>
                  <div class="tr-gpu__item">
                    <span class="rv-small rv-muted">VRAM 사용 / 전체 (GB)</span>
                    <span class="tr-gpu__big">{{ gpu.vram }}</span>
                  </div>
                </div>
              </div>

              <p v-if="isReal" class="tr-foot">같은 지표를 W&amp;B에서도 볼 수 있음: 학습 명령 끝에 <code class="tr-k">-- --wandb.enable=true</code> (외부 서비스, 계정 필요)</p>
            </template>
          </section>
        </template>

        <!-- 데이터 준비도 -->
        <section class="rv-card" aria-labelledby="tr-ready-h">
          <div class="rv-card__head">
            <h2 id="tr-ready-h">데이터 준비도</h2>
            <span v-if="ready.goals" class="rv-card__aside">권장: ACT {{ ready.goals.act_episodes }} 에피소드 · 분류 클래스당 물체 {{ ready.goals.objects_per_class }}개</span>
          </div>
          <rv-state v-if="!ready.loaded && ready.loading" state="loading" />
          <rv-state v-else-if="!ready.loaded" state="error" :message="ready.error || '데이터 준비도를 불러오지 못했습니다.'" @retry="s.refreshReadiness()" />
          <template v-else>
            <div v-if="readyRows.length" class="tr-ready">
              <rv-meter v-for="r in readyRows" :key="r.name" :label="r.name" :value="r.n" :max="r.goal" :text="r.text" />
            </div>
            <rv-state v-else state="empty" message="준비도 기준(권장 개수)을 서버에서 받지 못했습니다." />
            <p v-if="ready.error" class="rv-small rv-muted"><rv-status kind="warn" label="갱신 실패 — 마지막으로 받은 값입니다" /></p>
          </template>
          <div class="tr-note">
            <rv-status kind="info" hide-label label="정보" />
            <p class="rv-small rv-muted">그리퍼를 포함한 새 데이터는 기존 omx/scan 과 관절 수가 달라 섞이면 변환이 깨집니다. 새 repo-id로 녹화·변환하세요.</p>
          </div>
        </section>
      </section>`,

    setup() {
      const s = RVS.store;
      const api = RVS.api;
      const fmt = RVS.fmt;

      const snap = shallowRef(null);        // 마지막 서버 스냅샷. records 가 커서 깊은 반응형으로 두지 않는다
      const mode = ref('lerobot');
      const latest = ref(undefined);        // undefined = 모름(조회 실패), null = 실행 기록 없음
      const latestError = ref(null);
      const loadError = ref(null);
      const loading = ref(true);
      const busy = ref(false);
      const params = reactive({ epochs: 50, threshold_deg: 5, lr: 0.0001 });
      const formErr = reactive({ field: '', msg: '' });
      const copied = ref(0);                // 0 = 없음, n = n번 명령 복사됨, -1 = 실패
      let es = null;                        // EventSource (반응형 밖)
      let retryTimer = null;
      let active = false;
      let seq = 0;

      /* ---------------- 서버 스냅샷 → 화면 ---------------- */
      const isReal = computed(() => mode.value === 'lerobot');
      // 이 모드의 기록만 보여준다. 서버가 다른 모드를 들고 있거나 쉬는 중이면 null
      const view = computed(() => {
        const t = snap.value;
        return t && t.state !== 'IDLE' && t.params && t.params.mode === mode.value ? t : null;
      });
      const running = computed(() => !!snap.value && snap.value.state === 'RUNNING');
      const recs = computed(() => (view.value ? view.value.records || [] : []));
      const last = computed(() => recs.value[recs.value.length - 1] || null);
      const runId = computed(() => (view.value && view.value.params.run_id) || null);
      const batch = computed(() => num(view.value && view.value.params.batch_size));
      const xUnit = computed(() => (isReal.value ? '스텝' : '에폭'));
      const total = computed(() => {
        const p = view.value && view.value.params;
        const t = p && num(isReal.value ? p.total_steps : p.epochs);
        return t > 0 ? t : null;
      });
      const pct = computed(() => (total.value && last.value ? Math.min(100, Math.round((last.value.epoch / total.value) * 100)) : 0));
      const progressAria = computed(() => (last.value ? '진행률 ' + pct.value + '%, ' + fmtInt(total.value) + xUnit.value + ' 중 ' + fmtInt(last.value.epoch) + xUnit.value : ''));

      const meter = (r, k) => num(r.meters && r.meters[k]);
      const lastOf = (k) => {
        const r = recs.value;
        for (let i = r.length - 1; i >= 0; i--) { const v = meter(r[i], k); if (v !== null) return v; }
        return null;
      };
      // 키가 한 번이라도 왔는지 — lerobot 버전마다 있는 키가 달라 없는 키는 그리지 않는다
      const has = computed(() => ({ smpl: lastOf('smpl') !== null, ep: lastOf('ep') !== null, epch: lastOf('epch') !== null }));
      const ser = (get) => {
        const xs = [], ys = [];
        for (const r of recs.value) { const v = get(r); if (v !== null) { xs.push(r.epoch); ys.push(v); } }
        return { xs, ys };
      };
      const xr = computed(() => (recs.value.length ? [recs.value[0].epoch, last.value.epoch] : [0, 0]));
      const xl2 = computed(() => [fmtInt(xr.value[0]), fmtInt(xr.value[1])]);

      const stat = computed(() => {
        const st = view.value ? view.value.state : 'IDLE';
        const real = isReal.value;
        if (st === 'RUNNING') return { kind: 'run', label: real ? '학습 중' : '데모 실행 중' };
        if (st === 'DONE') return { kind: 'stop', label: real ? '학습 완료' : '데모 완료' };
        if (st === 'STOPPED') return { kind: 'stop', label: real ? '따라 읽기 중단됨' : '데모 정지됨' };
        if (st === 'FAILED') return { kind: 'warn', label: '실패' };
        return { kind: 'stop', label: '대기' };
      });

      /* 남은 시간. batch 를 알면 남은 스텝 × batch ÷ smp/s, 모르면 남은 스텝 × 최근 5건 평균(updt_s + data_s).
         데모의 스텝은 에폭이라 로그 처리량(smp/s)으로 못 구하고, 지금까지 걸린 시간으로 구한다. */
      const eta = computed(() => {
        const v = view.value;
        if (!v || v.state !== 'RUNNING' || !total.value || !last.value) return null;
        const remain = Math.max(0, total.value - last.value.epoch);
        if (!isReal.value) {
          return v.elapsed_sec > 0 && last.value.epoch > 0
            ? { sec: (v.elapsed_sec / last.value.epoch) * remain, basis: '경과 시간' } : null;
        }
        const sps = lastOf('smp/s');
        if (batch.value > 0 && sps > 0) return { sec: (remain * batch.value) / sps, basis: 'smp/s' };
        const t = [];
        for (let i = recs.value.length - 1; i >= 0 && t.length < 5; i--) {
          const u = meter(recs.value[i], 'updt_s'), d = meter(recs.value[i], 'data_s');
          if (u !== null && d !== null) t.push(u + d);
        }
        return t.length ? { sec: (remain * t.reduce((a, b) => a + b, 0)) / t.length, basis: '최근 ' + t.length + '건 평균 updt_s+data_s' } : null;
      });
      const progressText = computed(() => {
        const v = view.value;
        if (!v) return '';
        const parts = [];
        if (total.value) parts.push(pct.value + '%');
        if (v.state === 'RUNNING') parts.push(eta.value ? '남은 시간 ' + fmtDur(eta.value.sec) + ' (' + eta.value.basis + ' 기준)' : '남은 시간 계산 불가');
        else parts.push({ DONE: '완료', STOPPED: isReal.value ? '따라 읽기 중단' : '데모 정지', FAILED: '실패' }[v.state] || '');
        if (!total.value) parts.push('총 ' + xUnit.value + ' 수를 알 수 없음');
        return parts.filter(Boolean).join(' · ');
      });

      /* ---------------- 큰 손실 차트 ---------------- */
      const lossPlot = computed(() => {
        const r = recs.value;
        if (!r.length) return null;
        const t = ser((x) => num(x.train_loss));
        const ev = r.filter((x) => num(x.val_loss) !== null);
        const nr = niceRange(...rangeOf([t.ys, ev.map((x) => x.val_loss)]));
        const f = (v) => v.toFixed(nr.dp);
        const first = t.ys[0], now = t.ys[t.ys.length - 1];
        const chg = first > 0 ? Math.round((now / first - 1) * 100) : null;
        return {
          now,
          change: chg === null ? '' : '시작 대비 ' + (chg > 0 ? '+' : chg < 0 ? '−' : '') + Math.abs(chg) + '%',
          lines: [t],
          dots: ev.map((x) => ({ x: x.epoch, y: x.val_loss })),
          yr: [nr.lo, nr.hi],
          ylabels: [f(nr.hi), f(nr.mid), f(nr.lo)],
          xlabels: [fmtInt(xr.value[0]), fmtInt((xr.value[0] + xr.value[1]) / 2), fmtInt(xr.value[1])],
          aria: '손실 곡선, ' + xUnit.value + ' ' + fmtInt(xr.value[0]) + '부터 ' + fmtInt(xr.value[1]) + ', 세로축 ' + f(nr.lo) + '부터 ' + f(nr.hi)
            + ', 시작 ' + fmt.num(first) + ', 현재 ' + fmt.num(now) + (ev.length ? ', 검증 손실 점 ' + ev.length + '개' : ''),
        };
      });

      /* 손실 요약 한 줄 — 검증 손실(eval)이 있을 때만. 레거시 ilMetrics 를 옮겼다.
         갭은 '마지막 점'이 아니라 가장 최근 검증 점 기준이다(실제 로그는 평가 줄이 드물다). */
      const summary = computed(() => {
        const r = recs.value;
        const ev = [...r].reverse().find((x) => num(x.val_loss) !== null);
        if (!ev) return null;
        const first = r[0], lastR = r[r.length - 1];
        const gap = ev.val_loss - ev.train_loss;
        const gapRatio = ev.train_loss > 0 ? gap / ev.train_loss : 0;
        const lastVal = lastR.val_loss ?? lastR.train_loss;
        let diag = { text: running.value ? '학습 중' : '대기', warn: false };
        if (r.length >= 6) {
          const prev = r[r.length - 6];
          const prevVal = prev.val_loss ?? prev.train_loss;
          const recent = prevVal > 0 ? (prevVal - lastVal) / prevVal : 0;
          if (recent < -0.01 && gapRatio > 0.3) diag = { text: '과적합 의심', warn: true };
          else if (recent < 0.02) diag = { text: '수렴 · 정체', warn: false };
          else diag = { text: '개선 중', warn: false };
        }
        if (view.value.state === 'DONE') diag = { ...diag, text: '완료 · ' + diag.text };
        return {
          improvement: first.train_loss > 0 ? 1 - lastR.train_loss / first.train_loss : null,
          bestStep: view.value.best_epoch, bestVal: view.value.best_val_loss, gap, gapRatio, diag,
        };
      });

      /* ---------------- 작은 패널 ---------------- */
      const gpu = computed(() => {
        const g = view.value && view.value.gpu;
        if (!g) return null;
        const st = g.latest || {};
        const n = (v, unit = '') => (v == null ? '—' : Math.round(v) + unit);
        const gb = (mib) => (mib == null ? '—' : (mib / 1024).toFixed(1));
        return {
          util: n(g.util_avg, '%'), utilPct: Math.min(100, g.util_avg ?? 0), utilNow: n(st.util, '%'),
          power: n(st.power) + ' / ' + n(st.power_limit), temp: n(st.temp, '°C'),
          vram: gb(st.mem_used) + ' / ' + gb(st.mem_total),
          powerCap: !!g.power_cap, thermal: !!g.thermal,
        };
      });
      const panels = computed(() => {
        const out = [];
        const xs = xl2.value;
        const aria = (title, lo, hi, cur, flat) => title + ' 추이, ' + xUnit.value + ' ' + xs[0] + '부터 ' + xs[1] + ', '
          + (flat ? '값 ' + cur + ' 일정' : '값 ' + lo + '부터 ' + hi + ', 현재 ' + cur);
        // mkf(lo, hi) 가 눈금·값 글자 만드는 함수를 돌려준다(자리수를 범위에 맞추려고)
        const single = (id, title, code, sr, mkf, note, cap) => {
          if (!sr.ys.length) return;
          const [lo, hi] = rangeOf([sr.ys]);
          const f = mkf(lo, hi), cur = sr.ys[sr.ys.length - 1];
          out.push({
            id, title, code, cap, note, vals: [{ key: code, text: f(cur) }], lines: [sr], yr: [lo, hi],
            ylabels: hi > lo ? [f(hi), f(lo)] : [f(cur)], aria: aria(title, f(lo), f(hi), f(cur), !(hi > lo)),
          });
        };
        const m = (k) => ser((r) => meter(r, k));
        const dp3 = () => (v) => fmt.num(v);
        single('l1', 'L1 손실', 'l1_loss', m('l1_loss'), dp3, '행동 재현 오차');
        single('kld', 'KL 손실', 'kld_loss', m('kld_loss'), dp3, '잠재변수 정규화 · loss = l1 + 가중치 × kld');
        single('grdn', '기울기 크기', 'grdn', m('grdn'), (lo, hi) => { const dp = hi >= 10 ? 1 : 2; return (v) => v.toFixed(dp); }, '튀면 학습 불안정');
        const lr = ser((r) => num(r.lr));
        single('lr', '학습률', 'lr', lr, () => (v) => v.toExponential(1),
          lr.ys.length && rangeOf([lr.ys]).every((v) => v === lr.ys[0]) ? '일정 · ACT 기본: 스케줄러 없음' : '값이 바뀌는 스케줄러를 쓰는 중');

        const upd = m('updt_s'), dat = m('data_s'), sps = lastOf('smp/s');
        if (upd.ys.length || dat.ys.length) {
          const [lo, hi] = rangeOf([upd.ys, dat.ys]);
          const f = (v) => v.toFixed(3);
          const vals = [];
          if (upd.ys.length) vals.push({ key: 'updt_s', text: f(upd.ys[upd.ys.length - 1]), sw: 'solid' });
          if (dat.ys.length) vals.push({ key: 'data_s', text: f(dat.ys[dat.ys.length - 1]), sw: 'dash' });
          if (sps !== null) vals.push({ key: 'smp/s', text: Math.round(sps).toString() });
          out.push({
            id: 'time', title: '스텝 시간', code: 'updt_s · data_s', vals, cap: '',
            lines: [{ ...upd, dash: false }, { ...dat, dash: true }].filter((l) => l.ys.length),
            yr: [lo, hi], ylabels: hi > lo ? [f(hi), f(lo)] : [f(hi)],
            aria: '스텝 시간 추이, ' + xUnit.value + ' ' + xs[0] + '부터 ' + xs[1] + ', 값 ' + f(lo) + '부터 ' + f(hi) + ', 실선 updt_s, 점선 data_s',
            note: '`data_s`가 `updt_s`보다 크면 데이터 로딩 병목 → `--num-workers` 늘리기',
          });
        }
        const g = view.value && view.value.gpu && view.value.gpu.latest;
        single('mem', 'GPU 메모리', 'mem_gb', m('mem_gb'), () => (v) => v.toFixed(1), 'CUDA가 있을 때만 기록',
          g && g.mem_total ? '/ ' + (g.mem_total / 1024).toFixed(1) + ' GB' : '');
        return out;
      });

      /* ---------------- 데모 결과(VAL MAE, SUCCESS@τ, 관절별 MAE) ---------------- */
      const demoResult = computed(() => {
        const l = last.value;
        if (isReal.value || !l || num(l.val_mae_deg) === null) return null;
        const thr = num(view.value.params.threshold_deg) ?? params.threshold_deg;
        const per = (l.val_mae_per_joint || []).filter((v) => num(v) !== null);
        const top = Math.max(...per, thr, 1);
        return {
          mae: l.val_mae_deg, success: l.val_success_rate, thr,
          joints: per.map((v, i) => ({ name: fmt.jointName(i), value: v, pct: (v / top) * 100, over: v > thr })),
        };
      });

      /* ---------------- 데이터 준비도 ---------------- */
      const ready = s.readiness;
      const readyRows = computed(() => {
        const g = ready.goals;
        if (!g) return [];
        const row = (name, n, goal) => ({ name, n, goal, text: n + ' / ' + goal });
        return [
          row('ACT 에피소드', ready.act_episodes, g.act_episodes),
          row('양품 물체', ready.good_objects, g.objects_per_class),
          row('불량 물체', ready.defect_objects, g.objects_per_class),
        ];
      });

      /* ---------------- 따라 읽기: 상태·SSE ---------------- */
      function apply(t) {
        if (!t) return;
        snap.value = t;
        // 돌고 있는 것이 있으면 그 모드를 보여준다. 쉬는 중에는 보기를 건드리지 않는다
        // (서버에 끝난 데모가 남아 있어도 처음엔 실제 로그 보기로 열려야 돌고 있는 실행에 자동으로 붙는다).
        if (t.state === 'RUNNING' && t.params && t.params.mode) mode.value = t.params.mode;
      }
      function closeStream() {
        if (es) { es.close(); es = null; }
        clearTimeout(retryTimer);
      }
      function openStream() {
        closeStream();
        if (!active) return;
        const src = new EventSource('/api/train/stream');
        es = src;
        src.onmessage = (ev) => {
          try { apply(JSON.parse(ev.data)); } catch { /* 부분 프레임은 무시 */ }
        };
        src.onerror = () => {
          // 학습이 끝나면 서버가 스트림을 닫는다(EventSource 는 자동 재연결하므로 먼저 닫는다). 끊긴 거면 상태를 확인하고 다시 붙는다.
          if (es === src) closeStream();
          api.get('/api/train/status').then((d) => {
            apply(d.training);
            if (active && d.training.state === 'RUNNING') retryTimer = setTimeout(openStream, 3000);
          }).catch(() => { if (active && running.value) retryTimer = setTimeout(openStream, 3000); });
        };
      }
      async function reloadLatest() {
        try {
          latest.value = (await api.get('/api/train/runs/latest')).run || null;
          latestError.value = null;
        } catch (err) {
          latestError.value = fmt.errText(err);
        }
      }
      // 서버가 쉬는데 가장 최근 실행이 돌고 있으면 붙는다. 사용자가 방금 끊은 실행에는 다시 붙지 않는다.
      function maybeAttach() {
        const t = snap.value, r = latest.value;
        if (!isReal.value || !r || r.status !== 'RUNNING' || !t || t.state === 'RUNNING') return;
        if (t.state === 'STOPPED' && t.params && t.params.run_id === r.run_id) return;
        startReal();
      }
      async function init() {
        const my = ++seq;
        loadError.value = null;
        try {
          apply((await api.get('/api/train/status')).training);
        } catch (err) {
          if (my === seq) { loadError.value = fmt.errText(err); loading.value = false; }
          return;
        }
        if (my !== seq || !active) return;
        if (running.value) openStream();
        await reloadLatest();
        if (my !== seq || !active) return;
        loading.value = false;
        maybeAttach();
        s.refreshReadiness();
      }

      async function post(path, body) {
        if (busy.value) return;
        busy.value = true;
        try {
          const d = await api.post(path, body);
          apply(d.training);
          return d.training;
        } catch (err) {
          s.notify(err.message, 'error');
          try { apply((await api.get('/api/train/status')).training); } catch { /* 화면은 마지막 값을 유지 */ }
        } finally {
          busy.value = false;
        }
      }
      async function startReal() {
        if (await post('/api/train/start', { demo: false })) openStream();
        else if (running.value) openStream();   // 이미 돌고 있다는 거절 — 붙기만 한다
      }
      function validate() {
        const { epochs, threshold_deg: thr, lr } = params;
        if (!Number.isInteger(epochs) || epochs < 1 || epochs > 500) return { field: 'epochs', msg: '에폭은 1~500 사이 정수여야 합니다.' };
        if (!(thr >= 0.5 && thr <= 30)) return { field: 'threshold_deg', msg: '성공 임계값은 0.5~30° 사이여야 합니다.' };
        if (!(lr >= 0.00001 && lr <= 0.01)) return { field: 'lr', msg: '학습률은 0.00001~0.01 사이여야 합니다.' };
        return { field: '', msg: '' };
      }
      async function runDemo() {
        Object.assign(formErr, validate());
        if (formErr.msg) return;
        const t = await post('/api/train/start', {
          epochs: params.epochs, threshold_deg: params.threshold_deg, lr: params.lr, demo: true, num_joints: 5,
        });
        if (t) { s.notify('데모 곡선 시작'); openStream(); } else if (running.value) openStream();
      }
      async function stop() {
        const t = await post('/api/train/stop');
        if (t) s.notify(isReal.value ? '따라 읽기 중단을 요청했습니다 (학습 프로세스는 계속 돕니다)' : '데모 정지를 요청했습니다');
      }
      function pickMode(v) {
        if (running.value) return;
        mode.value = v;
        Object.assign(formErr, { field: '', msg: '' });
        maybeAttach();
      }
      const modeOptions = computed(() => [
        { value: 'lerobot', label: '실제 학습 로그', disabled: running.value },
        { value: 'demo', label: '데모 곡선', disabled: running.value },
      ]);

      const copyMsg = computed(() => (copied.value > 0 ? copied.value + '번 명령을 복사했습니다.' : copied.value === -1 ? '복사하지 못했습니다. 명령을 직접 선택해 복사하세요.' : ''));
      function copy(i) {
        const done = (ok) => { copied.value = ok ? i + 1 : -1; };
        try { navigator.clipboard.writeText(CMDS[i]).then(() => done(true), () => done(false)); } catch { done(false); }
      }

      // keep-alive: 처음 들어올 때도 onActivated 가 불린다. 비활성이면 SSE 를 닫는다.
      onActivated(() => { active = true; init(); });
      onDeactivated(() => { active = false; seq++; closeStream(); });

      return {
        s, fmt, fmtInt, fmtDec, approx, lastOf,
        mode, isReal, modeOptions, pickMode, running, loading, loadError, init, busy, params, formErr,
        view, recs, last, runId, batch, xUnit, total, pct, progressAria, progressText, stat, has, xr, xl2,
        lossPlot, summary, panels, gpu, demoResult, ready, readyRows,
        // stale = 서버가 heartbeat 로 '래퍼가 강제 종료됨'이라 판정한 실행(없는 필드여도 동작)
        latest, latestError, reloadLatest, runStatusText: (r) => (r.stale ? '실패 · 래퍼가 강제 종료된 것으로 보임' : RUN_STATUS[r.status] || r.status || '—'),
        cmds: CMDS, copy, copyMsg, startReal, runDemo, stop,
      };
    },
  };
})();
