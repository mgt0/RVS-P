/* =============================================================
   OMX SCAN CONTROL — 프론트엔드 로직
   Vue 3 global build (빌드 스텝 없음). 로봇 옆 노트북에서
   npm install 없이 바로 띄우는 것이 이 프로젝트의 제약이다.
   ============================================================= */

const { createApp, computed, onMounted, onUnmounted, reactive, ref } = Vue;

/* -------------------------------------------------------------
   API 클라이언트 — 네트워크 관심사를 컴포넌트에서 분리.
   서버 에러 바디({ error: { message } })를 Error 로 정규화해
   호출부가 성공 경로만 신경 쓰게 한다.
   ------------------------------------------------------------- */
const api = {
  async request(path, options = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });

    let data = null;
    try {
      data = await res.json();
    } catch {
      throw new Error(`서버 응답을 해석할 수 없습니다 (HTTP ${res.status})`);
    }

    if (!res.ok || data.ok === false) {
      throw new Error(data?.error?.message || `요청 실패 (HTTP ${res.status})`);
    }
    return data;
  },
  get(path) { return this.request(path); },
  post(path, body) { return this.request(path, { method: 'POST', body: body || {} }); },
};

createApp({
  setup() {
    /* ---------------- 상태 ---------------- */
    const ports = reactive({ arms: [], cameras: [], connected_count: 0, total_count: 7 });

    const status = reactive({
      recording: {
        state: 'IDLE', frame_count: 0, elapsed_sec: 0, max_skew_ms: 0,
        skew_violations: 0, total_episodes: 0, good_objects: 0, defect_objects: 0,
      },
      leader: { joints: [], connected: false },
      follower: { joints: [], connected: false },
      teleop: { running: false, fault: null },
      cameras: [],
    });

    const meta = reactive({
      cameras: ['front', 'back', 'left', 'right', 'top', 'global'],
      defect_types: ['scratch', 'dent', 'discoloration', 'crack', 'other'],
      sample_rate_hz: 30,
      max_relative_target: 5,
      simulate: false,
    });

    const form = reactive({ episodeId: '', objectId: '', label: 'good', defectType: null });
    const busy = reactive({
      ports: false, test: false, record: false, teleop: false, home: false,
      train: false, objects: false, recon: false,
    });
    const toasts = ref([]);

    /* ---- ACT 학습 메트릭 상태 (실시간) ---- */
    const lossChart = ref(null);      // <canvas> ref — 손실 곡선
    const successChart = ref(null);   // <canvas> ref — 성공률 곡선
    const training = reactive({
      params: { epochs: 50, threshold_deg: 5.0, lr: 0.0001, demo: true },
      state: 'IDLE',
      running: false,
      records: [],           // EpochRecord[]
      kpi: { trainLoss: 0, lossTrend: 0, valMae: 0, successRate: 0, epoch: 0 },
      jointMae: [],          // 관절별 오차 막대
      bestEpoch: null,
      bestValLoss: null,
    });

    /* ---- 3D 스캔 뷰어 상태 ---- */
    const viewerCanvas = ref(null);  // 뷰어 컨테이너 ref
    const scan = reactive({
      objects: [],
      selectedKey: null,     // "label/object_id" 형태
      currentObject: null,
      hasMesh: false,
      stats: { classification: {}, num_defect_regions: 0, num_vertices: 0, num_faces: 0, voxel_occupancy: 0, num_views: 0 },
    });
    let viewerInstance = null;   // Three.js 뷰어 핸들 (reactive 밖 — 프록시로 감싸면 안 됨)

    /* MJPEG 스트림 URL 은 한 번 정하면 바꾸지 않는다.
       reactive 값으로 만들면 렌더링마다 src 가 갱신돼 연결이 끊겼다 붙길 반복한다. */
    const streamUrls = reactive({});

    /* ---------------- 파생 값 ---------------- */
    const recording = computed(() => status.recording.state === 'RECORDING');
    const connectedCount = computed(() => ports.connected_count);
    const totalCount = computed(() => ports.total_count);
    const allConnected = computed(() => ports.connected_count === ports.total_count && ports.total_count > 0);

    const canStart = computed(() => {
      if (!form.episodeId.trim() || !form.objectId.trim()) return false;
      if (form.label === 'defect' && !form.defectType) return false;
      return true;
    });

    const cameraViews = computed(() =>
      status.cameras.map((cam) => ({ ...cam, streamUrl: streamUrls[cam.name] }))
    );

    /* ---------------- 토스트 ---------------- */
    let toastSeq = 0;
    function notify(message, kind = 'info') {
      const id = ++toastSeq;
      toasts.value.push({ id, message, kind });
      // 에러는 사용자가 읽을 시간을 더 준다.
      setTimeout(() => dismiss(id), kind === 'error' ? 9000 : 4000);
    }
    function dismiss(id) {
      toasts.value = toasts.value.filter((t) => t.id !== id);
    }

    /* ---------------- 액션 ---------------- */
    async function loadMeta() {
      try {
        const data = await api.get('/api/meta');
        Object.assign(meta, {
          cameras: data.cameras,
          defect_types: data.defect_types,
          sample_rate_hz: data.sample_rate_hz,
          max_relative_target: data.max_relative_target,
        });
        // 다음 에피소드 ID 를 미리 채워 오타와 중복을 줄인다.
        if (!form.episodeId) form.episodeId = data.next_episode_id;
      } catch (err) {
        notify(err.message, 'error');
      }
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

    function applyPorts(data) {
      ports.arms = data.arms;
      ports.cameras = data.cameras;
      ports.connected_count = data.connected_count;
      ports.total_count = data.total_count;
      meta.simulate = data.simulate;
      ensureStreamUrls(data.cameras);
    }

    function ensureStreamUrls(cameras) {
      cameras.forEach((cam) => {
        if (!streamUrls[cam.name]) {
          // 프리뷰는 12fps 로 충분하다. 브라우저의 호스트당 동시 연결 제한(6)을
          // 5개 스트림이 잡아먹으므로 대역폭과 연결 시간을 아낀다.
          streamUrls[cam.name] = `/api/stream/${cam.name}?fps=12&q=70`;
        }
      });
    }

    async function testConnections() {
      busy.test = true;
      try {
        const data = await api.post('/api/ports/test');
        const failed = data.results.filter((r) => !r.ok);
        if (failed.length === 0) {
          notify('모든 장치 통신 정상');
        } else {
          failed.forEach((r) => notify(`${r.role.toUpperCase()} — ${r.message}`, 'error'));
        }
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.test = false;
      }
    }

    async function identify(role) {
      try {
        await api.post('/api/ports/identify/start', { role });
        // CLI 의 blocking input() 대신 confirm() 으로 2-phase 를 잇는다.
        const proceed = window.confirm(
          `${role.toUpperCase()} arm 의 USB 케이블을 지금 뽑으세요.\n뽑은 뒤 확인을 누르면 사라진 포트를 해당 arm 으로 확정합니다.`
        );
        if (!proceed) return;
        const data = await api.post('/api/ports/identify/confirm', { role });
        notify(`${role.toUpperCase()} = ${data.device} 로 확정했습니다. 케이블을 다시 연결하세요.`);
        await refreshPorts();
      } catch (err) {
        notify(err.message, 'error');
      }
    }

    async function toggleTeleop() {
      busy.teleop = true;
      try {
        const path = status.teleop.running ? '/api/teleop/stop' : '/api/teleop/start';
        const data = await api.post(path);
        status.teleop = data.teleop;
        notify(data.teleop.running ? '텔레오퍼레이션 시작' : '텔레오퍼레이션 정지');
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.teleop = false;
      }
    }

    async function goHome() {
      busy.home = true;
      try {
        await api.post('/api/arm/home');
        notify('홈 포지션 복귀 완료');
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.home = false;
      }
    }

    function setLabel(value) {
      form.label = value;
      // GOOD 으로 되돌릴 때 defectType 을 반드시 지운다.
      // 남겨두면 서버로는 안 가더라도 UI 가 이전 선택을 기억해 사용자를 헷갈리게 한다.
      if (value === 'good') form.defectType = null;
    }

    async function startRecording() {
      busy.record = true;
      try {
        const data = await api.post('/api/recording/start', {
          episode_id: form.episodeId.trim(),
          object_id: form.objectId.trim(),
          label: form.label,
          defect_type: form.defectType,
        });
        status.recording = data.recording;
        notify(`에피소드 ${form.episodeId} 녹화 시작`);
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.record = false;
      }
    }

    async function stopRecording() {
      busy.record = true;
      try {
        const data = await api.post('/api/recording/stop');
        status.recording = data.recording;
        notify(`에피소드 저장 완료 — ${data.recording.frame_count} 프레임`);
        // 다음 에피소드 ID 를 자동 증가시켜 연속 수집의 마찰을 없앤다.
        const next = await api.get('/api/meta');
        form.episodeId = next.next_episode_id;
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.record = false;
      }
    }

    /* ---------------- 폴링 ----------------
       녹화 중엔 프레임 카운터가 살아있어야 하므로 빠르게,
       유휴 시엔 느리게. 5개 MJPEG 스트림과 연결 슬롯을 다투므로
       불필요한 요청을 줄이는 것이 실제로 프리뷰 지연에 영향을 준다. */
    let statusTimer = null;
    let portsTimer = null;

    async function pollStatus() {
      try {
        const data = await api.get('/api/status');
        Object.assign(status, {
          recording: data.recording,
          leader: data.leader,
          follower: data.follower,
          teleop: data.teleop,
          cameras: data.cameras,
        });
        ensureStreamUrls(data.cameras);
      } catch {
        // 폴링 실패는 토스트를 띄우지 않는다. 서버 재시작 중에 화면이 알림으로 덮인다.
      }
      statusTimer = setTimeout(pollStatus, recording.value ? 500 : 1500);
    }

    async function pollPorts() {
      try {
        applyPorts(await api.get('/api/ports'));
      } catch { /* 무시 */ }
      portsTimer = setTimeout(pollPorts, 5000);
    }

    /* ---------------- 라이프사이클 ---------------- */
    function warnIfRecording(event) {
      if (!recording.value) return;
      event.preventDefault();
      event.returnValue = '';   // 녹화 중 새로고침 = 에피소드 유실
    }

    /* ==================== ACT 학습 메트릭 (SSE 실시간) ====================
       백엔드는 /api/train/stream 으로 에폭마다 전체 스냅샷을 밀어준다.
       폴링 대신 SSE 를 쓰는 이유: 에폭이 끝나는 즉시 차트가 자라야
       '학습이 살아있다'는 느낌이 난다. 폴링은 간격만큼 늘 뒤처진다. */
    let trainingSource = null;   // EventSource 핸들 (reactive 밖)

    async function startTraining() {
      if (training.running) return;
      busy.train = true;
      try {
        // 1) 학습 시작 요청. 서버가 백그라운드 스레드에서 학습을 돌린다.
        const data = await api.post('/api/train/start', {
          epochs: training.params.epochs,
          threshold_deg: training.params.threshold_deg,
          lr: training.params.lr,
          demo: training.params.demo,
          num_joints: 5,
        });
        applyTraining(data.training);
        notify(`학습 시작 — ${training.params.demo ? 'DEMO' : 'LEROBOT'} 모드`);
        // 2) 실시간 스트림 구독
        openTrainingStream();
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.train = false;
      }
    }

    async function stopTraining() {
      busy.train = true;
      try {
        const data = await api.post('/api/train/stop');
        applyTraining(data.training);
        notify('학습 정지 요청됨');
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.train = false;
      }
    }

    function openTrainingStream() {
      closeTrainingStream();
      // EventSource 는 GET 만 지원. 서버가 종료 상태를 보내면 스스로 닫는다.
      const src = new EventSource('/api/train/stream');
      trainingSource = src;

      src.onmessage = (event) => {
        try {
          applyTraining(JSON.parse(event.data));
        } catch { /* 부분 프레임 무시 */ }
      };

      src.onerror = () => {
        // 스트림이 끊기면(학습 종료 포함) 폴링으로 최종 상태를 한 번 확인한다.
        closeTrainingStream();
        if (training.running) {
          api.get('/api/train/status').then((d) => applyTraining(d.training)).catch(() => {});
        }
      };
    }

    function closeTrainingStream() {
      if (trainingSource) {
        trainingSource.close();
        trainingSource = null;
      }
    }

    /* 서버 스냅샷(records 시계열)을 UI 상태로 정규화한다.
       KPI/차트가 읽기 좋은 형태로 여기서 한 번만 가공한다. */
    function applyTraining(t) {
      if (!t) return;
      training.state = t.state || 'IDLE';
      training.running = t.state === 'RUNNING';
      training.records = t.records || [];
      training.bestEpoch = t.best_epoch ?? null;
      training.bestValLoss = t.best_val_loss ?? null;

      const recs = training.records;
      if (recs.length) {
        const last = recs[recs.length - 1];
        const prev = recs[recs.length - 2] || last;
        training.kpi = {
          trainLoss: last.train_loss ?? 0,
          lossTrend: (last.train_loss ?? 0) - (prev.train_loss ?? 0),
          valMae: last.val_mae_deg ?? 0,
          successRate: last.val_success_rate ?? 0,
          epoch: last.epoch ?? 0,
        };
        // 관절별 MAE 막대 — 마지막 에폭의 val_mae_per_joint
        const per = last.val_mae_per_joint || [];
        const maxMae = Math.max(...per, training.params.threshold_deg, 1);
        training.jointMae = per.map((v, i) => ({
          name: `joint_${i}`,
          value: v,
          pct: (v / maxMae) * 100,
        }));
      }

      // 차트는 v-if 로 지금 막 나타났을 수 있으므로 다음 틱에 그린다.
      Vue.nextTick(renderTrainingCharts);
    }

    function renderTrainingCharts() {
      const recs = training.records;
      if (!recs.length) return;

      const ink = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim();
      const sunset = getComputedStyle(document.documentElement).getPropertyValue('--accent-sunset').trim();

      // 손실 곡선: train(실선 흰색) vs val(점선 오렌지)
      if (lossChart.value) {
        Chart.drawLines(lossChart.value, [
          { points: recs.map((r) => ({ x: r.epoch, y: r.train_loss })), color: ink, dashed: false },
          { points: recs.map((r) => ({ x: r.epoch, y: r.val_loss ?? r.train_loss })), color: sunset, dashed: true },
        ]);
      }

      // 성공률 곡선 (0~1). 오렌지 실선 하나.
      if (successChart.value) {
        Chart.drawLines(successChart.value, [
          { points: recs.map((r) => ({ x: r.epoch, y: r.val_success_rate ?? 0 })), color: sunset, dashed: false },
        ]);
      }
    }

    /* ==================== 3D 스캔 재구성 ==================== */
    async function refreshObjects() {
      busy.objects = true;
      try {
        const data = await api.get('/api/scan/objects');
        scan.objects = data.objects;
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.objects = false;
      }
    }

    async function reconstruct() {
      if (!scan.selectedKey) return;
      const [label, objectId] = scan.selectedKey.split('/');
      busy.recon = true;
      try {
        const data = await api.post('/api/scan/reconstruct', {
          object_id: objectId,
          label,
          resolution: 48,
        });
        applyMesh(data.mesh, `${objectId} · ${label.toUpperCase()}`);
        notify(`3D 재구성 완료 — ${data.mesh.stats.num_vertices} vertices`);
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.recon = false;
      }
    }

    async function reconstructDemo() {
      busy.recon = true;
      try {
        // 데이터 없이도 뷰어·하이라이트를 확인. defect 로 요청해 오렌지 오버레이를 본다.
        const data = await api.post('/api/scan/reconstruct_demo', { label: 'defect', resolution: 48 });
        applyMesh(data.mesh, 'DEMO OBJECT · DEFECT');
        notify('데모 형상 생성 — 오렌지 영역이 불량 부위입니다');
      } catch (err) {
        notify(err.message, 'error');
      } finally {
        busy.recon = false;
      }
    }

    /* 메시를 뷰어에 싣고 stats 를 UI 로 반영. */
    function applyMesh(mesh, objectLabel) {
      scan.stats = mesh.stats;
      scan.currentObject = objectLabel;
      scan.hasMesh = true;
      // v-show 로 뷰어 컨테이너가 지금 보이게 됐으니 다음 틱에 초기화/로드.
      Vue.nextTick(() => {
        ensureViewer();
        if (viewerInstance) viewerInstance.loadMesh(mesh);
      });
    }

    function ensureViewer() {
      if (viewerInstance || !viewerCanvas.value) return;
      if (!window.THREE) {
        notify('3D 라이브러리(Three.js) 로드 실패 — 네트워크를 확인하세요', 'error');
        return;
      }
      viewerInstance = ScanViewer.create(viewerCanvas.value);
    }

    function resetView() {
      if (viewerInstance) viewerInstance.reset();
    }

    /* ==================== 포맷 유틸 ==================== */
    function fmtDeg(value) {
      const n = Number(value ?? 0);
      return `${n >= 0 ? '+' : ''}${n.toFixed(1)}°`;
    }
    function fmtNum(value) {
      const n = Number(value ?? 0);
      return n.toFixed(n !== 0 && Math.abs(n) < 0.01 ? 4 : 3);
    }
    function fmtPct(value) {
      return `${(Number(value ?? 0) * 100).toFixed(1)}%`;
    }

    /* ==================== 라이프사이클 ==================== */
    onMounted(async () => {
      await loadMeta();
      await pollPorts();
      await pollStatus();
      await refreshObjects();
      // 페이지 재진입 시 이미 학습이 돌고 있을 수 있으니 상태를 한 번 확인.
      try {
        const d = await api.get('/api/train/status');
        applyTraining(d.training);
        if (d.training.state === 'RUNNING') openTrainingStream();
      } catch { /* 무시 */ }

      window.addEventListener('beforeunload', warnIfRecording);
      window.addEventListener('resize', renderTrainingCharts);
    });

    onUnmounted(() => {
      clearTimeout(statusTimer);
      clearTimeout(portsTimer);
      closeTrainingStream();
      if (viewerInstance) viewerInstance.dispose();
      window.removeEventListener('beforeunload', warnIfRecording);
      window.removeEventListener('resize', renderTrainingCharts);
    });

    return {
      ports, status, meta, form, busy, toasts,
      recording, connectedCount, totalCount, allConnected, canStart, cameraViews,
      refreshPorts, testConnections, identify, toggleTeleop, goHome,
      setLabel, startRecording, stopRecording, dismiss, fmtDeg,
      // ACT 학습 메트릭
      training, lossChart, successChart, startTraining, stopTraining, fmtNum, fmtPct,
      // 3D 스캔 검사
      scan, viewerCanvas, refreshObjects, reconstruct, reconstructDemo, resetView,
    };
  },
}).mount('#app');
