/* =============================================================
   RVS.ui — 공용 컴포넌트 9종. app.js 가 RVS.ui.install(app) 으로 전역 등록하므로
   화면 템플릿에서 import 없이 <rv-status …> 처럼 쓴다.
   스타일은 base.css 의 .rv-* 클래스. 화면이 색·크기를 덮어쓰지 말고 props 로 쓴다.

   ■ rv-status — 상태 기호. 색 + 모양 + 글자 셋을 항상 같이 낸다.
     props  kind    'run'(● 가동) | 'stop'(○ 정지) | 'fault'(■ 고장, 빨강) | 'warn'(▲ 주의) | 'info'(ⓘ 정보)
            label   글자. 생략하면 기본어(가동/정지/고장/주의/정보)
            size    'sm'(기본, 아이콘 12px) | 'md'(18px)
            hideLabel  true 면 글자를 화면에서만 숨긴다(스크린리더는 읽음) — 아이콘만 둘 때
     slot   default = label 대신 들어갈 글자
     예  <rv-status kind="warn" label="범위 이탈" />

   ■ rv-gauge — 아날로그 지시계(트랙 8px, 음영 = 정상 범위, 바늘 3×16). 범위를 벗어나면 바늘이
     --warn 으로 바뀌고 ▲ + '범위 이탈' 글자가 붙는다. 루트가 role="img" 이고 aria-label 이 자동으로
     만들어진다("C1 하단 14.9 fps, 정상 범위 13.5에서 15"). 한 줄 = [label][트랙][값] 그리드.
     props  value     숫자. null/undefined 면 바늘 없이 '—'
            min, max  트랙의 양 끝(기본 0, 100). normal 이 없으면 이 밖이 '이탈'
            normal    [lo, hi] 정상 범위 음영. 있으면 이 밖이 '이탈'. lo 나 hi 가 null 이면 그쪽이 열린 끝(예: [0, null] = 0 이상이면 정상)
            ghost     보조 바늘(리더 목표값 등, 옅은 회색 3×12)
            ghostLabel  aria 에 쓸 이름(기본 '기준')
            center    true 면 트랙 한가운데에 눈금(0 이 중앙인 관절 범위)
            label     왼쪽 이름. 없으면 이름 칸이 사라진다
            unit      값 뒤에 붙일 단위(기본 '')
            decimals  소수 자리(기본 1)
            format    (v)=>문자열. 주면 unit·decimals 대신 이걸로 값 글자를 만든다(예: RVS.fmt.deg)
            showValue false 면 값 칸을 뺀다(트랙만)
            outText   이탈 문구(기본 '범위 이탈')
     CSS 변수  --rv-gauge-label(이름 칸 폭, 기본 112px), --rv-gauge-value(값 칸 폭, 기본 64px)
     예  <rv-gauge :label="fmt.jointName(0)" :value="v" :min="-120" :max="120" center :format="fmt.deg" />

   ■ rv-meter — 진행 막대 + 수치(준비도 등). role="progressbar".
     props  value, max(기본 100)   text(우측 글자, 생략하면 "value / max")   unit   label(왼쪽 이름)
     CSS 변수  --rv-meter-label(기본 96px), --rv-meter-value(기본 56px)
     예  <rv-meter label="ACT 에피소드" :value="14" :max="50" />

   ■ rv-seg — 세그먼트(aria-pressed 버튼 묶음).
     props  modelValue, options [{ value, label, disabled? }], label(그룹 aria-label), size 'md'|'sm'
     events update:modelValue(value), change(value)   — v-model 로 쓴다. 이미 선택된 걸 눌러도 안 보낸다.
     예  <rv-seg v-model="dir" label="방향" :options="[{value:'cw',label:'정방향'},{value:'ccw',label:'역방향'}]" />

   ■ rv-switch — role="switch". 제어 컴포넌트다: 눌러도 스스로 뒤집히지 않고 modelValue 를 따른다
     (서버 응답을 받은 뒤 부모가 값을 바꾼다).
     props  modelValue, label, disabled, busy(true 면 잠그고 aria-busy)
     events update:modelValue(다음 값), change(다음 값)
     slot   default = label 대신 들어갈 내용
     예  <rv-switch :model-value="s.status.teleop.running" :busy="s.busy.teleop" label="텔레오퍼레이션" @change="toggleTeleop" />

   ■ rv-menu — 더보기(⋯) 메뉴. popover 라서 카드의 overflow 에 잘리지 않는다.
     Esc 닫기+포커스 복귀, ↑↓ Home End 이동, 바깥 클릭·스크롤·Tab 으로 닫힘.
     props  items [{ id, label, danger?, disabled? } | { separator: true }]   danger = 위험색(--danger)
            label  트리거 버튼의 aria-label(기본 '더보기'). "공급 벨트 더보기"처럼 대상을 넣을 것
            disabled
     events select(id)
     예  <rv-menu label="공급 벨트 더보기" :items="[{id:'zero',label:'원점 설정'},{separator:true},{id:'rel',label:'포트 해제',danger:true}]" @select="onMenu" />

   ■ rv-dialog — <dialog> 래퍼(showModal). 모든 대화상자 푸터에 비상 정지 버튼이 자동으로 들어간다
     (모달이 배경을 inert 로 만들어 상단바 비상 정지를 못 누르기 때문).
     Esc(네이티브 cancel)·배경 클릭·닫기 버튼으로 닫히고, 닫히면 연 요소로 포커스가 돌아온다.
     props  modelValue(열림), title, size 'sm'|'md'(기본)|'lg', returnFocus(포커스를 돌려줄 Element, 생략하면 열 때 포커스 있던 요소)
     events update:modelValue(false), close
     slots  header(스코프 { titleId } — 직접 헤더를 그릴 때 `<template #header="{ titleId }">` 안의 제목 요소에 :id="titleId")
            default = 본문(스크롤됨)
            note   = 푸터 왼쪽 안내 문장
            footer = 푸터 버튼들(비상 정지 뒤에 놓인다)
     예  <rv-dialog v-model="open" title="에피소드 편집"> … <template #footer><button class="rv-btn rv-btn--primary">저장</button></template></rv-dialog>

   ■ rv-cam-frame — 카메라 프레임: MJPEG + ROI 오버레이 + 신호 없음. 4:3.
     props  slot       카메라 슬롯 이름('cam1' …) — 스트림은 store.streamUrl(slot) 의 stream_port 로 붙는다
            roi        [x0,y0,x1,y1] 0~1(플립 적용 후, config roi). 있으면 점선 사각형을 그린다
            live       false 면 스트림을 끊는다(src 제거 → 연결 해제). 화면이 비활성(keep-alive)이면 자동으로 끊고 재진입 시 다시 붙는다
            connected  false 면 스트림을 열지 않고 '신호 없음'. 이미지 로드가 실패해도 '신호 없음'이고 5초마다 다시 붙는다
            label      alt 글자(기본 slot)
            fps, quality  스트림 쿼리(기본 12, 70)
     slot   default = 프레임 위에 얹을 내용(녹화 중 표시 등, 클릭은 통과)
     MJPEG 연결은 오리진당 6개 한도를 먹으니 장비 제어 화면에서만 live 로 쓴다(개요·진단에서는 쓰지 말 것).
     예  <rv-cam-frame :slot="c.name" :roi="roiOf(c.name)" :connected="c.connected" :live="active" />

   ■ rv-state — 데이터 블록의 로딩·비어 있음·오류 상태. 모든 데이터 블록이 셋을 다 가져야 한다.
     props  state    'loading' | 'empty' | 'error'
            message  loading: 기본 '불러오는 중…' / empty: 무엇이 비었는지 / error: 무슨 일이 났는지(▲ 와 함께 보임)
            hint     empty 의 보조 설명(다음에 뭘 하면 되는지)
            retryLabel  기본 '다시 시도'
     events retry   (error 상태의 버튼)
     slot   default = empty 상태에서 다음 동작 버튼
     예  <rv-state v-if="s.alertsError" state="error" :message="s.alertsError" @retry="s.refreshAlerts()" />

   ■ 알림(셸 소유) — 개요의 알림 목록이 셸의 알림 상세 창을 연다. 계약은 store.js 머리 주석 참고.
     s.alerts          마지막 /api/alerts 응답 전체 { counts:{fault,warn,info}, alerts:[{ id, severity, title, summary, since, detail, action }], generated_at }
     s.alertsError     마지막 갱신 실패 메시지(성공하면 null), s.alertsLoaded 는 첫 성공 여부
     s.openAlert(id)   셸 알림 상세 창을 연다(닫으면 포커스가 누른 행으로 돌아온다. 행에 :key="a.id" 고정)
     s.isAcked(id)     '확인함' 여부(localStorage). since 가 바뀌면 다시 false
     s.ackAlert(id)    확인함 저장
   ============================================================= */
