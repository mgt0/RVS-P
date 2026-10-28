/* =============================================================
   셸 — 해시 라우터 + keep-alive, 사이드바, sticky 상단바, 토스트 스택,
   서버 연결 끊김 배너, 알림 상세 <dialog>. RVS.Shell 이 루트 컴포넌트이고 app.js 가 마운트한다.

   ■ 라우트 이름: overview | collect | train | inspect | equipment | diagnostics  (#/이름?쿼리)
     화면은 RVS.screens.<이름> = { name, title, template, setup } 로 등록한다(js/screens/*.js).
     title 은 document.title 과 상단바 경로, 사이드바 글자에 쓰인다. 등록 안 된 라우트는 '준비 중' 안내가 뜬다.
   ■ 화면은 <keep-alive> 안에서 보존된다. 화면 진입(첫 진입 제외) 시 맨 위로 스크롤하고 그 화면의 h1 에 포커스한다
     (h1 에 tabindex="-1" 이 없어도 셸이 붙인다). onActivated/onDeactivated 로 화면 전용 폴링·SSE 를 켜고 끈다.
   ■ 비상 정지 버튼은 셸이 아니라 index.html 의 정적 HTML 이다(Vue 가 죽어도 눌려야 해서).
     상단바는 그 자리(--estop-w)를 오른쪽에 비워 둔다.
   ■ 개요의 알림 칩을 누르면 #/overview 로 가서 id="rv-alerts" 요소가 있으면 거기로 스크롤한다.
   ============================================================= */
