/* =============================================================
   장비 제어 화면 — 라우트 equipment, 클래스 접두어 eq-.
   실물 모터(컨베이어 벨트·로봇 팔)를 움직이는 화면이다. 로봇 옆에 사람이 있을 때만 쓴다.

   레거시 frontend/index.html scan 섹션(:143-315)과 static/js/app.js 의 벨트 로직(:472-635),
   텔레옵·홈·포트 식별(:430-470)을 그대로 옮겼다. 호출하는 API 경로·본문·메시지는 같다(아래 달라진 점 제외).
   벨트 상태 병합(제자리 갱신)·beltBusy(이름 키)·beltCall 은 store.js 가 쥐고 있고,
   이 파일은 그 위에서 액션만 건다(s.beltCall). 벨트 객체를 새로 만들지 않는다.

   레거시와 달라진 점(의도한 것만)
   - 드라이버(토크) 해제·포트 해제는 위험색 더보기 메뉴 안으로(한 단계 추가). 레거시에는 확인창이 없었다.
   - 구동부가 스텝모터(TB6600)에서 DC 모터 + HW-095(L298N)로 바뀌고 LM 가이드가 없어졌다. 엔코더가 없어 RPM·위치를
     측정하지 못하므로 속도는 PWM 듀티 %(본문 {duty})로만 지정하고, 위치 이동·원점·끝점·리밋 표시는 지웠다.
   - 속도(%)에 −/+ 1 단위 버튼. 0~100 정수가 아니면 요청을 보내지 않고 입력칸 아래에 이유를 보인다.
     서버의 duty 는 '설정 속도'라 정지 중에도 마지막 값이 남는다(도는지는 running). 칸은 치는 중이 아니면 그 값을 따라가고
     (store.syncDutyInput), 정지 상태는 '0%' 가 아니라 '정지 · 설정 N%' 로 보인다.
   - 방향은 정지 중에만 바꾼다. 달리는 중이면 펌웨어가 ERR: RUNNING 으로 거부하므로 눌러 보기 전에 잠그고 이유를 보인다.
   - 드라이버가 꺼져 있으면 서버가 시작을 거부한다(ERR: DISABLED). 서버 문구는 그대로 두고 '드라이버 켜기를 누르세요'
     안내를 앞에 붙인다. 그 밖의 펌웨어 오류(예: ERR: TIMER CONFLICT)는 서버 문구 그대로 오류 토스트로 보인다.
   - 홈 자세로 = 확인 대화상자 한 단계 추가(레거시는 바로 실행).
   - 팔 메뉴의 '포트 식별'은 리더·팔로워 각각이라 항목이 둘이다. 토크·포트 해제는 팔에 두지 않는다(팔로워 토크를 풀면 낙하).
     식별의 확인도 window.confirm 이 아니라 store.askConfirm 이다(네이티브 확인창은 떠 있는 동안 비상 정지까지 막는다).
   - 자동 사이클 진행 중(store.cycleRunning)에는 벨트·팔·홈 복귀·드라이버 켜기를 잠그고 이유를 보인다.
     벨트 정지는 사이클 중에도, 그 벨트의 다른 명령이 진행 중이어도 잠그지 않는다(셸의 비상 정지도 마찬가지).
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});
  RVS.screens = RVS.screens || {};

  /* 분류 게이트 서보. 실기 가동 범위는 15°(열림) ~ 130°(닫힘) — 기구에 막혀
     0°/90° 로는 원하는 자세가 안 나온다. 두 자세만 쓴다. (레거시 app.js:584-588) */
  const GATE_OPEN_DEG = 15;
  const GATE_CLOSE_DEG = 130;

  /* 속도(PWM 듀티 %) 입력 범위. 서버 /speed 의 duty 범위(0~100)와 같고, 화면은 정수만 받는다. */
  const DUTY_MIN = 0;
  const DUTY_MAX = 100;
  const DUTY_STEP = 1;

  /* 서버가 시작을 거부할 때 펌웨어의 'ERR: DISABLED'(드라이버 꺼짐)가 그대로 올라온다. 이 안내를 앞에 붙인다. */
  const DRIVER_OFF_MSG = '비상 정지 뒤 드라이버가 꺼져 있습니다 — 드라이버 켜기를 누르세요';

  /* 홈 복귀 요청의 대기 한도 = 한 방향 최대 이동(360°)을 홈 속도로 가는 시간 + 여유.
     기본 8초에 끊으면 팔이 움직이는 중에 화면 잠금이 풀린다. 30°/s 면 12초 + 8초 = 20초. */
  const HOME_SWEEP_DEG = 360;
  const HOME_MARGIN_SEC = 8;

  /* 홈 복귀 속도의 폴백. config.yaml safety.homing_speed_deg_per_s(30.0)와 같은 값이다.
     서버가 diagnostics.limits.homing_speed_deg_per_s 로 보내 주면 그 값이 이긴다(없거나 0 이하면 폴백). */
  const HOMING_DEG_PER_S_FALLBACK = 30;

  RVS.screens.equipment = {
    name: 'EquipmentScreen',
    title: '장비 제어',
    template: `
<section class="eq-screen" aria-labelledby="eq-h">
  <header class="rv-head">
    <h1 id="eq-h" tabindex="-1">장비 제어</h1>
    <p>여기 버튼은 실제 모터를 움직입니다. 로봇 옆에 사람이 있을 때만 쓰세요.</p>
  </header>

  <rv-state v-if="s.config.error && !s.config.loaded" state="error"
            :message="'관절 한계·검사 영역(ROI) 설정을 불러오지 못했습니다 — ' + s.config.error"
            @retry="s.refreshConfig(true)" />

  <!-- ===== 컨베이어 ===== -->
  <section class="eq-sec" aria-labelledby="eq-belt-h">
    <div class="eq-sec__head">
      <h2 id="eq-belt-h" class="rv-h2">컨베이어</h2>
      <div class="eq-sec__tools">
        <span class="eq-sec__note">비상 정지로 꺼진 드라이버만 다시 켭니다. 아무것도 움직이지 않습니다.</span>
        <button type="button" class="rv-btn rv-btn--sm" :disabled="s.busy.conveyor || cycle" @click="resetDrivers">드라이버 켜기</button>
      </div>
    </div>

    <rv-state v-if="!s.conveyors.length && beltsErr" state="error" :message="beltsErr" @retry="loadBelts" />
    <rv-state v-else-if="!s.conveyors.length && beltsLoading" state="loading" message="컨베이어 상태를 불러오는 중…" />
    <rv-state v-else-if="!s.conveyors.length" state="empty" message="설정된 컨베이어가 없습니다."
              hint="config.yaml 의 conveyors 에 벨트를 추가하고 서버를 다시 시작하세요." />

    <div v-else class="eq-grid eq-grid--belts">
      <article v-for="b in s.conveyors" :key="b.name" class="rv-card eq-belt" :aria-labelledby="'eq-belt-' + b.name">
        <div class="eq-belt__head">
          <h3 :id="'eq-belt-' + b.name">{{ b.label || b.name }}</h3>
          <rv-status :kind="beltStatus(b).kind" :label="beltStatus(b).text" />
        </div>
        <p v-if="b.port" class="eq-belt__meta rv-num">{{ beltMeta(b) }}</p>
        <p v-else class="eq-belt__meta">
          <rv-status kind="warn" :label="'포트 미설정 — config.yaml 의 conveyors.' + b.name + '.port 를 채우세요'" />
        </p>

        <p v-if="cycle" class="eq-belt__reason"><rv-status kind="info" label="자동 사이클 진행 중 — 정지만 쓸 수 있습니다." /></p>
        <div class="eq-belt__row">
          <div class="eq-speed">
            <span :id="'eq-speed-' + b.name" class="eq-speed__label">속도 (%)</span>
            <div class="eq-speed__ctl">
              <button type="button" class="eq-step" :aria-label="(b.label || b.name) + ' 속도 낮추기'"
                      :disabled="locked(b) || !(Number(b.dutyInput) > DUTY_MIN)" @click="stepDuty(b, -DUTY_STEP)">−</button>
              <input class="eq-speed__in" type="number" inputmode="numeric" :min="DUTY_MIN" :max="DUTY_MAX" :step="DUTY_STEP"
                     v-model.number="b.dutyInput" :data-duty-belt="b.name" :aria-labelledby="'eq-speed-' + b.name"
                     :aria-invalid="dutyOk(b) ? null : 'true'" :disabled="locked(b)" @change="setBeltSpeed(b)">
              <button type="button" class="eq-step" :aria-label="(b.label || b.name) + ' 속도 높이기'"
                      :disabled="locked(b) || !(Number(b.dutyInput) < DUTY_MAX)" @click="stepDuty(b, DUTY_STEP)">+</button>
            </div>
          </div>
          <div class="eq-dir">
            <span v-if="b.running && !cycle" :id="'eq-dirwhy-' + b.name" class="eq-belt__reason"><rv-status kind="info" label="정지한 뒤에 바꿀 수 있습니다" /></span>
            <rv-seg size="sm" :label="(b.label || b.name) + ' 방향'" :model-value="dirOf(b)"
                    :aria-describedby="b.running && !cycle ? 'eq-dirwhy-' + b.name : null"
                    :options="dirOptions(b)" @change="toggleBeltDir(b, $event)" />
          </div>
        </div>
        <p v-if="!dutyOk(b)" class="rv-field__error">
          <rv-status kind="warn" :label="DUTY_MIN + '~' + DUTY_MAX + ' 사이 정수(%)로 입력하세요'" />
        </p>

        <div v-if="hasGate(b)" class="eq-gate">
          <span class="eq-gate__name">분류 게이트</span>
          <rv-seg size="sm" label="분류 게이트" :model-value="b.servo_deg" :options="gateOptions(b)" @change="setGate(b, $event)" />
          <span v-if="b.servo_deg != null" class="rv-num rv-small rv-muted">{{ b.servo_deg }}°</span>
        </div>

        <div class="eq-belt__actions">
          <button type="button" class="rv-btn rv-btn--primary" :aria-label="(b.label || b.name) + ' 시작'"
                  :disabled="locked(b)" @click="startBelt(b)">시작</button>
          <button type="button" class="rv-btn" :aria-label="(b.label || b.name) + ' 정지'"
                  :disabled="!b.port" @click="stopBelt(b)">정지</button>
          <rv-menu :label="beltMenuLabel(b)" :items="beltMenu(b)" :disabled="locked(b)" @select="onBeltMenu(b, $event)" />
        </div>
      </article>
    </div>
  </section>

  <!-- ===== 로봇 팔 ===== -->
  <section class="rv-card eq-arm" aria-labelledby="eq-arm-h">
    <div class="eq-arm__head">
      <h2 id="eq-arm-h" class="rv-h2">로봇 팔</h2>
      <div class="eq-arm__tools">
        <rv-switch :model-value="s.status.teleop.running" :busy="s.busy.teleop" :disabled="cycle" label="텔레오퍼레이션" @change="toggleTeleop" />
        <button ref="homeBtn" type="button" class="rv-btn" :disabled="recording || cycle || s.busy.home" @click="homeOpen = true">
          {{ s.busy.home ? '홈 자세로 이동 중…' : '홈 자세로' }}
        </button>
        <rv-menu label="로봇 팔 더보기 — 포트 식별" :items="armMenu" :disabled="cycle" @select="onArmMenu" />
      </div>
    </div>
    <p v-if="cycle" class="eq-arm__why"><rv-status kind="info" label="자동 사이클 진행 중 — 수동 조작이 잠겼습니다. 비상 정지와 벨트 정지는 쓸 수 있습니다." /></p>
    <p v-if="recording" class="eq-arm__why"><rv-status kind="info" label="녹화 중에는 홈 자세로 보낼 수 없습니다." /></p>
    <p v-if="s.status.teleop.fault" class="eq-arm__fault" role="alert">
      <rv-status kind="fault" :label="'텔레오퍼레이션 오류 — ' + s.status.teleop.fault" />
    </p>
    <p class="eq-sec__note">팔로워는 한 틱에 최대 {{ s.meta.max_relative_target }}°까지만 따라갑니다. 그리퍼(관절 6) 안전값은 실측이 아니므로 홈 복귀를 처음 돌릴 때는 옆에서 지켜보세요.</p>

    <div class="eq-joints__head">
      <h3 id="eq-joints-h" class="eq-sub">관절 각도</h3>
      <span class="rv-chip rv-chip--sm rv-num">Δ MAX {{ s.meta.max_relative_target }}°/TICK</span>
    </div>
    <rv-state v-if="!joints.length" :state="s.online ? 'loading' : 'error'"
              :message="s.online ? '관절 값을 기다리는 중…' : '서버에서 관절 값을 받지 못했습니다.'" @retry="retryStatus" />
    <template v-else>
      <div class="rv-table-wrap" role="region" tabindex="0" aria-labelledby="eq-joints-h">
        <table class="rv-table eq-joints" role="table">
          <thead>
            <tr role="row">
              <th scope="col" role="columnheader">관절</th>
              <th scope="col" role="columnheader" class="rv-num-cell">리더</th>
              <th scope="col" role="columnheader" class="rv-num-cell">팔로워</th>
              <th scope="col" role="columnheader">팔로워 위치 / 소프트 한계</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="j in joints" :key="j.i" role="row">
              <th scope="row" role="rowheader">{{ j.name }}</th>
              <td role="cell" class="rv-num-cell eq-j__lead" data-label="리더">{{ fmt.deg(j.lead) }}</td>
              <td role="cell" class="rv-num-cell eq-j__follow" data-label="팔로워">{{ fmt.deg(j.follow) }}</td>
              <td role="cell" class="eq-j__range-cell" data-label="소프트 한계">
                <div v-if="j.lim" class="eq-j__range rv-num">
                  <span class="eq-j__lim">{{ limText(j.lim[0]) }}</span>
                  <rv-gauge :value="j.follow" :min="j.lim[0]" :max="j.lim[1]" :ghost="j.lead" ghost-label="리더"
                            :show-value="false" :format="fmt.deg"
                            :aria-label="j.name + ' 팔로워 ' + fmt.deg(j.follow) + ', 리더 ' + fmt.deg(j.lead) + ', 한계 ' + j.lim[0] + '에서 ' + j.lim[1] + '도'" />
                  <span class="eq-j__lim">{{ limText(j.lim[1]) }}</span>
                </div>
                <span v-else class="rv-muted">한계 —</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="eq-sec__note">짙은 눈금 = 팔로워, 옅은 눈금 = 리더(목표)</p>
    </template>
  </section>

  <!-- ===== 카메라 ===== -->
  <section class="eq-sec" aria-labelledby="eq-cam-h">
    <div class="eq-sec__head">
      <h2 id="eq-cam-h" class="rv-h2">카메라</h2>
      <span class="eq-sec__note">점선 = 검사 ROI(config). 사람·로봇팔이 이 안에 들어오면 형상이 틀어집니다</span>
    </div>

    <rv-state v-if="camsState === 'error'" state="error" message="서버에서 카메라 상태를 받지 못했습니다." @retry="retryStatus" />
    <rv-state v-else-if="camsState === 'loading'" state="loading" message="카메라 상태를 불러오는 중…" />
    <rv-state v-else-if="camsState === 'empty'" state="empty" message="설정된 카메라가 없습니다."
              hint="config.yaml 의 cameras 를 확인하고 서버를 다시 시작하세요." />

    <div v-else class="eq-grid eq-grid--cams">
      <article v-for="c in s.status.cameras" :key="c.name" class="eq-cam" :class="{ 'eq-cam--rec': recording && c.connected }">
        <rv-cam-frame :slot="c.name" :roi="roiOf(c.name)" :connected="c.connected" :label="camLabel(c) + ' 카메라 프리뷰'">
          <span v-if="recording && c.connected" class="eq-cam__rec"><rv-status kind="run" label="녹화 중" /></span>
        </rv-cam-frame>
        <div class="eq-cam__body">
          <div class="eq-cam__top">
            <span class="eq-cam__name">{{ camLabel(c) }}</span>
            <span class="eq-cam__fps rv-num">{{ c.connected ? fmt.num(c.fps, 1) : '—' }}<span class="rv-muted"> fps</span></span>
          </div>
          <rv-gauge v-if="c.connected && c.target_fps > 0" :value="Math.min(c.fps, c.target_fps)" :min="0" :max="c.target_fps"
                    :normal="fpsNormal(c)" :show-value="false" out-text="프레임 낮음"
                    :aria-label="camLabel(c) + ' ' + fmt.num(c.fps, 1) + ' fps, 정상 범위 ' + fpsNormal(c)[0].toFixed(1) + '에서 ' + c.target_fps" />
          <span class="eq-cam__meta rv-small rv-muted">{{ c.device }} · {{ c.resolution }}</span>
          <rv-status v-if="c.error" kind="warn" :label="c.error" />
        </div>
      </article>
    </div>
  </section>

  <!-- ===== 홈 자세로: 확인 대화상자(D4) ===== -->
  <rv-dialog v-model="homeOpen" title="홈 자세로 이동" size="sm">
    <p class="eq-home__warn">경로에 사람이나 물건이 없는지 확인하세요 · {{ homingSpeed }}°/s</p>
    <template #footer>
      <button type="button" class="rv-btn" @click="homeOpen = false">취소</button>
      <button type="button" class="rv-btn rv-btn--primary" :disabled="recording || cycle || s.busy.home" @click="goHome">홈 자세로 이동</button>
    </template>
  </rv-dialog>
</section>`,

    setup() {
      const { ref, computed, nextTick, onActivated, onBeforeUnmount } = Vue;
      const s = RVS.store;
      const api = RVS.api;
      const fmt = RVS.fmt;

      const recording = computed(() => s.status.recording.state === 'RECORDING');
      // 자동 사이클 진행 중(store.cycleRunning, 없으면 false) — 수동 조작을 잠근다. 비상 정지·벨트 정지는 잠그지 않는다.
      const cycle = computed(() => !!s.cycleRunning);

      /* ---------------- 컨베이어 ---------------- */
      /* 진입할 때마다 컨베이어 상태를 바로 읽는다(폴링은 5초 간격). 성공·실패를 알아야
         '불러오는 중 / 오류' 를 구분해 보일 수 있어서 store 폴링과 별개로 한 번 더 부른다. */
      const beltsErr = ref(null);
      const beltsLoading = ref(true);
      async function loadBelts() {
        beltsLoading.value = true;
        try {
          await s.refreshConveyors();
          beltsErr.value = null;
        } catch (err) {
          beltsErr.value = fmt.errText(err);
        } finally {
          beltsLoading.value = false;
        }
      }
      onActivated(loadBelts);

      /* 잠금은 벨트별(이름 키)이다 + 포트가 비어 있으면 어차피 못 쓴다 */
      const locked = (belt) => s.isBeltBusy(belt) || !belt.port || cycle.value;

      /* 엔코더가 없어 RPM 은 측정하지 못한다. duty 는 '설정 속도'라 정지 중에도 남는다 — 도는지는 running 으로 본다.
         정지를 '0%' 로 그리면 설정이 0 인 것처럼 읽히므로 '정지 · 설정 N%' 로 보인다. */
      function beltStatus(belt) {
        const set = Number.isFinite(belt.duty) ? `${Math.round(belt.duty)}%` : '';
        if (!belt.connected) return { kind: 'stop', text: '포트 닫힘' };
        if (!belt.enabled) return { kind: 'warn', text: '드라이버 꺼짐' };
        if (belt.running) return { kind: 'run', text: `속도 ${set || '—'}` };
        return { kind: 'stop', text: set ? `정지 · 설정 ${set}` : '정지' };
      }

      function beltMeta(belt) {
        const motor = belt.motor ? ` · 모터 M${belt.motor}` : '';
        return `${belt.port}${motor}`;
      }

      /* ---- 속도(PWM 듀티 %) ---- */
      const dutyOk = (belt) => {
        const raw = belt.dutyInput;
        if (raw === '' || raw === null || raw === undefined) return false;
        const v = Number(raw);
        return Number.isInteger(v) && v >= DUTY_MIN && v <= DUTY_MAX;
      };

      /* −/+ 는 한 번 누를 때마다 속도를 바로 보낸다(레거시: 숫자 입력의 스피너도 change 마다 전송). */
      async function stepDuty(belt, delta) {
        const cur = Number(belt.dutyInput);
        const base = Number.isFinite(cur) ? cur : DUTY_MIN;
        belt.dutyInput = Math.min(DUTY_MAX, Math.max(DUTY_MIN, Math.round(base + delta)));
        await setBeltSpeed(belt);
      }

      /* 정지 버튼은 어떤 잠금에서도 눌린다. 그런데 시작은 /speed → /start 두 번에 걸쳐 나가서, 그 사이에 누른
         정지가 먼저 도착하고 /start 가 뒤따라 나가면 정지했는데 벨트가 돈다(정지는 드라이버를 켜 둔 채라 START 가
         받아들여진다. 비상 정지는 드라이버를 꺼서 START 가 거부되지만 이 확인은 그대로 둔다). 정지(벨트별)와
         비상 정지(전체)마다 세대를 올려 두고, 시작은 /start 를 보내기 직전에 그 사이 정지가 있었는지 본다. */
      const stopGen = {};
      let estopGen = 0;
      const onEstopStart = () => { estopGen += 1; };
      window.addEventListener('rvs:estop-start', onEstopStart);
      onBeforeUnmount(() => window.removeEventListener('rvs:estop-start', onEstopStart));
      const genOf = (belt) => `${stopGen[belt.name] || 0}/${estopGen}`;
      async function startBelt(belt) {
        if (!dutyOk(belt)) {
          s.notify(`${belt.label || belt.name}: 속도는 ${DUTY_MIN}~${DUTY_MAX} 사이 정수(%)여야 합니다`, 'error');
          return;
        }
        const gen = genOf(belt);
        // 화면의 속도를 먼저 반영하고 출발한다. 버튼만 눌러 이전 속도로 도는 것보다
        // 눈에 보이는 값과 실제가 같은 편이 안전하다.
        await s.beltCall(belt, async () => {
          try {
            await api.post(`/api/conveyor/${belt.name}/speed`, { duty: belt.dutyInput });
            if (genOf(belt) !== gen) throw new Error('정지를 눌러 시작을 취소했습니다');
            return await api.post(`/api/conveyor/${belt.name}/start`);
          } catch (err) {
            // 드라이버가 꺼져 있으면(비상 정지 뒤) 서버가 펌웨어의 ERR: DISABLED 를 그대로 올린다 — 서버 문구는 두고 할 일을 앞에 붙인다.
            // 그 밖의 오류(ERR: TIMER CONFLICT 등)는 손대지 않고 서버 문구 그대로 beltCall 이 오류 토스트로 보인다.
            throw /DISABLED/i.test(err.message) ? new Error(`${DRIVER_OFF_MSG} (${err.message})`) : err;
          }
        }, `시작 · 속도 ${belt.dutyInput}%`);
      }

      async function stopBelt(belt) {
        stopGen[belt.name] = (stopGen[belt.name] || 0) + 1;
        await s.beltCall(belt, () => api.post(`/api/conveyor/${belt.name}/stop`), '정지');
      }

      async function setBeltSpeed(belt) {
        if (!dutyOk(belt)) return;   // 이유는 입력칸 아래에 이미 보인다
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/speed`, { duty: belt.dutyInput }),
          `속도 ${belt.dutyInput}%`);
      }

      /* ---- 방향 ---- */
      // 펌웨어 줄 규약은 'CW'/'CCW' 대문자다. 어느 쪽으로 오든 옵션 값('cw'/'ccw')과 맞춘다.
      const dirOf = (belt) => String(belt.direction || '').toLowerCase();
      // 달리는 중이면 펌웨어가 방향 변경을 거부한다(ERR: RUNNING). 눌러 보고 실패하는 대신 미리 잠그고 이유를 보인다.
      const dirOptions = (belt) => [
        { value: 'cw', label: '정방향', disabled: locked(belt) || !!belt.running },
        { value: 'ccw', label: '역방향', disabled: locked(belt) || !!belt.running },
      ];

      async function toggleBeltDir(belt, next) {
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/dir`, { direction: next }),
          next === 'cw' ? '정방향 전환' : '역방향 전환');
      }

      /* ---- 분류 게이트: 서보가 보드에 하나뿐이라 servo_deg 를 싣고 오는 M1 축 카드에만 붙는다.
         포트를 아직 안 열어 값이 없을 때도 카드가 모터 번호로 알아본다. ---- */
      const hasGate = (belt) => belt.servo_deg !== undefined || belt.motor === 1;
      const gateOptions = (belt) => [
        { value: GATE_CLOSE_DEG, label: '닫힘', disabled: locked(belt) },
        { value: GATE_OPEN_DEG, label: '열림', disabled: locked(belt) },
      ];

      async function setGate(belt, deg) {
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/servo`, { deg }),
          deg === GATE_OPEN_DEG ? '게이트 열림' : '게이트 닫힘');
      }

      /* ---- 위험색 메뉴: 드라이버(토크) 해제·포트 해제 ----
         드라이버를 끄면 모터가 브레이크로 멈추고, 다시 켤 때까지 START 는 거부된다(EN 이 5V 고정이라 프리런은 없다).
         포트 해제는 펌웨어를 다시 올리려고 COM 포트를 놓는 것이다(다음 START 에서 자동 재연결). */
      function beltMenu(belt) {
        return [
          belt.enabled === false
            ? { id: 'torque', label: '드라이버 켜기' }
            : { id: 'torque', label: '드라이버 해제', danger: true },
          { id: 'release', label: '포트 해제', danger: true },
        ];
      }

      const beltMenuLabel = (belt) => `${belt.label || belt.name} 더보기 — 드라이버 해제, 포트 해제`;

      function onBeltMenu(belt, id) {
        if (id === 'torque') return toggleBeltTorque(belt);
        if (id === 'release') return releaseBelt(belt);
      }

      async function toggleBeltTorque(belt) {
        const next = belt.enabled === false;   // 지금 꺼져 있으면 켜고, 켜져 있으면 끈다
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/enable`, { enabled: next }),
          next ? '드라이버 켜짐' : '드라이버 해제 — 멈췄습니다. 다시 돌리려면 드라이버를 켜세요');
      }

      async function releaseBelt(belt) {
        await s.beltCall(belt, () => api.post(`/api/conveyor/${belt.name}/release`), 'COM 포트 해제 — 펌웨어 업로드 가능');
      }

      /* 비상 정지로 꺼진 드라이버만 다시 켠다 — 아무것도 움직이지 않는다. 실패는 store 가 토스트로 알린다. */
      async function resetDrivers() {
        s.busy.conveyor = true;
        try {
          await s.emergencyReset();
        } finally {
          s.busy.conveyor = false;
        }
      }

      /* ---------------- 로봇 팔 ---------------- */
      // rv-switch 는 제어 컴포넌트다 — 스위치가 보여 준 값에서 나온 '다음 값'(next)으로 start/stop 을 고른다.
      // store 의 폴링 값(최대 1.5초 낡음)을 다시 읽어 고르면 스위치와 명령이 어긋날 여지가 생긴다.
      async function toggleTeleop(next) {
        s.busy.teleop = true;
        try {
          const data = await api.post(next ? '/api/teleop/start' : '/api/teleop/stop');
          s.status.teleop = data.teleop;
          s.notify(data.teleop.running ? '텔레오퍼레이션 시작' : '텔레오퍼레이션 정지');
        } catch (err) {
          s.notify(err.message, 'error');
        } finally {
          s.busy.teleop = false;
        }
      }

      /* 홈 복귀: 확인 대화상자를 거친다. 서버는 녹화 중이면 거부하고, 텔레옵이 켜져 있으면 먼저 끈다. */
      const homeOpen = ref(false);
      const homeBtn = ref(null);
      // 확인 창의 속도와 대기 한도가 같은 값에서 나온다. 서버 값이 없거나 0·비숫자면 폴백(0 으로 나누면 한도가 무한대가 된다).
      const homingSpeed = computed(() => {
        const v = Number(s.config.limits?.homing_speed_deg_per_s);
        return v > 0 ? v : HOMING_DEG_PER_S_FALLBACK;
      });
      const homeTimeoutSec = computed(() => Math.ceil(HOME_SWEEP_DEG / homingSpeed.value + HOME_MARGIN_SEC));

      async function goHome() {
        homeOpen.value = false;
        s.busy.home = true;
        try {
          await api.post('/api/arm/home', null, { timeout: homeTimeoutSec.value * 1000 });
          s.notify('홈 포지션 복귀 완료');
        } catch (err) {
          // 요청은 한도에서 끊기지만 서버의 홈 복귀는 계속된다
          s.notify(err.timeout ? `홈 복귀가 ${homeTimeoutSec.value}초를 넘기고 있습니다. 팔이 아직 움직이는 중일 수 있으니 눈으로 확인하세요.` : err.message, 'error');
        } finally {
          s.busy.home = false;
          // 대화상자가 돌려준 포커스는 이동 중 버튼이 잠겨 있어 사라졌다(body 이거나 닫힌 대화상자 안에 남는다).
          // 그 상태일 때만 끝난 뒤 버튼으로 되돌려 준다 — 사용자가 다른 곳으로 옮겨 둔 포커스는 건드리지 않는다.
          nextTick(() => {
            const a = document.activeElement;
            if (homeBtn.value && (!a || a === document.body || a.closest('dialog:not([open])'))) homeBtn.value.focus();
          });
        }
      }

      /* 포트 식별: USB 를 뽑아 사라진 포트를 그 팔로 확정한다(2단계 + 확인창). 팔로워·리더를 따로 식별한다. */
      const armMenu = [
        { id: 'leader', label: '리더 포트 식별' },
        { id: 'follower', label: '팔로워 포트 식별' },
      ];

      async function identify(role) {
        const name = fmt.ko.arm[role];
        try {
          await api.post('/api/ports/identify/start', { role });
          // CLI 의 blocking input() 대신 확인 창으로 2-phase 를 잇는다. window.confirm 은 떠 있는 동안 비상 정지까지 막는다.
          const proceed = await s.askConfirm({
            title: `${name} 포트 식별`,
            message: `${name} 팔의 USB 케이블을 지금 뽑으세요.\n뽑은 뒤 확인을 누르면 사라진 포트를 해당 팔로 확정합니다.`,
          });
          if (!proceed) return;
          const data = await api.post('/api/ports/identify/confirm', { role });
          s.notify(`${name} = ${data.device} 로 확정했습니다. 케이블을 다시 연결하세요.`);
          // s.refreshPorts()(POST /api/ports/refresh)는 방금 확정한 매칭을 지운다 — 진단 화면처럼 다시 읽기만 한다.
          // 포트 목록은 store 의 5초 폴링이 GET /api/ports 로 따라온다.
          await s.refreshConfig(true);
        } catch (err) {
          s.notify(err.message, 'error');
        }
      }

      const onArmMenu = (role) => identify(role);

      /* 관절 표. 리더·팔로워 값은 status, 소프트 한계는 진단 설정(store.config)에서 온다. */
      const joints = computed(() => {
        const lead = s.status.leader.joints || [];
        const follow = s.status.follower.joints || [];
        const lim = (s.config.limits && s.config.limits.joint_limits) || [];
        return Array.from({ length: Math.max(lead.length, follow.length) }, (_, i) => ({
          i,
          name: fmt.jointName(i),
          lead: lead[i],
          follow: follow[i],
          lim: lim[i] || null,
        }));
      });

      // fmt.deg 와 같은 마이너스 기호(−)로 맞춘다
      const limText = (v) => String(v).replace('-', '−');

      const retryStatus = () => s.refreshStatus().catch((err) => s.notify(fmt.errText(err), 'error'));

      /* ---------------- 카메라 ---------------- */
      // 서버가 응답을 못 줄 때(online=false)만 '오류'. 응답이 아직 없으면 '불러오는 중'이고,
      // 포트 스캔까지 끝났는데 카메라가 0 이면 '비어 있음'이다.
      const camsState = computed(() => {
        if (s.status.cameras.length) return 'ready';
        if (!s.online) return 'error';
        return s.ports.loaded && !s.ports.cameras.length ? 'empty' : 'loading';
      });

      const camLabel = (cam) => fmt.camName(cam);
      const roiOf = (slot) => (s.config.cameras.find((c) => c.slot === slot) || {}).roi || null;
      // 정상 음영의 하한 = '프레임 낮음' 판정 하한(fmt.FPS_LOW_RATIO)
      const fpsNormal = (cam) => [cam.target_fps * fmt.FPS_LOW_RATIO, cam.target_fps];

      return {
        s, fmt, recording, cycle,
        DUTY_MIN, DUTY_MAX, DUTY_STEP,
        // 컨베이어
        beltsErr, beltsLoading, loadBelts, locked, beltStatus, beltMeta,
        dutyOk, stepDuty, startBelt, stopBelt, setBeltSpeed, dirOf, dirOptions, toggleBeltDir, hasGate, gateOptions, setGate,
        beltMenu, beltMenuLabel, onBeltMenu, resetDrivers,
        // 로봇 팔
        toggleTeleop, homeOpen, homeBtn, homingSpeed, goHome, armMenu, onArmMenu, joints, limText, retryStatus,
        // 카메라
        camsState, camLabel, roiOf, fpsNormal,
      };
    },
  };
})();
