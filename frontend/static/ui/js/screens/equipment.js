/* =============================================================
   장비 제어 화면 — 라우트 equipment, 클래스 접두어 eq-.
   실물 모터(컨베이어 3축·로봇 팔)를 움직이는 화면이다. 로봇 옆에 사람이 있을 때만 쓴다.

   레거시 frontend/index.html scan 섹션(:143-315)과 static/js/app.js 의 벨트 로직(:472-635),
   텔레옵·홈·포트 식별(:430-470)을 그대로 옮겼다. 호출하는 API 경로·본문·메시지는 같다.
   벨트 상태 병합(제자리 갱신)·beltBusy(이름 키)·beltCall 은 store.js 가 쥐고 있고,
   이 파일은 그 위에서 액션만 건다(s.beltCall). 벨트 객체를 새로 만들지 않는다.

   레거시와 달라진 점(의도한 것만)
   - 토크 해제·포트 해제·원점·끝점은 위험색 더보기 메뉴 안으로(한 단계 추가). 레거시에는 확인창이 없었다.
   - 위치 이동은 슬라이더가 아니라 숫자 입력 + '이동'. 사용자가 치는 값만 이 화면이 따로 쥔다(moveDraft).
     치는 중이 아니면 칸은 store 가 실제 위치에 맞춰 둔 belt.moveInput 을 보인다. 원점·끝점·토크·이동 뒤와 화면을 떠날 때 draft 를 비운다.
   - RPM 에 −/+ 0.5 단위 버튼. 범위(0.5~112) 밖이면 요청을 보내지 않고 입력칸 아래에 이유를 보인다.
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

  /* 목표 RPM 입력 범위(레거시 index.html:185 의 min·max·step) */
  const RPM_MIN = 0.5;
  const RPM_MAX = 112;
  const RPM_STEP = 0.5;

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
          <div class="eq-rpm">
            <span :id="'eq-rpm-' + b.name" class="eq-rpm__label">목표 속도 (모터축 RPM)</span>
            <div class="eq-rpm__ctl">
              <button type="button" class="eq-step" :aria-label="(b.label || b.name) + ' 속도 낮추기'"
                      :disabled="locked(b) || !(Number(b.rpmInput) > RPM_MIN)" @click="stepRpm(b, -RPM_STEP)">−</button>
              <input class="eq-rpm__in" type="number" inputmode="decimal" :min="RPM_MIN" :max="RPM_MAX" step="0.5"
                     v-model.number="b.rpmInput" :aria-labelledby="'eq-rpm-' + b.name"
                     :aria-invalid="rpmOk(b) ? null : 'true'" :disabled="locked(b)" @change="setBeltSpeed(b)">
              <button type="button" class="eq-step" :aria-label="(b.label || b.name) + ' 속도 높이기'"
                      :disabled="locked(b) || !(Number(b.rpmInput) < RPM_MAX)" @click="stepRpm(b, RPM_STEP)">+</button>
            </div>
          </div>
          <rv-seg size="sm" :label="(b.label || b.name) + ' 방향'" :model-value="b.dir"
                  :options="dirOptions(b)" @change="toggleBeltDir(b, $event)" />
        </div>
        <p v-if="!rpmOk(b)" class="rv-field__error">
          <rv-status kind="warn" :label="RPM_MIN + '~' + RPM_MAX + ' RPM 사이로 입력하세요'" />
        </p>

        <div v-if="b.limit_rev" class="eq-lm">
          <div class="eq-lm__now">
            <span>현재 위치 <strong class="rv-num">{{ posMm(b) }} mm</strong></span>
            <rv-status v-if="atEnd(b)" kind="warn" label="끝점 도달" />
          </div>
          <rv-gauge :value="posMm(b)" :min="0" :max="strokeMm(b)" :show-value="false"
                    :aria-label="(b.label || b.name) + ' 현재 위치 ' + posMm(b) + ' mm, 전체 행정 ' + strokeMm(b) + ' mm'" />
          <div class="eq-lm__scale rv-num"><span>원점 0</span><span>끝점 {{ strokeMm(b) }} mm</span></div>
          <form class="eq-move" @submit.prevent="moveBeltTo(b)">
            <label class="rv-field">
              목표 위치 (mm)
              <input type="number" inputmode="numeric" min="0" :max="strokeMm(b)" step="1" :value="moveShown(b)"
                     :aria-invalid="moveOk(b) ? null : 'true'" :disabled="locked(b)"
                     @input="moveDraft[b.name] = $event.target.value">
            </label>
            <button type="submit" class="rv-btn" :aria-label="(b.label || b.name) + ' 목표 위치로 이동'"
                    :disabled="locked(b) || !moveOk(b)">이동</button>
          </form>
          <p v-if="!moveOk(b)" class="rv-field__error">
            <rv-status kind="warn" :label="'0~' + strokeMm(b) + ' mm 사이로 입력하세요'" />
          </p>
        </div>

        <div v-if="b.motor === 1" class="eq-gate">
          <span class="eq-gate__name">분류 게이트</span>
          <rv-seg size="sm" label="분류 게이트" :model-value="b.servo_deg" :options="gateOptions(b)" @change="setGate(b, $event)" />
          <span v-if="b.servo_deg !== undefined" class="rv-num rv-small rv-muted">{{ b.servo_deg }}°</span>
        </div>

        <p v-if="atLimit(b)" class="eq-belt__reason">
          <rv-status kind="info" label="시작 잠김 — 이 방향 끝입니다. 방향을 바꾸거나 원점을 다시 잡으세요." />
        </p>

        <div class="eq-belt__actions">
          <button type="button" class="rv-btn rv-btn--primary" :aria-label="(b.label || b.name) + ' 시작'"
                  :disabled="locked(b) || atLimit(b)" @click="startBelt(b)">시작</button>
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
      const { ref, reactive, computed, nextTick, onActivated, onDeactivated, onBeforeUnmount } = Vue;
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

      /* 소프트 리밋 축이 지금 방향으로 더 갈 수 있는가. 갈 수 없으면 펌웨어가
         START 를 거부하므로, 눌러보고 실패하는 대신 버튼을 미리 잠그고 이유를 보여준다. */
      function atLimit(belt) {
        if (!belt.limit_rev || belt.pos_rev === undefined) return false;
        return belt.dir === 'ccw' ? belt.pos_rev <= 0 : belt.pos_rev >= belt.limit_rev;
      }

      /* 끝(상한)에 닿았을 때만 강조한다. 0 은 원점이라 정상 상태다 —
         여기까지 경고로 칠하면 원점을 잡을 때마다 경고가 뜬다. */
      const atEnd = (belt) => belt.pos_rev >= belt.limit_rev;

      function beltStatus(belt) {
        if (!belt.connected) return { kind: 'stop', text: '포트 닫힘' };
        if (!belt.enabled) return { kind: 'warn', text: '드라이버 꺼짐' };
        if (belt.running) return { kind: 'run', text: `${Number(belt.rpm || 0).toFixed(1)} RPM` };
        if (atLimit(belt)) return { kind: 'stop', text: belt.dir === 'ccw' ? '정지 · 원점 끝' : '정지 · 리밋 끝' };
        return { kind: 'stop', text: '정지' };
      }

      /* 회전수가 아니라 mm 로 보여준다. 사람이 기구를 mm 로 재기 때문이다. */
      const strokeMm = (belt) => Math.round(belt.stroke_mm);
      const posMm = (belt) => Math.round(Number(belt.pos_rev || 0) * belt.mm_per_rev);

      function beltMeta(belt) {
        const motor = belt.motor ? ` · 모터 M${belt.motor}` : '';
        const stroke = belt.limit_rev ? ` · 행정 ${strokeMm(belt)} mm` : '';
        return `${belt.port}${motor}${stroke}`;
      }

      /* ---- RPM ---- */
      const rpmOk = (belt) => {
        const raw = belt.rpmInput;
        if (raw === '' || raw === null || raw === undefined) return false;
        const v = Number(raw);
        return Number.isFinite(v) && v >= RPM_MIN && v <= RPM_MAX;
      };

      /* −/+ 는 한 번 누를 때마다 목표 속도를 바로 보낸다(레거시: 숫자 입력의 스피너도 change 마다 전송). */
      async function stepRpm(belt, delta) {
        const cur = Number(belt.rpmInput);
        const base = Number.isFinite(cur) ? cur : RPM_MIN;
        belt.rpmInput = Math.min(RPM_MAX, Math.max(RPM_MIN, Math.round((base + delta) * 100) / 100));
        await setBeltSpeed(belt);
      }

      /* 정지 버튼은 어떤 잠금에서도 눌린다. 그런데 시작은 /speed → /start 두 번에 걸쳐 나가서, 그 사이에 누른
         정지가 먼저 도착하고 /start 가 뒤따라 나가면 정지했는데 벨트가 돈다(START 는 비상 정지가 끈 드라이버도
         다시 켠다). 정지(벨트별)와 비상 정지(전체)마다 세대를 올려 두고, 시작은 /start 를 보내기 직전에
         그 사이 정지가 있었는지 본다. */
      const stopGen = {};
      let estopGen = 0;
      const onEstopStart = () => { estopGen += 1; };
      window.addEventListener('rvs:estop-start', onEstopStart);
      onBeforeUnmount(() => window.removeEventListener('rvs:estop-start', onEstopStart));
      const genOf = (belt) => `${stopGen[belt.name] || 0}/${estopGen}`;
      async function startBelt(belt) {
        if (!rpmOk(belt)) {
          s.notify(`${belt.label || belt.name}: RPM 은 ${RPM_MIN}~${RPM_MAX} 사이여야 합니다`, 'error');
          return;
        }
        const gen = genOf(belt);
        // 화면의 RPM 을 먼저 반영하고 출발한다. 버튼만 눌러 이전 속도로 도는 것보다
        // 눈에 보이는 값과 실제가 같은 편이 안전하다.
        await s.beltCall(belt, async () => {
          await api.post(`/api/conveyor/${belt.name}/speed`, { rpm: belt.rpmInput });
          if (genOf(belt) !== gen) throw new Error('정지를 눌러 시작을 취소했습니다');
          return api.post(`/api/conveyor/${belt.name}/start`);
        }, `시작 · ${belt.rpmInput} RPM`);
      }

      async function stopBelt(belt) {
        stopGen[belt.name] = (stopGen[belt.name] || 0) + 1;
        await s.beltCall(belt, () => api.post(`/api/conveyor/${belt.name}/stop`), '정지');
      }

      async function setBeltSpeed(belt) {
        if (!rpmOk(belt)) return;   // 이유는 입력칸 아래에 이미 보인다
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/speed`, { rpm: belt.rpmInput }),
          `목표 속도 ${belt.rpmInput} RPM`);
      }

      /* ---- 방향 ---- */
      const dirOptions = (belt) => [
        { value: 'cw', label: '정방향', disabled: locked(belt) },
        { value: 'ccw', label: '역방향', disabled: locked(belt) },
      ];

      async function toggleBeltDir(belt, next) {
        // 구동 중이면 펌웨어가 잠깐 멈춘 뒤 저속에서 다시 가속한다(부하 상태 즉시 반전 = 탈조).
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/dir`, { direction: next }),
          next === 'cw' ? '정방향 전환' : '역방향 전환');
      }

      /* ---- 분류 게이트: 서보가 보드에 하나뿐이라 servo_deg 를 싣고 오는 M1 축 카드에만 붙는다 ---- */
      const gateOptions = (belt) => [
        { value: GATE_CLOSE_DEG, label: '닫힘', disabled: locked(belt) },
        { value: GATE_OPEN_DEG, label: '열림', disabled: locked(belt) },
      ];

      async function setGate(belt, deg) {
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/servo`, { deg }),
          deg === GATE_OPEN_DEG ? '게이트 열림' : '게이트 닫힘');
      }

      /* ---- 위치 이동(소프트 리밋 축) ----
         '목표' 입력칸은 사용자가 치는 동안만 이 화면이 쥔다(moveDraft). store 는 멈춰 있는 동안 belt.moveInput 을
         실제 위치로 맞추는데, 숫자를 치는 도중에 폴링이 그 값을 되돌려 놓으면 안 된다.
         draft 가 있으면 그 값이, 없으면 belt.moveInput(실제 위치)이 칸에 보인다.
         draft 를 남겨 두면 원점·끝점을 새로 잡은 뒤에도 칸이 옛 값이라 '이동' 이 엉뚱한 곳으로 간다 —
         위치가 바뀌는 동작 뒤와 화면을 떠날 때 비운다. */
      const moveDraft = reactive({});
      const clearMoveDraft = (belt) => { delete moveDraft[belt.name]; };
      onDeactivated(() => Object.keys(moveDraft).forEach((name) => delete moveDraft[name]));
      const moveShown = (belt) => moveDraft[belt.name] ?? belt.moveInput;
      const moveOk = (belt) => {
        const raw = moveShown(belt);
        if (raw === '' || raw === null || raw === undefined) return false;
        const v = Number(raw);
        return Number.isFinite(v) && v >= 0 && v <= strokeMm(belt);
      };

      /* 누르는 순간 그 위치로 이동하고 펌웨어가 스스로 멈춘다. */
      async function moveBeltTo(belt) {
        if (!moveOk(belt)) return;
        const mm = Math.round(Number(moveShown(belt)));
        const ok = await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/move`, { mm }),
          `${mm}mm 로 이동`);
        // 실패하면 친 값을 남겨 고쳐서 다시 누르게 한다. 성공하면 칸을 store 에 돌려주되, 움직이는 동안은
        // store 가 칸을 건드리지 않으므로 옛 위치가 아니라 목표를 보이게 해 둔다(멈추면 실제 위치로 맞춘다).
        if (ok) {
          belt.moveInput = mm;
          clearMoveDraft(belt);
        }
      }

      /* ---- 위험색 메뉴: 토크 해제·포트 해제·원점·끝점 ----
         토크를 풀면 축을 손으로 밀 수 있다(원점을 잡을 자리로 옮길 때). 단 손으로 민 거리는
         펌웨어가 모르므로 그 뒤에는 반드시 원점을 다시 잡아야 한다.
         포트 해제는 펌웨어를 다시 올리려고 COM 포트를 놓는 것이다(다음 START 에서 자동 재연결).
         원점 = 지금 자리를 0 으로, 끝점 = 지금 자리를 가동 범위의 끝으로 저장(리밋 스위치가 없다). */
      function beltMenu(belt) {
        const items = [];
        if (belt.limit_rev) {
          items.push({ id: 'zero', label: '원점 설정', danger: true }, { id: 'end', label: '끝점 설정', danger: true }, { separator: true });
        }
        items.push(
          belt.enabled === false
            ? { id: 'torque', label: '토크 걸기' }
            : { id: 'torque', label: '토크 해제', danger: true },
          { id: 'release', label: '포트 해제', danger: true },
        );
        return items;
      }

      const beltMenuLabel = (belt) =>
        `${belt.label || belt.name} 더보기 — ${belt.limit_rev ? '원점·끝점 설정, ' : ''}토크 해제, 포트 해제`;

      function onBeltMenu(belt, id) {
        if (id === 'zero') return setBeltZero(belt);
        if (id === 'end') return setBeltEnd(belt);
        if (id === 'torque') return toggleBeltTorque(belt);
        if (id === 'release') return releaseBelt(belt);
      }

      // 기준(원점·끝점)이나 토크가 바뀌면 옛 목표 draft 는 엉뚱한 위치다. 요청이 실패·시간 초과여도
      // 서버가 실행했을 수 있어 결과와 상관없이 비운다 — 칸은 실제 위치로 돌아간다.
      async function setBeltZero(belt) {
        await s.beltCall(belt, () => api.post(`/api/conveyor/${belt.name}/zero`), '현재 위치를 원점으로 설정');
        clearMoveDraft(belt);
      }

      /* 끝점 티칭. 원점을 잡고 반대쪽 끝까지 몰고 온 뒤 누른다 — 자로 재서 config 에
         옮겨 적는 대신 축을 직접 움직여 정하므로 값이 실물과 어긋나지 않는다. */
      async function setBeltEnd(belt) {
        await s.beltCall(belt, () => api.post(`/api/conveyor/${belt.name}/end`), '현재 위치를 가동 범위 끝으로 저장');
        clearMoveDraft(belt);
      }

      async function toggleBeltTorque(belt) {
        const next = belt.enabled === false;   // 지금 풀려 있으면 걸고, 걸려 있으면 푼다
        await s.beltCall(belt,
          () => api.post(`/api/conveyor/${belt.name}/enable`, { enabled: next }),
          next ? '토크 걸림' : '토크 해제 — 손으로 옮긴 뒤 원점을 다시 잡으세요');
        clearMoveDraft(belt);
      }

      async function releaseBelt(belt) {
        await s.beltCall(belt, () => api.post(`/api/conveyor/${belt.name}/release`), 'COM 포트 해제 — 펌웨어 업로드 가능');
      }

      /* 비상 정지로 꺼진 스텝 드라이버만 다시 켠다 — 아무것도 움직이지 않는다. 실패는 store 가 토스트로 알린다. */
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
        RPM_MIN, RPM_MAX, RPM_STEP,
        // 컨베이어
        beltsErr, beltsLoading, loadBelts, locked, atLimit, atEnd, beltStatus, beltMeta, strokeMm, posMm,
        rpmOk, stepRpm, startBelt, stopBelt, setBeltSpeed, dirOptions, toggleBeltDir, gateOptions, setGate,
        moveDraft, moveShown, moveOk, moveBeltTo, beltMenu, beltMenuLabel, onBeltMenu, resetDrivers,
        // 로봇 팔
        toggleTeleop, homeOpen, homeBtn, homingSpeed, goHome, armMenu, onArmMenu, joints, limText, retryStatus,
        // 카메라
        camsState, camLabel, roiOf, fpsNormal,
      };
    },
  };
})();
