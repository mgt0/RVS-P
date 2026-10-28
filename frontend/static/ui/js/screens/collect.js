/* =============================================================
   수집 화면 — 라우트 collect, 클래스 접두어 co-
   두 가지 수집이 한 화면에 있다. 데이터가 다르다(impl_plan 3.5·3.6, R7).
     검사 샘플   벨트 정지 → POST /api/scan/capture(resolution 48) → 라벨(POST /api/inspections/<id>/label)
                 저장 위치는 inspections/<id>/label.json. classification/ 에는 쓰지 않는다.
     시연 녹화   서버 세션(/api/demo/*)이 루프를 쥔다. 이 화면은 시작·다음·다시·끝내기만 보낸다.
                 에피소드는 kind=demo 로 저장되고 분류 이미지를 만들지 않는다.
   레거시 이식: 에피소드 CRUD(app.js:682-727 — PATCH/DELETE/확인 문구 그대로. 확인은 window.confirm 이 아니라 store.askConfirm),
   녹화 중 새로고침 경고는 store 가 맡는다.
   상단 '녹화 중' 칩과 수집 항목 점은 셸 몫이다.
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});
  RVS.screens = RVS.screens || {};

  const NEW = '__new__';                                  // 물체 선택의 '새 물체 등록…'
  const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;       // 서버 SAFE_ID_RE(storage.py:41)와 같다
  const ID_HINT = '영문·숫자·_·- 만 쓰고 64자 이내로 적으세요';
  const TOO_SHORT_SEC = 3;                                // 이보다 짧은 에피소드는 '너무 짧음'
  const EP_PAGE = 30;                                     // 에피소드 표가 한 번에 그리는 행 수
  const INSP_LIMIT = 50;                                  // GET /api/inspections?limit=
  const PREF_KEY = 'rvs.collect.demo';                    // 시연 설정을 기억한다(작업 설명을 매번 다시 쓰지 않게)
  const STEPS = ['물체를 검사 구역에', '벨트 정지 → 4대 촬영', '라벨 · 주황 평가', '저장 · 물체 돌리기'];
  const ORANGE_KO = { right: '맞음', partial: '일부만', wrong: '틀림' };

  RVS.screens.collect = {
    name: 'CollectScreen',
    title: '수집',
    template: `
      <section class="co-screen" aria-labelledby="co-h">
        <header class="co-head">
          <div class="rv-head">
            <h1 id="co-h" tabindex="-1">수집</h1>
            <p>불량 판별용 사진과 로봇 시연은 다른 데이터라 따로 모읍니다.</p>
          </div>
          <rv-seg v-model="mode" label="수집 종류" :options="modes" />
        </header>

        <!-- ================= 검사 샘플 ================= -->
        <template v-if="mode === 'station'">
          <ol class="co-steps" aria-label="검사 샘플 순서">
            <li v-for="(t, i) in steps" :key="i" class="co-step" :aria-current="i + 1 === stepNow ? 'step' : null">
              <span class="co-step__n">{{ i + 1 }}</span>
              <span class="co-step__t">{{ t }}</span>
            </li>
          </ol>

          <div class="co-work">
            <!-- 촬영 + 4장 + 주황 평가 -->
            <section class="rv-card" aria-labelledby="co-cap-h">
              <div class="rv-card__head">
                <h2 id="co-cap-h" class="rv-h2">{{ capTitle }}</h2>
                <span v-if="current" class="rv-card__aside"><code>{{ current }}</code></span>
              </div>
              <div class="co-bar-row">
                <rv-status v-if="s.cycleRunning" kind="warn" label="자동 사이클 진행 중 — 끝난 뒤 촬영하세요" />
                <rv-status v-else-if="!s.conveyors.length" kind="info" label="벨트 상태를 아직 받지 못했습니다" />
                <rv-status v-else-if="beltsMoving" kind="warn" label="벨트가 움직이는 중 — 멈춘 뒤 촬영하세요" />
                <rv-status v-else kind="stop" label="벨트 정지 확인" />
                <button type="button" class="rv-btn rv-btn--primary" :disabled="capBusy || !!s.cycleRunning" :aria-busy="capBusy ? 'true' : null" @click="capture">
                  {{ capBusy ? '촬영 중…' : '촬영 (Space)' }}
                </button>
              </div>

              <div v-if="views.length" class="co-views">
                <figure v-for="v in views" :key="v.cam" class="co-view">
                  <div class="co-view__img">
                    <img :src="v.src" :alt="v.name + ' 촬영본'" loading="lazy" decoding="async" @error="failed[v.src] = true">
                    <div v-if="failed[v.src]" class="co-view__fail"><rv-status kind="warn" label="사진 없음" /></div>
                  </div>
                  <figcaption>{{ v.name }}</figcaption>
                </figure>
              </div>
              <rv-state v-else state="empty" message="고른 촬영이 없습니다."
                        hint="물체를 검사 구역에 놓고 벨트를 멈춘 뒤 촬영하거나, 아래 목록에서 촬영을 고르세요." />

              <div v-if="current" class="co-eval">
                <span class="co-eval__q"><rv-status kind="warn" label="주황 표시가 실제 자국과 맞나요?" /></span>
                <rv-seg v-model="form.orange" label="주황 표시 평가" size="sm" :options="orangeOptions" />
                <p v-if="anomalyText" class="co-eval__hint">{{ anomalyText }}</p>
                <p class="co-eval__hint">이 답이 쌓이면 주황 표시의 정확도를 숫자로 잴 수 있습니다. 주황 표시를 확인하지 못했다면 '평가 안 함'으로 둡니다.</p>
              </div>
            </section>

            <!-- 라벨 패널 -->
            <section class="rv-card co-lab" aria-labelledby="co-lab-h">
              <h2 id="co-lab-h" class="rv-h2">라벨</h2>

              <div class="rv-field">
                <label for="co-l-obj">물체</label>
                <select id="co-l-obj" v-model="labelObj.sel" :disabled="objState === 'loading'">
                  <option value="" disabled>{{ objState === 'loading' ? '불러오는 중…' : '물체를 고르세요' }}</option>
                  <option v-for="o in objects" :key="o.object_id" :value="o.object_id">{{ o.object_id }} — {{ o.has_cad ? 'CAD 있음' : 'CAD 없음' }}</option>
                  <option :value="NEW">새 물체 등록…</option>
                </select>
                <template v-if="labelObj.sel === NEW">
                  <input type="text" v-model="labelObj.neu" aria-label="새 물체 ID" placeholder="새 물체 ID" maxlength="64" autocomplete="off"
                         :aria-invalid="labelObjErr ? 'true' : null">
                  <span v-if="labelObjErr" class="rv-field__error"><rv-status kind="warn" :label="labelObjErr" /></span>
                </template>
              </div>
              <rv-state v-if="objState === 'error'" state="error" :message="objError" @retry="loadObjects" />

              <div class="co-pose">
                <span class="rv-muted">이 물체의 자세</span>
                <span class="rv-num co-strong">{{ poseText }}</span>
              </div>

              <div class="rv-field">
                <span>품질</span>
                <rv-seg v-model="form.label" label="품질" class="co-seg-block" :options="labelOptions" />
              </div>

              <fieldset v-if="form.label === 'defect'" class="co-fieldset">
                <legend>불량 유형</legend>
                <div class="co-chips">
                  <button v-for="t in defectTypes" :key="t" type="button" class="co-chip"
                          :aria-pressed="form.defect === t ? 'true' : 'false'" @click="form.defect = t">{{ defectName(t) }}</button>
                </div>
              </fieldset>

              <div class="co-actions">
                <button type="button" class="rv-btn rv-btn--primary" :disabled="!canSave" @click="saveLabel">{{ saving ? '저장 중…' : saveText }}</button>
                <button type="button" class="rv-btn" :disabled="!canRetake" @click="retake">다시 찍기</button>
              </div>
              <p v-if="saveBlock" class="rv-small rv-muted">{{ saveBlock }}</p>
              <p class="rv-small rv-muted">Space 촬영 · Enter 저장 · Backspace 다시 찍기</p>
            </section>
          </div>

          <!-- 라벨 현황(레거시 불량률 도넛 대신) -->
          <section class="rv-card" aria-labelledby="co-stat-h">
            <div class="rv-card__head">
              <h2 id="co-stat-h" class="rv-h2">라벨 현황</h2>
              <span class="rv-card__aside">물체 수 기준입니다(촬영 장수가 아닙니다)</span>
            </div>
            <rv-state v-if="inspState === 'error'" state="error" :message="inspError" @retry="loadInspections" />
            <rv-state v-else-if="inspState === 'loading'" state="loading" />
            <template v-else>
              <dl class="co-stats">
                <div><dt>양품 물체</dt><dd class="rv-num">{{ counts.good === null ? '—' : counts.good }}</dd></div>
                <div><dt>불량 물체</dt><dd class="rv-num">{{ counts.defect === null ? '—' : counts.defect }}</dd></div>
                <div><dt>라벨 없는 촬영</dt><dd class="rv-num">{{ counts.unlabeled }}</dd></div>
                <div><dt>불량률</dt><dd class="rv-num">{{ defectRate }}</dd></div>
              </dl>
              <div v-if="goals" class="co-goals">
                <rv-meter label="양품 물체" :value="s.readiness.good_objects" :max="goals.objects_per_class" />
                <rv-meter label="불량 물체" :value="s.readiness.defect_objects" :max="goals.objects_per_class" />
              </div>
              <rv-state v-else-if="s.readiness.error" state="error" :message="s.readiness.error" retry-label="목표 다시 불러오기" @retry="s.refreshReadiness()" />
            </template>
          </section>

          <!-- 촬영 목록 -->
          <section class="rv-card" aria-labelledby="co-list-h">
            <div class="rv-card__head">
              <h2 id="co-list-h" class="rv-h2">촬영 목록 <span v-if="inspState === 'ready'" class="co-count">{{ counts.total }}</span></h2>
              <rv-seg v-if="inspState === 'ready'" v-model="listFilter" label="촬영 목록 거르기" size="sm" :options="filterOptions" />
            </div>
            <rv-state v-if="inspState === 'error'" state="error" :message="inspError" @retry="loadInspections" />
            <rv-state v-else-if="inspState === 'loading'" state="loading" />
            <rv-state v-else-if="!listRows.length" state="empty"
                      :message="listFilter === 'pending' ? '라벨을 기다리는 촬영이 없습니다.' : '아직 촬영이 없습니다.'"
                      :hint="listFilter === 'pending' ? '새로 찍으면 여기에 쌓입니다. 이미 라벨을 단 촬영은 ‘전체’에서 볼 수 있습니다.' : '물체를 검사 구역에 놓고 벨트를 멈춘 뒤 위의 ‘촬영’을 누르세요.'" />
            <template v-else>
              <div class="rv-table-wrap" role="region" tabindex="0" aria-labelledby="co-list-h">
                <table class="rv-table co-table">
                  <thead>
                    <tr>
                      <th scope="col"><span class="rv-sr-only">사진</span></th>
                      <th scope="col">촬영 시각</th>
                      <th scope="col">촬영 ID</th>
                      <th scope="col">상태</th>
                      <th scope="col"><span class="rv-sr-only">동작</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="it in listRows" :key="it.capture_id" :class="{ 'co-row--on': it.capture_id === current }"
                        :aria-current="it.capture_id === current ? 'true' : null">
                      <td class="co-thumbcell">
                        <img v-if="it.cameras && it.cameras.length" class="co-thumb" :src="thumbOf(it)" alt="" loading="lazy" decoding="async">
                      </td>
                      <td class="rv-num">{{ fmt.dateTime(it.captured_at, { seconds: true }) }}</td>
                      <td><code>{{ it.capture_id }}</code></td>
                      <td>
                        <template v-if="it.label">
                          <span class="co-strong">{{ labelText(it) }}</span>
                          <span v-if="orangeText(it)" class="co-sub">{{ orangeText(it) }}</span>
                        </template>
                        <span v-else class="rv-muted">라벨 없음</span>
                      </td>
                      <td>
                        <div class="co-rowact">
                          <button type="button" class="rv-link" :aria-label="it.capture_id + (it.label ? ' 촬영 보기' : ' 촬영에 라벨 달기')" @click="select(it.capture_id)">{{ it.label ? '보기' : '라벨 달기' }}</button>
                          <rv-menu :label="it.capture_id + ' 촬영 메뉴 — 삭제'" :items="inspMenu(it)" @select="onInspMenu(it, $event)" />
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p v-if="counts.total > inspections.length" class="rv-small rv-muted">전체 {{ counts.total }}건 중 최근 {{ inspections.length }}건만 보여 줍니다.</p>
            </template>
          </section>
        </template>

        <!-- ================= 시연 녹화 ================= -->
        <template v-else>
          <section class="rv-card" aria-labelledby="co-set-h">
            <div class="rv-card__head">
              <h2 id="co-set-h" class="rv-h2">녹화 설정</h2>
              <span v-if="s.meta.loaded" class="rv-card__aside">샘플링 {{ s.meta.sample_rate_hz }} Hz</span>
            </div>

            <div v-if="!demoActive" class="co-fields">
              <div class="rv-field co-field--wide">
                <label for="co-d-task">작업 설명 (ACT 학습에 그대로 들어갑니다)</label>
                <input id="co-d-task" type="text" v-model="dform.task" maxlength="200" autocomplete="off"
                       placeholder="예: 공급 벨트의 물체를 집어 작업 벨트로 옮긴다">
              </div>
              <div class="rv-field">
                <label for="co-d-obj">물체</label>
                <select id="co-d-obj" v-model="demoObj.sel" :disabled="objState === 'loading'">
                  <option value="" disabled>{{ objState === 'loading' ? '불러오는 중…' : '물체를 고르세요' }}</option>
                  <option v-for="o in objects" :key="o.object_id" :value="o.object_id">{{ o.object_id }}</option>
                  <option :value="NEW">새 물체 등록…</option>
                </select>
                <template v-if="demoObj.sel === NEW">
                  <input type="text" v-model="demoObj.neu" aria-label="새 물체 ID" placeholder="새 물체 ID" maxlength="64" autocomplete="off"
                         :aria-invalid="demoObjErr ? 'true' : null">
                  <span v-if="demoObjErr" class="rv-field__error"><rv-status kind="warn" :label="demoObjErr" /></span>
                </template>
              </div>
              <div class="rv-field">
                <label for="co-d-ep">에피소드 길이 (초)</label>
                <input id="co-d-ep" type="number" inputmode="numeric" min="1" max="600" step="1" v-model.number="dform.episode_sec"
                       :aria-invalid="epSecErr ? 'true' : null">
                <span v-if="epSecErr" class="rv-field__error"><rv-status kind="warn" :label="epSecErr" /></span>
              </div>
              <div class="rv-field">
                <label for="co-d-rs">리셋 시간 (초)</label>
                <input id="co-d-rs" type="number" inputmode="numeric" min="0" max="120" step="1" v-model.number="dform.reset_sec"
                       :aria-invalid="resetSecErr ? 'true' : null">
                <span v-if="resetSecErr" class="rv-field__error"><rv-status kind="warn" :label="resetSecErr" /></span>
              </div>
              <div class="rv-field">
                <span>다음 에피소드</span>
                <span class="co-nextid"><code>{{ s.meta.next_episode_id || '—' }}</code><span class="rv-muted">자동</span></span>
              </div>
            </div>
            <!-- 세션 중에는 서버가 쥔 값을 그대로 보여 준다(폼 값이 아니라) -->
            <dl v-else class="co-sess">
              <div class="co-field--wide"><dt>작업 설명</dt><dd>{{ demo.task || '—' }}</dd></div>
              <div><dt>물체</dt><dd>{{ demo.object_id || '—' }}</dd></div>
              <div><dt>에피소드 길이</dt><dd class="rv-num">{{ demo.episode_sec }}초</dd></div>
              <div><dt>리셋 시간</dt><dd class="rv-num">{{ demo.reset_sec }}초</dd></div>
            </dl>

            <div v-if="!demoActive" class="co-pre">
              <h3 id="co-pre-h">사전 점검</h3>
              <ul class="co-checks" aria-labelledby="co-pre-h">
                <li v-for="c in checks" :key="c.id" class="co-check">
                  <template v-if="c.kind === 'ok'">
                    <svg class="co-check__icon" viewBox="0 0 14 14" aria-hidden="true" focusable="false"><path d="M3 7.5l2.5 2.5L11 4.5"/></svg>
                    <span><span class="rv-sr-only">정상: </span>{{ c.text }}</span>
                  </template>
                  <rv-status v-else :kind="c.kind" :label="c.text" />
                </li>
              </ul>
              <p class="co-note">
                시작에 꼭 필요한 것은 텔레옵과 팔 연결이고, 서버가 직접 확인합니다. 나머지는 안내라서 시작을 막지 않습니다.
                <button type="button" class="rv-link" @click="s.go('equipment')">장비 제어 열기</button>
              </p>
            </div>

            <p class="co-note">
              <rv-status kind="info" label="안내" hide-label />
              <span>이 세션은 분류 이미지(classification/)를 만들지 않습니다. 로봇 학습(ACT)용 에피소드만 저장하며, 양품·불량 사진은 ‘검사 샘플’에서 따로 모읍니다.</span>
            </p>
          </section>

          <section class="rv-card co-run" aria-labelledby="co-run-h">
            <div class="co-run__main">
              <rv-status v-if="demoActive" :kind="demoState === 'RECORDING' ? 'run' : 'stop'" :label="phaseLabel" />
              <h2 id="co-run-h" class="co-run__title">{{ runTitle }}</h2>
              <div class="co-bar" role="progressbar" aria-label="진행" aria-valuemin="0" aria-valuemax="100"
                   :aria-valuenow="runPct" :aria-valuetext="runPct + '%'">
                <div class="co-bar__fill" :style="{ width: runPct + '%' }"></div>
              </div>
              <p class="co-run__hint">{{ runHint }}</p>
              <p v-if="demoFault" class="co-run__alert" role="alert"><rv-status kind="warn" :label="'세션이 중단됐습니다 — ' + demoFault" /></p>
              <p v-if="demoError" class="co-run__alert" role="alert"><rv-status kind="warn" :label="demoError" /></p>
            </div>
            <div class="co-run__btns">
              <template v-if="!demoActive">
                <button type="button" class="rv-btn rv-btn--primary" :disabled="!canStart" :aria-describedby="startBlock ? 'co-start-why' : null" @click="demoStart">
                  {{ demoBusy ? '시작하는 중…' : '세션 시작' }}
                </button>
                <p v-if="startBlock" id="co-start-why" class="co-why">{{ startBlock }}</p>
              </template>
              <template v-else>
                <button type="button" class="rv-btn rv-btn--primary" :disabled="demoBusy" @click="demoNext">다음 (→ n)</button>
                <button type="button" class="rv-btn" :disabled="demoBusy || demoState !== 'RECORDING'" :title="demoState !== 'RECORDING' ? '리셋 중에는 버릴 에피소드가 없습니다' : null" @click="demoRedo">다시 (← r)</button>
                <button type="button" class="rv-btn" :disabled="demoBusy" @click="demoEnd">끝내기 (Esc q)</button>
              </template>
            </div>
            <dl class="co-run__stats">
              <div><dt>{{ demoActive ? '이번 세션에 저장' : '지난 세션에 저장' }}</dt><dd class="rv-num">{{ demo.saved ? demo.saved : (demoActive ? 0 : '—') }}</dd></div>
              <div><dt>현재 에피소드</dt><dd><code>{{ demoActive ? demo.episode_id : (s.meta.next_episode_id || '—') }}</code></dd></div>
              <div><dt>프레임</dt><dd class="rv-num">{{ demoState === 'RECORDING' ? s.status.recording.frame_count : '—' }}</dd></div>
              <div><dt>동기 오차</dt><dd class="rv-num"><rv-status v-if="skewOver" kind="warn" :label="skewText" /><template v-else>{{ skewText }}</template></dd></div>
              <div><dt>마지막 저장</dt><dd><code v-if="demo.last_saved">{{ demo.last_saved }}</code><template v-else>—</template></dd></div>
            </dl>
            <dl class="co-keys">
              <div><dt>→ 또는 n</dt><dd>저장하고 다음</dd></div>
              <div><dt>← 또는 r</dt><dd>버리고 다시</dd></div>
              <div><dt>Esc 또는 q</dt><dd>세션 끝내기</dd></div>
              <div><dt>USB 풋스위치</dt><dd>키를 보내는 제품이면 그대로 동작</dd></div>
            </dl>
            <span class="rv-sr-only" role="status">{{ announce }}</span>
          </section>

          <section class="rv-card" aria-labelledby="co-ep-h">
            <div class="rv-card__head">
              <h2 id="co-ep-h" class="rv-h2">녹화한 에피소드 <span class="co-count">{{ episodes.length }}</span></h2>
              <div class="co-tools">
                <span class="rv-card__aside">3초 미만이거나 동기 오차가 넘으면 ‘주의’로 표시합니다</span>
                <button type="button" class="rv-btn rv-btn--sm" :disabled="epBusy" @click="refreshEpisodes">{{ epBusy ? '불러오는 중…' : '새로고침' }}</button>
              </div>
            </div>
            <rv-meter v-if="goals" label="ACT 에피소드" :value="s.readiness.act_episodes" :max="goals.act_episodes" />
            <rv-state v-else-if="s.readiness.error" state="error" :message="s.readiness.error" retry-label="목표 다시 불러오기" @retry="s.refreshReadiness()" />

            <rv-state v-if="epState === 'error'" state="error" :message="epError" @retry="refreshEpisodes" />
            <rv-state v-else-if="epState === 'loading'" state="loading" />
            <rv-state v-else-if="!episodes.length" state="empty" message="아직 저장된 에피소드가 없습니다."
                      hint="위에서 ‘세션 시작’을 누르면 에피소드가 여기에 쌓입니다." />
            <template v-else>
              <div class="rv-table-wrap" role="region" tabindex="0" aria-labelledby="co-ep-h">
                <table class="rv-table co-table co-table--ep">
                  <thead>
                    <tr>
                      <th scope="col">에피소드</th>
                      <th scope="col">물체</th>
                      <th scope="col" class="rv-num-cell">프레임 · 시간</th>
                      <th scope="col">점검</th>
                      <th scope="col"><span class="rv-sr-only">메뉴</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="r in epRows" :key="r.id">
                      <th scope="row">
                        <code>{{ r.id }}</code>
                        <span v-if="r.when" class="co-sub rv-num">{{ r.when }}</span>
                      </th>
                      <td>
                        <span class="co-strong">{{ r.ep.object_id }}</span>
                        <span v-if="r.sub" class="co-sub">{{ r.sub }}</span>
                      </td>
                      <td class="rv-num-cell rv-muted">{{ r.size }}</td>
                      <td>
                        <template v-if="r.flags.length">
                          <rv-status v-for="f in r.flags" :key="f" kind="warn" :label="f" class="co-flag" />
                        </template>
                        <span v-else class="rv-muted">통과</span>
                      </td>
                      <td>
                        <div class="co-rowact">
                          <rv-menu :label="r.id + ' 에피소드 메뉴 — 편집, 삭제'" :items="epMenu" @select="onEpMenu(r.ep, $event)" />
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <button v-if="episodes.length > epShown" type="button" class="rv-btn co-more" @click="epShown += EP_PAGE">
                더 보기 ({{ epShown }} / {{ episodes.length }})
              </button>
            </template>
          </section>

          <rv-dialog v-model="editOpen" title="에피소드 편집">
            <div class="co-edit">
              <p class="rv-muted">에피소드 <code>{{ edit.id }}</code></p>
              <div class="rv-field">
                <label for="co-e-obj">물체</label>
                <input id="co-e-obj" type="text" v-model="edit.object" list="co-obj-list" maxlength="64" autocomplete="off"
                       :aria-invalid="editObjErr ? 'true' : null" @keydown.enter.prevent="editCanSave && saveEdit()">
                <datalist id="co-obj-list"><option v-for="o in objects" :key="o.object_id" :value="o.object_id"></option></datalist>
                <span v-if="editObjErr" class="rv-field__error"><rv-status kind="warn" :label="editObjErr" /></span>
              </div>
              <template v-if="!edit.demo">
                <div class="rv-field">
                  <span>품질</span>
                  <rv-seg v-model="edit.label" label="품질" class="co-seg-block" :options="labelOptions" />
                </div>
                <div v-if="edit.label === 'defect'" class="rv-field">
                  <label for="co-e-def">불량 유형</label>
                  <select id="co-e-def" v-model="edit.defect">
                    <option value="" disabled>불량 유형을 고르세요</option>
                    <option v-for="t in defectTypes" :key="t" :value="t">{{ defectName(t) }}</option>
                  </select>
                </div>
              </template>
              <p v-if="edit.error" class="co-run__alert" role="alert"><rv-status kind="warn" :label="edit.error" /></p>
            </div>
            <template v-if="!edit.demo" #note>물체나 라벨을 바꾸면 분류 이미지 폴더도 함께 옮겨집니다.</template>
            <template #footer>
              <button type="button" class="rv-btn" @click="editOpen = false">취소</button>
              <button type="button" class="rv-btn rv-btn--primary" :disabled="!editCanSave" @click="saveEdit">{{ edit.saving ? '저장 중…' : '저장' }}</button>
            </template>
          </rv-dialog>
        </template>
      </section>`,

    setup() {
      const { ref, reactive, computed, watch, onMounted, onActivated, onDeactivated, onBeforeUnmount } = Vue;
      const s = RVS.store;
      const { api, fmt } = RVS;
      const errText = (err) => fmt.errText(err);

      const mode = ref('station');
      const modes = [{ value: 'station', label: '검사 샘플' }, { value: 'demo', label: '시연 녹화' }];
      const active = ref(false);

      const defectTypes = computed(() => s.meta.defect_types);
      const defectName = (t) => fmt.ko.defect[t] || t;
      const labelOptions = [{ value: 'good', label: '양품' }, { value: 'defect', label: '불량' }];
      const orangeOptions = [
        { value: 'right', label: '맞음' }, { value: 'partial', label: '일부만' },
        { value: 'wrong', label: '틀림' }, { value: '', label: '평가 안 함' },
      ];
      const goals = computed(() => (s.readiness.loaded ? s.readiness.goals : null));

      /* ---------------- 물체 목록 + 선택(두 흐름이 각자 선택을 가진다) ---------------- */
      const objects = ref([]);
      const recommendedPoses = ref(null);
      const objState = ref('loading');           // loading | ready | error
      const objError = ref('');
      const labelObj = reactive({ sel: '', neu: '' });
      const demoObj = reactive({ sel: '', neu: '' });
      const objIdOf = (o) => (o.sel === NEW ? o.neu.trim() : o.sel);
      const objErrOf = (o) => (o.sel === NEW && o.neu.trim() && !ID_RE.test(o.neu.trim()) ? ID_HINT : '');
      const labelObjId = computed(() => objIdOf(labelObj));
      const demoObjId = computed(() => objIdOf(demoObj));
      const labelObjErr = computed(() => objErrOf(labelObj));
      const demoObjErr = computed(() => objErrOf(demoObj));

      async function loadObjects() {
        try {
          const d = await api.get('/api/objects');
          objects.value = d.objects || [];
          recommendedPoses.value = d.recommended_poses ?? null;
          objState.value = 'ready';
          objError.value = '';
        } catch (err) {
          objState.value = 'error';
          objError.value = errText(err);
        }
      }
      // 목록이 비었거나 못 불러왔으면 새 물체 입력으로 바로 간다(못 쓰는 선택창만 남기지 않는다)
      watch([objects, objState], () => {
        for (const o of [labelObj, demoObj]) {
          if (o.sel === NEW) continue;
          if (!o.sel && (objState.value === 'error' || (objState.value === 'ready' && !objects.value.length))) o.sel = NEW;
          else if (o.sel && objState.value === 'ready' && !objects.value.some((x) => x.object_id === o.sel)) o.sel = '';
        }
      });

      /* ---------------- 검사 샘플: 촬영 목록 ---------------- */
      const inspections = ref([]);
      const summary = ref(null);
      const inspState = ref('loading');
      const inspError = ref('');
      const listFilter = ref('pending');
      const current = ref('');                   // 고른 촬영의 capture_id
      const failed = reactive({});               // 사진 로드 실패(src → true)

      async function loadInspections() {
        try {
          const d = await api.get('/api/inspections?limit=' + INSP_LIMIT);
          inspections.value = d.inspections || [];
          summary.value = d.summary || null;
          inspState.value = 'ready';
          inspError.value = '';
        } catch (err) {
          inspState.value = 'error';
          inspError.value = errText(err);
        }
      }

      const counts = computed(() => {
        const sm = summary.value;
        const items = inspections.value;
        return {
          total: sm?.total ?? items.length,
          unlabeled: sm?.unlabeled ?? items.filter((i) => !i.label).length,
          good: sm?.objects?.good ?? null,
          defect: sm?.objects?.defect ?? null,
        };
      });
      const defectRate = computed(() => {
        const { good, defect } = counts.value;
        return good === null || defect === null || good + defect === 0 ? '—' : fmt.pct(defect / (good + defect));
      });
      const filterOptions = computed(() => [
        { value: 'pending', label: '라벨 없음 ' + counts.value.unlabeled },
        { value: 'all', label: '전체 ' + counts.value.total },
      ]);
      const listRows = computed(() => inspections.value.filter((i) => listFilter.value === 'all' || !i.label));

      const imgUrl = (id, cam) => '/api/inspections/' + id + '/' + cam + '.jpg';
      const thumbOf = (it) => imgUrl(it.capture_id, it.cameras[0]);
      const camTitle = (cam) => fmt.camName(s.status.cameras.find((c) => c.name === cam) || cam);
      const labelText = (it) => {
        const L = it.label;
        return [fmt.ko.label[L.label] || L.label, L.defect_type ? defectName(L.defect_type) : null, L.object_id,
          L.pose_index != null ? L.pose_index + '번째 자세' : null].filter(Boolean).join(' · ');
      };
      const orangeText = (it) => (it.label?.orange_eval ? '주황 표시 ' + ORANGE_KO[it.label.orange_eval] : '');
      const inspMenu = (it) => [{ id: 'delete', label: it.label ? '삭제 (라벨 있는 촬영은 지울 수 없음)' : '삭제', danger: true, disabled: !!it.label }];

      // 막 찍은 촬영은 목록을 다시 받기 전에도 화면에 나와야 한다
      const currentInsp = computed(() => {
        const id = current.value;
        if (!id) return null;
        const found = inspections.value.find((i) => i.capture_id === id);
        if (found) return found;
        const last = s.capture.last;
        return last && last.capture_id === id ? { capture_id: id, captured_at: null, cameras: last.cameras || [], label: null } : null;
      });
      const views = computed(() => (currentInsp.value?.cameras || []).map((cam) => ({
        cam, src: imgUrl(current.value, cam), name: camTitle(cam),
      })));
      const capTitle = computed(() => (views.value.length ? '촬영 ' + views.value.length + '장' : '촬영'));

      /* ---------------- 검사 샘플: 라벨 폼 ---------------- */
      const form = reactive({ label: '', defect: '', orange: '' });
      const saving = ref(false);
      watch(() => form.label, (v) => { if (v !== 'defect') form.defect = ''; });

      function select(id) {
        current.value = id;
        form.orange = '';
        const L = inspections.value.find((i) => i.capture_id === id)?.label;
        if (!L) return;                          // 라벨 없는 촬영은 물체·품질을 그대로 이어 쓴다(같은 물체의 다음 자세)
        form.label = L.label || '';
        form.defect = L.defect_type || '';
        form.orange = L.orange_eval || '';
        if (objects.value.some((o) => o.object_id === L.object_id)) labelObj.sel = L.object_id;
        else { labelObj.sel = NEW; labelObj.neu = L.object_id || ''; }
      }

      const objInfo = computed(() => objects.value.find((o) => o.object_id === labelObj.sel) || null);
      const poseText = computed(() => {
        const L = currentInsp.value?.label;
        if (!L && !labelObjId.value) return '물체를 고르면 표시됩니다';
        const n = L && L.pose_index != null ? L.pose_index : (objInfo.value?.poses || 0) + 1;
        return n + '번째' + (recommendedPoses.value ? ' / 권장 ' + recommendedPoses.value : '');
      });

      // 막 찍은 촬영이면 서버가 계산한 표면 이상 요약이 capture.last 에 있다
      const anomalyNow = computed(() => {
        const last = s.capture.last;
        return last && last.capture_id === current.value ? (last.mesh?.stats?.anomaly || null) : null;
      });
      const anomalyText = computed(() => {
        const a = anomalyNow.value;
        if (!a) return '';
        if (a.skipped) return '이 촬영은 주황 표시를 만들지 못했습니다(' + a.skipped + ').';
        return '이 촬영: 표면 이상 ' + fmt.pct(a.flagged_ratio) + ' · 영역 ' + (a.num_regions ?? 0) + '개.';
      });

      const canSave = computed(() => !!current.value && !saving.value && !!labelObjId.value && !labelObjErr.value
        && !!form.label && (form.label !== 'defect' || !!form.defect));
      const saveText = computed(() => (currentInsp.value?.label ? '라벨 수정 저장' : '저장하고 다음 자세'));
      const saveBlock = computed(() => {
        if (!current.value) return '먼저 촬영하거나 목록에서 촬영을 고르세요.';
        if (!labelObjId.value) return '물체를 고르세요.';
        if (labelObjErr.value) return '물체 ID 형식을 고치세요.';
        if (!form.label) return '품질(양품·불량)을 고르세요.';
        if (form.label === 'defect' && !form.defect) return '불량 유형을 고르세요.';
        return '';
      });
      const canRetake = computed(() => !!currentInsp.value && !currentInsp.value.label && !capBusy.value && !saving.value);

      async function saveLabel() {
        if (!canSave.value) return;
        saving.value = true;
        const id = current.value;
        const body = {
          object_id: labelObjId.value, label: form.label,
          defect_type: form.label === 'defect' ? form.defect : null,
          orange_eval: form.orange || null,
        };
        const a = anomalyNow.value;
        if (a && !a.skipped && Number.isFinite(a.flagged_ratio)) {
          body.anomaly = { flagged_ratio: a.flagged_ratio, num_regions: a.num_regions ?? 0, max_severity: a.max_severity ?? 0 };
        }
        try {
          const d = await api.post('/api/inspections/' + id + '/label', body);
          const pose = d.inspection?.label?.pose_index;
          s.notify('라벨 저장 — ' + body.object_id + (pose != null ? ' ' + pose + '번째 자세' : ''));
          await Promise.all([loadInspections(), loadObjects()]);
          s.refreshReadiness();
          // 새로 등록한 물체는 이제 목록에 있다 — 다음 자세부터는 목록에서 고른 것으로 이어 간다
          if (labelObj.sel === NEW && objects.value.some((o) => o.object_id === body.object_id)) {
            labelObj.sel = body.object_id;
            labelObj.neu = '';
          }
          current.value = '';                    // 다음 자세: 물체를 돌려 놓고 다시 촬영
          form.orange = '';
        } catch (err) {
          s.notify(errText(err), 'error');
        } finally {
          saving.value = false;
        }
      }

      /* ---------------- 검사 샘플: 촬영 ---------------- */
      const capturing = ref(false);
      // 검사 화면 재구성·진단의 캘리브레이션 촬영이 같은 카메라를 쓰는 중이면 겹쳐 찍게 되고 잠금이 먼저 풀린다
      const capBusy = computed(() => capturing.value || s.capture.busy || s.busy.recon);
      const beltsMoving = computed(() => s.conveyors.some((b) => b.running));
      const stepNow = computed(() => {
        if (beltsMoving.value) return 1;
        if (!current.value) return 2;
        return currentInsp.value?.label ? 4 : 3;
      });

      async function capture() {
        if (capBusy.value || s.cycleRunning) return;
        capturing.value = true;
        try {
          // 벨트 폴링은 5초 간격이라 낡은 값일 수 있다 — 찍기 직전에 다시 읽어 확인한다(서버도 한 번 더 확인한다)
          try { await s.refreshConveyors(true); } catch { /* 서버가 다시 확인한다 */ }
          if (beltsMoving.value) {
            s.notify('벨트가 움직이는 중입니다. 멈춘 뒤 촬영하세요.', 'error');
            return;
          }
          await s.captureNow({ resolution: 48 });   // 실패하면 store 가 토스트를 띄운다. 성공한 뒤 선택은 아래 watch 가 맡는다
        } finally {
          capturing.value = false;
        }
      }

      // 이 화면에서 찍었든 자동 촬영이 찍었든, 새 촬영이 생기면 목록을 다시 받고 라벨 대상으로 고른다
      watch(() => s.capture.last, async (last) => {
        if (!last || !last.capture_id) return;
        await loadInspections();
        if (active.value && mode.value === 'station') select(last.capture_id);
      });

      async function removeInspection(it) {
        if (it.label) return false;
        if (!(await s.askConfirm({
          title: '촬영 삭제', okLabel: '삭제',
          message: '촬영 ' + it.capture_id + ' 을(를) 삭제합니다.\n사진 ' + ((it.cameras || []).length) + '장이 영구히 사라집니다.',
        }))) return false;
        try {
          await api.del('/api/inspections/' + it.capture_id);
          s.notify('촬영 ' + it.capture_id + ' 삭제됨');
          if (current.value === it.capture_id) current.value = '';
          await loadInspections();
          return true;
        } catch (err) {
          s.notify(errText(err), 'error');
          return false;
        }
      }
      const onInspMenu = (it, id) => { if (id === 'delete') removeInspection(it); };

      // 다시 찍기 = 라벨 없는 이 촬영을 지우고(확인) 바로 새로 찍는다
      async function retake() {
        if (!canRetake.value) return;
        if (await removeInspection(currentInsp.value)) capture();
      }

      /* ---------------- 시연 녹화: 서버 세션 ---------------- */
      const demo = computed(() => s.status.demo || { state: 'IDLE' });
      const demoState = computed(() => demo.value.state || 'IDLE');
      const demoActive = computed(() => demoState.value !== 'IDLE');
      const demoFault = computed(() => (demoActive.value ? '' : demo.value.fault || ''));
      const demoBusy = ref(false);
      const demoError = ref('');
      // 이미 도는 세션(다른 탭에서 시작했거나 새로고침 직후)이 있으면 그 화면으로 연다. 첫 폴링 응답이 와야 알 수 있어서 enter() 가 아니라 여기서 본다
      watch(demoActive, (on) => { if (on && active.value) mode.value = 'demo'; });

      const prefs = (() => { try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch { return {}; } })();
      const dform = reactive({ task: prefs.task || '', episode_sec: prefs.episode_sec || 20, reset_sec: prefs.reset_sec ?? 10 });
      const savePrefs = () => {
        try { localStorage.setItem(PREF_KEY, JSON.stringify({ task: dform.task, episode_sec: dform.episode_sec, reset_sec: dform.reset_sec })); } catch { /* 저장 못 해도 동작에는 지장 없다 */ }
      };
      const secErr = (v, lo, hi) => (Number.isFinite(v) && v >= lo && v <= hi ? '' : lo + '~' + hi + '초 사이로 적으세요');
      const epSecErr = computed(() => secErr(dform.episode_sec, 1, 600));
      const resetSecErr = computed(() => secErr(dform.reset_sec, 0, 120));

      // 서버가 강제하는 것(텔레옵·팔·다른 녹화·자율 실행)은 시작을 막고, 나머지는 안내만 한다
      const blockers = computed(() => {
        const st = s.status;
        const out = [];
        if (s.cycleRunning) out.push('자동 사이클이 진행 중입니다. 끝난 뒤 시작하세요.');
        if (!st.teleop.running) out.push('텔레옵이 꺼져 있습니다. 장비 제어에서 켜세요.');
        if (!st.leader.connected || !st.follower.connected) out.push('팔이 연결되지 않았습니다.');
        if (st.recording.state !== 'IDLE' && !demoActive.value) out.push('다른 녹화가 진행 중입니다.');
        if (st.autorun && st.autorun.running) out.push('자율 실행(rollout) 중입니다.');
        return out;
      });
      const startBlock = computed(() => {
        if (blockers.value.length) return blockers.value[0];
        if (!dform.task.trim()) return '작업 설명을 적으세요.';
        if (!demoObjId.value) return '물체를 고르세요.';
        if (demoObjErr.value) return '물체 ID 형식을 고치세요.';
        if (epSecErr.value || resetSecErr.value) return '길이와 리셋 시간을 고치세요.';
        return '';
      });
      const canStart = computed(() => !startBlock.value && !demoBusy.value);

      const checks = computed(() => {
        const st = s.status;
        const out = [];
        if (s.cycleRunning) out.push({ id: 'cycle', kind: 'warn', text: '자동 사이클 진행 중 — 시작할 수 없습니다' });
        out.push(st.teleop.running ? { id: 'tele', kind: 'ok', text: '텔레옵 켜짐' }
          : { id: 'tele', kind: 'warn', text: '텔레옵이 꺼져 있습니다 — 시작할 수 없습니다' });
        out.push(st.leader.connected && st.follower.connected ? { id: 'arms', kind: 'ok', text: '팔 연결(리더·팔로워)' }
          : { id: 'arms', kind: 'warn', text: '팔이 연결되지 않았습니다 — 시작할 수 없습니다' });
        const cams = st.cameras || [];
        const up = cams.filter((c) => c.connected).length;
        out.push(!cams.length ? { id: 'cams', kind: 'info', text: '카메라 상태를 아직 받지 못했습니다' }
          : up === cams.length ? { id: 'cams', kind: 'ok', text: '카메라 ' + up + '/' + cams.length }
          : { id: 'cams', kind: 'warn', text: '카메라 ' + up + '/' + cams.length + ' — 연결을 확인하세요(시작은 막지 않음)' });
        // 기준값(diagnostics.limits.max_sync_skew_ms)은 서버가 준 것만 쓴다. 없으면 판정하지 않는다
        const lim = s.config.limits?.max_sync_skew_ms;
        const sk = st.sync_skew_ms;
        out.push(sk === null || sk === undefined ? { id: 'skew', kind: 'info', text: '동기 오차 측정 불가(카메라 프레임이 2대 미만)' }
          : lim === null || lim === undefined ? { id: 'skew', kind: 'info', text: '동기 오차 ' + fmt.num(sk, 1) + ' ms — 기준값을 불러오지 못해 판정하지 않습니다' }
          : sk <= lim ? { id: 'skew', kind: 'ok', text: '동기 오차 ' + fmt.num(sk, 1) + ' ms (' + lim + ' ms 이하)' }
          : { id: 'skew', kind: 'warn', text: '동기 오차 ' + fmt.num(sk, 1) + ' ms — ' + lim + ' ms 초과(시작은 막지 않음)' });
        // 홈 자세는 실기에서 좌표계를 확인하기 전이라 자동으로 맞다·틀리다를 판정하지 않는다(R16)
        out.push({ id: 'home', kind: 'info', text: '팔로워 홈 자세 ±5° — 실기 확인 전이라 자동 판정하지 않습니다. 직접 확인하세요' });
        return out;
      });

      function applyDemo(d) {
        if (d && d.demo) s.status.demo = d.demo;
        s.refreshStatus().catch(() => { /* 폴링이 따라잡는다 */ });
      }
      async function demoCall(path, body, okMsg) {
        demoBusy.value = true;
        demoError.value = '';
        try {
          applyDemo(await api.post('/api/demo/' + path, body));
          if (okMsg) s.notify(okMsg);
          return true;
        } catch (err) {
          demoError.value = errText(err);
          s.notify(demoError.value, 'error');
          s.refreshStatus().catch(() => { /* 폴링이 따라잡는다 */ });   // 시간 초과여도 서버는 저장을 마쳤을 수 있다 — 실제 상태로 맞춘다
          return false;
        } finally {
          demoBusy.value = false;
        }
      }
      async function demoStart() {
        if (!canStart.value) return;
        const ok = await demoCall('start', {
          object_id: demoObjId.value, task: dform.task.trim(),
          episode_sec: Number(dform.episode_sec), reset_sec: Number(dform.reset_sec),
        }, '시연 세션을 시작했습니다.');
        if (ok) savePrefs();
      }
      const demoNext = () => { if (!demoBusy.value && demoActive.value) demoCall('next'); };
      // 서버가 리셋 중의 redo 를 409 로 거절한다(버릴 에피소드가 없다) — 보내지 않는다
      const demoRedo = () => { if (!demoBusy.value && demoState.value === 'RECORDING') demoCall('redo'); };
      async function demoEnd() {
        if (demoBusy.value || !demoActive.value) return;
        if (await demoCall('end', {}, '세션을 끝냈습니다. 현재 에피소드는 저장했습니다.')) refreshAfterSave();
      }

      // 진행 표시: 서버가 준 phase_elapsed_sec 를 폴링 사이에는 로컬 시계로 이어 붙인다(리셋 중엔 폴링이 느리다)
      const tick = ref(0);
      let syncAt = performance.now();
      let timer = null;
      watch(() => { const d = s.status.demo; return d ? d.state + '|' + d.episode_id + '|' + d.phase_elapsed_sec : ''; }, () => { syncAt = performance.now(); });
      watch([demoActive, active], () => {
        const on = demoActive.value && active.value;
        if (on && !timer) timer = setInterval(() => { tick.value += 1; }, 250);
        else if (!on && timer) { clearInterval(timer); timer = null; }
      }, { immediate: true });

      const phaseTotal = computed(() => Number(demo.value.phase_total_sec) || 0);
      const phaseElapsed = computed(() => {
        tick.value;                              // 250ms 마다 다시 계산
        if (!demoActive.value) return 0;
        const base = Number(demo.value.phase_elapsed_sec) || 0;
        const t = base + (performance.now() - syncAt) / 1000;
        return phaseTotal.value ? Math.min(phaseTotal.value, t) : t;
      });
      const runPct = computed(() => (phaseTotal.value ? Math.round((phaseElapsed.value / phaseTotal.value) * 100) : 0));
      const phaseLabel = computed(() => (demoState.value === 'RECORDING' ? '녹화 중' : demoState.value === 'RESET' ? '리셋 중' : '대기'));
      const runTitle = computed(() => {
        if (!demoActive.value) return '준비됨 — 다음 에피소드 ' + (s.meta.next_episode_id || '—');
        const clock = fmt.time(phaseElapsed.value) + ' / ' + fmt.time(phaseTotal.value);
        return demoState.value === 'RECORDING' ? '에피소드 ' + demo.value.episode_id + ' · ' + clock : '리셋 · ' + clock;
      });
      const runHint = computed(() => {
        const d = demo.value;
        if (demoState.value === 'RECORDING') return d.episode_sec + '초가 지나면 자동으로 저장하고 ' + d.reset_sec + '초 리셋 후 다음 에피소드를 시작합니다.';
        if (demoState.value === 'RESET') return '물체를 시작 위치로 되돌려 놓으세요. 리셋이 끝나면 다음 에피소드가 자동으로 시작됩니다.';
        return '시작하면 ' + dform.episode_sec + '초 녹화 → ' + dform.reset_sec + '초 리셋이 반복됩니다. 손은 리더 팔에, 조작은 키보드로.';
      });
      // 녹화 중에는 이번 에피소드의 최대 오차, 아닐 때는 지금 카메라 프레임 사이의 오차(null = 측정 불가)
      const skewNow = computed(() => (demoState.value === 'RECORDING' ? s.status.recording.max_skew_ms : s.status.sync_skew_ms));
      const skewText = computed(() => {
        const v = skewNow.value;
        if (v === null || v === undefined) return '측정 불가';
        const r = s.status.recording;
        return fmt.num(v, 1) + ' ms' + (demoState.value === 'RECORDING' && r.skew_violations > 0 ? ' · 초과 ' + r.skew_violations + '프레임' : '');
      });
      const skewOver = computed(() => {
        const lim = s.config.limits?.max_sync_skew_ms;
        const v = skewNow.value;
        return lim !== null && lim !== undefined && v !== null && v !== undefined && v > lim;
      });
      const announce = computed(() => (demoActive.value ? phaseLabel.value + ' — 에피소드 ' + demo.value.episode_id : ''));

      // 저장된 에피소드가 늘면 표·준비도·다음 ID 를 바로 다시 읽는다
      function refreshAfterSave() { loadEpisodes(); s.refreshReadiness(); s.loadMeta(); }
      watch(() => { const d = s.status.demo; return d ? d.saved + '|' + d.last_saved : ''; }, () => { if (active.value) refreshAfterSave(); });

      /* ---------------- 에피소드 표(레거시 app.js:682-727 이식) ---------------- */
      const episodes = ref([]);
      const epState = ref('loading');
      const epError = ref('');
      const epBusy = ref(false);
      const epShown = ref(EP_PAGE);

      async function loadEpisodes() {
        try {
          const d = await api.get('/api/episodes');
          // 최근 것부터. 폴더 이름 순서가 아니라 녹화 시각 순(ID 가 '50', '100010' 처럼 섞여 있다)
          episodes.value = (d.episodes || []).slice().sort((a, b) =>
            (b.started_at_epoch || 0) - (a.started_at_epoch || 0)
            || String(b.episode_id).localeCompare(String(a.episode_id), 'en', { numeric: true }));
          epState.value = 'ready';
          epError.value = '';
        } catch (err) {
          epState.value = 'error';
          epError.value = errText(err);
        }
      }
      async function refreshEpisodes() {
        epBusy.value = true;
        try { await loadEpisodes(); } finally { epBusy.value = false; }
      }

      const epRows = computed(() => episodes.value.slice(0, epShown.value).map((ep) => {
        const isDemo = ep.kind === 'demo';
        const flags = [];
        if (Number.isFinite(ep.duration_sec) && ep.duration_sec < TOO_SHORT_SEC) flags.push('너무 짧음 (' + TOO_SHORT_SEC + '초 미만)');
        if (ep.sync_skew_violations > 0) flags.push('동기 오차 초과 (최대 ' + fmt.num(ep.max_sync_skew_ms, 1) + ' ms · ' + ep.sync_skew_violations + '프레임)');
        // 시연 에피소드엔 라벨이 없다. 예전(분류 라벨) 에피소드만 품질을 보여 준다
        const sub = isDemo ? '시연'
          : [fmt.ko.label[ep.label] || ep.label, ep.defect_type ? defectName(ep.defect_type) : null].filter(Boolean).join(' · ');
        return {
          ep, id: ep.episode_id, flags, sub,
          when: ep.started_at_epoch ? fmt.dateTime(ep.started_at_epoch) : '',
          size: ep.num_frames + 'f · ' + fmt.num(ep.duration_sec, 1) + 's',
        };
      }));
      const epMenu = [{ id: 'edit', label: '편집' }, { separator: true }, { id: 'delete', label: '삭제', danger: true }];

      async function deleteEpisode(ep) {
        if (!(await s.askConfirm({
          title: '에피소드 삭제', okLabel: '삭제',
          message: '에피소드 ' + ep.episode_id + ' (' + ep.object_id + ') 를 삭제합니다.\n프레임 ' + ep.num_frames + '장이 영구히 사라집니다.',
        }))) return;
        try {
          await api.del('/api/episodes/' + ep.episode_id);
          s.notify('에피소드 ' + ep.episode_id + ' 삭제됨');
          refreshAfterSave();
        } catch (err) {
          s.notify(errText(err), 'error');
        }
      }
      const onEpMenu = (ep, id) => { if (id === 'edit') openEdit(ep); else if (id === 'delete') deleteEpisode(ep); };

      /* ---- 편집 대화상자(PATCH: 물체, 라벨, 불량 유형) ---- */
      const editOpen = ref(false);
      const edit = reactive({ id: '', demo: false, object: '', label: 'good', defect: '', error: '', saving: false });
      const editObjErr = computed(() => (edit.object.trim() && !ID_RE.test(edit.object.trim()) ? ID_HINT : ''));
      const editCanSave = computed(() => !edit.saving && ID_RE.test(edit.object.trim()) && (edit.demo || edit.label !== 'defect' || !!edit.defect));
      watch(() => edit.label, (v) => { if (v !== 'defect') edit.defect = ''; });

      function openEdit(ep) {
        Object.assign(edit, {
          id: ep.episode_id, demo: ep.kind === 'demo', object: ep.object_id || '',
          label: ep.label || 'good', defect: ep.defect_type || '', error: '', saving: false,
        });
        editOpen.value = true;
      }
      async function saveEdit() {
        if (!editCanSave.value) return;
        edit.saving = true;
        edit.error = '';
        try {
          // 시연 에피소드는 라벨이 없으니 물체만 보낸다
          const body = edit.demo ? { object_id: edit.object.trim() }
            : { object_id: edit.object.trim(), label: edit.label, defect_type: edit.label === 'defect' ? edit.defect : null };
          await api.patch('/api/episodes/' + edit.id, body);
          s.notify('에피소드 ' + edit.id + ' 수정 완료');
          editOpen.value = false;
          loadEpisodes(); loadObjects(); s.refreshReadiness();
        } catch (err) {
          edit.error = errText(err);              // 대화상자가 최상위 레이어라 토스트가 가려진다 — 안에서 보여 준다
        } finally {
          edit.saving = false;
        }
      }

      /* ---------------- 단축키 ---------------- */
      // input/textarea/select/[contenteditable]/dialog 안에서는 무시한다. 열린 메뉴(popover)도 Esc 를 쓰므로 비켜 준다
      function typing(e) {
        const t = e.target;
        if (t instanceof Element && t.closest('input, textarea, select, [contenteditable], dialog')) return true;
        if (document.querySelector('dialog[open]')) return true;
        try { if (document.querySelector(':popover-open')) return true; } catch { /* :popover-open 을 모르는 브라우저 */ }
        return false;
      }
      function onKey(e) {
        if (e.defaultPrevented || e.repeat || e.ctrlKey || e.metaKey || e.altKey || typing(e)) return;
        const code = e.code;                      // 글자는 e.key 가 아니라 e.code(한글 입력기가 켜져 있어도 같다)
        if (mode.value === 'demo') {
          if (!demoActive.value) return;
          if (e.key === 'ArrowRight' || code === 'KeyN') demoNext();
          else if (e.key === 'ArrowLeft' || code === 'KeyR') demoRedo();
          else if (e.key === 'Escape' || code === 'KeyQ') demoEnd();
          else return;
          e.preventDefault();
          return;
        }
        // Space·Enter 는 버튼·링크에 포커스가 있으면 그 버튼을 누르는 키이므로 가로채지 않는다
        const onBtn = e.target instanceof Element && !!e.target.closest('button, a');
        if (code === 'Space' && !onBtn) capture();
        else if ((code === 'Enter' || code === 'NumpadEnter') && !onBtn) saveLabel();
        else if (code === 'Backspace') retake();
        else return;
        e.preventDefault();
      }

      /* ---------------- 생명주기(keep-alive) ---------------- */
      function enter() {
        if (demoActive.value) mode.value = 'demo';
        loadInspections(); loadObjects(); loadEpisodes();
        s.refreshReadiness(); s.loadMeta();
      }
      function activate() {
        if (active.value) return;
        active.value = true;
        window.addEventListener('keydown', onKey);
        enter();
      }
      function deactivate() {
        if (!active.value) return;
        active.value = false;
        window.removeEventListener('keydown', onKey);
      }
      onMounted(activate);
      onActivated(activate);
      onDeactivated(deactivate);
      onBeforeUnmount(() => { deactivate(); clearInterval(timer); });

      return {
        s, fmt, NEW, EP_PAGE, steps: STEPS, mode, modes, defectTypes, defectName, labelOptions, orangeOptions, goals,
        // 물체
        objects, objState, objError, loadObjects, labelObj, demoObj, labelObjErr, demoObjErr,
        // 검사 샘플
        inspections, inspState, inspError, loadInspections, counts, defectRate, filterOptions, listFilter, listRows,
        current, failed, thumbOf, labelText, orangeText, inspMenu, onInspMenu, select, views, capTitle,
        form, saving, poseText, anomalyText, canSave, saveText, saveBlock, canRetake, saveLabel, retake,
        capBusy, beltsMoving, stepNow, capture,
        // 시연
        demo, demoState, demoActive, demoFault, demoBusy, demoError, dform, epSecErr, resetSecErr,
        startBlock, canStart, checks, demoStart, demoNext, demoRedo, demoEnd,
        runPct, phaseLabel, runTitle, runHint, skewText, skewOver, announce,
        // 에피소드
        episodes, epState, epError, epBusy, epShown, epRows, epMenu, refreshEpisodes, onEpMenu,
        editOpen, edit, editObjErr, editCanSave, saveEdit,
      };
    },
  };
})();
