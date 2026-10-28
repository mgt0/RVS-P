/* =============================================================
   비상 정지 — Vue 와 CDN 에 기대지 않는 바닐라 JS.
   index.html 의 첫 스크립트다. unpkg 가 막히거나 Vue 가 죽어도 이 버튼은 눌려야 한다.

   - [data-rvs-estop] 를 단 모든 버튼(상단바 정적 버튼, rv-dialog 푸터 복제본)을 문서 단위
     위임으로 잡는다. <dialog> 가 열려 배경이 inert 여도 대화상자 안 버튼은 눌린다.
   - 확인창을 띄우지 않는다(망설임을 요구하는 UI 가 더 위험하다). 연속으로 눌러도 매번 보낸다.
   - 누른 즉시 'rvs:estop-start', 끝나면 window 에 'rvs:estop' 이벤트를 쏜다. detail = { ok, failed, error?, data?, at }
       ok      전부 정지했으면 true
       failed  일부만 실패한 장치 이름들(서버가 200 으로 돌려준 실패 항목)
       error   요청 자체가 실패했을 때의 사유(서버 무응답 등)
       at      Date.now()
     store.js 가 이 이벤트로 상태를 즉시 다시 읽고 토스트를 띄운다.
   - 화면 바로 옆에 결과를 보여 주려고 #rvs-estop-msg(aria-live) 에 짧은 문장을 쓴다.
   - 노출: window.RVS.estop() → Promise<detail>
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});

  // 정상 명령은 수십 ms. 8초(api.js)는 비상 정지에는 너무 길어 5초에서 끊고 다시 누르게 한다.
  const TIMEOUT_MS = 5000;
  const MSG_MS = 8000;

  let inflight = 0;
  let hideTimer = null;

  function say(text, kind) {
    const el = document.getElementById('rvs-estop-msg');
    if (!el) return;
    clearTimeout(hideTimer);
    el.textContent = text;
    el.dataset.kind = kind;
    // 실패 문구는 다음 입력까지 남긴다. 성공·진행 문구만 잠시 뒤 지운다.
    if (kind !== 'fail') hideTimer = setTimeout(() => { el.textContent = ''; }, MSG_MS);
  }

  function clock(ms) {
    const d = new Date(ms);
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
  }

  // hub.emergency_stop() 은 한 장치가 실패해도 200 으로 돌려주고 실패를 값에 담는다.
  // 그걸 '성공'으로 읽으면 안 멈춘 장치가 있는데 정지 완료로 보인다.
  function failedParts(data) {
    const failed = [];
    for (const key of ['autorun', 'teleop']) {
      if (data && data[key] && data[key].error) failed.push(key);
    }
    for (const [name, outcome] of Object.entries((data && data.conveyors) || {})) {
      if (String(outcome).includes('실패')) failed.push(name);
    }
    return failed;
  }

  async function estop() {
    // 요청이 나가는 순간을 알린다 — store 가 그 직후 몇 초 동안의 벨트 정지를 '비상 정지 탓'으로 보고 자동 촬영을 막는다
    window.dispatchEvent(new CustomEvent('rvs:estop-start'));
    inflight += 1;
    document.querySelectorAll('[data-rvs-estop]').forEach((b) => { b.dataset.state = 'busy'; });
    say('비상 정지 요청 중…', 'busy');

    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), TIMEOUT_MS);
    let detail;
    try {
      const res = await fetch('/api/emergency_stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        signal: abort.signal,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok || !data || data.ok === false) {
        throw new Error((data && data.error && data.error.message) || `HTTP ${res.status}`);
      }
      const failed = failedParts(data);
      detail = { ok: failed.length === 0, failed, data, at: Date.now() };
    } catch (err) {
      // fetch 가 서버에 못 닿으면 브라우저가 TypeError('Failed to fetch') 를 던진다
      const why = err.name === 'AbortError' ? `서버가 ${TIMEOUT_MS / 1000}초 안에 응답하지 않았습니다`
        : err instanceof TypeError ? '서버에 연결할 수 없습니다' : err.message;
      detail = { ok: false, failed: [], error: why, at: Date.now() };
    } finally {
      clearTimeout(timer);
      inflight -= 1;
      if (inflight === 0) document.querySelectorAll('[data-rvs-estop]').forEach((b) => { delete b.dataset.state; });
    }

    if (detail.error) say(`비상 정지 실패 — ${detail.error}. 다시 누르거나 장치 전원을 직접 차단하세요.`, 'fail');
    else if (!detail.ok) say(`일부 정지 실패(${detail.failed.join(', ')}) — 해당 장치를 직접 확인하세요.`, 'fail');
    else say(`비상 정지 완료 · ${clock(detail.at)}`, 'ok');

    window.dispatchEvent(new CustomEvent('rvs:estop', { detail }));
    return detail;
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('[data-rvs-estop]')) estop();
  });

  RVS.estop = estop;
})();