(function () {
  const { computed, ref, watch, nextTick, onMounted, onBeforeUnmount } = Vue;
  const RVS = (window.RVS = window.RVS || {});
  RVS.screens = RVS.screens || {};
  const store = RVS.store;
  const fmt = RVS.fmt;

  // 아이콘은 초안 Main 보드의 사이드바 아이콘이다(24×24, 선 1.8).
  const ROUTES = [
    { name: 'overview', title: '개요', group: '모니터링',
      icon: '<path d="M4 15a8 8 0 1 1 16 0"/><path d="M12 15l4.5-4.5"/>' },
    { name: 'collect', title: '수집', group: '작업',
      icon: '<circle cx="12" cy="12" r="8"/><circle class="rv-nav__fill" cx="12" cy="12" r="3"/>' },
    { name: 'train', title: '학습', group: '작업',
      icon: '<path d="M4 18l5-6 4 3 7-9"/>' },
    { name: 'inspect', title: '검사', group: '작업',
      icon: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>' },
    { name: 'equipment', title: '장비 제어', group: '장비',
      icon: '<path d="M4 6h16M4 12h16M4 18h16"/><circle class="rv-nav__knob" cx="9" cy="6" r="2"/><circle class="rv-nav__knob" cx="15" cy="12" r="2"/><circle class="rv-nav__knob" cx="8" cy="18" r="2"/>' },
    { name: 'diagnostics', title: '진단', group: '장비',
      icon: '<path d="M3 12h4l2-5 4 10 2-5h6"/>' },
  ];
  const BY_NAME = Object.fromEntries(ROUTES.map((r) => [r.name, r]));
  RVS.routes = ROUTES;

  const titleOf = (r) => (RVS.screens[r.name] && RVS.screens[r.name].title) || r.title;
  const groups = [];
  ROUTES.forEach((r) => {
    let g = groups.find((x) => x.title === r.group);
    if (!g) groups.push((g = { title: r.group, routes: [] }));
    g.routes.push(r);
  });

  /* ---------------- 라우터 ---------------- */
  function parseHash() {
    const m = /^#\/?([a-z]+)?(?:\?(.*))?$/.exec(location.hash);
    const name = m && BY_NAME[m[1]] ? m[1] : null;
    return { name, query: Object.fromEntries(new URLSearchParams((m && m[2]) || '')) };
  }

  function focusHeading() {
    const h = document.querySelector('#rv-main h1');
    if (!h) return;
    if (!h.hasAttribute('tabindex')) h.tabIndex = -1;
    h.focus({ preventScroll: true });
  }

  let firstRoute = true;
  async function syncRoute() {
    const { name: parsed, query } = parseHash();
    const name = parsed || 'overview';
    // 주소가 비었거나 모르는 이름이면 개요로 바로잡는다. 주소의 쿼리도 함께 버려지므로 store.query 도 비운다
    if (!parsed) history.replaceState(null, '', `#/${name}`);
    const changed = name !== store.route;
    store.route = name;
    store.query = parsed ? query : {};
    document.title = `${titleOf(BY_NAME[name])} · RVS-P`;
    if (changed && !firstRoute) {
      window.scrollTo(0, 0);
      await nextTick();
      focusHeading();
    }
    firstRoute = false;
  }

  /* 화면 파일이 아직 없거나 로드에 실패한 라우트 */
  const Missing = {
    name: 'ScreenMissing',
    props: { title: String },
    template: `
      <section aria-labelledby="rv-missing-h">
        <h1 id="rv-missing-h" tabindex="-1">{{ title }}</h1>
        <rv-state state="empty" message="이 화면은 아직 준비되지 않았습니다." hint="화면 스크립트가 로드되지 않았거나 등록되지 않았습니다." />
      </section>`,
  };

  const TOAST = {
    info: { icon: 'info', word: '알림' },
    error: { icon: 'warn', word: '오류' },
    fault: { icon: 'fault', word: '긴급' },
  };

  const Shell = {
    name: 'RvShell',
    setup() {
      syncRoute();   // 첫 렌더 전에 라우트를 정해 개요가 잠깐 비치지 않게 한다

      /* ---- 현재 화면 ---- */
      const cur = computed(() => BY_NAME[store.route]);
      const view = computed(() => {
        const screen = RVS.screens[store.route];
        return screen
          ? { comp: screen, props: {} }
          : { comp: Missing, props: { title: titleOf(cur.value) } };
      });

      /* ---- 640px 이하 메뉴 패널 ---- */
      const menuOpen = ref(false);
      const menuBtn = ref(null);
      const sidebar = ref(null);
      const narrow = window.matchMedia('(max-width: 640px)');

      function closeMenu(refocus) {
        if (!menuOpen.value) return;
        menuOpen.value = false;
        if (refocus) menuBtn.value?.focus();
      }
      function toggleMenu() {
        menuOpen.value = !menuOpen.value;
        // 패널은 DOM 에서 버튼보다 앞에 있어 Tab 으로 못 들어가므로 열 때 첫 링크로 포커스를 옮긴다
        if (menuOpen.value) {
          nextTick(() => (sidebar.value.querySelector('[aria-current="page"]') || sidebar.value.querySelector('a'))?.focus());
        }
      }
      const onKey = (e) => { if (e.key === 'Escape') closeMenu(true); };
      const onPointer = (e) => {
        if (menuOpen.value && !sidebar.value.contains(e.target) && !menuBtn.value.contains(e.target)) closeMenu();
      };
      const onNarrow = () => closeMenu();
      const onHash = () => { closeMenu(); syncRoute(); };

      onMounted(() => {
        window.addEventListener('hashchange', onHash);
        document.addEventListener('keydown', onKey);
        document.addEventListener('pointerdown', onPointer, true);
        narrow.addEventListener('change', onNarrow);
      });
      onBeforeUnmount(() => {
        window.removeEventListener('hashchange', onHash);
        document.removeEventListener('keydown', onKey);
        document.removeEventListener('pointerdown', onPointer, true);
        narrow.removeEventListener('change', onNarrow);
      });

      /* ---- 상단바 칩 ---- */
      const rec = computed(() => store.status.recording.state !== 'IDLE');
      const recText = computed(() => {
        const r = store.status.recording;
        return r.state === 'RECORDING' ? `녹화 중 ${fmt.time(r.elapsed_sec)}` : (fmt.ko.rec[r.state] || r.state);
      });

      const chipItems = computed(() => {
        const c = store.alerts.counts;
        return [
          { kind: 'fault', n: c.fault, word: '고장' },
          { kind: 'warn', n: c.warn, word: '주의' },
          { kind: 'info', n: c.info, word: '정보' },
        ].filter((i) => i.n > 0);
      });
      const chipState = computed(() => {
        if (!store.alertsLoaded) return store.alertsError ? 'error' : 'loading';
        return chipItems.value.length ? 'list' : 'none';
      });
      const chipAria = computed(() => {
        if (chipState.value === 'loading') return '알림 확인 중';
        if (chipState.value === 'error') return '알림을 불러오지 못했습니다. 개요로 이동';
        if (chipState.value === 'none') return '알림 없음. 개요로 이동';
        return `알림 ${chipItems.value.map((i) => `${i.word} ${i.n}건`).join(', ')}. 개요의 알림 목록으로 이동`;
      });
      function scrollToAlerts() {
        const el = document.getElementById('rv-alerts');
        if (el) el.scrollIntoView({ block: 'start' });
      }
      function onChip(e) {
        if (store.route === 'overview') {
          e.preventDefault();
          scrollToAlerts();
        } else {
          setTimeout(scrollToAlerts, 50);   // 이동한 화면이 그려진 뒤
        }
      }

      /* ---- 알림 상세 창 ---- */
      // 서버 문구는 마크업 없는 텍스트다(detail.problem/impact/cause/steps/note/evidence). 초안의 소제목에 그대로 대응시킨다.
      const a = computed(() => store.alertDialog.alert);
      const detail = computed(() => (a.value && a.value.detail) || {});
      const sections = computed(() =>
        [['무엇이 문제인가', detail.value.problem], ['영향', detail.value.impact], ['원인', detail.value.cause]].filter(([, t]) => t));
      const evidence = computed(() =>
        Object.entries(detail.value.evidence || {}).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)]));
      const acked = computed(() => !!a.value && store.isAcked(a.value));
      const resolved = computed(() =>
        !!a.value && store.alertsLoaded && !store.alerts.alerts.some((x) => x.id === a.value.id));
      const actionRoute = computed(() => {
        const act = a.value && a.value.action;
        return act && BY_NAME[act.route] ? act.route : null;
      });
      const toggleAck = () => store.ackAlert(a.value, !acked.value);

      /* ---- 자동 사이클 위젯(상단바, 비상 정지 왼쪽). 서버에 /api/cycle 이 없으면(404) 통째로 숨는다 ---- */
      const cyc = computed(() => (store.cycle.supported && store.cycle.loaded ? store.cycle.data : null));
      const cycRunning = computed(() => !!cyc.value && cyc.value.state === 'running');
      const cycDlg = ref(false);
      // ready = { ok, reasons[](시작을 막는 이유), notes[](막지는 않는 안내 — 예: 시작하면 벨트 포트를 연다) }
      const ready = computed(() => (cyc.value && cyc.value.ready) || { ok: true, reasons: [], notes: [] });
      const readyNotes = computed(() => (ready.value.notes || []).filter(Boolean).map(String));
      const canStart = computed(() => !!cyc.value && !cycRunning.value && ready.value.ok && !store.cycle.busy);
      const baskets = computed(() => (cyc.value && cyc.value.baskets) || {});
      const BASKET_KO = { good: '양품', defect: '불량' };
      const fullKeys = computed(() => ['good', 'defect'].filter((k) => baskets.value[k] && baskets.value[k].count >= baskets.value[k].capacity));
      const basketText = computed(() => ['good', 'defect'].filter((k) => baskets.value[k])
        .map((k) => `${BASKET_KO[k]} ${baskets.value[k].count}/${baskets.value[k].capacity}`).join(' · '));
      const fullText = computed(() => `${fullKeys.value.map((k) => BASKET_KO[k]).join('·')} 바스켓을 비워 주세요`);
      const result = computed(() => (cyc.value && cyc.value.state === 'done' && cyc.value.result) || null);
      const STEP_ICON = { pending: 'stop', active: 'run', failed: 'warn', skipped: 'stop' };
      const STEP_WORD = { pending: '대기', active: '진행 중', done: '완료', failed: '실패', skipped: '건너뜀' };
      // 1줄: 지금 상태, 2줄: 보조(메시지·준비 안 된 이유·바스켓). 상태마다 다르다
      const cycLine1 = computed(() => {
        const c = cyc.value;
        if (!c || c.state === 'running') return null;
        if (result.value) {
          const bad = result.value.verdict === 'defect';
          return { kind: bad ? 'warn' : 'run', text: bad ? `불량 · ${result.value.reason || ''}` : '양품' };
        }
        if (c.state === 'failed') return { kind: 'warn', text: c.error || c.message || '사이클이 실패했습니다' };
        if (c.state === 'aborted') return { kind: 'warn', text: c.error || c.message || '사이클이 중단되었습니다' };
        return { kind: null, text: basketText.value };
      });
      const cycLine2 = computed(() => {
        const c = cyc.value;
        if (!c) return null;
        if (c.state === 'running') return { kind: null, text: c.message || '' };
        if (fullKeys.value.length) return { kind: 'warn', text: fullText.value };
        if (!ready.value.ok) return { kind: 'warn', text: (ready.value.reasons || [])[0] || '시작할 준비가 되지 않았습니다' };
        if (cycLine1.value && cycLine1.value.kind === null) {
          // 대기 상태. 시작을 막지 않는 안내는 막는 이유(▲ 주의)와 구분해 ⓘ 정보로 비어 있는 둘째 줄에 짧게(마지막 문장만) 낸다
          return readyNotes.value.length ? { kind: 'info', text: readyNotes.value[0].split('. ').pop() } : null;
        }
        return { kind: null, text: basketText.value };
      });
      // 위젯의 두 줄은 폭이 모자라면 말줄임으로 잘린다 — 실패·판정 사유와 준비 안 된 이유를 마우스를 올리면 전부 읽게 한다.
      // 안내(notes)는 전체 문장을 '안내:' 를 붙여 맨 뒤에 모은다(막는 이유와 섞이지 않게)
      const cycTitle = computed(() => {
        const lines = ready.value.ok
          ? [cycLine1.value, cycLine2.value && cycLine2.value.kind !== 'info' ? cycLine2.value : null].map((l) => l && l.text).filter(Boolean)
          : (ready.value.reasons || []);
        return [...lines, ...readyNotes.value.map((n) => `안내: ${n}`)].join('\n');
      });
      const openResult = () => store.go('inspect', { capture: result.value.capture_id });
      const emptyFull = () => store.emptyBaskets(fullKeys.value.length === 2 ? 'all' : fullKeys.value[0]);
      async function confirmStart() {
        cycDlg.value = false;
        await store.cycleStart();
      }
      // 상태가 '바뀔 때만' 읽어 준다(메시지는 1초마다 바뀌어 시끄럽다, 페이지를 열었을 때의 옛 결과도 읽지 않는다)
      const cycAnnounce = ref('');
      watch(() => (cyc.value ? [cyc.value.state, ((cyc.value.steps || []).find((x) => x.state === 'active') || {}).id].join('/') : null), (now, was) => {
        const c = cyc.value;
        if (!c || was === undefined || was === null) return;
        const act = (c.steps || []).find((x) => x.state === 'active');
        if (c.state === 'running') cycAnnounce.value = `사이클 진행 중${act ? ` · ${act.label}` : ''}`;
        else if (c.state === 'done') cycAnnounce.value = `사이클 완료${c.result ? ` · ${c.result.verdict === 'defect' ? '불량' : '양품'}` : ''}`;
        else if (c.state === 'failed') cycAnnounce.value = `사이클 실패 · ${c.error || ''}`;
        else if (c.state === 'aborted') cycAnnounce.value = '사이클 중단됨';
      });

      /* ---- 토스트 ---- */
      const infoToasts = computed(() => store.toasts.filter((t) => t.kind === 'info'));
      const alertToasts = computed(() => store.toasts.filter((t) => t.kind !== 'info'));

      return {
        s: store, fmt, ko: fmt.ko, groups, titleOf, cur, view,
        menuOpen, menuBtn, sidebar, toggleMenu,
        rec, recText, chipItems, chipState, chipAria, onChip,
        a, detail, sections, evidence, acked, resolved, actionRoute, toggleAck,
        infoToasts, alertToasts, TOAST,
        cyc, cycRunning, cycDlg, canStart, fullKeys, result, cycLine1, cycLine2, cycTitle, cycAnnounce,
        STEP_ICON, STEP_WORD, openResult, emptyFull, confirmStart,
      };
    },
    template: `
      <div class="rv-layout">

        <div id="rv-sidebar" ref="sidebar" class="rv-sidebar" :data-open="menuOpen ? 'true' : 'false'">
          <div class="rv-brand">
            <div class="rv-brand__name">RVS-P</div>
            <div class="rv-brand__sub">검사 스테이션</div>
          </div>
          <div class="rv-sidebar__here">
            <span class="rv-small rv-muted">{{ cur.group }} <span aria-hidden="true">›</span> {{ titleOf(cur) }}</span>
            <span v-if="s.meta.simulate" class="rv-chip"><rv-status kind="stop" label="시뮬레이션 모드" /></span>
          </div>
          <nav class="rv-nav" aria-label="화면">
            <div v-for="g in groups" :key="g.title" class="rv-nav__group" role="group" :aria-label="g.title">
              <div class="rv-nav__title" aria-hidden="true">{{ g.title }}</div>
              <a v-for="r in g.routes" :key="r.name" class="rv-nav__link" :href="'#/' + r.name"
                 :aria-current="s.route === r.name ? 'page' : null">
                <svg class="rv-nav__icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false" v-html="r.icon"></svg>
                {{ titleOf(r) }}
                <span v-if="r.name === 'collect' && rec" class="rv-nav__rec"><rv-status kind="run" label="녹화 중" hide-label /></span>
              </a>
            </div>
          </nav>
          <div class="rv-sidebar__foot">
            <div v-if="s.ports.loaded">포트 {{ s.ports.connected_count }}/{{ s.ports.total_count }} 연결</div>
            <div v-else>포트 확인 중…</div>
            <div v-if="s.meta.simulate" class="rv-sidebar__sim">시뮬레이션 모드</div>
            <div v-if="s.estop.lastAt">마지막 비상 정지 {{ fmt.clock(s.estop.lastAt) }}</div>
          </div>
        </div>

        <div class="rv-content">
          <header class="rv-topbar">
            <button ref="menuBtn" type="button" class="rv-icon-btn rv-menu-btn" aria-label="메뉴" aria-controls="rv-sidebar"
                    :aria-expanded="menuOpen ? 'true' : 'false'" @click="toggleMenu">
              <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" focusable="false"><path d="M3 6h16M3 11h16M3 16h16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            </button>
            <p class="rv-crumb">{{ cur.group }} <span aria-hidden="true">›</span> {{ titleOf(cur) }}</p>
            <div class="rv-topbar__chips">
              <span v-if="s.meta.simulate" class="rv-chip rv-chip--sim"><rv-status kind="stop" label="시뮬레이션 모드" /></span>
              <a class="rv-alertchip" href="#/overview" :aria-label="chipAria" @click="onChip">
                <template v-if="chipState === 'loading'"><span class="rv-muted">알림 확인 중…</span></template>
                <template v-else-if="chipState === 'error'"><rv-status kind="warn" label="알림 불러오기 실패" /></template>
                <template v-else-if="chipState === 'none'"><span class="rv-muted">알림 없음</span></template>
                <template v-else>
                  <template v-for="(i, n) in chipItems" :key="i.kind">
                    <span v-if="n" class="rv-alertchip__sep" aria-hidden="true">·</span>
                    <rv-status :kind="i.kind"><span class="rv-alertchip__word">{{ i.word + ' ' }}</span>{{ i.n }}</rv-status>
                  </template>
                </template>
              </a>
              <span v-if="rec" class="rv-chip rv-chip--rec"><rv-status kind="run" :label="recText" /></span>
            </div>
            <div v-if="cyc" class="rv-cyc" role="group" aria-label="자동 사이클" :title="cycTitle">
              <button v-if="!cycRunning" type="button" class="rv-btn rv-btn--primary rv-cyc__btn" :disabled="!canStart" @click="cycDlg = true">
                <svg class="rv-cyc__ico" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M3 1.5v9l7.5-4.5z"/></svg>사이클 시작
              </button>
              <button v-else type="button" class="rv-btn rv-cyc__btn" @click="s.cycleStop()">
                <svg class="rv-cyc__ico" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><rect x="1.5" y="1.5" width="9" height="9" rx="1.5"/></svg>사이클 정지
              </button>
              <div class="rv-cyc__info">
                <ol v-if="cycRunning" class="rv-cyc__steps" role="list" aria-label="사이클 단계">
                  <template v-for="(st, i) in (cyc.steps || [])" :key="st.id">
                    <li v-if="i" class="rv-cyc__arrow" aria-hidden="true">›</li>
                    <li class="rv-cyc__step" :class="'rv-cyc__step--' + st.state">
                      <svg v-if="st.state === 'done'" class="rv-cyc__done" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M2.5 6.5l2.5 2.5 4.5-5.5"/></svg>
                      <rv-status v-else :kind="STEP_ICON[st.state] || 'stop'" :label="st.label + ', ' + STEP_WORD[st.state]" hide-label />
                      <span aria-hidden="true">{{ st.label }}</span>
                      <span v-if="st.state === 'done'" class="rv-sr-only">{{ st.label }}, 완료</span>
                    </li>
                  </template>
                </ol>
                <p v-else-if="cycLine1" class="rv-cyc__l1">
                  <rv-status v-if="cycLine1.kind" :kind="cycLine1.kind" :label="cycLine1.text" />
                  <template v-else>{{ cycLine1.text }}</template>
                </p>
                <p v-if="cycLine2 && cycLine2.text" class="rv-cyc__l2" :class="{ 'rv-cyc__l2--warn': cycLine2.kind === 'warn' }">
                  <rv-status v-if="cycLine2.kind === 'warn'" kind="warn" :label="cycLine2.text" />
                  <rv-status v-else-if="cycLine2.kind === 'info'" kind="info" :label="cycLine2.text" />
                  <template v-else>{{ cycLine2.text }}</template>
                </p>
              </div>
              <button v-if="!cycRunning && fullKeys.length" type="button" class="rv-btn rv-btn--sm rv-cyc__act" @click="emptyFull">비웠습니다</button>
              <button v-else-if="!cycRunning && result && result.capture_id" type="button" class="rv-btn rv-btn--sm rv-cyc__act" @click="openResult">검사 결과 보기</button>
              <span class="rv-sr-only" role="status" aria-live="polite">{{ cycAnnounce }}</span>
            </div>
          </header>

          <div v-if="!s.online" class="rv-banner" role="alert">
            <rv-status kind="warn" label="서버 연결 끊김" class="rv-banner__title" />
            <span>서버가 꺼졌거나 네트워크가 끊겼습니다. 화면의 값은 마지막으로 받은 값이며, 자동으로 다시 연결을 시도합니다.</span>
          </div>

          <main id="rv-main" class="rv-main">
            <keep-alive>
              <component :is="view.comp" v-bind="view.props" />
            </keep-alive>
          </main>
        </div>

        <rv-dialog v-model="cycDlg" title="자동 사이클 시작" size="sm">
          <p>로봇팔과 벨트가 자동으로 움직입니다. 로봇 주변에 사람이 없는지, 집을 물체가 픽업 자리에 있는지 확인하세요.</p>
          <template #footer>
            <button type="button" class="rv-btn" @click="cycDlg = false">취소</button>
            <button type="button" class="rv-btn rv-btn--primary" @click="confirmStart">시작</button>
          </template>
        </rv-dialog>

        <rv-dialog v-model="s.alertDialog.open" :return-focus="s.alertDialog.trigger">
          <template #header="{ titleId }">
            <template v-if="a">
              <div class="rv-alert__grade">
                <rv-status :kind="a.severity" size="md" :label="ko.severity[a.severity] || '정보'" />
                <span role="status"><span v-if="acked" class="rv-chip rv-chip--sm">확인됨</span></span>
              </div>
              <h2 :id="titleId" class="rv-dialog__title">{{ a.title }}</h2>
              <p v-if="a.since" class="rv-alert__meta">처음 감지 {{ fmt.dateTime(a.since) }}</p>
            </template>
          </template>

          <div v-if="a" class="rv-alert__secs">
            <div v-for="[heading, text] in sections" :key="heading" class="rv-alert__sec">
              <h3>{{ heading }}</h3>
              <p>{{ text }}</p>
            </div>
            <div v-if="detail.steps && detail.steps.length" class="rv-alert__sec">
              <h3>해결 방법</h3>
              <ol class="rv-steps" role="list">
                <li v-for="(step, n) in detail.steps" :key="n">
                  <span class="rv-steps__n" aria-hidden="true">{{ n + 1 }}</span>
                  <span class="rv-steps__t">{{ step }}</span>
                </li>
              </ol>
            </div>
            <p v-if="detail.note" class="rv-alert__note">{{ detail.note }}</p>
            <div v-if="evidence.length" class="rv-alert__sec">
              <h3>근거</h3>
              <dl class="rv-alert__ev">
                <template v-for="[k, v] in evidence" :key="k"><dt>{{ k }}</dt><dd><code>{{ v }}</code></dd></template>
              </dl>
            </div>
            <p v-if="resolved" class="rv-alert__note">이 알림은 방금 해소되어 목록에서 사라졌습니다.</p>
          </div>

          <template #note>알림은 문제가 해결될 때까지 목록에 남습니다. 확인은 ‘봤다’는 뜻이지 해결이 아닙니다.</template>
          <template #footer>
            <button type="button" class="rv-btn" @click="toggleAck">{{ acked ? '확인 취소' : '확인함' }}</button>
            <a v-if="actionRoute" class="rv-btn rv-btn--primary" :href="'#/' + actionRoute" @click="s.closeAlert()">{{ (a.action && a.action.label) || '관련 화면 열기' }}</a>
          </template>
        </rv-dialog>

        <!-- window.confirm 대신 쓰는 확인 창(store.askConfirm). 푸터의 비상 정지는 rv-dialog 가 넣는다 -->
        <rv-dialog v-model="s.confirmDialog.open" :title="s.confirmDialog.title" size="sm" @close="s.answerConfirm(false)">
          <p class="rv-confirm__msg">{{ s.confirmDialog.message }}</p>
          <template #footer>
            <button type="button" class="rv-btn" @click="s.answerConfirm(false)">취소</button>
            <button type="button" class="rv-btn rv-btn--primary" @click="s.answerConfirm(true)">{{ s.confirmDialog.okLabel }}</button>
          </template>
        </rv-dialog>

        <div class="rv-toasts">
          <div class="rv-toasts__live" role="status">
            <div v-for="t in infoToasts" :key="t.id" class="rv-toast">
              <span class="rv-toast__icon"><rv-status :kind="TOAST[t.kind].icon" :label="TOAST[t.kind].word" hide-label /></span>
              <span class="rv-toast__msg">{{ t.message }}</span>
              <button type="button" class="rv-icon-btn rv-toast__x" aria-label="알림 닫기" @click="s.dismiss(t.id)">
                <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M5.5 5.5l9 9M14.5 5.5l-9 9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
              </button>
            </div>
          </div>
          <div class="rv-toasts__live" role="alert">
            <div v-for="t in alertToasts" :key="t.id" class="rv-toast">
              <span class="rv-toast__icon"><rv-status :kind="(TOAST[t.kind] || TOAST.error).icon" :label="(TOAST[t.kind] || TOAST.error).word" hide-label /></span>
              <span class="rv-toast__msg">{{ t.message }}</span>
              <button type="button" class="rv-icon-btn rv-toast__x" aria-label="알림 닫기" @click="s.dismiss(t.id)">
                <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M5.5 5.5l9 9M14.5 5.5l-9 9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
              </button>
            </div>
          </div>
        </div>

      </div>`,
  };

  RVS.Shell = Shell;
})();
