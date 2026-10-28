/* =============================================================
   RVS.store — 모든 화면이 같이 쓰는 반응형 상태, 폴링, 액션.
   (Vue.reactive 한 개. 화면 안에서는 `const s = RVS.store` 로 쓴다.)

   ■ 상태 필드 (모두 서버가 준 값. 초안의 예시 숫자는 어디에도 없다)
   route, query     현재 라우트 이름과 해시 쿼리(문자열 맵). 셸이 hashchange 로 채운다.
   online           false = /api/status 가 연속 3회 실패(셸이 '서버 연결 끊김' 배너를 띄운다)
   meta             /api/meta + { loaded, simulate }. cameras, defect_types, sample_rate_hz,
                    max_relative_target, stream_port, next_episode_id. simulate 는 /api/ports 에서 온다.
   ports            /api/ports: { loaded, error, arms[], cameras[], serial_ports[], connected_count, total_count }. 5초 폴링(시리얼 열거가 비싸서 벨트 폴링과 따로 돈다)
   status           /api/status 응답의 모든 키(recording, leader, follower, teleop, autorun,
                    cameras, sync_skew_ms, demo, cycle …) + loaded, error. 0.5초(녹화 중)/1초(사이클 중)/1.5초 폴링.
   conveyors        /api/conveyor/status 의 벨트 배열. 각 벨트에 rpmInput, moveInput(사용자가
                    조작 중인 값)이 더 붙는다. 5초/1초(자동 촬영 켜짐) 폴링. 벨트 객체는 '제자리'로
                    갱신된다 — 새 객체로 갈아끼우면 진행 중인 명령이 옛 객체를 들고 버튼이 굳는다.
   beltBusy         { [벨트 이름]: 진행 중인 명령 수 } 0 보다 크면 그 축만 잠근다. 키는 이름이다(객체 아님).
                    정지는 잠금과 상관없이 나가므로 명령이 겹칠 수 있다 — 먼저 끝난 쪽이 잠금을 풀지 않게 개수로 센다.
   busy             { ports, test, record, teleop, home, train, objects, recon, conveyor, episodes }
                    레거시 키 그대로. 화면이 필요하면 키를 더 붙여도 된다(반응형).
   alerts           마지막 /api/alerts 응답 전체: { counts:{fault,warn,info}, alerts:[…], generated_at }
                    (첫 응답 전에는 counts 0, alerts [], generated_at null)
   alertsError      마지막 갱신이 실패했으면 메시지, 성공하면 null
   alertsLoaded     한 번이라도 성공했으면 true (로딩 상태와 '알림 없음'을 구분할 때)
   alertDialog      셸 알림 상세 창 상태 { open, alert(열 때의 스냅샷), trigger } — 직접 건드리지 말고 openAlert 로
   confirmDialog    셸 확인 창 상태 { open, title, message, okLabel } — 직접 건드리지 말고 askConfirm 으로
   acked            { [알림 id]: since } localStorage 'rvs.alertAck' 와 동기화
   config           /api/diagnostics: { loaded, loading, error, env[], serial[], cameras[], limits, fetchedAt }
                    부팅 때 한 번 읽는다. limits.joint_limits 와 cameras[].roi 는 여기에만 있다.
   readiness        /api/readiness: { loaded, loading, error, goals, act_episodes, good_objects, defect_objects }
   capture          { auto, busy, last, error } 자동 촬영 스위치와 마지막 촬영 응답(markRaw, mesh 가 커서)
   estop            { lastAt, lastOk, failed[] } 마지막 비상 정지(래치 없음 — 재가동은 각 장치 시작)
   cycle            { supported, loaded, error, data, busy } 자동 사이클. data = 서버 CYCLE 객체. supported=false 면 서버에 없음(404)
   cycleRunning     사이클 진행 중(data.state === 'running'). 화면은 이 값으로 벨트·팔·녹화·시연 수동 조작을 잠근다
   status.loaded / status.error, ports.error, conveyorsLoaded / conveyorsError
                    첫 응답을 받았는지, 마지막 폴링이 실패했는지(성공하면 null). 값이 없을 때 '로딩'과 '오류'를 가르는 데 쓴다.
                    (conveyors 는 배열이라 속성 대신 store.conveyorsLoaded / store.conveyorsError)
   toasts           [{ id, message, kind }] 셸이 그린다. 직접 push 하지 말고 notify 로

   ■ 액션
   notify(message, kind='info')   kind: 'info' | 'error'(▲) | 'fault'(■ 빨강, 비상 정지 전용)
   dismiss(id)
   go(name, query?)               #/name?k=v 로 이동. 예: go('inspect', { capture: id })
   streamUrl(slot, {fps=12, q=70})  MJPEG URL(stream_port 분리 반영). meta 가 오기 전에는 null
   loadMeta()
   refreshStatus()                즉시 /api/status 를 다시 읽는다(실패하면 throw)
   refreshPorts()                 POST /api/ports/refresh + 토스트('포트 다시 찾기' 버튼용)
   refreshConveyors(suppress)     즉시 /api/conveyor/status (suppress=true 면 자동 촬영 금지)
   applyBelt(data)                벨트 명령 응답(평면 상태 하나)을 해당 벨트에 병합
   beltCall(belt, fn, message)    beltBusy[belt.name] 잠금 + applyBelt(await fn()) + 토스트. 성공하면 true, 실패(토스트를 띄움)하면 false
   isBeltBusy(belt)
   cycleStart() / cycleStop() / emptyBaskets('good'|'defect'|'all')   /api/cycle/* 호출(성공하면 true). 정지는 확인 없음
   refreshCycle()                 즉시 사이클 상태를 다시 읽는다(/api/status 에 cycle 키가 있으면 그것, 없으면 /api/cycle/status. 404 면 cycle.supported=false)
   refreshAlerts(refresh=false)   /api/alerts[?refresh=1] (10초 폴링. 실패하면 alertsError). 서버가 5초 캐시를 쥐고 있어서
                                  배경 캡처·캘리브레이션 계산 같은 조치 직후에는 true 로 불러 캐시를 우회한다.
   openAlert(id[, triggerEl])     셸 알림 상세 창을 그 알림으로 연다. id 대신 알림 객체도 받는다.
                                  닫으면 triggerEl(없으면 열 때 포커스 있던 요소)로 포커스가 돌아온다.
   closeAlert()
   askConfirm({ title, message, okLabel='확인' })
                                  window.confirm 대신 쓰는 확인 창 → Promise<boolean>(확인 true, 취소·닫기·Esc·배경 클릭 false).
                                  네이티브 confirm 은 떠 있는 동안 상단바 비상 정지까지 눌리지 않게 막는다. 이 창은 푸터에 비상 정지가 있다.
                                  message 의 \n 은 줄바꿈으로 보인다. 기본 포커스는 닫기 버튼이라 Enter 도 취소다.
   isAcked(id)                   그 알림을 사용자가 '확인함' 했는지. since 가 바뀌면(재발생) false
   ackAlert(id[, on=true])        확인함 저장. on=false 면 취소
   refreshConfig(refresh=false)   /api/diagnostics[?refresh=1]
   refreshReadiness()             /api/readiness (30초 폴링)
   captureNow(opts)               POST /api/scan/capture { settle_sec:0.5, resolution:96, ...opts }.
                                  성공하면 응답을 돌려주고 capture.last 에 둔다. 실패하면 토스트 + null.
                                  busy.recon 도 함께 잠근다(호출하는 쪽이 따로 잠그지 말 것).
   onBeltStopped(fn)              벨트가 running→정지로 바뀔 때 fn({ names }) 호출. 해제 함수를 돌려준다.
                                  비상 정지를 누른 뒤 5초와 드라이버 켜기 직후의 정지 전이는 부르지 않는다(자동 촬영도 마찬가지).
   emergencyStop()                = RVS.estop() (바닐라, 상단바 버튼과 같은 함수)
   emergencyReset()               POST /api/emergency_reset — 장비 제어의 '드라이버 켜기'. 응답의 'not_connected'(포트가 닫혀 건너뜀)는
                                  실패가 아니라 '연결 안 된 벨트 n개는 건너뜀' 안내로 붙인다
   start()                        app.js 가 마운트 직후 한 번 부른다(폴링 시작)
   ============================================================= */
