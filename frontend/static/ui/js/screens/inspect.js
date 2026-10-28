/* =============================================================
   검사 화면(#/inspect) — 라우트 inspect, 클래스 접두어 in-.
   레거시 app.js 의 스캔 흐름(촬영·재구성·CAD 기준 모델·배경 캡처·데모·업로드)을 호출 API·본문·확인창·
   잠금 키(busy.recon)까지 그대로 옮겼다. 달라진 것은 결과를 판정 근거·치수·뷰 품질 패널로 나눠 보이는 것,
   그리고 자동 촬영이 store(capture.auto, captureNow)에 있어 이 화면은 스위치와 결과(capture.last)만 다루는 것.

   - scan_viewer.js 는 수정 없이 쓴다. create() 가 컨테이너를 innerHTML='' 로 비우므로 칩·버튼은 컨테이너의 형제다.
   - keep-alive 로 가려져도 뷰어(WebGL 캔버스·시점)는 DOM 과 함께 남는다. 가려진 사이 도착한 결과(다른 화면에서
     켜 둔 자동 촬영)는 meshDirty 로 표시해 두었다가 다시 보일 때 싣는다.
   - stats 에서 알려진 키 밖의 값(서버가 새로 붙인 신뢰도·사유 등)은 버리지 않고 기술 정보에 그대로 보인다.
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});
  RVS.screens = RVS.screens || {};
  const { ref, reactive, computed, watch, nextTick, onActivated, onDeactivated, onBeforeUnmount } = Vue;

  // reconstruct3d.py·cad.py 가 보내는 stats 키. 이 밖의 키는 extras 로 모아 기술 정보에 그대로 보인다.
  const KNOWN_STATS = new Set([
    'num_views', 'views_used', 'dropped_views', 'view_scores', 'resolution', 'num_vertices', 'num_faces',
    'voxel_occupancy', 'silhouette_coverage', 'classification', 'num_defect_regions', 'anomaly',
    'mesh_method', 'view_dir_consistency', 'quality', 'size_mm', 'evidence',
    'source', 'file', 'scale_divisor', 'alignment_score', 'comparison_skipped', 'cad_fallback',
    'flagged_ratio', 'flagged_vertices', 'max_severity', 'mean_severity', 'deviation_ratio_per_view',
  ]);
  const KNOWN_QUALITY = new Set(['level', 'consistency', 'solidity', 'views_used', 'reasons', 'actions']);
  const KNOWN_ANOMALY = new Set(['flagged_ratio', 'max_severity', 'num_regions', 'per_view', 'skipped']);
  // 이런 이름의 새 필드는 기술 정보에 묻히지 않게 판정 근거 패널에도 올린다
  const NOTABLE = /reason|confidence|gate|warn|note|trust|why|skipped/i;

  const METHOD = { visual_hull: '정사영 근사', visual_hull_metric: '원근 투영(캘리브레이션)' };
  const BUSY_TEXT = {
    capture: '촬영하고 형상을 만드는 중입니다. 몇 초 걸립니다.',
    reconstruct: '저장된 물체로 형상을 만드는 중입니다.',
    demo: '데모 형상을 만드는 중입니다.',
    background: '배경을 저장하는 중입니다.',
    upload: '기준 모델을 올리는 중입니다.',
  };
  // 서버(core/inspections.py CAPTURE_ID_RE)의 촬영 ID 형식. 같은 초에 또 찍으면 _02, _03 … 접미사가 붙는다(JS \d 는 ASCII 숫자만이라 서버의 [0-9] 와 같다)
  const CAPTURE_ID = /^\d{8}_\d{6}(?:_\d{2,})?$/;

  const hasValue = (v) => v !== null && v !== undefined && v !== ''
    && !(Array.isArray(v) && v.length === 0)
    && !(typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0);
  const strings = (x) => (Array.isArray(x) ? x.filter((t) => typeof t === 'string' && t) : []);
  const mmText = (a) => a.map((v) => Number(v).toFixed(1)).join(' × ');

  function brief(v) {
    let text;
    if (Array.isArray(v)) text = v.every((x) => x === null || typeof x !== 'object') ? v.join(' · ') : JSON.stringify(v);
    else if (v && typeof v === 'object') text = JSON.stringify(v);
    else text = String(v);
    return text.length > 200 ? `${text.slice(0, 200)}…` : text;
  }

  function unknownEntries(obj, known, prefix) {
    if (!obj || typeof obj !== 'object') return [];
    return Object.entries(obj)
      .filter(([k, v]) => !known.has(k) && hasValue(v))
      .map(([k, v]) => ({ key: prefix + k, text: brief(v), notable: NOTABLE.test(k) }));
  }

  // [[x,y],…] → svg points. 서버 형식이 어긋나도 그림만 빠지고 화면은 죽지 않게 걸러 낸다
  function polyPoints(poly) {
    if (!Array.isArray(poly)) return '';
    return poly
      .filter((p) => Array.isArray(p) && Number.isFinite(+p[0]) && Number.isFinite(+p[1]))
      .map((p) => `${+p[0]},${+p[1]}`)
      .join(' ');
  }
  function evidenceOf(e) {
    if (!e || typeof e !== 'object') return null;
    const sil = polyPoints(e.silhouette);
    const anom = (Array.isArray(e.anomaly) ? e.anomaly : []).map(polyPoints).filter(Boolean);
    return sil || anom.length ? { sil, anom } : null;
  }

  RVS.screens.inspect = {
    name: 'InspectScreen',
    title: '검사',
    template: `
      <section class="in-screen" aria-labelledby="in-h">
        <header class="rv-head">
          <h1 id="in-h" tabindex="-1">검사</h1>
          <p>벨트가 멈추면 5대가 동시에 찍고, 실루엣으로 형상을 만들고, 표면의 어두운 자국·변색을 주황으로 표시합니다. 촬영만 하고 모터는 움직이지 않습니다.</p>
        </header>

        <div class="in-actions" role="group" aria-label="검사 동작">
          <button type="button" class="rv-btn rv-btn--primary" :disabled="capLocked" @click="captureScan"
                  title="컨베이어가 멈춘 상태에서 카메라 5대로 촬영해 형상을 만듭니다. 모터는 움직이지 않습니다.">
            {{ s.capture.busy ? '촬영 중…' : '촬영 → 형상 생성' }}
          </button>
          <span class="in-pick">
            <select v-model="selectedKey" aria-label="저장된 물체" :disabled="locked || s.busy.objects || !objects.length">
              <option :value="null" disabled>{{ pickText }}</option>
              <option v-for="o in objects" :key="o.label + '/' + o.object_id" :value="o.label + '/' + o.object_id">
                {{ o.object_id }} · {{ ko.label[o.label] || o.label }} ({{ o.views.length }}뷰)
              </option>
            </select>
            <button type="button" class="rv-btn" :disabled="!selectedKey || locked" @click="reconstruct">형상 생성</button>
            <button v-if="objectsError" type="button" class="rv-link" @click="refreshObjects">다시 시도</button>
          </span>
          <button type="button" class="rv-btn" :disabled="capLocked" @click="captureBackground"
                  title="물체를 치운 상태에서 한 번 눌러 빈 배경을 저장합니다. 배경이 복잡해도 물체만 분리됩니다.">배경 캡처</button>
          <rv-menu label="검사 더보기 — 기준 모델 등록, 데모 형상" :items="menuItems" :disabled="locked" @select="onMenu" />
          <input ref="modelInput" type="file" accept=".stl,.stp,.step" hidden @change="uploadModel">
          <div class="in-auto">
            <rv-switch :model-value="s.capture.auto" label="벨트 정지 시 자동 촬영" @change="setAuto" />
          </div>
        </div>

        <div class="in-meta">
          <span v-if="autoState" class="in-meta__item" role="status"><rv-status :kind="autoState.kind" :label="autoState.text" /></span>
          <span class="in-meta__item">
            <rv-status kind="info" :label="calibText" />
            <a v-if="!calib.calibrated" class="in-meta__link" href="#/diagnostics">보정 절차</a>
          </span>
          <span v-if="bgNote" class="in-meta__item"><rv-status :kind="bgNote.kind" :label="bgNote.text" /></span>
        </div>

        <section v-if="fail" class="rv-card in-fail" role="alert" aria-labelledby="in-fail-h">
          <h2 id="in-fail-h"><rv-status kind="warn" size="md" label="형상을 만들 수 없습니다" /></h2>
          <p>{{ fail.reason }}</p>
          <ul v-if="fail.reasons.length" class="in-list"><li v-for="(t, i) in fail.reasons" :key="'fr' + i">{{ t }}</li></ul>
          <ul v-if="fail.actions.length" class="in-list"><li v-for="(t, i) in fail.actions" :key="'fa' + i">{{ t }}</li></ul>
          <p v-else class="rv-small rv-muted">{{ fail.hint }}</p>
          <div class="in-fail__actions">
            <button v-if="fail.retry" type="button" class="rv-btn" :disabled="locked" @click="retryFail">다시 시도</button>
            <button v-if="fail.bg" type="button" class="rv-btn" :disabled="capLocked" @click="captureBackground">배경 다시 캡처</button>
            <button type="button" class="rv-link" @click="fail = null">닫기</button>
          </div>
        </section>

        <section v-if="!hasMesh && !fail" class="rv-card in-empty" aria-label="3D 형상">
          <rv-state v-if="locked" state="loading" :message="busyText" />
          <rv-state v-else state="empty" message="아직 만든 형상이 없습니다."
                    hint="컨베이어로 물체를 스캔 위치에 옮겨 멈춘 뒤 '촬영 → 형상 생성'을 누르세요. 저장된 물체는 골라서 '형상 생성', 데이터 없이 화면만 보려면 더보기의 '데모 형상'을 쓰세요.">
            <div class="in-empty__actions">
              <button type="button" class="rv-btn rv-btn--primary" :disabled="capLocked" @click="captureScan">촬영 → 형상 생성</button>
              <button type="button" class="rv-btn" @click="reconstructDemo">데모 형상</button>
            </div>
          </rv-state>
        </section>

        <p v-if="fail && hasMesh" class="in-stale">아래는 이전 결과입니다.</p>

        <div v-if="hasMesh" class="in-main">
          <div class="in-main__viewer">
            <div class="in-viewer">
              <div ref="viewerBox" class="in-viewer__canvas" role="img" :aria-label="viewerAria"></div>
              <span class="in-viewer__title rv-chip">{{ res.title }}<span v-if="res.ref" class="rv-id">{{ res.ref }}</span></span>
              <div v-if="viewerError" class="in-viewer__off">
                <rv-status kind="warn" label="3D 뷰어를 쓸 수 없습니다 — 아래 수치는 그대로 볼 수 있습니다" />
              </div>
              <div v-if="locked" class="in-viewer__busy" role="status">{{ busyText }}</div>
              <div class="in-viewer__foot">
                <span class="rv-chip">드래그 회전 · 스크롤 확대</span>
                <button type="button" class="rv-btn" @click="resetView">시점 초기화</button>
              </div>
            </div>
            <div class="in-legend">
              <span class="in-legend__item"><span class="in-legend__sw in-legend__sw--tex" aria-hidden="true"></span>흰·회색 = 촬영 텍스처</span>
              <span class="in-legend__item"><span class="in-legend__sw in-legend__sw--mesh" aria-hidden="true"></span>주황 = 표면 이상(어두운 자국) · 윤곽 결손</span>
            </div>
            <p v-if="anom.skipped" class="in-legend__note"><rv-status kind="info" label="주황 표시 생략 — 판정 근거의 사유를 확인하세요" /></p>
            <p v-if="res.stats.cad_fallback" class="in-legend__note"><rv-status kind="info" :label="'CAD 기준 모델과 비교하지 못해 스캔 형상을 보여 줍니다 — ' + res.stats.cad_fallback" /></p>
          </div>

          <div class="in-main__side">
            <section class="rv-card" aria-labelledby="in-verdict-h">
              <h2 id="in-verdict-h">판정 근거</h2>

              <div v-if="quality && quality.low" class="in-quality" role="group" aria-label="형상 신뢰도">
                <rv-status kind="warn" size="md" label="신뢰도 낮음" />
                <div v-if="quality.reasons.length" class="in-quality__sec">
                  <h3>이유</h3>
                  <ul class="in-list"><li v-for="(t, i) in quality.reasons" :key="'qr' + i">{{ t }}</li></ul>
                </div>
                <div v-if="quality.actions.length" class="in-quality__sec">
                  <h3>할 일</h3>
                  <ul class="in-list"><li v-for="(t, i) in quality.actions" :key="'qa' + i">{{ t }}</li></ul>
                </div>
              </div>

              <div class="in-verdict">
                <p v-if="verdict.kind === 'ok'" class="in-verdict__ok">{{ verdict.text }}</p>
                <rv-status v-else :kind="verdict.kind" size="md" :label="verdict.text" class="in-verdict__head" />
                <p v-if="verdict.sub" class="rv-small rv-muted rv-num">{{ verdict.sub }}</p>
              </div>

              <dl v-if="kvRows.length" class="in-kv">
                <div v-for="r in kvRows" :key="r.label">
                  <dt :class="{ 'in-kv__mono': r.keyCode }">{{ r.label }}</dt>
                  <dd><span :class="{ 'in-kv__mono': r.mono }">{{ r.text }}</span><span v-if="r.note" class="in-kv__note">{{ r.note }}</span></dd>
                </div>
              </dl>

              <p class="rv-small rv-muted">최종 판정은 사람이 확정합니다. 라벨은 <a href="#/collect">수집 화면</a>의 검사 샘플에서 지정합니다.</p>
            </section>

            <section class="rv-card" aria-labelledby="in-dim-h">
              <h2 id="in-dim-h">치수</h2>
              <template v-if="sizeText">
                <p class="in-size">{{ sizeText }} mm</p>
                <p class="rv-small rv-muted">{{ isCad ? 'STL 단위를 mm 로 가정' : 'visual hull 이라 실제보다 클 수 있음' }}</p>
              </template>
              <template v-else>
                <p class="rv-muted">치수 없음 — 치수는 리그 캘리브레이션 후 mm로 표시됩니다.</p>
                <div><a class="in-link" href="#/diagnostics">보정 절차</a></div>
              </template>
            </section>

            <section class="rv-card">
              <details class="in-tech">
                <summary>기술 정보</summary>
                <dl class="in-kv">
                  <div v-for="r in techRows" :key="r.label">
                    <dt :class="{ 'in-kv__mono': r.keyCode }">{{ r.label }}</dt>
                    <dd><span :class="{ 'in-kv__mono': r.mono }">{{ r.text }}</span><span v-if="r.note" class="in-kv__note">{{ r.note }}</span></dd>
                  </div>
                </dl>
              </details>
            </section>
          </div>
        </div>

        <section v-if="hasMesh && views.length" class="in-views" aria-labelledby="in-views-h">
          <div class="in-views__head">
            <h2 id="in-views-h" class="rv-h2">뷰 품질</h2>
            <span class="rv-small rv-muted">점선 = 검사 ROI<template v-if="hasEvidence"> · 흰 다각형 = 실루엣 · 주황 = 이상 기여</template></span>
          </div>
          <p class="rv-small rv-muted">일치도 = 나머지 뷰로 깎은 형상이 이 뷰의 실루엣을 얼마나 맞히는지(0~1). 나머지와 어긋나는 뷰는 자동으로 제외합니다.</p>
          <ul class="in-views__grid">
            <li v-for="v in views" :key="v.slot" class="in-view" :class="{ 'in-view--dropped': v.dropped }">
              <div class="rv-cam">
                <img v-if="v.src && !thumbFail[v.slot]" class="rv-cam__img" :src="v.src" :alt="v.name + ' 촬영본'" @error="thumbFail[v.slot] = true">
                <span v-else class="in-view__none">촬영본 없음</span>
                <svg v-if="v.roi || v.evidence" class="rv-cam__roi" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true" focusable="false">
                  <polygon v-if="v.evidence && v.evidence.sil" class="in-ev in-ev--sil" :points="v.evidence.sil" />
                  <polygon v-for="(p, i) in (v.evidence ? v.evidence.anom : [])" :key="i" class="in-ev in-ev--anom" :points="p" />
                  <template v-if="v.roi">
                    <rect class="rv-cam__roi-bg" v-bind="v.roi" />
                    <rect class="rv-cam__roi-line" v-bind="v.roi" />
                  </template>
                </svg>
              </div>
              <div class="in-view__body">
                <div class="in-view__row">
                  <span class="in-view__name">{{ v.name }}</span>
                  <span class="in-view__sil">실루엣 {{ v.sil }}</span>
                </div>
                <div class="in-view__score">일치도 {{ v.score }}</div>
                <div class="in-view__badge" :class="{ 'in-view__badge--drop': v.dropped, 'in-view__badge--anom': !v.dropped && v.anomaly && !anom.skipped }">
                  <rv-status v-if="v.dropped" kind="warn" label="제외됨" />
                  <rv-status v-else-if="v.anomaly && !anom.skipped" kind="warn" label="이상 기여" />
                  <span v-else-if="anom.skipped" role="img" aria-label="이상 표시 생략" class="rv-muted">—</span>
                  <span v-else role="img" aria-label="이상 기여 없음" class="rv-muted">—</span>
                </div>
              </div>
            </li>
          </ul>
        </section>
      </section>`,

    setup() {
      const s = RVS.store;
      const { api, fmt } = RVS;
      const { ko } = fmt;

      /* ---------------- 상태 ---------------- */
      const objects = ref([]);
      const objectsError = ref('');
      const selectedKey = ref(null);        // "label/object_id"
      const res = ref(null);                // { stats, title, ref, captureId } — 메시 본체는 res 밖(mesh)
      const fail = ref(null);
      const busyKind = ref(null);
      const viewerError = ref(false);
      const calib = reactive({ loaded: false, error: false, calibrated: false, created_at: null });
      const thumbFail = reactive({});
      const viewerBox = ref(null);
      const modelInput = ref(null);

      let viewer = null;       // Three.js 뷰어 핸들. reactive 로 감싸면 안 된다
      let mesh = null;         // 수 MB 라 반응형 밖에 둔다
      let meshDirty = false;   // 뷰어에 아직 안 실은 메시가 있다
      let active = false;
      let handledCapture = null;

      const menuItems = [
        { id: 'model', label: '기준 모델 등록' },
        { id: 'demo', label: '데모 형상' },
      ];

      /* ---------------- 파생 값 ---------------- */
      const hasMesh = computed(() => !!res.value);
      const stats = computed(() => (res.value && res.value.stats) || null);
      const isCad = computed(() => !!stats.value && stats.value.source === 'cad_stl');
      const locked = computed(() => s.busy.recon || s.capture.busy);
      // 사이클(시연 세션 등)이 도는 동안은 수동 촬영·배경 캡처를 잠근다. store 에 없으면 false
      const capLocked = computed(() => locked.value || !!s.cycleRunning);
      const busyText = computed(() => BUSY_TEXT[busyKind.value] || BUSY_TEXT.capture);

      const pickText = computed(() => {
        if (s.busy.objects) return '물체 목록 불러오는 중…';
        if (objectsError.value) return '물체 목록을 불러오지 못했습니다';
        return objects.value.length ? '저장된 물체 선택' : '저장된 물체 없음';
      });

      const camLabel = (slot) => {
        const c = (s.config.cameras || []).find((x) => x.slot === slot);
        return fmt.camName(c ? c.label : '') || slot;
      };
      const camShort = (slot) => camLabel(slot).split(' ')[0];
      const roiRect = (slot) => {
        const c = (s.config.cameras || []).find((x) => x.slot === slot);
        const r = c && c.roi;
        return Array.isArray(r) && r.length === 4 ? { x: r[0], y: r[1], width: r[2] - r[0], height: r[3] - r[1] } : null;
      };

      const calibText = computed(() => {
        if (calib.error) return '리그 캘리브레이션 상태를 읽지 못했습니다';
        if (!calib.loaded) return '리그 캘리브레이션 확인 중…';
        return calib.calibrated
          ? `리그 캘리브레이션 ${fmt.dateTime(calib.created_at)} — 원근 투영, 치수(mm) 표시`
          : '리그 캘리브레이션 없음 — 형상은 근사치, 치수(mm) 표시 안 함';
      });

      // 배경 캡처 시각은 API 가 없다. 서버가 계산하는 알림(없음·7일 경과)으로 상태만 말한다.
      const bgNote = computed(() => {
        if (!s.alertsLoaded) return null;
        const list = s.alerts.alerts;
        if (list.some((a) => a.id === 'background_missing')) {
          return { kind: 'warn', text: '배경 캡처 없음 — 물체를 치운 뒤 배경 캡처를 먼저 하세요' };
        }
        const stale = list.find((a) => a.id === 'background_stale');
        if (stale) {
          const at = stale.detail && stale.detail.evidence && stale.detail.evidence.captured_at;
          return {
            kind: 'info',
            text: `${stale.title}${at ? ` · ${fmt.dateTime(at)} 캡처` : ''} — 조명이나 배치가 바뀌었으면 다시 찍으세요`,
          };
        }
        return { kind: 'info', text: '배경 캡처 있음' };
      });

      const autoState = computed(() => {
        if (!s.capture.auto) return null;
        if (s.capture.busy) return { kind: 'run', text: '자동 촬영 — 촬영 중…' };
        const running = s.conveyors.filter((b) => b.running).length;
        const last = s.capture.last ? ` · 마지막 촬영 ${fmt.clock(s.capture.last.at)}` : '';
        return {
          kind: 'info',
          text: (running ? `자동 촬영 켜짐 — 벨트 ${running}개 동작 중, 멈추면 촬영합니다` : '자동 촬영 켜짐 — 벨트가 멈추면 촬영합니다') + last,
        };
      });

      // 표면 이상. 품질 게이트(quality.level==='low')나 CAD 정합 실패로 '표시를 생략'했으면 비율을 판정처럼 보이지 않는다.
      const quality = computed(() => {
        const st = stats.value;
        const q = st && st.quality;
        if (!q || typeof q !== 'object') return null;       // 구버전 응답 — 줄을 숨긴다(값을 지어내지 않는다)
        const views = typeof q.views_used === 'number' ? q.views_used : (Array.isArray(st.views_used) ? st.views_used.length : null);
        return {
          low: q.level === 'low',
          level: q.level,
          reasons: strings(q.reasons),
          actions: strings(q.actions),
          views,
        };
      });

      const anom = computed(() => {
        const st = stats.value;
        if (!st) return { skipped: null, ratio: null, regions: 0, severity: null };
        const a = st.anomaly || {};
        const lowQ = quality.value && quality.value.low;
        return {
          skipped: st.comparison_skipped || a.skipped
            || (lowQ ? '형상 신뢰도가 낮아 표면 이상 표시를 신뢰할 수 없습니다.' : null),
          ratio: st.flagged_ratio ?? a.flagged_ratio ?? null,
          regions: Math.max(Number(st.num_defect_regions) || 0, Number(a.num_regions) || 0),
          severity: st.max_severity ?? a.max_severity ?? null,
        };
      });

      const verdict = computed(() => {
        const st = stats.value;
        const a = anom.value;
        if (!st) return { kind: 'info', text: '', sub: '' };
        if (a.skipped) return { kind: 'info', text: '표면 이상 판단 불가', sub: a.skipped };
        if (isCad.value && a.ratio === null) return { kind: 'info', text: '기준 모델만 표시 중', sub: '스캔과 비교하지 않았습니다' };
        const flagged = (a.ratio ?? 0) > 0 || a.regions > 0;
        // 0 인 값은 '이상 없음'일 때만 보인다(외부 검출 영역만 있는 데모에 '정점 0.0%' 가 붙으면 모순처럼 읽힌다)
        const sub = [
          a.ratio !== null && (a.ratio > 0 || !flagged) ? `이상 정점 ${fmt.pct(a.ratio)}` : '',
          a.severity > 0 ? `최대 심각도 ${fmt.num(a.severity, 2)}` : '',
        ].filter(Boolean).join(' · ');
        if (flagged) {
          return { kind: 'warn', text: a.regions > 0 ? `표면 이상 ${a.regions}곳 · 불량 의심` : '표면 이상 · 불량 의심', sub };
        }
        return { kind: 'ok', text: '표면 이상 없음', sub };
      });

      const extras = computed(() => {
        const st = stats.value;
        if (!st) return [];
        return [
          ...unknownEntries(st, KNOWN_STATS, ''),
          ...unknownEntries(st.quality, KNOWN_QUALITY, 'quality.'),
          ...unknownEntries(st.anomaly, KNOWN_ANOMALY, 'anomaly.'),
        ];
      });

      const kvRows = computed(() => {
        const st = stats.value;
        if (!st) return [];
        const rows = [];
        if (isCad.value) {
          rows.push({ label: '기준 모델', text: st.file || 'CAD', mono: true });
        } else {
          const cl = st.classification || {};
          rows.push({
            label: '라벨',
            text: ko.label[cl.label] || '미분류',
            note: cl.confidence ? `신뢰도 ${fmt.pct(cl.confidence)}` : 'CNN 미연결',
          });
          const used = Array.isArray(st.views_used) ? st.views_used.length : st.num_views;
          const dropped = Object.keys(st.dropped_views || {});
          rows.push({
            label: '카메라 일치도',
            text: fmt.num(st.view_dir_consistency, 2),
            note: `${used}뷰 사용${dropped.length ? ` · ${dropped.map(camShort).join(', ')} 제외` : ''}`,
          });
        }
        const q = quality.value;
        if (q && !q.low) {
          rows.push({
            label: '신뢰도',
            text: q.level === 'ok' ? '정상' : String(q.level),
            note: q.views !== null ? `사용 카메라 ${q.views}대` : '',
          });
        }
        extras.value.filter((e) => e.notable).forEach((e) => rows.push({ label: e.key, text: e.text, keyCode: true }));
        return rows;
      });

      const sizeText = computed(() => {
        const m = stats.value && stats.value.size_mm;
        return Array.isArray(m) && m.length ? mmText(m) : '';
      });

      const techRows = computed(() => {
        const st = stats.value;
        if (!st) return [];
        const rows = [];
        const add = (label, text, extra = {}) => {
          if (text !== undefined && text !== null && text !== '') rows.push({ label, text: String(text), ...extra });
        };
        const n = (v) => (v === undefined || v === null ? undefined : Number(v).toLocaleString('ko-KR'));
        add('정점 수', n(st.num_vertices));
        add('면 수', n(st.num_faces));
        add('정합 일치도', st.alignment_score);
        add('mesh_method', st.mesh_method, { mono: true, note: METHOD[st.mesh_method] || '' });
        add('해상도', st.resolution, { note: st.resolution !== undefined ? '복셀 격자 한 변' : '' });
        add('볼록도', st.quality && st.quality.solidity);
        extras.value.forEach((e) => add(e.key, e.text, { keyCode: true }));
        return rows;
      });

      const views = computed(() => {
        const st = stats.value;
        if (!st || (!st.silhouette_coverage && !st.view_scores)) return [];
        const cov = st.silhouette_coverage || {};
        const scores = st.view_scores || {};
        const dropped = st.dropped_views || {};
        const per = (st.anomaly && st.anomaly.per_view) || {};
        const evidence = st.evidence || {};
        const names = [...new Set([...Object.keys(cov), ...Object.keys(scores), ...Object.keys(dropped)])].sort();
        const capId = res.value.captureId;
        return names.map((slot) => ({
          slot,
          name: camLabel(slot),
          sil: fmt.pct(cov[slot]),
          score: fmt.num(slot in scores ? scores[slot] : dropped[slot], 2),
          dropped: slot in dropped,
          anomaly: Number(per[slot]) > 0,
          roi: roiRect(slot),
          // 촬영 원본은 inspections/<capture_id>/<cam>.jpg. 저장된 물체·데모에는 촬영 ID 가 없다
          src: capId ? `/api/inspections/${encodeURIComponent(capId)}/${slot}.jpg` : null,
          evidence: evidenceOf(evidence[slot]),
        }));
      });
      const hasEvidence = computed(() => views.value.some((v) => v.evidence));

      const viewerAria = computed(() => `3D 형상 뷰어. ${verdict.value.text}. 마우스로 회전하고 스크롤로 확대합니다.`);

      /* ---------------- 3D 뷰어 ---------------- */
      function syncViewer(tries = 0) {
        if (!active || !res.value || !viewerBox.value) return;
        const el = viewerBox.value;
        if (!el.clientWidth || !el.clientHeight) {          // 방금 보이게 됐으면 레이아웃이 한 틱 늦을 수 있다
          if (tries < 10) requestAnimationFrame(() => syncViewer(tries + 1));
          return;
        }
        if (!viewer) {
          // ScanViewer 는 scan_viewer.js 최상위 const 라 window 속성이 아니다 — 이름으로만 접근된다
          if (!window.THREE || typeof ScanViewer === 'undefined') {
            viewerError.value = true;
            s.notify('3D 라이브러리(Three.js) 로드 실패 — 네트워크를 확인하세요', 'error');
            return;
          }
          try {
            viewer = ScanViewer.create(el);
          } catch (err) {
            viewerError.value = true;
            s.notify(`3D 뷰어를 만들 수 없습니다 — ${err.message}`, 'error');
            return;
          }
        }
        if (meshDirty) {
          viewer.loadMesh(mesh);
          meshDirty = false;
        }
      }

      function applyMesh(m, info) {
        mesh = m;
        meshDirty = true;
        Object.keys(thumbFail).forEach((k) => delete thumbFail[k]);
        res.value = { stats: m.stats || {}, title: info.title, ref: info.ref || '', captureId: info.captureId || null };
        fail.value = null;
        nextTick(() => syncViewer());
      }

      const resetView = () => { if (viewer) viewer.reset(); };

      /* ---------------- 실패 표시 ---------------- */
      function hintOf(message, code) {
        if (code === 'empty_visual_hull' || /실루엣|배경과 거의 구분/.test(message)) {
          return { hint: '배경 캡처가 최신인지, 물체가 모든 카메라에 담겼는지 확인하세요.', bg: true };
        }
        if (/컨베이어가 움직이는/.test(message)) return { hint: '벨트가 완전히 멈춘 뒤 다시 촬영하세요.', bg: false };
        if (/카메라|프레임/.test(message)) return { hint: '카메라 연결과 신호를 확인하세요(장비 제어 화면).', bg: false };
        return { hint: '서버 상태와 카메라·컨베이어 연결을 확인한 뒤 다시 시도하세요.', bg: false };
      }

      // 서버가 품질 게이트 사유(reasons·actions)를 에러 detail 에 실어 보내면 그걸 쓴다
      function showFail(err, retry) {
        const text = typeof err === 'string';
        const message = text ? err : fmt.errText(err);
        const d = (!text && err.detail) || {};
        const q = d.quality || d;
        fail.value = {
          reason: message,
          reasons: strings(q.reasons),
          actions: strings(q.actions),
          ...hintOf(message, text ? null : err.code),
          retry: retry || null,
        };
      }

      function retryFail() {
        const again = fail.value && fail.value.retry;
        fail.value = null;
        if (again) again();
      }

      /* ---------------- 동작(레거시 app.js 의 스캔 흐름) ---------------- */
      // 촬영·재구성·업로드·배경·데모가 서로 잠근다(레거시 busy.recon 하나).
      async function run(kind, fn) {
        if (s.busy.recon) return;
        s.busy.recon = true;
        busyKind.value = kind;
        try {
          await fn();
        } finally {
          s.busy.recon = false;
          busyKind.value = null;
        }
      }

      async function refreshObjects() {
        s.busy.objects = true;
        try {
          objects.value = (await api.get('/api/scan/objects')).objects;
          objectsError.value = '';
          if (selectedKey.value && !objects.value.some((o) => `${o.label}/${o.object_id}` === selectedKey.value)) {
            selectedKey.value = null;
          }
        } catch (err) {
          objectsError.value = fmt.errText(err);
        } finally {
          s.busy.objects = false;
        }
      }

      async function refreshCalibration() {
        try {
          Object.assign(calib, await api.get('/api/scan/calibration'), { loaded: true, error: false });
        } catch {
          calib.error = true;
        }
      }

      /* 컨베이어가 멈춘 지금의 5뷰를 찍어 형상을 만든다. 서버가 벨트 정지를 재확인한다.
         요청·잠금·토스트는 store.captureNow 가 하고, 결과는 capture.last 를 지켜보는 watch 가 싣는다. */
      async function captureScan() {
        if (s.cycleRunning) return;
        busyKind.value = 'capture';
        const data = await s.captureNow();
        busyKind.value = null;
        if (!data && !s.capture.busy) showFail(s.capture.error || '촬영에 실패했습니다.', captureScan);
      }

      function applyCapture(d) {
        if (d && d.mesh) applyMesh(d.mesh, { title: '촬영', ref: d.capture_id, captureId: d.capture_id });
      }

      function reconstruct() {
        if (!selectedKey.value) return undefined;
        const [label, objectId] = selectedKey.value.split('/');
        const lab = ko.label[label] || label;
        return run('reconstruct', async () => {
          try {
            // CAD 기준 모델이 있으면 그걸 쓴다. 스캔으로 만든 형상보다 설계 형상이 정확하다 —
            // 5뷰 visual hull 은 오목부를 복원하지 못한다. 없으면(400) 스캔 재구성으로 넘어간다.
            let cad = null;
            try {
              cad = await api.get(`/api/scan/model/${objectId}?label=${label}`);
            } catch { /* 기준 모델 없음 */ }
            if (cad) {
              const st = cad.mesh.stats;
              if (st.cad_fallback) {
                // CAD 를 자세에 못 맞춰 서버가 스캔 형상(표면 이상 주황 포함)으로 대신 보냈다.
                applyMesh(cad.mesh, { title: `스캔 형상 (CAD 정합 실패) · ${lab}`, ref: objectId });
                s.notify(`${st.cad_fallback} — 대신 스캔 형상을 보여줍니다. ${fmt.scanNotes(st)}`, 'error');
                return;
              }
              applyMesh(cad.mesh, { title: 'CAD 기준 모델', ref: objectId });
              if (st.comparison_skipped) {
                // 정합 실패를 조용히 넘기면 '불량 없음' 으로 오해한다. 색이 안 뜬 이유를 말한다.
                s.notify(st.comparison_skipped, 'error');
              } else if (st.flagged_ratio > 0) {
                s.notify(`CAD 대비 차이 ${(st.flagged_ratio * 100).toFixed(1)}% (정합 일치도 ${st.alignment_score}) — 주황 = 윤곽 결손·표면 자국 위치`);
              } else {
                s.notify(`CAD 모델 로드 — ${st.file} (${mmText(st.size_mm)} mm)`);
              }
              return;
            }

            const data = await api.post('/api/scan/reconstruct', {
              object_id: objectId,
              label,
              // 서버 상한값. 복셀이 잘아져 계단이 덜 보인다. 대가는 전송량(정점 5.6만개에 JSON 5MB, 실측).
              resolution: 96,
            });
            applyMesh(data.mesh, { title: `저장된 물체 · ${lab}`, ref: objectId });
            s.notify(`3D 재구성 완료 — ${fmt.scanNotes(data.mesh.stats)}`,
                     data.mesh.stats.anomaly && data.mesh.stats.anomaly.skipped ? 'error' : undefined);
          } catch (err) {
            showFail(err, reconstruct);
          }
        });
      }

      // #/inspect?capture=<id> — 그 촬영으로 형상을 만든다. 서버가 capture_id 재구성을 아직 모르면 안내만 한다.
      function openCapture(id) {
        if (!CAPTURE_ID.test(id)) {
          showFail(`촬영 ID 형식이 올바르지 않습니다: ${id}`, null);
          return undefined;
        }
        return run('reconstruct', async () => {
          try {
            const data = await api.post('/api/scan/reconstruct', { capture_id: id, resolution: 96 });
            applyMesh(data.mesh, { title: '촬영', ref: id, captureId: id });
            s.notify(`3D 재구성 완료 — ${fmt.scanNotes(data.mesh.stats)}`,
                     data.mesh.stats.anomaly && data.mesh.stats.anomaly.skipped ? 'error' : undefined);
          } catch (err) {
            if (err.status === 400 && /object_id/.test(err.message)) {
              // 지금 서버는 object_id 만 받는다
              fail.value = {
                reason: `이 서버는 촬영 ID(${id})로 형상을 다시 만드는 기능이 아직 없습니다.`,
                reasons: [],
                actions: [],
                hint: "새로 촬영하려면 '촬영 → 형상 생성', 저장된 물체는 골라서 '형상 생성'을 누르세요.",
                bg: false,
                retry: null,
              };
            } else {
              showFail(err, () => openCapture(id));
            }
          }
        });
      }

      function handleQuery() {
        const id = s.query && s.query.capture;
        if (!id) { handledCapture = null; return; }
        if (id === handledCapture) return;
        handledCapture = id;
        openCapture(id);
      }

      function reconstructDemo() {
        return run('demo', async () => {
          try {
            // 데이터 없이도 뷰어·하이라이트를 확인한다. defect 로 요청해 주황 오버레이를 본다.
            const data = await api.post('/api/scan/reconstruct_demo', { label: 'defect', resolution: 48 });
            applyMesh(data.mesh, { title: '데모 형상 — 합성 5뷰', ref: '' });
            s.notify('데모 형상 생성 — 주황 영역이 불량 부위입니다');
          } catch (err) {
            showFail(err, reconstructDemo);
          }
        });
      }

      async function captureBackground() {
        if (s.cycleRunning) return undefined;
        // window.confirm 은 떠 있는 동안 상단바 비상 정지까지 막는다
        if (!(await s.askConfirm({
          title: '배경 캡처',
          message: '스캔대에서 물체를 모두 치운 뒤 확인을 누르세요.\n지금 보이는 화면을 "빈 배경"으로 저장합니다.',
        }))) return undefined;
        return run('background', async () => {
          try {
            const data = await api.post('/api/scan/background');
            s.notify(`배경 저장 완료 — ${data.cameras.join(', ')} (${data.cameras.length}대)`);
            s.refreshAlerts();
          } catch (err) {
            s.notify(err.message, 'error');
          }
        });
      }

      /* 기준 CAD 모델 업로드. 선택된 물체의 object_id 로 저장된다. */
      function pickModelFile() {
        if (!selectedKey.value) {
          s.notify('먼저 저장된 물체를 선택하세요. 그 물체의 기준 모델로 등록됩니다.', 'error');
          return;
        }
        if (modelInput.value) modelInput.value.click();
      }

      async function uploadModel(event) {
        const file = event.target.files && event.target.files[0];
        event.target.value = '';          // 같은 파일을 다시 골라도 change 가 뜨게
        if (!file || !selectedKey.value) return;
        const objectId = selectedKey.value.split('/')[1];
        let uploaded = false;
        await run('upload', async () => {
          try {
            const d = await api.upload(`/api/scan/model/${objectId}`, file);
            s.notify(`${objectId} 기준 모델 등록 — ${d.file} (면 ${d.num_faces}개, ${mmText(d.size_mm)} mm)`);
            uploaded = true;
          } catch (err) {
            s.notify(err.message, 'error');
          }
        });
        if (uploaded) await reconstruct();    // 바로 띄워서 확인시킨다
      }

      function onMenu(id) {
        if (id === 'model') pickModelFile();
        else if (id === 'demo') reconstructDemo();
      }

      const setAuto = (v) => { s.capture.auto = v; };

      /* ---------------- 연결 ---------------- */
      // 자동 촬영(store)이 다른 화면에 있는 사이 끝났어도 capture.last 로 따라잡는다
      watch(() => s.capture.last, applyCapture, { immediate: true });
      // 같은 문구가 되풀이되면 값이 안 바뀌어 안 뜨지만, 수동 촬영의 실패는 captureScan 이 직접 보인다
      watch(() => s.capture.error, (msg) => { if (msg) showFail(msg, captureScan); });
      watch(() => s.query.capture, () => { if (active && s.route === 'inspect') handleQuery(); });

      onActivated(() => {
        active = true;
        refreshObjects();
        refreshCalibration();
        nextTick(() => syncViewer());
        handleQuery();
      });
      onDeactivated(() => {
        active = false;
        handledCapture = null;
      });
      onBeforeUnmount(() => {
        if (viewer) viewer.dispose();
        viewer = null;
      });

      return {
        s, ko, objects, objectsError, selectedKey, res, fail, calib, thumbFail, viewerBox, modelInput, viewerError,
        menuItems, hasMesh, isCad, locked, capLocked, busyText, pickText, calibText, bgNote, autoState,
        quality, anom, verdict, kvRows, sizeText, techRows, views, hasEvidence, viewerAria,
        refreshObjects, captureScan, reconstruct, reconstructDemo, captureBackground, uploadModel,
        onMenu, setAuto, resetView, retryFail,
      };
    },
  };
})();
