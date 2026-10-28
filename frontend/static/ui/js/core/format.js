/* =============================================================
   표시용 포맷터 — RVS.fmt
   값이 없으면(null/undefined/NaN) 0 이 아니라 '—' 를 돌려준다. 0 으로 찍으면
   '측정했더니 0' 으로 읽혀 정상처럼 보인다.

   짧은 이름과 fmt 접두어 이름을 둘 다 내보낸다(fmt.deg === fmt.fmtDeg).

   deg(v)            12.3 → '+12.3°', -90 → '−90.0°'(진짜 마이너스 기호), 0 → '0.0°'
   num(v[, digits])  digits 를 안 주면 레거시 규칙: 0.01 미만이면 4자리, 아니면 3자리
   pct(v[, digits])  0.412 → '41.2%'  (v 는 0~1 비율, digits 기본 1)
   time(sec)         경과 시간 → '03:07', 1시간 이상은 '1:03:07'
   clock(ms)         epoch 밀리초 → 로컬 '15:03:21'
   dateTime(v)       epoch 초·밀리초 또는 ISO 문자열 → '10/28 13:52' (두 번째 인자 {seconds:true} 면 초까지)
   ago(v)            '방금 전' / '3분 전' / '2시간 전' / '16일 전'
   camName(x)        카메라 라벨의 ' 캠' 을 뗀다: 'C2 하단 대각 캠 1' → 'C2 하단 대각 1'. x 는 문자열 또는 { label, name }
   jointName(i)      0 부터 센 관절 번호 → '관절 1' … '관절 5', 6번째는 '관절 6 · 그리퍼'. 관절을 이름으로 보이는 화면은 모두 이걸 쓴다
   FPS_LOW_RATIO     0.7. 실측 fps 가 목표의 이 비율 미만이면 '프레임 낮음'(개요·장비 제어·진단 공통, 지시계의 정상 음영 하한도 같은 값)
   scanNotes(stats)  /api/scan/* 의 mesh.stats → 사람이 읽는 한 줄
   errText(err)      Error → 화면에 보여 줄 문장. 404 면 '서버에 이 기능이 아직 없습니다' 안내
   codeSpans(text)   '`a` b' → [{ text:'a', code:true }, { text:' b', code:false }] (v-html 없이 코드 서식을 낼 때)
   ko                한글 라벨 맵: ko.label(good/defect), ko.defect, ko.rec(녹화 상태), ko.arm, ko.severity
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});

  const MINUS = '−';
  const blank = (v) => v === null || v === undefined || Number.isNaN(Number(v));
  const pad2 = (n) => String(n).padStart(2, '0');

  function deg(value) {
    if (blank(value)) return '—';
    const n = Number(value);
    const s = Math.abs(n).toFixed(1);
    // -0.04 → '−0.0°' 가 되지 않게, 반올림 결과가 0 이면 부호를 뗀다
    return (Number(s) === 0 ? '' : n > 0 ? '+' : MINUS) + s + '°';
  }

  function num(value, digits) {
    if (blank(value)) return '—';
    const n = Number(value);
    return n.toFixed(digits ?? (n !== 0 && Math.abs(n) < 0.01 ? 4 : 3));
  }

  function pct(value, digits = 1) {
    if (blank(value)) return '—';
    return `${(Number(value) * 100).toFixed(digits)}%`;
  }

  function time(sec) {
    if (blank(sec)) return '—';
    const t = Math.max(0, Math.floor(Number(sec)));
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const s = t % 60;
    return h ? `${h}:${pad2(m)}:${pad2(s)}` : `${pad2(m)}:${pad2(s)}`;
  }

  function toDate(v) {
    if (v instanceof Date) return v;
    if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v);   // 서버는 epoch 초, JS 는 밀리초
    return new Date(v);
  }

  function clock(ms) {
    if (blank(ms) && !(ms instanceof Date)) return '—';
    const d = toDate(ms);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function dateTime(value, opts = {}) {
    if (value === null || value === undefined || value === '') return '—';
    const d = toDate(value);
    if (Number.isNaN(d.getTime())) return '—';
    const base = `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    return opts.seconds ? `${base}:${pad2(d.getSeconds())}` : base;
  }

  function ago(value) {
    if (value === null || value === undefined || value === '') return '—';
    const sec = (Date.now() - toDate(value).getTime()) / 1000;
    if (Number.isNaN(sec)) return '—';
    if (sec < 60) return '방금 전';
    if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
    return `${Math.floor(sec / 86400)}일 전`;
  }

  // config.yaml 의 label 은 'C2 하단 대각 캠 1'. 화면에서는 ' 캠' 을 뗀 'C2 하단 대각 1' 으로 쓴다(config 는 건드리지 않는다).
  function camName(x) {
    const raw = typeof x === 'string' ? x : (x && (x.label || x.name)) || '';
    return String(raw).replace(/ 캠/g, '');
  }

  // 6번째 관절(config.yaml 의 관절 ID 6)이 그리퍼다. 관절 수가 5개뿐인 목록(학습 데모)에서도 번호가 어긋나지 않게 끝이 아니라 번호로 판단한다.
  const jointName = (i) => (i === 5 ? '관절 6 · 그리퍼' : `관절 ${i + 1}`);

  // 레거시 index.html 의 경고 기준(목표의 70% 미만)을 세 화면이 같이 쓰도록 한 곳에 둔다. ponytail: 고정 비율, 현장 기준이 생기면 config 로.
  const FPS_LOW_RATIO = 0.7;

  /* 재구성 stats → 사람이 읽을 한 줄. 뺀 뷰와 주황을 못 칠한 이유는 꼭 말한다 —
     조용히 넘기면 '주황 없음 = 양품'으로 읽힌다. (레거시 app.js:996 이식) */
  function scanNotes(s) {
    if (!s) return '';
    const parts = [];
    const dropped = Object.keys(s.dropped_views || {});
    if (dropped.length) parts.push(`${dropped.join(', ')} 제외(다른 카메라와 형상 불일치)`);
    if (s.anomaly?.skipped) parts.push(s.anomaly.skipped);
    else if (s.anomaly) parts.push(`표면 이상 ${(s.anomaly.flagged_ratio * 100).toFixed(1)}%`);
    if (s.size_mm) parts.push(`외접 ${s.size_mm.map((v) => v.toFixed(1)).join(' × ')} mm`);
    return parts.join(' · ') || `${s.num_vertices} vertices`;
  }

  function errText(err) {
    if (!err) return '';
    // 서버를 옛 코드로 띄워 둔 채 새 화면을 열면 404 가 난다. 원인을 짚어 준다.
    if (err.status === 404) return '서버에 이 기능이 아직 없습니다(404). 서버를 새 코드로 다시 시작했는지 확인하세요.';
    return err.message || String(err);
  }

  function codeSpans(text) {
    return String(text ?? '').split('`').map((t, i) => ({ text: t, code: i % 2 === 1 })).filter((s) => s.text !== '');
  }

  const ko = {
    label: { good: '양품', defect: '불량' },
    defect: { scratch: '스크래치', dent: '찍힘', discoloration: '변색', crack: '균열', other: '기타' },
    rec: { IDLE: '대기', RECORDING: '녹화 중', SAVING: '저장 중' },
    arm: { leader: '리더', follower: '팔로워' },
    severity: { fault: '고장', warn: '주의', info: '정보' },
  };

  RVS.fmt = {
    deg, num, pct, time, clock, dateTime, ago, camName, jointName, FPS_LOW_RATIO, scanNotes, errText, codeSpans, ko,
    fmtDeg: deg, fmtNum: num, fmtPct: pct, fmtTime: time, fmtClock: clock, fmtDateTime: dateTime, fmtAgo: ago,
  };
})();
