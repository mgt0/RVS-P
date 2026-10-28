/* =============================================================
   API 클라이언트 — 레거시 app.js:21-75 를 옮겨 왔다.
   서버 에러 바디({ ok:false, error:{ code, message } })를 Error 로 정규화해
   호출부가 성공 경로만 신경 쓰게 한다. 실패하면 Error(message) 를 throw 한다.

   레거시와 다른 점은 셋이다(기존 호출부는 영향 없음). 하나, Error 에 속성을 덧붙였다.
     err.status   HTTP 상태. 서버에 닿지 못했거나 시간 초과면 0
     err.code     서버가 준 error.code (없으면 undefined)
     err.network  서버에 닿지 못했을 때 true
     err.timeout  시간 초과일 때 true

   둘, 아래 keepBody 옵션. 셋, 아래 timeout 옵션.

   RVS.api.get(path) / post(path, body) / patch(path, body) / del(path) / upload(path, file)
   get/post/patch/del 의 마지막 인자로 { keepBody: true } 를 주면, 서버가 JSON 본문을 보낸 응답은
   ok:false 이거나 4xx 여도 throw 하지 않고 본문을 그대로 돌려준다(예: /api/ports/test 는 일부 장치가
   실패하면 ok:false + results[] 를 준다). 서버에 못 닿았거나 JSON 이 아니면 그대로 throw.
   같은 자리에 { timeout: 밀리초 } 를 주면 그 요청만 기본 8초 대신 그 시간까지 기다린다(홈 복귀처럼 오래
   걸리는 동작용). 예: api.post('/api/arm/home', null, { timeout: 20000 })
   ============================================================= */
(function () {
  const RVS = (window.RVS = window.RVS || {});

  /* 요청 하나가(응답 본문을 다 읽을 때까지) 이 시간을 넘기면 실패로 친다.
     브라우저 기본 타임아웃은 수 분이라, 그 사이 요청을 기다리는 화면은 통째로
     굳는다(버튼이 눌린 채 안 돌아온다). 서버가 죽었든, 연결이 모자라 큐에
     걸렸든, 원인이 무엇이든 여기서 끊고 사용자에게 이유를 보여주는 편이 낫다.
     정상 명령은 실측 16~62ms 라 8초면 충분히 넉넉하다. 오래 걸리는 동작은 호출부가 timeout 옵션으로 늘린다. */
  const REQUEST_TIMEOUT_MS = 8000;

  const api = {
    async request(path, options = {}) {
      const { keepBody, timeout = REQUEST_TIMEOUT_MS, ...fetchOpts } = options;
      options = fetchOpts;
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), timeout);
      const timedOut = () => Object.assign(new Error(
        `서버가 ${timeout / 1000}초 안에 응답하지 않았습니다 (${path}). ` +
        '서버가 떠 있는지, 두 개가 동시에 떠 있지 않은지 확인하세요.'
      ), { status: 0, network: true, timeout: true });
      // 타이머는 본문을 다 읽은 뒤에 끈다. 헤더만 오고 본문이 멈춘 응답에서 res.json() 이 영원히
      // 끝나지 않으면 호출부의 busy 가 풀리지 않는다.
      try {
        let res;
        try {
          res = await fetch(path, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
            signal: abort.signal,
            body: options.body ? JSON.stringify(options.body) : undefined,
          });
        } catch (err) {
          if (err.name === 'AbortError') throw timedOut();
          throw Object.assign(new Error(`서버에 연결할 수 없습니다 (${path}): ${err.message}`), { status: 0, network: true });
        }

        let data = null;
        try {
          data = await res.json();
        } catch (err) {
          if (err.name === 'AbortError') throw timedOut();
          throw Object.assign(new Error(`서버 응답을 해석할 수 없습니다 (HTTP ${res.status})`), { status: res.status });
        }

        if (keepBody && data && typeof data === 'object') return data;
        if (!res.ok || data.ok === false) {
          throw Object.assign(new Error(data?.error?.message || `요청 실패 (HTTP ${res.status})`), {
            status: res.status,
            code: data?.error?.code,
          });
        }
        return data;
      } finally {
        clearTimeout(timer);
      }
    },
    get(path, opt) { return this.request(path, { ...opt }); },
    post(path, body, opt) { return this.request(path, { ...opt, method: 'POST', body: body || {} }); },
    patch(path, body, opt) { return this.request(path, { ...opt, method: 'PATCH', body: body || {} }); },
    del(path, opt) { return this.request(path, { ...opt, method: 'DELETE' }); },

    /* 파일 업로드는 request() 를 타지 않는다. FormData 를 JSON.stringify 하면
       "[object FormData]" 가 되고, Content-Type 을 직접 지정하면 브라우저가
       multipart 경계 문자열을 못 붙여 서버가 파일을 못 찾는다. 둘 다 비워야 한다. */
    async upload(path, file) {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch(path, { method: 'POST', body: form });
      const data = await res.json().catch(() => null);
      if (!res.ok || data?.ok === false) {
        throw Object.assign(new Error(data?.error?.message || `업로드 실패 (HTTP ${res.status})`), {
          status: res.status,
          code: data?.error?.code,
        });
      }
      return data;
    },
  };

  RVS.api = api;
})();
