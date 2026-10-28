/* =============================================================
   진단 화면 (#/diagnostics) — 접두어 dg-
   실행 환경 · 직렬 포트(포트 식별) · 카메라 장치 · 안전 한계 · 리그 캘리브레이션.

   데이터
     s.config(= GET /api/diagnostics, store 가 부팅 때 읽음)  env · serial · cameras · limits
       → 이 화면에 들어올 때와 포트·식별 동작 뒤에 ?refresh=1 로 다시 읽는다(서버 30초 캐시를 건너뜀).
     GET /api/ports        팔·카메라의 실시간 연결 상태(5초마다, 화면이 켜져 있을 때만)
     GET /api/scan/calibration · POST /api/scan/calibrate/shot|solve · DELETE /api/scan/calibrate/shots

   레거시 이식
     포트 식별  app.js:430-444  (start → 확인 창(store.askConfirm) → confirm)
     캘리브레이션  app.js:1006-1053  (busy.recon 으로 잠그는 것까지 그대로)
   레거시와 다른 곳(의도)
     · 식별 직후 POST /api/ports/refresh 를 부르지 않는다. 그 API 는 방금 확정한 매칭(오버라이드)을
       지워서 레거시에서는 식별 결과가 곧바로 사라졌다. GET /api/ports 로만 다시 읽는다.
     · 연결 시험은 api.post 를 쓰지 않는다. 한 곳이라도 실패하면 서버가 ok:false 를 200 으로 주는데
       api.post 는 그걸 에러로 던져 결과 목록(어디가 왜 실패했는지)을 버린다.
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});
  RVS.screens = RVS.screens || {};

  // backend/core/rig_calibration.py 의 RMS_WARN_PX 와 같은 값 — 서버가 이 값 초과를 경고로 로그한다.
  const RMS_WARN_PX = 2;
  const POLL_MS = 5000;
  const TEST_TIMEOUT_MS = 20000;

  // /api/diagnostics env[].severity → rv-status 모양과 글자
  const ENV_KIND = {
    ok: { kind: 'run', word: '정상' },
    info: { kind: 'info', word: '정보' },
    warn: { kind: 'warn', word: '주의' },
    fault: { kind: 'fault', word: '고장' },
  };

  RVS.screens.diagnostics = {
    name: 'DiagnosticsScreen',
    title: '진단',
    template: `
<section class="dg-screen" aria-labelledby="dg-h">
  <header class="rv-head">
    <h1 id="dg-h" tabindex="-1">진단</h1>
    <p>포트·카메라 장치·보정·실행 환경. 문제가 생기면 여기서 원인을 찾습니다.</p>
  </header>

  <rv-state v-if="cfg.loaded && cfg.error" state="error" :message="'최신 값을 읽지 못했습니다 — ' + cfg.error" @retry="reloadConfig" />

  <!-- 실행 환경 -->
  <section class="rv-card" aria-labelledby="dg-env-h">
    <h2 id="dg-env-h">실행 환경</h2>
    <rv-state v-if="cfgState !== 'ready'" :state="cfgState" :message="cfg.error" @retry="reloadConfig" />
    <rv-state v-else-if="!envRows.length" state="empty" message="점검 결과가 없습니다." hint="서버가 환경 점검 항목을 주지 않았습니다. 서버를 새 코드로 다시 시작했는지 확인하세요.">
      <button type="button" class="rv-btn rv-btn--sm" @click="reloadConfig">다시 읽기</button>
    </rv-state>
    <ul v-else class="dg-env">
      <li v-for="e in envRows" :key="e.id" class="dg-env__row">
        <rv-status size="md" :kind="e.kind" :label="e.word" />
        <div class="dg-env__text">
          <div><span class="dg-env__title" :class="{ 'dg-warn': e.kind === 'warn' }">{{ e.title }}</span><span v-if="e.detail" :class="{ 'rv-muted': e.kind !== 'warn' }"> — {{ e.detail }}</span></div>
          <div v-if="e.hint" class="rv-small rv-muted">{{ e.hint }}</div>
        </div>
      </li>
    </ul>
  </section>

  <!-- 직렬 포트 -->
  <section class="rv-card" aria-labelledby="dg-port-h">
    <div class="dg-card-head">
      <h2 id="dg-port-h">직렬 포트</h2>
      <div class="dg-actions">
        <button type="button" class="rv-btn" :disabled="s.busy.ports" @click="refreshPorts">{{ s.busy.ports ? '찾는 중…' : '포트 다시 찾기' }}</button>
        <button type="button" class="rv-btn" :disabled="s.busy.test" @click="testConnections">{{ s.busy.test ? '시험 중…' : '연결 시험' }}</button>
      </div>
    </div>
    <p class="rv-small rv-muted">'포트 다시 찾기'는 '포트 식별'로 확정한 매칭을 지우고, 설정 파일의 serial_number·힌트 기준으로 다시 찾습니다.</p>
    <p v-if="ports && ports.simulate" class="rv-small rv-muted">시뮬레이션 모드 — 실제 포트를 열지 않아 팔은 항상 연결된 것으로 보입니다.</p>
    <rv-state v-if="portsErr" state="error" :message="'실시간 포트 상태를 읽지 못했습니다 — ' + portsErr" @retry="loadPorts" />
    <rv-state v-if="cfgState !== 'ready'" :state="cfgState" :message="cfg.error" @retry="reloadConfig" />
    <rv-state v-else-if="!serialRows.length" state="empty" message="직렬 포트 정보가 없습니다." hint="서버가 장치 목록을 주지 않았습니다. 서버를 새 코드로 다시 시작했는지 확인하세요.">
      <button type="button" class="rv-btn rv-btn--sm" @click="reloadConfig">다시 읽기</button>
    </rv-state>
    <div v-else class="rv-table-wrap" role="region" aria-labelledby="dg-port-h" tabindex="0">
      <table class="rv-table dg-table dg-table--port dg-table--stack">
        <thead>
          <tr><th scope="col">장치</th><th scope="col">매칭</th><th scope="col">포트</th><th scope="col">상태</th><th scope="col">동작</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in serialRows" :key="r.id">
            <th scope="row">{{ r.name }}</th>
            <td data-label="매칭">
              <template v-if="r.match && r.match.by === 'serial_number'"><code>serial_number</code> 우선<template v-if="r.match.hint">, 힌트 {{ r.match.hint }}</template></template>
              <template v-else-if="r.match && r.match.by === 'port_hint'"><code>port_hint</code> {{ r.match.hint }}</template>
              <span v-else-if="r.arm" class="dg-warn">기준 없음 — 포트 식별이 필요합니다</span>
              <span v-else class="rv-muted" role="img" aria-label="매칭 규칙 없음. 설정 파일의 포트를 그대로 씁니다">—</span>
            </td>
            <td data-label="포트">
              <span v-if="r.port" class="rv-id dg-nowrap">{{ r.port }}</span>
              <span v-else class="rv-muted">찾지 못함</span>
              <div v-if="r.arm" class="rv-small rv-muted">S/N {{ r.serial_number || '—' }}</div>
            </td>
            <td data-label="상태">
              <rv-status v-if="r.connected" kind="run" label="연결" />
              <rv-status v-else-if="r.arm" kind="warn" label="연결 안 됨" />
              <rv-status v-else kind="stop" label="포트 닫힘" />
              <div v-if="r.error" class="rv-small dg-warn">{{ r.error }}</div>
              <div v-else-if="!r.connected && !r.arm" class="rv-small rv-muted">벨트를 처음 시작할 때 자동으로 연결합니다.</div>
            </td>
            <td>
              <button v-if="r.arm" type="button" class="rv-btn rv-btn--sm" :disabled="!!identifying" :aria-label="r.name + ' 포트 식별'" @click="identify(r)">{{ identifying === r.id ? '식별 중…' : '포트 식별' }}</button>
              <span v-else class="rv-muted dg-na" role="img" aria-label="식별 대상 아님">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="testResult" class="dg-test">
      <h3 id="dg-test-h">연결 시험 결과 <span class="rv-small rv-muted rv-num">{{ fmt.clock(testResult.at) }}</span></h3>
      <ul class="dg-test__list" aria-labelledby="dg-test-h">
        <li v-for="r in testRows" :key="r.role" class="dg-test__row">
          <rv-status :kind="r.ok ? 'run' : 'warn'" :label="r.ok ? '정상' : '실패'" />
          <div>
            <div class="dg-test__name">{{ r.name }}</div>
            <div class="rv-small rv-muted">{{ r.message }}</div>
          </div>
        </li>
      </ul>
    </div>
  </section>

  <!-- 카메라 장치 -->
  <section class="rv-card" aria-labelledby="dg-cam-h">
    <h2 id="dg-cam-h">카메라 장치</h2>
    <rv-state v-if="cfgState !== 'ready'" :state="cfgState" :message="cfg.error" @retry="reloadConfig" />
    <rv-state v-else-if="!camRows.length" state="empty" message="설정된 카메라가 없습니다." hint="config.yaml 의 cameras 항목을 확인한 뒤 서버를 다시 시작하세요.">
      <button type="button" class="rv-btn rv-btn--sm" @click="reloadConfig">다시 읽기</button>
    </rv-state>
    <div v-else class="rv-table-wrap" role="region" aria-labelledby="dg-cam-h" tabindex="0">
      <table class="rv-table dg-table dg-table--cam dg-table--stack">
        <thead>
          <tr><th scope="col">슬롯</th><th scope="col">장치</th><th scope="col">설정</th><th scope="col">ROI</th><th scope="col" class="dg-nowrap">3D 재구성</th><th scope="col">상태</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in camRows" :key="c.slot">
            <th scope="row"><span class="dg-nowrap">{{ c.name }}</span><div class="rv-small rv-muted rv-id">{{ c.slot }}</div></th>
            <td class="dg-nowrap" data-label="장치">{{ c.device || '—' }}</td>
            <td class="rv-num" data-label="설정">
              <div>{{ c.settingsMain }}</div>
              <div v-if="c.settingsTune" class="rv-muted">{{ c.settingsTune }}</div>
            </td>
            <td data-label="ROI">
              <div v-if="c.roi" class="dg-roi-cell">
                <svg class="dg-roi" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true" focusable="false">
                  <rect class="dg-roi__frame" x="0" y="0" width="1" height="1" />
                  <rect class="dg-roi__box" :x="c.roi[0]" :y="c.roi[1]" :width="c.roi[2] - c.roi[0]" :height="c.roi[3] - c.roi[1]" />
                </svg>
                <span class="rv-num dg-nowrap">{{ c.roiText }}</span>
              </div>
              <span v-else-if="c.used_in_3d" class="rv-muted">지정 안 함</span>
              <span v-else class="rv-muted" role="img" aria-label="해당 없음">—</span>
            </td>
            <td data-label="3D 재구성">{{ c.used_in_3d ? '사용' : '미사용' }}</td>
            <td data-label="상태">
              <rv-status v-if="c.connected" kind="run" label="연결" />
              <rv-status v-else kind="warn" label="신호 없음" />
              <div v-if="c.connected && c.fpsText" class="rv-small rv-num dg-nowrap" :class="c.fpsLow ? 'dg-warn' : 'rv-muted'">{{ c.fpsText }}</div>
              <div v-if="c.error" class="rv-small dg-warn">{{ c.error }}</div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <!-- 안전 한계 -->
  <section class="rv-card" aria-labelledby="dg-lim-h">
    <h2 id="dg-lim-h">안전 한계</h2>
    <rv-state v-if="cfgState !== 'ready'" :state="cfgState" :message="cfg.error" @retry="reloadConfig" />
    <rv-state v-else-if="!cfg.limits" state="empty" message="안전 한계 값을 받지 못했습니다." hint="서버를 새 코드로 다시 시작했는지 확인하세요.">
      <button type="button" class="rv-btn rv-btn--sm" @click="reloadConfig">다시 읽기</button>
    </rv-state>
    <template v-else>
      <dl class="dg-facts">
        <div>
          <dt>한 틱당 최대 변화량<span class="dg-key rv-id">safety.max_relative_target</span></dt>
          <dd class="rv-num">{{ fmt.num(cfg.limits.max_relative_target, 1) }}°</dd>
        </div>
        <div>
          <dt>카메라 동기 오차 한계<span class="dg-key rv-id">recording.max_sync_skew_ms</span></dt>
          <dd class="rv-num">{{ fmt.num(cfg.limits.max_sync_skew_ms, 1) }} ms</dd>
        </div>
        <div>
          <dt>샘플링<span class="dg-key rv-id">recording.sample_rate_hz</span></dt>
          <dd class="rv-num">{{ fmt.num(cfg.limits.sample_rate_hz, 0) }} Hz</dd>
        </div>
      </dl>
      <h3 id="dg-joint-h">관절 한계와 홈 자세</h3>
      <div class="rv-table-wrap" role="region" aria-labelledby="dg-joint-h" tabindex="0">
        <table class="rv-table dg-table dg-table--joint">
          <thead>
            <tr><th scope="col">관절</th><th scope="col" class="rv-num-cell">최소</th><th scope="col" class="rv-num-cell">최대</th><th scope="col" class="rv-num-cell">홈(팔로워)</th></tr>
          </thead>
          <tbody>
            <tr v-for="j in jointRows" :key="j.n">
              <th scope="row">{{ j.name }}</th>
              <td class="rv-num-cell">{{ fmt.deg(j.min) }}</td>
              <td class="rv-num-cell">{{ fmt.deg(j.max) }}</td>
              <td class="rv-num-cell">{{ fmt.deg(j.home) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="rv-small rv-muted">설정 위치: <span class="rv-id">safety.joint_limits</span>, <span class="rv-id">arms.follower.home_position</span>. 6번 관절(그리퍼)의 한계·홈 값은 정밀 실측값이 아니라 넉넉하게 잡은 값입니다. 그리퍼가 걸린 동작(홈 복귀, rollout)을 처음 돌릴 때는 저속으로 가까이서 지켜보세요.</p>
    </template>
  </section>

  <!-- 리그 캘리브레이션 -->
  <section class="rv-card" aria-labelledby="dg-cal-h">
    <h2 id="dg-cal-h">리그 캘리브레이션</h2>
    <rv-state v-if="!cal.loaded && cal.error" state="error" :message="cal.error" @retry="loadCalib" />
    <rv-state v-else-if="!cal.loaded" state="loading" />
    <div v-else class="dg-cal">
      <div class="dg-cal__main">
        <rv-state v-if="cal.error" state="error" :message="'최신 값을 읽지 못했습니다 — ' + cal.error" @retry="loadCalib" />
        <div class="dg-cal__state">
          <span class="rv-small rv-muted">상태</span>
          <rv-status v-if="cal.calibrated" kind="run" size="md" label="보정됨" />
          <rv-status v-else kind="info" size="md" label="미보정" />
          <span class="dg-cal__stext">{{ calText }}</span>
        </div>
        <ol class="rv-steps">
          <li><span class="rv-steps__n" aria-hidden="true">1</span><div class="rv-steps__t"><code>python scripts/make_calib_cube.py</code> 로 큐브 전개도(A4)를 실제 크기(배율 100%)로 인쇄합니다. 50mm 눈금으로 크기를 확인하세요.</div></li>
          <li><span class="rv-steps__n" aria-hidden="true">2</span><div class="rv-steps__t">오려 접어 50mm 정육면체에 붙입니다.</div></li>
          <li><span class="rv-steps__n" aria-hidden="true">3</span><div class="rv-steps__t">컨베이어를 멈추고 큐브를 검사 위치에 놓습니다. 첫 촬영은 '앞(ID2)' 면이 C2·C4 사이를 향하게 합니다.</div></li>
          <li><span class="rv-steps__n" aria-hidden="true">4</span><div class="rv-steps__t">'캘리브레이션 촬영'을 3~5번 합니다. 촬영마다 큐브를 조금씩 돌리고, 그중 1~2장은 기울여서 찍습니다.</div></li>
          <li><span class="rv-steps__n" aria-hidden="true">5</span><div class="rv-steps__t">'계산'을 누르고 카메라별 RMS를 확인합니다(2 px 이하 권장).</div></li>
        </ol>
        <div class="dg-cal__actions">
          <button type="button" class="rv-btn rv-btn--primary" :disabled="s.busy.recon" @click="calibrationShot">캘리브레이션 촬영 ({{ cal.pending_shots }})</button>
          <button type="button" class="rv-btn" :disabled="s.busy.recon || !cal.pending_shots" @click="calibrationSolve">계산</button>
          <button type="button" class="rv-btn" :disabled="s.busy.recon || !cal.pending_shots" @click="calibrationClear">샷 초기화</button>
        </div>
        <p class="rv-small rv-muted">촬영만 하며 모터는 움직이지 않습니다. 컨베이어가 움직이는 중이면 촬영을 거부하니 먼저 멈추세요.</p>
        <rv-status v-if="cal.missing.length" kind="warn" :label="'마지막 샷에서 마커를 못 찾은 카메라: ' + cal.missing.map(camLabel).join(', ')" />
      </div>

      <div class="dg-cal__result">
        <h3 id="dg-cal-res-h">카메라별 결과</h3>
        <rv-state v-if="!calRows.length" state="empty" message="보정 결과가 아직 없습니다." hint="캘리브레이션 촬영을 한 뒤 '계산'을 누르면 카메라별 오차가 여기에 나옵니다." />
        <div v-else class="rv-table-wrap" role="region" aria-labelledby="dg-cal-res-h" tabindex="0">
          <table class="rv-table dg-table dg-table--cal">
            <thead>
              <tr><th scope="col">카메라</th><th scope="col" class="rv-num-cell">검출 마커</th><th scope="col" class="rv-num-cell">RMS(px)</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in calRows" :key="r.slot">
                <th scope="row">
                  {{ r.name }}
                  <div v-if="r.failed" class="rv-small dg-warn">계산 실패 — {{ r.failed }}</div>
                  <div v-if="r.c && r.c.focal_fixed" class="rv-small dg-warn">초점거리 가정값 — 기울인 샷을 더 찍고 다시 계산하세요</div>
                  <div v-if="r.rmsHigh" class="rv-small dg-warn">오차 {{ RMS_WARN_PX }} px 초과 — 마커가 흐리거나 작게 찍혔을 수 있습니다</div>
                </th>
                <td class="rv-num-cell">
                  <template v-if="r.c && r.c.markers_seen">{{ r.c.markers_seen.length }}</template>
                  <span v-else class="rv-muted" role="img" aria-label="값 없음">—</span>
                </td>
                <td class="rv-num-cell">
                  <rv-status v-if="r.rmsHigh" kind="warn" :label="fmt.num(r.c.rms, 2)" />
                  <template v-else-if="r.c && r.c.rms != null">{{ fmt.num(r.c.rms, 2) }}</template>
                  <span v-else class="rv-muted" role="img" aria-label="값 없음">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>
</section>`,

    setup() {
      const s = RVS.store;
      const { api, fmt } = RVS;
      const { ref, reactive, computed, onMounted, onActivated, onDeactivated, onUnmounted } = Vue;

      // store.config 는 제자리에서만 갱신된다(store.js:120) — 변수로 잡아도 반응형이 유지된다.
      const cfg = s.config;
      const cfgState = computed(() => (cfg.loaded ? 'ready' : cfg.error ? 'error' : 'loading'));
      const reloadConfig = () => s.refreshConfig(true);

      /* ---------------- 실시간 포트 ---------------- */
      const ports = ref(null);        // GET /api/ports 응답
      const portsErr = ref('');
      const identifying = ref('');    // 식별 중인 role. 식별하는 동안은 폴링을 멈춘다
      const testResult = ref(null);   // { at, results[] }

      async function loadPorts() {
        try {
          ports.value = await api.get('/api/ports');
          portsErr.value = '';
        } catch (err) {
          portsErr.value = fmt.errText(err);
        }
      }

      /* ---------------- 환경 ---------------- */
      const envRows = computed(() => (cfg.env || []).map((e) => ({ ...e, ...(ENV_KIND[e.severity] || ENV_KIND.info) })));

      /* ---------------- 직렬 포트 표 ----------------
         행은 진단(설정 기준), 연결 상태는 실시간 값을 덮어쓴다. 진단 응답은 서버가 30초 캐시한다. */
      const serialRows = computed(() => (cfg.serial || []).map((r) => {
        const arm = r.id === 'leader' || r.id === 'follower';
        const row = { ...r, arm };
        const live = arm && ports.value && ports.value.arms.find((a) => a.role === r.id);
        if (live) {
          Object.assign(row, { connected: live.connected, port: live.device, serial_number: live.serial_number, error: live.error });
        } else if (r.axes && r.axes.length) {
          // 컨베이어 보드: 벨트 상태 폴링(store)이 더 새롭다. 벨트 전부가 보여야 덮어쓴다.
          const belts = r.axes.map((n) => s.conveyors.find((c) => c.name === n));
          if (belts.every(Boolean)) row.connected = belts.every((b) => b.connected);
        }
        return row;
      }));

      async function refreshPorts() {
        await s.refreshPorts();   // POST /api/ports/refresh + 토스트 (store.js)
        await Promise.all([loadPorts(), s.refreshConfig(true)]);
      }

      // 한 곳이라도 실패하면 서버가 ok:false 를 200 으로 준다 — api.post 는 그걸 던져서 쓸 수 없다(머리 주석).
      async function postTest() {
        const abort = new AbortController();
        const timer = setTimeout(() => abort.abort(), TEST_TIMEOUT_MS);
        try {
          const res = await fetch('/api/ports/test', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', signal: abort.signal,
          });
          const data = await res.json().catch(() => null);
          if (!data || !Array.isArray(data.results)) {
            throw new Error((data && data.error && data.error.message) || `요청 실패 (HTTP ${res.status})`);
          }
          return data;
        } catch (err) {
          if (err.name === 'AbortError') throw new Error(`서버가 ${TEST_TIMEOUT_MS / 1000}초 안에 응답하지 않았습니다 (/api/ports/test).`);
          if (err instanceof TypeError) throw new Error(`서버에 연결할 수 없습니다 (/api/ports/test): ${err.message}`);
          throw err;
        } finally {
          clearTimeout(timer);
        }
      }

      async function testConnections() {
        s.busy.test = true;
        try {
          const data = await postTest();
          testResult.value = { at: Date.now(), results: data.results };
          const failed = data.results.filter((r) => !r.ok).length;
          if (failed === 0) s.notify('모든 장치 통신 정상');
          else s.notify(`연결 시험 — ${failed}곳 실패. 아래 결과에서 원인을 확인하세요.`, 'error');
        } catch (err) {
          s.notify(err.message, 'error');
        } finally {
          s.busy.test = false;
        }
        loadPorts();
      }

      // 연결 시험 결과의 role(leader, follower, camera_<슬롯>)을 사람이 읽는 이름으로
      function roleName(role) {
        const serial = (cfg.serial || []).find((r) => r.id === role);
        if (serial) return serial.name;
        if (role === 'leader' || role === 'follower') return `${fmt.ko.arm[role]} 팔`;
        const slot = role.startsWith('camera_') ? role.slice(7) : null;
        const cam = slot && (cfg.cameras || []).find((c) => c.slot === slot);
        return cam ? fmt.camName(cam.label || cam.slot) : role;
      }
      const testRows = computed(() => (testResult.value ? testResult.value.results : []).map((r) => ({ ...r, name: roleName(r.role) })));

      // 레거시 app.js:430-444 — CLI 의 blocking input() 대신 확인 창으로 2단계를 잇는다.
      // window.confirm 은 떠 있는 동안 상단바 비상 정지까지 막는다(store.askConfirm 은 푸터에 비상 정지가 있다).
      async function identify(row) {
        if (identifying.value) return;
        identifying.value = row.id;
        try {
          await api.post('/api/ports/identify/start', { role: row.id });
          const proceed = await s.askConfirm({
            title: `${row.name} 포트 식별`,
            message:
              `${row.name}의 USB 케이블을 지금 뽑으세요.\n` +
              '뽑은 뒤 [확인]을 누르면 사라진 포트를 이 팔로 확정합니다.\n\n' +
              '팔 이름이 맞는지 다시 확인하세요. 리더와 팔로워가 바뀌면 하드웨어가 손상될 수 있습니다.\n' +
              '텔레오퍼레이션이나 녹화 중이면 [취소]를 누르고 먼저 멈추세요.',
          });
          if (!proceed) return;
          const data = await api.post('/api/ports/identify/confirm', { role: row.id });
          s.notify(`${row.name} = ${data.device} 로 확정했습니다. 케이블을 다시 연결하세요.`);
          // 레거시처럼 POST /api/ports/refresh 로 다시 읽으면 방금 확정한 매칭이 지워진다
          await Promise.all([loadPorts(), s.refreshConfig(true)]);
        } catch (err) {
          s.notify(err.message, 'error');
        } finally {
          identifying.value = '';
        }
      }

      /* ---------------- 카메라 ---------------- */
      // 설정 칸 두 줄: 촬영 형식 / 노출·전원 주파수·뒤집기
      function camSettings(c) {
        const st = c.settings || {};
        const main = [];
        if (st.width && st.height) main.push(`${st.width}×${st.height}`);
        if (st.fps != null) main.push(`${st.fps}fps`);
        if (st.fourcc) main.push(st.fourcc);
        const tune = [st.exposure == null ? '노출 자동' : `노출 ${String(st.exposure).replace('-', '−')}`];
        if (st.power_line_hz != null) tune.push(`${st.power_line_hz}Hz`);
        if (st.vflip && st.hflip) tune.push('상하·좌우 반전');
        else if (st.vflip) tune.push('상하 반전');
        else if (st.hflip) tune.push('좌우 반전');
        return { main: main.join(' · '), tune: tune.join(' · ') };
      }

      const pctInt = (v) => Math.round(v * 100);

      const camRows = computed(() => (cfg.cameras || []).map((c) => {
        const live = ports.value && ports.value.cameras.find((x) => x.name === c.slot);
        const settings = camSettings(c);
        const fps = live && live.fps != null ? Number(live.fps) : null;
        const target = live ? Number(live.target_fps) : null;
        // 목표의 fmt.FPS_LOW_RATIO 미만이면 경고색(개요·장비 제어와 같은 기준)
        const fpsLow = fps != null && target > 0 && fps < target * fmt.FPS_LOW_RATIO;
        return {
          ...c,
          name: fmt.camName(c.label || c.slot),
          device: (live && live.device) || c.device,
          connected: live ? live.connected : c.connected,
          error: live ? live.error : c.error,
          settingsMain: settings.main,
          settingsTune: settings.tune,
          roiText: c.roi ? `가로 ${pctInt(c.roi[0])}–${pctInt(c.roi[2])}% · 세로 ${pctInt(c.roi[1])}–${pctInt(c.roi[3])}%` : '',
          fpsLow,
          fpsText: fps == null ? '' : `실측 ${fps.toFixed(1)} fps${target > 0 ? ` (목표 ${target})` : ''}${fpsLow ? ' · 프레임 낮음' : ''}`,
        };
      }));

      // 슬롯 이름(cam1) → 화면 이름(C1 하단). 진단에 없으면 슬롯 그대로
      function camLabel(slot) {
        const c = (cfg.cameras || []).find((x) => x.slot === slot);
        return c ? fmt.camName(c.label || c.slot) : slot;
      }

      /* ---------------- 안전 한계 ---------------- */
      const jointRows = computed(() => {
        const l = cfg.limits;
        if (!l) return [];
        const limits = l.joint_limits || [];
        const home = l.home || [];
        return Array.from({ length: Math.max(limits.length, home.length) }, (_, i) => ({
          n: i + 1,
          name: fmt.jointName(i),
          min: limits[i] ? limits[i][0] : null,
          max: limits[i] ? limits[i][1] : null,
          home: home[i],
        }));
      });

      /* ---------------- 리그 캘리브레이션 (레거시 app.js:1006-1053) ---------------- */
      // 서버 rig_summary 그대로 + 마지막 샷에서 마커를 못 본 카메라(missing). 레거시 scan.calib 와 같은 모양.
      const cal = reactive({
        loaded: false, error: '', calibrated: false, created_at: null,
        cameras: {}, failed: {}, pending_shots: 0, missing: [],
      });

      async function loadCalib() {
        try {
          Object.assign(cal, await api.get('/api/scan/calibration'), { loaded: true, error: '' });
        } catch (err) {
          cal.error = fmt.errText(err);
        }
      }

      const calText = computed(() => (cal.calibrated
        ? `${fmt.dateTime(String(cal.created_at || '').replace(' ', 'T'))} 보정 · 원근 투영 · 치수(mm) 표시`
        : '공칭 각도 + 정사영 근사 — 치수(mm)는 표시되지 않습니다'));

      // 3D 재구성에 쓰는 카메라를 먼저(진단 순서), 그 밖에 결과가 있는 키를 뒤에
      const calRows = computed(() => {
        const slots = (cfg.cameras || []).filter((c) => c.used_in_3d).map((c) => c.slot);
        [...Object.keys(cal.cameras), ...Object.keys(cal.failed)].forEach((k) => { if (!slots.includes(k)) slots.push(k); });
        return slots.map((slot) => {
          const c = cal.cameras[slot] || null;
          return {
            slot, c, name: camLabel(slot), failed: cal.failed[slot] || '',
            rmsHigh: !!c && c.rms != null && c.rms > RMS_WARN_PX,
          };
        });
      });

      async function calibrationShot() {
        if (s.busy.recon) return;
        s.busy.recon = true;
        try {
          const d = await api.post('/api/scan/calibrate/shot', { settle_sec: 0.5 });
          cal.pending_shots = d.pending_shots;
          cal.missing = d.missing;
          const names = d.missing.map(camLabel).join(', ');
          s.notify(d.missing.length
            ? `샷 ${d.shot + 1} 저장 — ${names} 에서 마커를 못 찾았습니다. `
              + (d.shot === 0 ? '기준 샷이니 큐브를 옮겨 다시 찍으세요(샷 초기화)' : '이 샷은 그 카메라에 안 쓰입니다')
            : `샷 ${d.shot + 1} 저장 — 모든 카메라에서 마커 검출`,
          d.missing.length && d.shot === 0 ? 'error' : undefined);
        } catch (err) {
          s.notify(err.message, 'error');
        } finally {
          s.busy.recon = false;
        }
      }

      async function calibrationSolve() {
        if (s.busy.recon) return;
        s.busy.recon = true;
        try {
          Object.assign(cal, await api.post('/api/scan/calibrate/solve'), { missing: [] });
          const fixed = Object.entries(cal.cameras).filter(([, c]) => c.focal_fixed).map(([n]) => camLabel(n));
          s.notify(`캘리브레이션 저장 — ${Object.keys(cal.cameras).length}대`
            + (fixed.length ? ` · ${fixed.join(', ')} 는 초점거리 가정값(기울인 샷을 추가하세요)` : ''),
          fixed.length ? 'error' : undefined);
        } catch (err) {
          s.notify(err.message, 'error');
        } finally {
          s.busy.recon = false;
        }
      }

      async function calibrationClear() {
        try {
          Object.assign(cal, await api.del('/api/scan/calibrate/shots'), { missing: [] });
          s.notify('캘리브레이션 샷을 지웠습니다(저장된 캘리브레이션은 그대로)');
        } catch (err) {
          s.notify(err.message, 'error');
        }
      }

      /* ---------------- 라이프사이클 (keep-alive: 비활성이면 폴링을 멈춘다) ---------------- */
      let timer = null;
      function start() {
        if (timer) return;           // onMounted 와 onActivated 가 둘 다 불려도 한 번만
        loadPorts();
        s.refreshConfig(true);
        loadCalib();
        timer = setInterval(() => {
          if (document.hidden || identifying.value) return;
          loadPorts();
        }, POLL_MS);
      }
      function stop() {
        clearInterval(timer);
        timer = null;
      }
      onMounted(start);
      onActivated(start);
      onDeactivated(stop);
      onUnmounted(stop);

      return {
        s, fmt, cfg, cfgState, reloadConfig, RMS_WARN_PX,
        ports, portsErr, loadPorts, identifying, testResult, testRows,
        envRows, serialRows, refreshPorts, testConnections, identify,
        camRows, camLabel, jointRows,
        cal, calText, calRows, loadCalib, calibrationShot, calibrationSolve, calibrationClear,
      };
    },
  };
})();