(function () {
  const { ref, computed, watch, nextTick, onMounted, onBeforeUnmount, onActivated, onDeactivated } = Vue;
  const RVS = (window.RVS = window.RVS || {});
  const store = RVS.store;

  let uid = 0;
  const STATUS_WORD = { run: '가동', stop: '정지', fault: '고장', warn: '주의', info: '정보' };

  /* ---------------- rv-status ---------------- */
  const RvStatus = {
    name: 'RvStatus',
    props: {
      kind: { type: String, required: true },
      label: String,
      size: { type: String, default: 'sm' },
      hideLabel: Boolean,
    },
    setup(props) {
      const k = computed(() => (STATUS_WORD[props.kind] ? props.kind : 'info'));
      const text = computed(() => props.label || STATUS_WORD[k.value]);
      return { k, text };
    },
    template: `
      <span class="rv-status" :class="['rv-status--' + k, 'rv-status--' + size]">
        <svg v-if="k === 'run'" class="rv-status__icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><circle cx="10" cy="10" r="7"/></svg>
        <svg v-else-if="k === 'stop'" class="rv-status__icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><circle cx="10" cy="10" r="6.25"/></svg>
        <svg v-else-if="k === 'fault'" class="rv-status__icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><rect x="3" y="3" width="14" height="14" rx="2.5"/></svg>
        <svg v-else-if="k === 'warn'" class="rv-status__icon" viewBox="0 0 14 13" aria-hidden="true" focusable="false"><path d="M7 1L13 12H1z"/></svg>
        <svg v-else class="rv-status__icon" viewBox="0 0 14 14" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="6"/><path d="M7 6v4M7 4v.01"/></svg>
        <span :class="hideLabel ? 'rv-sr-only' : 'rv-status__label'"><slot>{{ text }}</slot></span>
      </span>`,
  };

  /* ---------------- rv-gauge ---------------- */
  const RvGauge = {
    name: 'RvGauge',
    components: { RvStatus },
    props: {
      value: Number,
      min: { type: Number, default: 0 },
      max: { type: Number, default: 100 },
      normal: Array,
      ghost: Number,
      ghostLabel: { type: String, default: '기준' },
      center: Boolean,
      label: String,
      unit: { type: String, default: '' },
      decimals: { type: Number, default: 1 },
      format: Function,
      showValue: { type: Boolean, default: true },
      outText: { type: String, default: '범위 이탈' },
    },
    setup(props) {
      const finite = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
      const pct = (v) => Math.min(100, Math.max(0, ((v - props.min) / ((props.max - props.min) || 1)) * 100));
      const has = computed(() => finite(props.value));
      const pos = computed(() => (has.value ? pct(Number(props.value)) : 0));
      const ghostPos = computed(() => (finite(props.ghost) ? pct(Number(props.ghost)) : null));
      const band = computed(() => {
        if (!props.normal) return null;
        const lo = pct(props.normal[0] ?? props.min);
        return { left: `${lo}%`, width: `${pct(props.normal[1] ?? props.max) - lo}%` };
      });
      const out = computed(() => {
        if (!has.value) return false;
        const [lo, hi] = props.normal || [props.min, props.max];
        const v = Number(props.value);
        return (lo !== null && v < lo) || (hi !== null && v > hi);
      });
      const fmtVal = (v) => (props.format ? props.format(v) : `${Number(v).toFixed(props.decimals)}${props.unit}`);
      const text = computed(() => (has.value ? fmtVal(Number(props.value)) : '—'));
      const aria = computed(() => {
        const parts = [];
        if (props.label) parts.push(props.label);
        parts.push(has.value ? text.value : '값 없음');
        parts.push(props.normal ? `정상 범위 ${props.normal[0] ?? '제한 없음'}에서 ${props.normal[1] ?? '제한 없음'}` : `범위 ${props.min}에서 ${props.max}`);
        if (ghostPos.value !== null) parts.push(`${props.ghostLabel} ${fmtVal(Number(props.ghost))}`);
        if (out.value) parts.push(props.outText);
        return parts.join(', ');
      });
      return { has, pos, ghostPos, band, out, text, aria };
    },
    template: `
      <div class="rv-gauge" role="img" :aria-label="aria"
           :class="{ 'rv-gauge--out': out, 'rv-gauge--nolabel': !label, 'rv-gauge--novalue': !showValue }">
        <span v-if="label" class="rv-gauge__label">{{ label }}</span>
        <div class="rv-gauge__track">
          <span v-if="band" class="rv-gauge__band" :style="band"></span>
          <span v-if="center" class="rv-gauge__center"></span>
          <span v-if="ghostPos !== null" class="rv-gauge__ghost" :style="{ left: ghostPos + '%' }"></span>
          <span v-if="has" class="rv-gauge__needle" :style="{ left: pos + '%' }"></span>
        </div>
        <span v-if="showValue" class="rv-gauge__value">{{ text }}</span>
        <span v-if="out" class="rv-gauge__note"><rv-status kind="warn" :label="outText" /></span>
      </div>`,
  };

  /* ---------------- rv-meter ---------------- */
  const RvMeter = {
    name: 'RvMeter',
    props: {
      value: Number,
      max: { type: Number, default: 100 },
      label: String,
      text: String,
      unit: { type: String, default: '' },
    },
    setup(props) {
      const has = computed(() => props.value !== null && props.value !== undefined && Number.isFinite(props.value));
      const pct = computed(() => (has.value ? Math.min(100, Math.max(0, (props.value / (props.max || 1)) * 100)) : 0));
      const shown = computed(() => props.text ?? (has.value ? `${props.value} / ${props.max}${props.unit}` : '—'));
      return { has, pct, shown };
    },
    template: `
      <div class="rv-meter" :class="{ 'rv-meter--nolabel': !label }" role="progressbar" :aria-label="label || '진행률'"
           aria-valuemin="0" :aria-valuemax="max" :aria-valuenow="has ? value : null" :aria-valuetext="shown">
        <span v-if="label" class="rv-meter__label" aria-hidden="true">{{ label }}</span>
        <div class="rv-meter__bar" aria-hidden="true"><div class="rv-meter__fill" :style="{ width: pct + '%' }"></div></div>
        <span class="rv-meter__text" aria-hidden="true">{{ shown }}</span>
      </div>`,
  };

  /* ---------------- rv-seg ---------------- */
  const RvSeg = {
    name: 'RvSeg',
    props: {
      modelValue: null,
      options: { type: Array, required: true },
      label: String,
      size: { type: String, default: 'md' },
    },
    emits: ['update:modelValue', 'change'],
    setup(props, { emit }) {
      function pick(o) {
        if (o.disabled || o.value === props.modelValue) return;
        emit('update:modelValue', o.value);
        emit('change', o.value);
      }
      return { pick };
    },
    template: `
      <div class="rv-seg" :class="'rv-seg--' + size" role="group" :aria-label="label">
        <button v-for="o in options" :key="o.value" type="button" class="rv-seg__btn"
                :aria-pressed="o.value === modelValue ? 'true' : 'false'" :disabled="o.disabled" @click="pick(o)">{{ o.label }}</button>
      </div>`,
  };

  /* ---------------- rv-switch ---------------- */
  const RvSwitch = {
    name: 'RvSwitch',
    props: { modelValue: Boolean, label: String, disabled: Boolean, busy: Boolean },
    emits: ['update:modelValue', 'change'],
    setup(props, { emit }) {
      function toggle() {
        emit('update:modelValue', !props.modelValue);
        emit('change', !props.modelValue);
      }
      return { toggle };
    },
    template: `
      <label class="rv-switch" :class="{ 'rv-switch--off': disabled || busy }">
        <span v-if="label || $slots.default"><slot>{{ label }}</slot></span>
        <button type="button" role="switch" class="rv-switch__btn" :aria-checked="modelValue ? 'true' : 'false'"
                :disabled="disabled || busy" :aria-busy="busy ? 'true' : null" @click="toggle">
          <span class="rv-switch__knob"></span>
        </button>
      </label>`,
  };

  /* ---------------- rv-menu ---------------- */
  const RvMenu = {
    name: 'RvMenu',
    props: {
      items: { type: Array, required: true },
      label: { type: String, default: '더보기' },
      disabled: Boolean,
    },
    emits: ['select'],
    setup(props, { emit }) {
      const open = ref(false);
      const btn = ref(null);
      const pop = ref(null);
      const id = `rv-menu-${++uid}`;

      const entries = () => (pop.value ? [...pop.value.querySelectorAll('[role="menuitem"]:not(:disabled)')] : []);

      // popover 는 최상위 레이어라 fixed 좌표로 직접 놓는다: 트리거 오른쪽 끝에 맞춰 아래로, 모자라면 위로.
      function place() {
        const r = btn.value.getBoundingClientRect();
        const p = pop.value;
        const w = p.offsetWidth;
        const h = p.offsetHeight;
        let top = r.bottom + 4;
        if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
        p.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
        p.style.top = `${top}px`;
      }

      // popover="manual" — 자동 light-dismiss 는 트리거를 눌러 닫을 때 pointerdown 에서 닫히고 click 에서 다시 열린다.
      function away(e) {
        if (!pop.value.contains(e.target) && !btn.value.contains(e.target)) close();
      }

      function show(last) {
        if (open.value || props.disabled) return;
        pop.value.showPopover();
        place();
        open.value = true;
        document.addEventListener('pointerdown', away, true);
        window.addEventListener('scroll', close, true);   // fixed 좌표가 어긋나므로 닫는다
        window.addEventListener('resize', close);
        const list = entries();
        (last ? list[list.length - 1] : list[0])?.focus({ preventScroll: true });
      }

      function close(refocus) {
        if (!open.value) return;
        open.value = false;
        pop.value.hidePopover();
        document.removeEventListener('pointerdown', away, true);
        window.removeEventListener('scroll', close, true);
        window.removeEventListener('resize', close);
        if (refocus === true) btn.value.focus();
      }

      const toggle = () => (open.value ? close(true) : show(false));

      function choose(item) {
        if (item.disabled) return;
        close(true);
        emit('select', item.id);
      }

      function onKey(e) {
        const list = entries();
        const i = list.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') list[(i + 1) % list.length]?.focus();
        else if (e.key === 'ArrowUp') list[(i - 1 + list.length) % list.length]?.focus();
        else if (e.key === 'Home') list[0]?.focus();
        else if (e.key === 'End') list[list.length - 1]?.focus();
        else if (e.key === 'Escape') close(true);
        else if (e.key === 'Tab') { close(true); return; }   // 기본 동작이 포커스를 트리거 다음으로 옮긴다
        else return;
        e.preventDefault();   // Escape 가 바깥 <dialog> 까지 닫지 않게 하는 것도 이것
      }

      onBeforeUnmount(() => close());
      onDeactivated(() => close());   // keep-alive 로 화면이 가려질 때

      return { open, btn, pop, id, toggle, show, choose, onKey };
    },
    template: `
      <span class="rv-menu">
        <button ref="btn" type="button" class="rv-icon-btn" aria-haspopup="menu" :aria-expanded="open ? 'true' : 'false'"
                :aria-controls="id" :aria-label="label" :disabled="disabled"
                @click="toggle" @keydown.down.prevent="show(false)" @keydown.up.prevent="show(true)">
          <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
            <circle cx="4" cy="10" r="1.7" fill="currentColor"/><circle cx="10" cy="10" r="1.7" fill="currentColor"/><circle cx="16" cy="10" r="1.7" fill="currentColor"/>
          </svg>
        </button>
        <div :id="id" ref="pop" class="rv-menu__pop" role="menu" popover="manual" :aria-label="label" @keydown="onKey">
          <template v-for="(it, i) in items" :key="it.id || ('sep' + i)">
            <div v-if="it.separator" class="rv-menu__sep" role="separator"></div>
            <button v-else type="button" role="menuitem" class="rv-menu__item" :class="{ 'rv-menu__item--danger': it.danger }"
                    :disabled="it.disabled" tabindex="-1" @click="choose(it)">{{ it.label }}</button>
          </template>
        </div>
      </span>`,
  };

  /* ---------------- rv-dialog ---------------- */
  const RvDialog = {
    name: 'RvDialog',
    props: {
      modelValue: Boolean,
      title: String,
      size: { type: String, default: 'md' },
      returnFocus: null,
    },
    emits: ['update:modelValue', 'close'],
    setup(props, { emit }) {
      const el = ref(null);
      const titleId = `rv-dlg-${++uid}`;
      const estopMsg = ref('');
      let opener = null;     // 닫을 때 포커스를 돌려줄 요소
      let wasActive = null;  // 열 때 포커스가 있던 요소(브라우저가 닫으면서 네이티브로 되돌려 주는 쪽)
      let downOnBackdrop = false;

      // 모달이 열려 있으면 배경(상단바 옆의 비상 정지 결과 문구와 토스트)이 inert 에 scrim 으로 가려진다.
      // 푸터의 비상 정지 결과를 대화상자 안에서도 보이고 읽히게 estop.js 가 쓴 문장을 그대로 비춘다.
      const onEstop = () => {
        const m = document.getElementById('rvs-estop-msg');
        if (el.value && el.value.open) estopMsg.value = m ? m.textContent : '';
      };
      onMounted(() => window.addEventListener('rvs:estop', onEstop));
      onBeforeUnmount(() => window.removeEventListener('rvs:estop', onEstop));

      function sync() {
        const d = el.value;
        if (!d) return;
        if (props.modelValue && !d.open) {
          wasActive = document.activeElement;
          opener = props.returnFocus instanceof Element ? props.returnFocus : wasActive;
          estopMsg.value = '';
          d.showModal();
        } else if (!props.modelValue && d.open) {
          d.close();
        }
      }
      watch(() => props.modelValue, sync, { flush: 'post' });
      onMounted(sync);

      // Esc(cancel → close), 닫기 버튼, 배경 클릭, 코드의 close() 가 모두 여기로 온다
      function onClose() {
        if (props.modelValue) emit('update:modelValue', false);
        emit('close');
        const back = opener;
        const was = wasActive;
        opener = wasActive = null;
        // 목록이 다시 그려져 opener 가 교체됐을 수 있으니 한 틱 뒤에, 아직 붙어 있을 때만 돌려준다.
        // 아무도 포커스를 옮기지 않았을 때(body 이거나 브라우저가 되돌려 준 요소)만 돌려준다 —
        // 닫으면서 다른 화면으로 이동해 셸이 h1 에 포커스를 줬다면 뺏지 않는다.
        nextTick(() => {
          const cur = document.activeElement;
          const untouched = !cur || cur === document.body || cur === was;
          if (back && back.isConnected && untouched) back.focus();
        });
      }

      // 배경 클릭 = 눌림과 뗌이 모두 <dialog> 자신일 때(패널 안에서 끌다 밖에서 놓은 경우는 닫지 않는다)
      const down = (e) => { downOnBackdrop = e.target === el.value; };
      const click = (e) => {
        if (downOnBackdrop && e.target === el.value) el.value.close();
        downOnBackdrop = false;
      };

      // keep-alive 로 화면이 가려지면 DOM 에서 떨어진 모달이 반쯤 열린 채 남는다
      onDeactivated(() => { if (el.value && el.value.open) el.value.close(); });

      return { el, titleId, estopMsg, onClose, down, click };
    },
    template: `
      <dialog ref="el" class="rv-dialog" :class="'rv-dialog--' + size" :aria-labelledby="titleId"
              @close="onClose" @pointerdown="down" @click="click">
        <div class="rv-dialog__panel">
          <div class="rv-dialog__head">
            <div class="rv-dialog__headmain">
              <slot name="header" :titleId="titleId"><h2 :id="titleId" class="rv-dialog__title">{{ title }}</h2></slot>
            </div>
            <button type="button" class="rv-dialog__x" aria-label="닫기" autofocus @click="el.close()">
              <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M5.5 5.5l9 9M14.5 5.5l-9 9"/></svg>
            </button>
          </div>
          <div class="rv-dialog__body"><slot /></div>
          <div class="rv-dialog__foot">
            <p class="rv-dialog__estop" :class="{ 'rv-dialog__estop--empty': !estopMsg, 'rv-dialog__estop--fail': estopMsg.includes('실패') }"
               role="status" aria-live="assertive">{{ estopMsg }}</p>
            <p v-if="$slots.note" class="rv-dialog__note"><slot name="note" /></p>
            <div class="rv-dialog__actions">
              <button type="button" class="rv-btn rv-btn--danger" data-rvs-estop>
                <svg class="rv-btn__stop" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><rect x="1" y="1" width="10" height="10" rx="2"/></svg>비상 정지
              </button>
              <slot name="footer" />
            </div>
          </div>
        </div>
      </dialog>`,
  };

  /* ---------------- rv-cam-frame ---------------- */
  const RvCamFrame = {
    name: 'RvCamFrame',
    components: { RvStatus },
    props: {
      slot: String,
      roi: Array,
      live: { type: Boolean, default: true },
      connected: { type: Boolean, default: true },
      label: String,
      fps: { type: Number, default: 12 },
      quality: { type: Number, default: 70 },
    },
    setup(props) {
      const img = ref(null);
      const active = ref(true);    // 화면이 keep-alive 로 가려져 있지 않은가
      const failed = ref(false);
      const retry = ref(0);
      let timer = null;

      // MJPEG 은 끝나지 않는 응답이라 가려진 화면이 연결을 계속 물고 있으면 오리진당 6개 한도를 먹는다.
      // 연결은 <img> 의 src 속성을 '떼야' 끊긴다(실측: 속성 제거 = ERR_ABORTED, 요소를 DOM 에서 떼기만 하면 안 끊김).
      // 그래서 <img> 는 항상 두고 src 만 토글하며, 가려지거나 사라질 때는 직접 먼저 뗀다.
      const release = () => { if (img.value) img.value.removeAttribute('src'); };
      onActivated(() => { active.value = true; });
      onDeactivated(() => { active.value = false; clearTimeout(timer); release(); });
      onBeforeUnmount(() => { clearTimeout(timer); release(); });

      const src = computed(() => {
        if (!props.live || !props.connected || !active.value || !props.slot) return null;
        const url = store.streamUrl(props.slot, { fps: props.fps, q: props.quality });
        return url && retry.value ? `${url}&n=${retry.value}` : url;
      });
      const rect = computed(() => {
        const r = props.roi;
        return r && r.length === 4 ? { x: r[0], y: r[1], width: r[2] - r[0], height: r[3] - r[1] } : null;
      });
      const noSignal = computed(() => !props.connected || failed.value);

      // 스트림을 끊으면(live=false, 화면 가려짐) 지난 실패 표시도 같이 지운다
      watch(src, (v) => { if (!v) failed.value = false; });

      const onLoad = () => { failed.value = false; };
      function onError() {
        failed.value = true;
        clearTimeout(timer);
        timer = setTimeout(() => { retry.value += 1; }, 5000);   // 서버가 돌아오면 자동으로 다시 붙는다
      }

      return { img, src, rect, noSignal, onLoad, onError };
    },
    template: `
      <div class="rv-cam">
        <img ref="img" class="rv-cam__img" :src="src || null" :alt="label || slot" @load="onLoad" @error="onError">
        <svg v-if="rect" class="rv-cam__roi" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true" focusable="false">
          <rect class="rv-cam__roi-bg" v-bind="rect"/>
          <rect class="rv-cam__roi-line" v-bind="rect"/>
        </svg>
        <div v-if="noSignal" class="rv-cam__nosig"><rv-status kind="warn" label="신호 없음" /></div>
        <div class="rv-cam__over"><slot /></div>
      </div>`,
  };

  /* ---------------- rv-state ---------------- */
  const RvState = {
    name: 'RvState',
    components: { RvStatus },
    props: {
      state: { type: String, required: true },
      message: String,
      hint: String,
      retryLabel: { type: String, default: '다시 시도' },
    },
    emits: ['retry'],
    template: `
      <div class="rv-state" :class="'rv-state--' + state" :role="state === 'error' ? 'alert' : (state === 'loading' ? 'status' : null)">
        <template v-if="state === 'loading'">{{ message || '불러오는 중…' }}</template>
        <template v-else-if="state === 'error'">
          <rv-status kind="warn" :label="message || '불러오지 못했습니다'" />
          <button type="button" class="rv-link" @click="$emit('retry')">{{ retryLabel }}</button>
        </template>
        <template v-else>
          <p>{{ message }}</p>
          <p v-if="hint" class="rv-state__hint">{{ hint }}</p>
          <slot />
        </template>
      </div>`,
  };

  const components = {
    'rv-status': RvStatus,
    'rv-gauge': RvGauge,
    'rv-meter': RvMeter,
    'rv-seg': RvSeg,
    'rv-switch': RvSwitch,
    'rv-menu': RvMenu,
    'rv-dialog': RvDialog,
    'rv-cam-frame': RvCamFrame,
    'rv-state': RvState,
  };

  RVS.ui = {
    components,
    STATUS_WORD,
    install(app) {
      Object.entries(components).forEach(([name, def]) => app.component(name, def));
    },
  };
})();
