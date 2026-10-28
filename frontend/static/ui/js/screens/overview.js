/* =============================================================
   개요 화면 (#/overview) — 클래스 접두어 ov-
   초안 Main.dc.html 의 본문(알림 상세 창·사이드바·상단바는 셸 몫).
   값은 전부 서버에서 온다. 이 화면이 직접 부르는 API 는 /api/inspections?limit=1 하나뿐이고,
   나머지는 RVS.store 의 폴링 결과(status, ports, conveyors, alerts, config, readiness)를 읽기만 한다.
   MJPEG 스트림은 연결하지 않는다 — 촬영 썸네일은 일반 JPEG 요청이다.
   ============================================================= */
window.RVS = window.RVS || {};
RVS.screens = RVS.screens || {};

RVS.screens.overview = {
  name: 'OverviewScreen',
  title: '개요',
  template: `
    <section class="ov-screen" aria-labelledby="ov-h">
      <header class="rv-head">
        <h1 id="ov-h" tabindex="-1">개요</h1>
        <p>정상인 것은 회색으로 조용히, 이상한 것만 색으로 보입니다.</p>
      </header>

      <section class="ov-banner" aria-label="스테이션 상태">
        <svg class="ov-banner__icon" viewBox="0 0 44 44" aria-hidden="true" focusable="false">
          <template v-if="banner.tone === 'ok'">
            <circle class="ov-ic-line" cx="22" cy="22" r="19"/>
            <path class="ov-ic-line" d="M14 22.5l5.5 5.5L30 17"/>
          </template>
          <rect v-else-if="banner.tone === 'fault'" class="ov-ic-fault" x="5" y="5" width="34" height="34" rx="6"/>
          <path v-else-if="banner.tone === 'warn'" class="ov-ic-warn" d="M22 4L41 38H3z"/>
          <circle v-else class="ov-ic-wait" cx="22" cy="22" r="19"/>
        </svg>
        <div class="ov-banner__main">
          <div class="ov-banner__title" aria-live="polite">{{ banner.title }}</div>
          <div v-if="banner.sub" class="ov-banner__sub">{{ banner.sub }}</div>
          <div v-if="banner.meta" class="ov-banner__meta">{{ banner.meta }}</div>
          <ul v-if="banner.chips" class="ov-states" role="list" aria-label="작동 상태">
            <li v-for="c in banner.chips" :key="c.key"><rv-status :kind="c.kind" :label="c.label" /></li>
          </ul>
        </div>
        <button v-if="banner.action" type="button" class="rv-btn" :class="{ 'rv-btn--primary': banner.action.primary }" @click="banner.action.run($event)">{{ banner.action.label }}</button>
      </section>

      <div class="ov-row">
        <section class="rv-card ov-map" aria-labelledby="ov-map-h">
          <div class="rv-card__head">
            <h2 id="ov-map-h">스테이션</h2>
            <span class="rv-card__aside">● 가동 · ○ 정지 · ▲ 주의 · ■ 고장</span>
          </div>
          <rv-state v-if="mapWait" :state="mapWait.state" :message="mapWait.message" @retry="mapWait.retry()" />
          <template v-else>
            <svg class="ov-map__svg" role="img" :aria-label="mapLabel" viewBox="0 0 760 300" focusable="false">
              <defs>
                <symbol id="ov-sym-run" viewBox="0 0 12 12"><circle cx="6" cy="6" r="5"/></symbol>
                <symbol id="ov-sym-stop" viewBox="0 0 12 12"><circle cx="6" cy="6" r="4.25"/></symbol>
                <symbol id="ov-sym-warn" viewBox="0 0 14 13"><path d="M7 1L13 12H1z"/></symbol>
                <symbol id="ov-sym-fault" viewBox="0 0 12 12"><rect x="1.5" y="1.5" width="9" height="9" rx="1.5"/></symbol>
              </defs>

              <rect class="ov-m-belt" x="16" y="118" width="252" height="58" rx="8"/>
              <template v-if="rig.feed">
                <use :href="'#ov-sym-' + rig.feed.kind" x="27" y="91" width="14" height="14" :class="'ov-sym ov-sym--' + rig.feed.kind"/>
                <text class="ov-t" x="48" y="103">{{ rig.feed.name }}</text>
                <text :class="rig.feed.kind === 'warn' ? 'ov-t2 ov-t--warn' : 'ov-t2'" x="34" y="152">{{ rig.feed.text }}</text>
              </template>

              <rect class="ov-m-zone" x="290" y="96" width="172" height="104" rx="12"/>
              <text class="ov-t2" x="300" y="84">검사 구역 (여기서 멈춰 촬영)</text>
              <line v-for="c in rig.rays" :key="'ray-' + c.key" class="ov-m-ray" x1="376" y1="148" :x2="c.x" :y2="c.y"/>
              <circle class="ov-m-ring" cx="376" cy="148" r="30"/>
              <rect class="ov-m-cube" x="356" y="128" width="40" height="40" rx="4"/>
              <g v-for="c in rig.cams" :key="c.key">
                <use :href="'#ov-sym-' + c.kind" :x="c.x - 7" :y="c.y - 7" width="14" height="14" :class="'ov-sym ov-sym--' + c.kind"/>
                <text :class="c.kind === 'warn' ? 'ov-t2 ov-t--warn' : 'ov-t2'" :x="c.lx" :y="c.ly" :text-anchor="c.anchor">{{ c.name }}</text>
              </g>

              <rect class="ov-m-belt" x="484" y="118" width="260" height="58" rx="8"/>
              <template v-if="rig.work">
                <use :href="'#ov-sym-' + rig.work.kind" x="495" y="91" width="14" height="14" :class="'ov-sym ov-sym--' + rig.work.kind"/>
                <text class="ov-t" x="516" y="103">{{ rig.work.name }}</text>
                <text :class="rig.work.kind === 'warn' ? 'ov-t2 ov-t--warn' : 'ov-t2'" x="502" y="152">{{ rig.work.text }}</text>
              </template>

              <template v-if="rig.lm">
                <text v-if="rig.lm.pos" class="ov-t2" x="514" y="200">{{ rig.lm.pos.text }}</text>
                <rect class="ov-m-rail" x="514" y="212" width="210" height="10" rx="5"/>
                <rect v-if="rig.lm.pos" class="ov-m-carriage" :x="514 + rig.lm.pos.ratio * 207" y="206" width="3" height="22" rx="1.5"/>
                <use :href="'#ov-sym-' + rig.lm.kind" x="514" y="239" width="14" height="14" :class="'ov-sym ov-sym--' + rig.lm.kind"/>
                <text :class="rig.lm.kind === 'warn' ? 'ov-t2 ov-t--warn' : 'ov-t2'" x="534" y="250">{{ rig.lm.name }} · {{ rig.lm.text }}</text>
              </template>

              <template v-if="rig.follower">
                <line class="ov-m-link" x1="700" y1="58" x2="700" y2="116"/>
                <circle class="ov-m-arm" cx="700" cy="40" r="18"/>
                <use :href="'#ov-sym-' + rig.follower.kind" x="693" y="33" width="14" height="14" :class="'ov-sym ov-sym--' + rig.follower.kind"/>
                <text class="ov-t" x="672" y="45" text-anchor="end">{{ rig.follower.name }} · {{ rig.follower.text }}</text>
              </template>
              <template v-if="rig.leader">
                <circle class="ov-m-arm" cx="52" cy="40" r="18"/>
                <use :href="'#ov-sym-' + rig.leader.kind" x="45" y="33" width="14" height="14" :class="'ov-sym ov-sym--' + rig.leader.kind"/>
                <text class="ov-t" x="80" y="45">{{ rig.leader.name }} · {{ rig.leader.text }}</text>
              </template>
            </svg>

            <div class="ov-maplist">
              <section v-for="g in mapGroups" :key="g.key" :aria-labelledby="'ov-ml-' + g.key">
                <h3 :id="'ov-ml-' + g.key" class="ov-maplist__h">{{ g.title }}</h3>
                <ul role="list">
                  <li v-for="it in g.items" :key="it.key" class="ov-maplist__row">
                    <span class="ov-maplist__name">{{ it.name }}</span>
                    <rv-status :kind="it.kind" :label="it.full" />
                  </li>
                </ul>
              </section>
            </div>

            <div class="ov-ports">
              <rv-status v-if="portsDown.length" kind="warn" :label="'포트 ' + s.ports.connected_count + '/' + s.ports.total_count + ' 연결'" />
              <span v-else>포트 {{ s.ports.connected_count }}/{{ s.ports.total_count }} 연결</span>
              <span v-if="portsDown.length" class="ov-ports__down">연결 안 됨: {{ portsDown.join(', ') }}</span>
              <a v-if="portsDown.length" class="rv-link" href="#/diagnostics" @click.prevent="s.go('diagnostics')">진단에서 확인</a>
            </div>
          </template>
        </section>

        <section id="ov-alerts" class="rv-card ov-alerts" aria-labelledby="ov-alarm-h">
          <div class="rv-card__head">
            <h2 id="ov-alarm-h">확인 필요</h2>
            <span v-if="s.alertsLoaded" class="rv-card__aside">고장 {{ s.alerts.counts.fault }} · 주의 {{ s.alerts.counts.warn }} · 정보 {{ s.alerts.counts.info }}</span>
          </div>
          <rv-state v-if="!s.alertsLoaded && s.alertsError" state="error" :message="s.alertsError" @retry="s.refreshAlerts()" />
          <rv-state v-else-if="!s.alertsLoaded" state="loading" message="알림을 불러오는 중…" />
          <template v-else>
            <rv-state v-if="s.alertsError" state="error" :message="'알림을 갱신하지 못했습니다(마지막 목록 표시 중) — ' + s.alertsError" @retry="s.refreshAlerts()" />
            <rv-state v-if="!alertList.length" state="empty" message="확인할 알림이 없습니다." hint="문제가 생기면 원인과 해결 방법이 여기에 나타납니다." />
            <ul v-else class="ov-alerts__list" role="list">
              <li v-for="a in alertList" :key="a.id">
                <button type="button" class="ov-alert" aria-haspopup="dialog" @click="s.openAlert(a.id, $event)">
                  <rv-status class="ov-alert__sev" :kind="a.severity" size="md" hide-label :label="ko.severity[a.severity]" />
                  <span class="ov-alert__body">
                    <span class="ov-alert__top">
                      <span class="ov-alert__title">{{ a.title }}</span>
                      <span v-if="s.isAcked(a.id)" class="rv-chip rv-chip--sm">확인됨</span>
                    </span>
                    <span class="ov-alert__sum">{{ a.summary }}</span>
                  </span>
                  <span class="ov-alert__more">자세히<span aria-hidden="true"> ›</span></span>
                </button>
              </li>
            </ul>
          </template>
          <p class="ov-alerts__foot rv-small rv-muted">항목을 누르면 원인과 해결 방법이 열립니다.</p>
        </section>
      </div>

      <div class="ov-grid">
        <section class="rv-card ov-cams" aria-labelledby="ov-fps-h">
          <div class="rv-card__head">
            <h2 id="ov-fps-h">카메라 프레임</h2>
            <span class="rv-card__aside">음영 = 목표 fps의 {{ Math.round(fmt.FPS_LOW_RATIO * 100) }}% 이상</span>
          </div>
          <rv-state v-if="camWait" :state="camWait.state" :message="camWait.message" @retry="camWait.retry()" />
          <ul v-else class="ov-list" role="list">
            <li v-for="c in camRows" :key="c.key">
              <rv-gauge v-if="c.connected" :label="c.name" :value="c.fps" :min="0" :max="c.max" :normal="c.normal" unit=" fps" :decimals="1" out-text="프레임 낮음" />
              <div v-else class="ov-off">
                <span class="ov-off__name">{{ c.name }}</span>
                <span class="ov-off__state">
                  <rv-status kind="warn" label="연결 안 됨" />
                  <span v-if="c.error" class="rv-small rv-muted">{{ c.error }}</span>
                </span>
              </div>
            </li>
          </ul>
        </section>

        <section class="rv-card ov-joints" aria-labelledby="ov-joint-h">
          <div class="rv-card__head">
            <h2 id="ov-joint-h">팔로워 관절</h2>
            <span class="rv-card__aside">막대 = 소프트 한계 전 구간 · 연한 눈금 = 리더</span>
          </div>
          <rv-state v-if="jointWait" :state="jointWait.state" :message="jointWait.message" @retry="jointWait.retry()" />
          <rv-state v-else-if="!followerUp" state="empty" message="팔로워 팔이 연결돼 있지 않습니다." hint="연결되면 관절 각도가 여기에 표시됩니다. 연결 상태는 장비 제어에서 확인하세요.">
            <a class="rv-link" href="#/equipment" @click.prevent="s.go('equipment')">장비 제어로 이동</a>
          </rv-state>
          <ul v-else class="ov-list" role="list">
            <li v-for="j in jointRows" :key="j.key">
              <rv-gauge :label="j.name" :value="j.value" :ghost="j.ghost" ghost-label="리더" :min="j.min" :max="j.max" :normal="j.normal" center :format="fmt.deg" out-text="한계 이탈" />
            </li>
          </ul>
        </section>

        <section class="rv-card ov-sync" aria-labelledby="ov-sync-h">
          <h2 id="ov-sync-h">동기화 · 위치</h2>
          <div class="ov-block">
            <div class="ov-block__row">
              <span>카메라 동기 오차</span>
              <span class="ov-big">{{ skewNum }}<span class="ov-big__unit"> ms</span></span>
            </div>
            <rv-state v-if="syncWait" :state="syncWait.state" :message="syncWait.message" @retry="syncWait.retry()" />
            <template v-else>
              <rv-gauge v-if="skewTol && skew !== null" :value="skew" :min="0" :max="skewTol * 2" :normal="[0, skewTol]" unit=" ms" :decimals="1" :show-value="false" out-text="허용 초과" />
              <span v-if="skew === null" class="rv-small rv-muted">측정할 수 없습니다. 카메라 2대 이상이 프레임을 보내야 계산됩니다.</span>
              <span v-else-if="skewTol" class="rv-small rv-muted">허용 {{ skewTol }} ms 이하(설정 max_sync_skew_ms) · 넘으면 주의로 바뀜</span>
              <span v-else class="rv-small rv-muted">허용치를 읽지 못해 범위를 표시하지 않습니다.</span>
            </template>
          </div>

          <div v-for="a in axes" :key="a.key" class="ov-block">
            <div class="ov-block__row">
              <span>{{ a.label }} 위치</span>
              <span class="ov-big">{{ a.mm === null ? '—' : Math.round(a.mm) }}<span class="ov-big__unit"> / {{ Math.round(a.stroke) }} mm</span></span>
            </div>
            <rv-gauge :value="a.mm" :min="0" :max="a.stroke" unit=" mm" :decimals="0" :show-value="false" out-text="범위 이탈" />
          </div>

          <div class="ov-block ov-ready">
            <span>학습 데이터 준비도</span>
            <rv-state v-if="!s.readiness.loaded && s.readiness.error" state="error" :message="s.readiness.error" @retry="s.refreshReadiness()" />
            <rv-state v-else-if="!s.readiness.loaded" state="loading" message="준비도를 불러오는 중…" />
            <template v-else>
              <rv-meter v-for="r in readyRows" :key="r.key" :label="r.label" :value="r.value" :max="r.goal || 1" :text="r.goal ? undefined : r.value + '개'" />
              <rv-state v-if="s.readiness.error" state="error" :message="'준비도를 갱신하지 못했습니다 — ' + s.readiness.error" @retry="s.refreshReadiness()" />
              <a class="rv-link ov-ready__go" href="#/collect" @click.prevent="s.go('collect')">수집 화면으로 이동</a>
            </template>
          </div>
        </section>
      </div>

      <section class="rv-card ov-cap" aria-labelledby="ov-cap-h">
        <div class="rv-card__head">
          <h2 id="ov-cap-h">최근 검사 촬영</h2>
          <span v-if="cap.total !== null" class="rv-card__aside">전체 {{ cap.total }}건<template v-if="cap.unlabeled"> · 라벨 없음 {{ cap.unlabeled }}건</template></span>
        </div>
        <rv-state v-if="cap.state === 'loading'" state="loading" message="최근 촬영을 불러오는 중…" />
        <rv-state v-else-if="cap.state === 'error'" state="error" :message="cap.error" @retry="loadCap()" />
        <rv-state v-else-if="cap.state === 'empty'" state="empty" message="아직 검사 촬영이 없습니다." hint="검사 화면에서 컨베이어를 멈추고 촬영하면 최근 촬영이 여기에 나타납니다.">
          <a class="rv-btn rv-btn--sm" href="#/inspect" @click.prevent="s.go('inspect')">검사로 이동</a>
        </rv-state>
        <template v-else>
          <rv-state v-if="cap.error" state="error" :message="'촬영 목록을 갱신하지 못했습니다(마지막 값 표시 중) — ' + cap.error" @retry="loadCap()" />
          <a class="ov-cap__link" :href="'#/inspect?capture=' + cap.item.capture_id" :aria-label="'촬영 ' + cap.item.capture_id + ' 검사 화면에서 열기'" @click.prevent="s.go('inspect', { capture: cap.item.capture_id })">
            <span class="ov-cap__meta">
              <span class="rv-num">{{ fmt.dateTime(cap.item.captured_at, { seconds: true }) }}</span>
              <code class="rv-id">{{ cap.item.capture_id }}</code>
              <span class="rv-chip rv-chip--sm">{{ capLabel }}</span>
              <span class="ov-cap__open">검사에서 열기<span aria-hidden="true"> ›</span></span>
            </span>
            <span class="ov-cap__thumbs">
              <span v-for="t in capThumbs" :key="t.key" class="ov-cap__fig">
                <span v-if="broken[t.id]" class="ov-cap__miss">이미지 없음</span>
                <img v-else class="ov-cap__img" :src="t.src" :alt="t.label + ' 촬영 썸네일'" loading="lazy" decoding="async" @error="broken[t.id] = true">
                <span class="ov-cap__cam rv-small rv-muted">{{ t.label }}</span>
              </span>
            </span>
          </a>
          <p class="rv-small rv-muted">원본은 <code>inspections/</code> 폴더에 보존되며 결과는 열 때 다시 계산합니다.</p>
        </template>
      </section>
    </section>`,

  setup() {
    const s = RVS.store;
    const { fmt, api } = RVS;
    const { ko } = fmt;
    const { reactive, computed, watch, onMounted, onActivated, onDeactivated, onBeforeUnmount } = Vue;

    /* ---------- 판정 기준 (값이 아니라 표시 규칙이다) ---------- */
    // 카메라 정상 fps 하한은 fmt.FPS_LOW_RATIO(장비 제어·진단과 같은 기준)
    const FPS_MAX = 1.1;       // 트랙 오른쪽 끝(바늘은 여기서 멈춘다)
    const FPS_NORMAL_HI = 3;   // 목표를 넘는 쪽은 이상이 아니다 — 측정 구간이 짧으면 순간적으로 튄다(시뮬레이션에서 22fps 실측)
    const JOINT_PAD = 0.1;     // 트랙을 한계 양옆으로 10% 넓혀야 한계를 넘은 바늘이 음영 밖에 보인다
    // 홈 자세가 관절 1 한계 끝(180°)이라 센서 잡음 ±1° 로 상시 '한계 이탈'이 뜨는 걸 막는다(표시값만 한계로 붙인다)
    const JOINT_SLACK = 1;
    const SEV_ORDER = { fault: 0, warn: 1, info: 2 };

    /* ---------- 로딩·오류 판정 (스토어 폴링 기반 블록) ---------- */
    async function reload() {
      const results = await Promise.allSettled([s.refreshStatus(), s.refreshConveyors(), s.refreshAlerts(), s.refreshReadiness(), loadCap()]);
      const bad = results.find((r) => r.status === 'rejected');
      if (bad) s.notify(fmt.errText(bad.reason), 'error');
    }
    // 서버가 안 닿으면 오류, 아직 첫 값 전이면 로딩. 값이 있으면 null(= 데이터를 그린다)
    const wait = () => (s.online
      ? { state: 'loading', message: '불러오는 중…', retry: reload }
      : { state: 'error', message: '서버에 연결할 수 없습니다.', retry: reload });

    const statusReady = computed(() => s.status.cameras.length > 0);
    const rigReady = computed(() => s.ports.loaded && s.conveyors.length > 0 && statusReady.value);
    const mapWait = computed(() => (rigReady.value ? null : wait()));
    const camWait = computed(() => (statusReady.value ? null : wait()));
    const syncWait = computed(() => (statusReady.value ? null : wait()));
    const jointWait = computed(() => {
      if (!statusReady.value) return wait();
      if (s.config.limits) return null;
      return s.config.error
        ? { state: 'error', message: '관절 한계를 불러오지 못했습니다 — ' + s.config.error, retry: () => s.refreshConfig() }
        : { state: 'loading', message: '관절 한계를 불러오는 중…', retry: () => s.refreshConfig() };
    });

    /* ---------- 최근 검사 촬영 (이 화면이 직접 읽는 유일한 API) ---------- */
    const cap = reactive({ state: 'loading', item: null, total: null, unlabeled: null, error: null });
    const broken = reactive({});
    let capBusy = false;
    let capAgain = false;   // 진행 중에 새 촬영이 생기면 끝난 뒤 한 번 더 읽는다(진행 중 응답은 그 촬영 이전 것일 수 있다)
    async function loadCap() {
      if (capBusy) { capAgain = true; return; }
      capBusy = true;
      try {
        const d = await api.get('/api/inspections?limit=1');
        cap.item = (d.inspections || [])[0] || null;
        cap.total = d.summary ? d.summary.total : null;
        cap.unlabeled = d.summary ? d.summary.unlabeled : null;
        cap.error = null;
        cap.state = cap.item ? 'ready' : 'empty';
      } catch (err) {
        cap.error = fmt.errText(err);
        if (!cap.item) cap.state = 'error';
      } finally {
        capBusy = false;
        if (capAgain) { capAgain = false; loadCap(); }
      }
    }
    const camLabel = (slot) => {
      const c = s.status.cameras.find((x) => x.name === slot);
      return c ? fmt.camName(c) : slot;
    };
    const capThumbs = computed(() => {
      const it = cap.item;
      if (!it) return [];
      return (it.cameras || []).map((slot) => ({
        key: slot,
        id: it.capture_id + '/' + slot,
        label: camLabel(slot),
        src: '/api/inspections/' + encodeURIComponent(it.capture_id) + '/' + encodeURIComponent(slot) + '.jpg',
      }));
    });
    const capLabel = computed(() => {
      const l = cap.item && cap.item.label;
      return l ? (ko.label[l.label] || l.label) : '라벨 없음';
    });

    // 15초 폴링은 화면이 보일 때만(keep-alive 로 가려지면 멈춘다). 새 촬영은 store.capture.last 로 즉시 따라간다.
    let timer = null;
    function startPolling() {
      if (timer) return;
      loadCap();
      timer = setInterval(loadCap, 15000);
    }
    function stopPolling() {
      clearInterval(timer);
      timer = null;
    }
    onMounted(startPolling);
    onActivated(startPolling);
    onDeactivated(stopPolling);
    onBeforeUnmount(stopPolling);
    watch(() => s.capture.last, loadCap);

    /* ---------- 알림 ---------- */
    const alertList = computed(() => [...s.alerts.alerts].sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9)));

    /* ---------- 상태 배너 ---------- */
    const beltsRunning = computed(() => s.conveyors.filter((b) => b.running).length);
    const camsUp = computed(() => s.status.cameras.filter((c) => c.connected).length);
    const demoOn = computed(() => !!s.status.demo && s.status.demo.state !== 'IDLE');

    // 알림 목록은 10초 주기라 늦을 수 있다. 상태 폴링이 이미 알려준 고장은 바로 배너에 올린다.
    const fault = computed(() => {
      const first = alertList.value.find((a) => a.severity === 'fault');
      if (first) return { id: first.id, title: first.title, summary: first.summary };
      const { teleop, autorun, demo } = s.status;
      if (teleop.fault) return { id: null, title: '텔레오퍼레이션 고장', summary: String(teleop.fault) };
      if (autorun.fault) return { id: null, title: '자율 실행 고장', summary: String(autorun.fault) };
      // demo.fault 는 '지난 세션이 끝난 이유'로 다음 시작 전까지 남는다 — 세션이 도는 동안만 고장으로 본다
      if (demo && demo.fault && demo.state !== 'IDLE') return { id: null, title: '시연 녹화 고장', summary: String(demo.fault) };
      return null;
    });

    const chips = computed(() => {
      const { teleop, recording: rec, autorun, demo } = s.status;
      const on = (v, fault) => (fault ? 'fault' : v ? 'run' : 'stop');
      const word = (v, fault) => (fault ? '고장' : v ? '켜짐' : '꺼짐');
      let recChip;
      if (demo && demo.state !== 'IDLE') {
        recChip = { kind: demo.fault ? 'fault' : 'run', label: '시연 녹화 ' + (demo.state === 'RECORDING' ? '녹화 중' : '리셋 중') + ' · ' + (demo.saved ?? 0) + '건 저장' };
      } else if (rec.state === 'IDLE') {
        recChip = { kind: 'stop', label: '녹화 대기' };
      } else {
        recChip = { kind: 'run', label: (ko.rec[rec.state] || rec.state) + (rec.state === 'RECORDING' ? ' ' + fmt.time(rec.elapsed_sec) : '') };
      }
      return [
        { key: 'teleop', kind: on(teleop.running, teleop.fault), label: '텔레오퍼레이션 ' + word(teleop.running, teleop.fault) },
        { key: 'rec', ...recChip },
        { key: 'autorun', kind: on(autorun.running, autorun.fault), label: '자율 실행 ' + word(autorun.running, autorun.fault) },
      ];
    });

    const meta = computed(() => {
      const p = [];
      if (s.conveyors.length) p.push(beltsRunning.value ? '벨트 ' + s.conveyors.length + '대 중 ' + beltsRunning.value + '대 가동 중' : '벨트 ' + s.conveyors.length + '대 정지');
      if (s.status.cameras.length) p.push('카메라 ' + camsUp.value + '/' + s.status.cameras.length + ' 연결');
      if (s.alerts.counts.warn) p.push('주의 ' + s.alerts.counts.warn + '건');
      if (cap.item) p.push('마지막 검사 촬영 ' + fmt.dateTime(cap.item.captured_at));
      return p.join(' · ');
    });

    const banner = computed(() => {
      const retry = { label: '다시 시도', run: reload };
      if (!s.online) return { tone: 'warn', title: '서버 연결 끊김', sub: '위험 항목을 확인할 수 없습니다. 서버가 켜져 있는지 확인하세요.', action: retry };
      if (!s.alertsLoaded && s.alertsError) {
        return { tone: 'warn', title: '알림 상태를 확인하지 못했습니다', sub: s.alertsError, action: { label: '다시 시도', run: () => s.refreshAlerts() } };
      }
      if (!s.alertsLoaded || !statusReady.value) return { tone: 'wait', title: '상태를 불러오는 중…' };

      const stale = s.alertsError ? '알림 갱신에 실패해 마지막으로 받은 목록 기준입니다.' : null;
      if (fault.value) {
        const f = fault.value;
        return {
          tone: 'fault', title: f.title, sub: f.summary, meta: meta.value, chips: chips.value,
          action: f.id ? { label: '자세히 보기', primary: true, run: (e) => s.openAlert(f.id, e) } : null,
        };
      }
      let doing = '대기 중';
      if (s.status.autorun.running) doing = '자율 실행 중';
      else if (demoOn.value || s.status.recording.state === 'RECORDING') doing = '녹화 중';
      else if (s.status.teleop.running) doing = '텔레오퍼레이션 중';
      else if (beltsRunning.value) doing = '벨트 가동 중';
      return {
        tone: 'ok', title: doing + ' — 위험 항목 없음', sub: stale, meta: meta.value, chips: chips.value,
        action: { label: '검사로 이동', primary: true, run: () => s.go('inspect') },
      };
    });

    /* ---------- 리그 지도 + 목록 ---------- */
    const beltItem = (b) => {
      let kind = 'stop';
      let text = '정지';
      if (b.enabled === false) { kind = 'warn'; text = '드라이버 꺼짐'; }
      else if (b.running) { kind = 'run'; text = '가동 ' + (Number(b.rpm) || 0).toFixed(1) + ' RPM'; }
      else if (!b.connected) text = '정지 · 포트 닫힘';
      // 가동 거리가 있는 축(LM 가이드)만 위치를 가진다. 위치를 모르면(pos_rev 없음) 아무것도 그리지 않는다.
      const mm = b.stroke_mm > 0 && b.mm_per_rev > 0 && b.pos_rev != null ? b.pos_rev * b.mm_per_rev : null;
      const pos = mm === null ? null : {
        ratio: Math.min(1, Math.max(0, mm / b.stroke_mm)),
        text: '위치 ' + Math.round(mm) + ' / ' + Math.round(b.stroke_mm) + ' mm',
      };
      return {
        key: b.name, name: (b.label || b.name) + (b.motor ? ' M' + b.motor : ''), kind, text, pos,
        full: pos ? text + ' · ' + pos.text : text,
      };
    };
    const mapBelts = computed(() => s.conveyors.map(beltItem));

    // 카메라 슬롯 → 지도 좌표. 기구 배치는 데이터가 아니라 구조라서 슬롯 이름(config.yaml)으로 고정한다.
    // 목록에 없는 슬롯(글로벌 캠 등)은 위쪽 줄에 차례로 놓는다.
    const CAM_POS = {
      cam1: { x: 406, y: 148, lx: 418, ly: 152, anchor: 'start', ray: false },
      cam3: { x: 300, y: 246, lx: 290, ly: 272, anchor: 'end', ray: true },
      cam2: { x: 334, y: 232, lx: 324, ly: 290, anchor: 'end', ray: true },
      cam4: { x: 418, y: 232, lx: 428, ly: 290, anchor: 'start', ray: true },
      cam5: { x: 452, y: 246, lx: 462, ly: 272, anchor: 'start', ray: true },
    };
    const mapCams = computed(() => {
      let extra = 0;
      return s.status.cameras.map((c) => {
        let pos = CAM_POS[c.name];
        if (!pos) {
          const x = 250 + 150 * extra;
          extra += 1;
          pos = { x, y: 40, lx: x + 14, ly: 45, anchor: 'start', ray: false };
        }
        const text = c.connected ? (Number.isFinite(c.fps) ? Number(c.fps).toFixed(1) + ' fps' : '연결됨') : '연결 안 됨';
        return { key: c.name, name: fmt.camName(c), kind: c.connected ? 'run' : 'warn', text, full: text, ...pos };
      });
    });

    const armItem = (role) => {
      const arm = s.ports.arms.find((a) => a.role === role);
      const { teleop, autorun } = s.status;
      const name = ko.arm[role] || role;
      let kind = 'stop';
      let text = '연결됨';
      if (!arm || !arm.connected) { kind = 'warn'; text = '연결 안 됨'; }
      else if (role === 'follower' && (teleop.fault || autorun.fault)) { kind = 'fault'; text = '고장'; }
      // 리더는 텔레옵 때만 움직이고, 팔로워는 텔레옵·자율 실행 둘 다에서 움직인다
      else if (teleop.running || (role === 'follower' && autorun.running)) { kind = 'run'; text = '가동 중'; }
      return { key: role, name, kind, text, full: text };
    };
    const mapArms = computed(() => (s.ports.loaded ? [armItem('leader'), armItem('follower')] : []));

    const mapGroups = computed(() => [
      { key: 'belt', title: '벨트', items: mapBelts.value },
      { key: 'cam', title: '카메라', items: mapCams.value },
      { key: 'arm', title: '로봇 팔', items: mapArms.value },
    ].filter((g) => g.items.length));

    const rig = computed(() => {
      const by = Object.fromEntries(mapBelts.value.map((b) => [b.key, b]));
      return {
        feed: by.feed || null, work: by.work || null, lm: by.lm || null,
        cams: mapCams.value, rays: mapCams.value.filter((c) => c.ray),
        leader: mapArms.value[0] || null, follower: mapArms.value[1] || null,
      };
    });

    const mapLabel = computed(() => [
      ...mapBelts.value.map((b) => b.name + ' ' + b.full),
      '카메라 ' + s.status.cameras.length + '대 중 ' + camsUp.value + '대 연결',
      ...mapArms.value.map((a) => a.name + ' ' + a.text),
    ].join(', '));

    const portsDown = computed(() => [
      ...s.ports.arms.filter((a) => !a.connected).map((a) => ko.arm[a.role] || a.role),
      ...s.ports.cameras.filter((c) => !c.connected).map((c) => fmt.camName(c)),
    ]);

    /* ---------- 지시계 ---------- */
    const camRows = computed(() => s.status.cameras.map((c) => {
      const t = c.target_fps || 1;
      return { key: c.name, name: fmt.camName(c), connected: !!c.connected, fps: c.fps, max: t * FPS_MAX, normal: [t * fmt.FPS_LOW_RATIO, t * FPS_NORMAL_HI], error: c.error };
    }));

    // 한계를 JOINT_SLACK 이내로 넘은 값은 한계로 붙여 보인다(센서 잡음)
    const snap = (v, lo, hi) => {
      if (v < lo && lo - v <= JOINT_SLACK) return lo;
      if (v > hi && v - hi <= JOINT_SLACK) return hi;
      return v;
    };
    const followerUp = computed(() => !!s.status.follower.connected && s.status.follower.joints.length > 0);
    const jointRows = computed(() => {
      const lim = (s.config.limits && s.config.limits.joint_limits) || [];
      const f = s.status.follower.joints;
      const l = s.status.leader.connected ? s.status.leader.joints : [];
      return f.map((v, i) => {
        const r = lim[i];
        const pad = r ? (r[1] - r[0]) * JOINT_PAD : 0;
        return {
          key: i,
          name: fmt.jointName(i),
          value: r ? snap(v, r[0], r[1]) : v,
          ghost: l[i],
          min: r ? r[0] - pad : -180,
          max: r ? r[1] + pad : 180,
          normal: r || undefined,
        };
      });
    });

    const skew = computed(() => (Number.isFinite(s.status.sync_skew_ms) ? s.status.sync_skew_ms : null));
    const skewTol = computed(() => (s.config.limits && s.config.limits.max_sync_skew_ms) || null);
    const skewNum = computed(() => (skew.value === null ? '—' : skew.value.toFixed(1)));

    // 소프트 리밋(가동 거리)이 있는 축만 위치 지시계를 둔다. 지금은 LM 가이드 하나.
    const axes = computed(() => s.conveyors
      .filter((b) => b.stroke_mm > 0 && b.mm_per_rev > 0)
      .map((b) => ({ key: b.name, label: b.label || b.name, stroke: b.stroke_mm, mm: b.pos_rev == null ? null : b.pos_rev * b.mm_per_rev })));

    const readyRows = computed(() => {
      const r = s.readiness;
      const g = r.goals || {};
      return [
        { key: 'act', label: 'ACT 에피소드', value: r.act_episodes, goal: g.act_episodes },
        { key: 'good', label: '양품 물체', value: r.good_objects, goal: g.objects_per_class },
        { key: 'defect', label: '불량 물체', value: r.defect_objects, goal: g.objects_per_class },
      ];
    });

    return {
      s, fmt, ko, banner, mapWait, camWait, syncWait, jointWait, rig, mapGroups, mapLabel, portsDown,
      alertList, camRows, followerUp, jointRows, skew, skewTol, skewNum, axes, readyRows,
      cap, capThumbs, capLabel, broken, loadCap,
    };
  },
};