(function () {
  const { reactive, markRaw, watch } = Vue;
  const RVS = (window.RVS = window.RVS || {});
  const { api, fmt } = RVS;

  const store = reactive({
    route: 'overview',
    query: {},
    online: true,

    meta: {
      loaded: false,
      cameras: ['cam1', 'cam2', 'cam3', 'cam4', 'cam5', 'global'],
      defect_types: ['scratch', 'dent', 'discoloration', 'crack', 'other'],
      sample_rate_hz: 30,
      max_relative_target: 5,
      simulate: false,
      // 0 이면 프리뷰도 같은 포트에서 받는다(스트림 분리 없음)
      stream_port: 0,
      next_episode_id: '',
    },

    ports: { loaded: false, arms: [], cameras: [], serial_ports: [], connected_count: 0, total_count: 0, error: null },

    status: {
      recording: { state: 'IDLE', frame_count: 0, elapsed_sec: 0, max_skew_ms: 0, skew_violations: 0 },
      leader: { joints: [], connected: false },
      follower: { joints: [], connected: false },
      teleop: { running: false, fault: null },
      autorun: { running: false, fault: null },
      cameras: [],
      sync_skew_ms: null,
      demo: null,
      cycle: null,
      loaded: false,   // 첫 응답을 받았는가
      error: null,     // 마지막 폴링이 실패했으면 메시지
    },

    conveyors: [],
    conveyorsLoaded: false,
    conveyorsError: null,   // 배열에는 속성을 못 붙이므로 conveyors.error 대신 이 이름
    beltBusy: {},
    busy: {
      ports: false, test: false, record: false, teleop: false, home: false,
      train: false, objects: false, recon: false, conveyor: false, episodes: false,
    },

    alerts: { counts: { fault: 0, warn: 0, info: 0 }, alerts: [], generated_at: null },
    alertsError: null,
    alertsLoaded: false,
    alertDialog: { open: false, alert: null, trigger: null },
    confirmDialog: { open: false, title: '', message: '', okLabel: '확인' },
    acked: {},

    config: { loaded: false, loading: false, error: null, env: [], serial: [], cameras: [], limits: null, fetchedAt: null },
    readiness: {
      loaded: false, loading: false, error: null,
      goals: null, act_episodes: 0, good_objects: 0, defect_objects: 0,
    },

    // 자동 사이클(시작 버튼 한 번 = 물체 집기 → 이송 → 검사 → 분류). 계약은 cycle_contract.md
    cycle: { supported: true, loaded: false, error: null, data: null, busy: false },
    cycleRunning: false,

    capture: { auto: false, busy: false, last: null, error: null },
    estop: { lastAt: null, lastOk: null, failed: [] },
    toasts: [],
  });

  // 아래 객체들은 제자리에서만 갱신한다(통째로 갈아끼우지 않는다). 그래서 구조분해해도 안전하다.
  // store.alerts·conveyors·toasts 는 통째로 교체되므로 항상 store.* 로 읽는다.
  const { meta, ports, status, busy, capture, config, readiness } = store;
  const isRecording = () => store.status.recording.state === 'RECORDING';

  /* ---------------- 토스트 ---------------- */
  let toastSeq = 0;
  function notify(message, kind = 'info') {
    const id = ++toastSeq;
    store.toasts.push({ id, message, kind });
    // 에러는 사용자가 읽을 시간을 더 준다.
    setTimeout(() => dismiss(id), kind === 'info' ? 4000 : 9000);
  }
  function dismiss(id) {
    store.toasts = store.toasts.filter((t) => t.id !== id);
  }

  /* ---------------- 이동 ---------------- */
  function go(name, query) {
    const qs = query ? new URLSearchParams(query).toString() : '';
    const hash = `#/${name}${qs ? `?${qs}` : ''}`;
    if (location.hash !== hash) location.hash = hash;
  }

  /* 프리뷰만 다른 포트에서 받는다. 브라우저는 오리진당 동시 연결이 6개인데
     MJPEG 은 연결을 하나씩 영구히 물고 있어서, 카메라 6대면 그 예산을 전부
     써버려 API 요청이 큐에서 무한 대기한다 — 버튼을 눌러도 요청이 나가지 않고
     화면이 잠긴 채 굳는다(실측). 포트가 다르면 연결 예산도 따로 잡힌다. */
  function streamUrl(slot, { fps = 12, q = 70 } = {}) {
    // 포트를 알기 전에 붙으면 잘못된 오리진으로 연결해 그 예산을 쓴다
    if (!meta.loaded) return null;
    const base = meta.stream_port && String(meta.stream_port) !== location.port
      ? `${location.protocol}//${location.hostname}:${meta.stream_port}`
      : '';
    return `${base}/api/stream/${slot}?fps=${fps}&q=${q}`;
  }

  /* ---------------- 서버 값 읽기 ---------------- */
  async function loadMeta() {
    try {
      const data = await api.get('/api/meta');
      Object.assign(meta, {
        cameras: data.cameras,
        defect_types: data.defect_types,
        sample_rate_hz: data.sample_rate_hz,
        max_relative_target: data.max_relative_target,
        stream_port: data.stream_port ?? 0,
        next_episode_id: data.next_episode_id ?? '',
        loaded: true,
      });
    } catch { /* ports 폴링이 meta.loaded 가 될 때까지 다시 시도한다 */ }
  }

  async function refreshStatus() {
    const { ok, ...rest } = await api.get('/api/status');
    Object.assign(status, rest);
    status.loaded = true;
    status.error = null;
  }

  function applyPorts(data) {
    ports.arms = data.arms;
    ports.cameras = data.cameras;
    ports.serial_ports = data.serial_ports || [];
    ports.connected_count = data.connected_count;
    ports.total_count = data.total_count;
    ports.loaded = true;
    ports.error = null;
    meta.simulate = data.simulate;
  }

  async function refreshPorts() {
    busy.ports = true;
    try {
      const data = await api.post('/api/ports/refresh');
      applyPorts(data);
      notify(`포트 스캔 완료 — ${data.connected_count}/${data.total_count} 연결됨`);
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      busy.ports = false;
    }
  }

  /* ---------------- 컨베이어 (레거시 app.js:487-555 이식, 정리하지 않았다) ---------------- */
  const wasRunning = {};          // 벨트별 직전 running — true→false 전이로 '정지' 를 감지
  const stopListeners = new Set();

  function onBeltStopped(fn) {
    stopListeners.add(fn);
    return () => stopListeners.delete(fn);
  }

  /* 서버의 전 벨트 스냅샷을 병합한다. 사용자가 입력 중인 rpmInput 은
     폴링이 덮어쓰지 않도록 보존한다(입력 도중 값이 되돌아가면 미치게 답답하다). */
  // 비상 정지를 누른 직후 몇 초의 정지는 '정상 정지'가 아니다. 응답보다 먼저 도착한 일반 폴링이
  // 그 전이를 보더라도 자동 촬영이 나가지 않게 한다.
  const ESTOP_QUIET_MS = 5000;
  let quietUntil = 0;

  /* '목표 위치' 입력칸의 값(moveInput)을 실제 위치(mm)에 맞춘다. 칸이 보이는 값은 이것이 단일 출처다.
     안 맞추면 원점·끝점을 새로 잡은 뒤에도 칸이 옛 위치에 남아, 그대로 '이동' 을 누르면 엉뚱한 곳으로 간다.
     움직이는 동안은 건드리지 않는다(이동 목표가 곧 칸의 값이다). 사용자가 치는 중인 값은
     화면(equipment.js)의 moveDraft 가 따로 쥐고 이 값을 가린다. 폴링 병합과 명령 응답 병합이 같이 쓴다. */
  function syncMoveInput(belt) {
    if (!belt.running && belt.mm_per_rev) belt.moveInput = Math.round((belt.pos_rev || 0) * belt.mm_per_rev);
  }

  function applyConveyors(list, suppressAutoCapture = false) {
    if (!Array.isArray(list)) return;
    const stopped = [];
    for (const b of list) {
      if (wasRunning[b.name] && !b.running) stopped.push(b.name);
      wasRunning[b.name] = !!b.running;
    }
    if (stopped.length && !suppressAutoCapture && Date.now() >= quietUntil) {
      // 사이클 중에는 사이클이 직접 찍는다. 수동 촬영은 서버가 409 로 거절해서 정지 때마다 오류 토스트만 쌓인다.
      if (capture.auto && !capture.busy && !busy.recon && !store.cycleRunning) captureNow();
      stopListeners.forEach((fn) => {
        try { fn({ names: stopped }); } catch (err) { console.error(err); }
      });
    }
    const prev = Object.fromEntries(store.conveyors.map((b) => [b.name, b]));
    store.conveyors = list.map((b) => {
      const belt = prev[b.name];
      // 이미 있는 벨트는 '제자리'로 갱신한다. 새 객체로 갈아끼우면 진행 중인
      // 명령이 들고 있던 참조가 화면에서 떨어져 나가, 완료 시점에 푸는
      // belt.busy = false 가 버려진 객체에만 적용된다 — 버튼이 잠긴 채 굳는다.
      if (belt) {
        Object.assign(belt, b);
        // 명령이 오가는 동안에는 건드리지 않는다 — 그 응답이 applyBelt 로 맞춘다.
        if (!store.beltBusy[b.name]) syncMoveInput(belt);
        return belt;
      }
      return {
        // rpmInput/moveInput 은 사용자가 조작 중인 값이라 서버 응답에 없다
        // (폴링이 못 덮어쓴다). 처음 만들 때만 현재 값으로 맞춰둔다.
        rpmInput: b.target_rpm ? Math.round(b.target_rpm * 100) / 100 : 2,
        moveInput: Math.round((b.pos_rev || 0) * (b.mm_per_rev || 0)),
        ...b,
      };
    });
  }

  async function refreshConveyors(suppressAutoCapture = false) {
    applyConveyors((await api.get('/api/conveyor/status')).conveyors, suppressAutoCapture);
    store.conveyorsLoaded = true;
    store.conveyorsError = null;
  }

  /* 명령 응답(벨트 하나의 평면 상태)을 해당 벨트에 병합.

     연결 여부만은 같은 포트를 쓰는 축에도 함께 반영한다. 포트는 보드 단위라
     한 축이 열면 세 축이 다 열린 것인데, 명령 응답은 그 축의 상태만 담고 있어
     다음 폴링(5초)까지 나머지 카드가 'PORT CLOSED' 로 남아 보였다. */
  function applyBelt(data) {
    const belt = store.conveyors.find((b) => b.name === data.name);
    if (belt) {
      Object.assign(belt, data);
      syncMoveInput(belt);
    }
    if (data.port && data.connected !== undefined) {
      store.conveyors.forEach((b) => {
        if (b !== belt && b.port === data.port) b.connected = data.connected;
      });
    }
  }

  /* 잠금은 벨트별이다. 전역 플래그를 쓰면 한 벨트를 조작하는 동안 나머지 축의
     버튼까지 전부 잠겨(정지 버튼 포함) '전부 멈춘 것처럼' 보인다 — 축이 3개가
     되면서 실제로 그렇게 보였다. 축끼리는 서로 독립이어야 한다.
     정지는 잠겨 있어도 나가므로 같은 벨트의 명령이 겹친다. 그래서 불리언이 아니라 개수로 센다 —
     먼저 끝난 명령이 잠금을 풀면 아직 진행 중인 명령 위로 다른 버튼이 열린다. */
  async function beltCall(belt, fn, message) {
    const key = belt.name;
    store.beltBusy[key] = (store.beltBusy[key] || 0) + 1;
    try {
      applyBelt(await fn());
      if (message) notify(`${belt.label || belt.name} — ${message}`);
      return true;
    } catch (err) {
      notify(`${belt.label || belt.name}: ${err.message}`, 'error');
      return false;
    } finally {
      store.beltBusy[key] -= 1;
    }
  }

  const isBeltBusy = (belt) => !!store.beltBusy[belt.name];

  /* ---------------- 비상 정지 ---------------- */
  /* 요청 자체는 estop.js(바닐라)가 보낸다 — Vue 와 CDN 이 죽어도 눌려야 해서다.
     여기서는 끝난 뒤의 화면 반영만 한다. 확인창은 없다. */
  function onEstop(event) {
    const d = event.detail;
    if (!d.error) {
      store.estop.lastAt = d.at;
      store.estop.lastOk = d.ok;
      store.estop.failed = d.failed;
    }
    if (d.error) {
      notify(`비상 정지 실패 — ${d.error}. 다시 누르거나 장치 전원을 직접 차단하세요.`, 'fault');
    } else if (!d.ok) {
      notify(`비상 정지 일부 실패: ${d.failed.join(', ')} — 해당 장치를 직접 확인하세요.`, 'fault');
    } else {
      notify('비상 정지 — 텔레오퍼레이션 · 컨베이어 · 녹화를 세웠습니다. 다시 움직이려면 각 장치를 직접 시작하세요.', 'fault');
    }
    // 폴링 주기를 기다리지 않고 화면을 즉시 실제 상태로. 비상 정지 직후엔 자동 촬영 금지.
    Promise.all([refreshStatus(), refreshConveyors(true), refreshAlerts(true)]).catch(() => { /* 폴링이 곧 따라잡는다 */ });
  }

  const emergencyStop = () => RVS.estop();

  /* 비상 정지로 꺼진 스텝 드라이버만 다시 켠다 — 아무것도 움직이지 않는다. */
  async function emergencyReset() {
    try {
      const data = await api.post('/api/emergency_reset');
      // 값: 'enabled' | 'not_connected'(포트가 닫혀 건너뜀 — 실패가 아니다) | { error }
      const entries = Object.entries(data.conveyors);
      const failed = entries.filter(([, v]) => v !== 'enabled' && v !== 'not_connected').map(([k]) => k);
      const skipped = entries.filter(([, v]) => v === 'not_connected').length;
      const skipNote = skipped ? `연결 안 된 벨트 ${skipped}개는 건너뜀` : '';
      if (failed.length) notify(`드라이버 켜기 일부 실패: ${failed.join(', ')}${skipNote ? ` · ${skipNote}` : ''}`, 'error');
      else if (skipped && skipped === entries.length) notify(`켤 드라이버가 없습니다 — ${skipNote}`);
      else notify(`컨베이어 드라이버를 다시 켰습니다. 각 장치를 직접 시작하세요.${skipNote ? ` ${skipNote}.` : ''}`);
      try { await refreshConveyors(true); } catch { /* 폴링이 따라잡는다 */ }
    } catch (err) {
      notify(`드라이버 켜기 실패: ${err.message}`, 'error');
    }
  }

  /* ---------------- 촬영 (레거시 captureScan 의 래퍼) ---------------- */
  /* 컨베이어가 멈춘 지금의 5뷰를 찍는다. 서버가 벨트 정지를 재확인한다.
     자동 촬영(capture.auto)은 벨트가 멈추는 순간 applyConveyors 가 이걸 부른다. */
  async function captureNow(opts = {}) {
    if (capture.busy) return null;
    capture.busy = true;
    busy.recon = true;
    try {
      const data = await api.post('/api/scan/capture', { settle_sec: 0.5, resolution: 96, ...opts });
      capture.last = markRaw({ ...data, at: Date.now() });
      capture.error = null;
      const stats = data.mesh?.stats;
      notify(`촬영 완료 — ${data.cameras.length}대${stats ? ` · ${fmt.scanNotes(stats)}` : ` · ${data.capture_id}`}`,
             stats?.anomaly?.skipped ? 'error' : 'info');
      return data;
    } catch (err) {
      capture.error = err.message;
      notify(err.message, 'error');
      return null;
    } finally {
      capture.busy = false;
      busy.recon = false;
    }
  }

  /* ---------------- 알림 ---------------- */
  const ACK_KEY = 'rvs.alertAck';
  function loadAck() {
    try { return JSON.parse(localStorage.getItem(ACK_KEY)) || {}; } catch { return {}; }
  }
  function saveAck() {
    try { localStorage.setItem(ACK_KEY, JSON.stringify(store.acked)); } catch { /* 사생활 보호 모드 등 — 이번 세션에서만 유지 */ }
  }
  store.acked = loadAck();

  const findAlert = (x) => (x && typeof x === 'object' ? x : store.alerts.alerts.find((a) => a.id === x));

  async function refreshAlerts(refresh = false) {
    try {
      const d = await api.get(`/api/alerts${refresh ? '?refresh=1' : ''}`);
      store.alerts = {
        counts: { fault: 0, warn: 0, info: 0, ...(d.counts || {}) },
        alerts: d.alerts || [],
        generated_at: d.generated_at ?? null,
      };
      store.alertsError = null;
      store.alertsLoaded = true;
    } catch (err) {
      store.alertsError = fmt.errText(err);
    }
  }

  /* 확인은 id 가 아니라 id + since 로 기억한다. 해소됐다가 다시 생기면(since 가 바뀌면) 새 알림이다. */
  function isAcked(x) {
    const a = findAlert(x);
    return !!a && store.acked[a.id] !== undefined && store.acked[a.id] === (a.since ?? null);
  }

  function ackAlert(x, on = true) {
    const a = findAlert(x);
    if (!a) return;
    if (on) store.acked[a.id] = a.since ?? null;
    else delete store.acked[a.id];
    saveAck();
  }

  function openAlert(x, trigger) {
    const a = findAlert(x);
    if (!a) return;
    // 열 때의 스냅샷을 그린다. 폴링이 목록을 갈아끼워도 읽는 도중 내용이 바뀌지 않는다.
    store.alertDialog.alert = JSON.parse(JSON.stringify(a));
    // 템플릿에서 $event 를 통째로 넘겨도 받는다
    const el = trigger instanceof Event ? trigger.currentTarget : trigger;
    store.alertDialog.trigger = el instanceof Element ? el : document.activeElement;
    store.alertDialog.open = true;
  }

  const closeAlert = () => { store.alertDialog.open = false; };

  /* ---------------- 확인 창 ---------------- */
  /* window.confirm 은 떠 있는 동안 페이지 전체를 막아 상단바 비상 정지까지 눌리지 않는다.
     rv-dialog 는 푸터에 비상 정지가 있어서 대신 쓴다. 셸이 한 개를 그리고, 여기서는 답만 Promise 로 돌려준다.
     닫는 길(확인·취소·닫기·Esc·배경 클릭)이 모두 answerConfirm 으로 모이고, 먼저 온 답이 이긴다. */
  let confirmDone = null;
  function answerConfirm(ok) {
    const done = confirmDone;
    confirmDone = null;
    store.confirmDialog.open = false;
    if (done) done(ok);
  }
  function askConfirm({ title, message, okLabel = '확인' }) {
    if (confirmDone) answerConfirm(false);   // 떠 있던 질문은 취소로 닫는다
    return new Promise((resolve) => {
      confirmDone = resolve;
      Object.assign(store.confirmDialog, { title, message, okLabel, open: true });
    });
  }

  /* ---------------- 진단 설정 · 준비도 ---------------- */
  async function refreshConfig(refresh = false) {
    config.loading = true;
    try {
      const d = await api.get(`/api/diagnostics${refresh ? '?refresh=1' : ''}`);
      config.env = d.env || [];
      config.serial = d.serial || [];
      config.cameras = d.cameras || [];
      config.limits = d.limits || null;
      config.error = null;
      config.loaded = true;
      config.fetchedAt = Date.now();
    } catch (err) {
      config.error = fmt.errText(err);
    } finally {
      config.loading = false;
    }
    return config;
  }

  async function refreshReadiness() {
    readiness.loading = true;
    try {
      const d = await api.get('/api/readiness');
      readiness.goals = d.goals || null;
      readiness.act_episodes = d.act_episodes ?? 0;
      readiness.good_objects = d.good_objects ?? 0;
      readiness.defect_objects = d.defect_objects ?? 0;
      readiness.error = null;
      readiness.loaded = true;
    } catch (err) {
      readiness.error = fmt.errText(err);
    } finally {
      readiness.loading = false;
    }
  }

  /* ---------------- 자동 사이클 ---------------- */
  /* cycle.data = 서버 CYCLE 객체({ state, step, steps[], message, error, result, baskets, ready, completed }).
     cycle.supported=false 면 서버에 /api/cycle 이 없다(404) — 셸이 위젯을 숨긴다.
     cycleRunning 은 state === 'running'. 화면들은 이 값으로 수동 조작을 잠근다(서버도 따로 거부한다). */
  function applyCycle(c) {
    store.cycle.data = c;
    store.cycle.loaded = true;
    store.cycle.error = null;
    store.cycle.supported = true;
    store.cycleRunning = !!c && c.state === 'running';
  }

  async function refreshCycle() {
    // /api/status 에 cycle 키가 있으면 그것을 쓴다(요청 하나 절약). 없으면 전용 엔드포인트.
    if (status.cycle) { applyCycle(status.cycle); return; }
    try {
      applyCycle((await api.get('/api/cycle/status')).cycle);
    } catch (err) {
      if (err.status === 404) { store.cycle.supported = false; store.cycleRunning = false; } else store.cycle.error = fmt.errText(err);
    }
  }

  async function cycleCall(path, body) {
    store.cycle.busy = true;
    try {
      applyCycle((await api.post(path, body)).cycle);
      refreshStatus().catch(() => {});
      return true;
    } catch (err) {
      notify(err.message, 'error');
      return false;
    } finally {
      store.cycle.busy = false;
    }
  }
  const cycleStart = () => cycleCall('/api/cycle/start');
  const cycleStop = () => cycleCall('/api/cycle/stop');   // 확인 없이 바로 — 정지는 늘 안전한 방향
  const emptyBaskets = (basket = 'all') => cycleCall('/api/cycle/baskets/empty', { basket });

  /* ---------------- 폴링 ----------------
     녹화 중엔 프레임 카운터가 살아있어야 하므로 빠르게, 유휴 시엔 느리게. 다음 호출은
     이번 호출이 끝난 뒤에 예약한다 — 서버가 느릴 때 요청이 겹쳐 쌓이지 않는다. */
  function loop(fn, delay) {
    let timer = null;
    let running = false;
    const tick = async () => {
      clearTimeout(timer);
      if (running) return;   // 진행 중이면 그 호출이 끝나며 새 간격으로 다시 예약한다
      running = true;
      try { await fn(); } finally {
        running = false;
        timer = setTimeout(tick, typeof delay === 'function' ? delay() : delay);
      }
    };
    tick();
    return tick;   // 바로 한 번 더 돌리고 싶을 때 부른다(대기 중인 타이머는 취소된다)
  }

  let failures = 0;
  async function pollStatus() {
    try {
      await refreshStatus();
      failures = 0;
      if (!store.online) {
        store.online = true;
        notify('서버에 다시 연결했습니다.');
        // 끊긴 동안 못 읽은 값을 바로 다시 읽는다
        loadMeta(); refreshAlerts(); refreshConfig(); refreshReadiness();
      }
    } catch (err) {
      // 폴링 실패는 토스트를 띄우지 않는다. 서버 재시작 중에 화면이 알림으로 덮인다.
      // 대신 status.error 로 노출하고, 연속 3회 실패하면 셸이 '서버 연결 끊김' 배너를 띄운다.
      status.error = fmt.errText(err);
      if (++failures >= 3) store.online = false;
    }
  }

  async function pollPorts() {
    if (!meta.loaded) await loadMeta();
    try {
      applyPorts(await api.get('/api/ports'));
    } catch (err) { ports.error = fmt.errText(err); }
  }

  // 컨베이어 상태는 포트를 열지 않고 읽는다(release 한 포트를 폴링이 다시 잡으면
  // 펌웨어 업로드(DFU)를 방해한다). 서버 쪽에서 보장한다.
  async function pollConveyors() {
    try {
      await refreshConveyors();
    } catch (err) { store.conveyorsError = fmt.errText(err); }
  }

  /* ---------------- 라이프사이클 ---------------- */
  function warnIfRecording(event) {
    if (!isRecording()) return;
    event.preventDefault();
    event.returnValue = '';   // 녹화 중 새로고침 = 에피소드 유실
  }

  let started = false;
  function start() {
    if (started) return;
    started = true;
    window.addEventListener('beforeunload', warnIfRecording);
    window.addEventListener('rvs:estop-start', () => { quietUntil = Date.now() + ESTOP_QUIET_MS; });
    window.addEventListener('rvs:estop', onEstop);
    refreshConfig();
    loop(pollStatus, () => (isRecording() ? 500 : store.cycleRunning ? 1000 : 1500));
    // 포트(시리얼 열거 포함)는 늘 5초. 자동 촬영 때문에 빨라지는 건 벨트 폴링뿐이다.
    loop(pollPorts, 5000);
    // 자동 촬영은 정지 순간을 놓치지 않도록 벨트를 1초로 줄인다(status 는 포트를 안 열고 락도 안 기다린다).
    const kickBelts = loop(pollConveyors, () => (capture.auto ? 1000 : 5000));
    // 스위치를 켠 순간부터 빨라지게 한다 — 안 그러면 이미 걸려 있는 5초 타이머가 끝날 때까지
    // 그 사이의 시작→정지를 못 본다.
    watch(() => capture.auto, (on) => { if (on) kickBelts(); });
    // 없는 API(404)를 10초마다 두드리지 않게, 실패했으면 간격을 늘린다.
    loop(refreshAlerts, () => (store.alertsError ? 30000 : 10000));
    loop(refreshReadiness, () => (readiness.error ? 60000 : 30000));
    // 진행 중에는 1초, 아니면 5초. 서버에 없으면(404) 60초마다만 다시 본다.
    const kickCycle = loop(refreshCycle, () => (store.cycleRunning ? 1000 : store.cycle.supported ? 5000 : 60000));
    // 시작·정지 직후에는 기다리지 않고 바로 본다(status 폴링이 cycle 키를 가져온 경우 포함)
    watch(() => status.cycle, (c) => { if (c) applyCycle(c); });
    window.addEventListener('rvs:estop', () => { kickCycle(); });
  }

  Object.assign(store, {
    notify, dismiss, go, streamUrl,
    loadMeta, refreshStatus, refreshPorts, refreshConveyors,
    applyBelt, beltCall, isBeltBusy,
    refreshAlerts, openAlert, closeAlert, askConfirm, answerConfirm, isAcked, ackAlert,
    refreshConfig, refreshReadiness,
    captureNow, onBeltStopped,
    emergencyStop, emergencyReset,
    refreshCycle, cycleStart, cycleStop, emptyBaskets,
    start,
  });

  RVS.store = store;
})();
