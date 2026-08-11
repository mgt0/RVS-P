# OMX SCAN CONTROL

ROBOTIS OMX-L(리더) / OMX-F(팔로워) 텔레오퍼레이션 + 5축 카메라 스캔 데이터 수집 대시보드.
한 번의 시연으로 **LeRobot ACT 학습용 시퀀스**와 **양품/불량 CNN 분류용 정지 이미지**를 동시에 수집합니다.

---

## 빠른 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) 하드웨어 없이 UI/파이프라인부터 확인 (config.yaml 의 simulate: true)
python run.py
# → http://localhost:5000

# 2) 실기 연결 전, 포트를 한 번만 확정한다
python scripts/find_ports.py
#   출력된 serial_number 를 config.yaml 의 arms.*.serial_number 에 기입

# 3) 실기 실행
OMX_SIMULATE=0 python run.py
```

데모/수집 시에는 개발 서버 대신:

```bash
./scripts/serve.sh
```

---

## 왜 이렇게 만들었는가

### 포트 식별 — 3단계 폴백

`/dev/ttyACM*` 번호는 USB 열거 순서로 부여됩니다. 재부팅하거나 케이블을 다시 꽂으면
리더와 팔로워가 **뒤바뀝니다.** 이 상태로 텔레오퍼레이션을 켜면 팔로워가 리더 역할을 하며
하드웨어가 손상될 수 있습니다.

| 순위 | 방법 | 비고 |
|---|---|---|
| 1 | `serial_number` 매칭 | 가장 견고. 한 번 설정하면 영구히 안정 |
| 2 | 언플러그 diff | `lerobot-find-port` 와 동일. 대시보드의 `IDENTIFY` 버튼 |
| 3 | `port_hint` 수동 지정 | 최후 수단 |

실패 시 예외에 `role` 이 실려 나가므로, 대시보드가 **어느 arm 인지 명시**해 보여줍니다.

### 카메라 동기화 — 왜 "최신 프레임"이 아닌가

카메라 5대를 각각 스레드로 캡처하면 위상이 제각각입니다. 각 카메라의 *최신* 프레임을 그냥
모으면, 샘플링 주기와 캡처 주기가 같을 때(둘 다 30Hz) 경계에서 어떤 카메라는 새 프레임을,
어떤 카메라는 직전 프레임을 들고 있어 **한 주기(33ms)만큼 어긋납니다**(엘리어싱).

해결: 카메라마다 8프레임 링버퍼를 두고,
1. 각 카메라 최신 타임스탬프의 **중앙값**을 공통 기준시각으로 잡고
   (평균이 아닌 이유: 한 대가 멈춰도 기준이 끌려가지 않게)
2. 각 링버퍼에서 기준시각 **최근접** 프레임을 선택합니다.

측정 결과 (시뮬레이션 30Hz × 5대):

| | 개선 전 | 개선 후 |
|---|---|---|
| skew 중앙값 | 32ms | **7.2ms** |
| 15ms 초과 프레임 | 76% | **3.6%** |

남은 skew 는 숨기지 않고 프레임별로 `joint_states.parquet` 에 기록합니다.
학습 단계에서 임계값 초과 구간을 필터링할 수 있어야 하기 때문입니다.

### 안전 제한

팔로워에 쓰기 전 **항상** 두 단계로 클램프합니다.

1. 절대 소프트리밋 (`joint_limits`) — 하드웨어 리밋보다 안쪽
2. 스텝당 상대 변화량 (`max_relative_target`) — 리더가 튀어도 팔로워는 서서히 추종

홈 복귀는 목표값을 한 번에 쓰지 않고 `homing_speed_deg_per_s` 로 보간해 보냅니다.
한 번에 쓰면 모터가 최대 속도로 튑니다. 서버 종료 시 `atexit` 에서 홈 복귀 → 토크 해제 →
포트 닫기 순으로 정리하며, 홈 복귀가 실패해도 토크 해제는 `finally` 로 보장합니다.

### 저장 — 두 소비자, 두 Writer

ACT(시퀀스)와 CNN(단일 스냅샷)은 요구가 완전히 다릅니다. 한 클래스에 섞으면 조건 분기가
폭발하므로 `DatasetWriter` 인터페이스 뒤에 분리했습니다(Strategy).

CNN 쪽은 에피소드 전체가 아니라 **대표 한 세트**만 남깁니다. 시퀀스 프레임을 전부 분류
데이터로 쓰면 거의 동일한 이미지가 수천 장 들어가 과적합과 train/val 누수를 부릅니다.

중단된 에피소드는 `abort()` 에서 디렉터리째 삭제합니다. 반만 남은 에피소드는 학습 품질을
조용히 갉아먹는 최악의 데이터입니다.

---

## 데이터 구조

```
lerobot_data/
├── episodes/
│   └── episode_000042/
│       ├── camera_front/frame_000000.jpg …
│       ├── camera_back/  camera_left/  camera_right/  camera_top/
│       ├── joint_states.parquet
│       └── metadata.json
└── classification/
    ├── good/<object_id>/{front,back,left,right,top}.jpg + labels.json
    └── defect/<object_id>/{front,back,left,right,top}.jpg + labels.json
```

`joint_states.parquet` 컬럼:

| 컬럼 | 설명 |
|---|---|
| `frame_index` | 0부터 증가 |
| `timestamp` | 에피소드 시작 기준 경과 초 |
| `sync_skew_ms` | 해당 프레임 세트의 카메라 간 시간 편차 |
| `leader_joint_{0..4}` | 리더 관절 각도 (deg) |
| `follower_joint_{0..4}` | 팔로워 관절 각도 (deg) |
| `ts_{front..top}` | 카메라별 캡처 시각 (monotonic) |

`metadata.json` 의 `monotonic_offset` 을 더하면 monotonic → epoch 로 복원됩니다.
(monotonic 을 쓰는 이유: wall clock 은 NTP 보정으로 역행할 수 있어 프레임 정렬에 부적합)

---

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/meta` | 카메라 순서, 불량 유형, 다음 에피소드 ID |
| GET | `/api/status` | 녹화·arm·텔레오퍼·카메라 통합 상태 (메인 폴링) |
| GET | `/api/ports` | 포트 + 카메라 상태 스냅샷 |
| POST | `/api/ports/refresh` | 오버라이드 초기화 후 재스캔 |
| POST | `/api/ports/test` | 실제 open + ping 검증 |
| POST | `/api/ports/identify/{start,confirm}` | 언플러그 diff 2-phase |
| GET | `/api/stream/<name>?fps=&q=` | MJPEG 스트림 |
| GET | `/api/snapshot/<name>` | 단일 JPEG |
| POST | `/api/recording/{start,stop}` | 수집 제어 |
| POST | `/api/teleop/{start,stop}` | 텔레오퍼레이션 |
| POST | `/api/arm/home` | 홈 복귀 (녹화 중 거부) |
| POST | `/api/train/{start,stop}` | ACT 학습 제어 (demo/lerobot 모드) |
| GET | `/api/train/status` | 학습 상태 + 메트릭 시계열 (폴링) |
| GET | `/api/train/stream` | 학습 메트릭 SSE 실시간 스트림 |
| GET | `/api/scan/objects` | 재구성 가능한 스캔 물체 목록 |
| POST | `/api/scan/reconstruct` | 5뷰 → 3D 메시 (visual hull) |
| POST | `/api/scan/reconstruct_demo` | 데이터 없이 즉시 확인용 합성 재구성 |

에러 응답은 전부 동일한 형태입니다:

```json
{ "ok": false, "error": { "code": "arm_port_not_found",
  "message": "LEADER arm 포트를 찾지 못했습니다…", "detail": { "role": "leader" } } }
```

---

## ACT 학습 메트릭 (실시간)

ACT는 관절 각도를 예측하는 **회귀** 모델이라 분류의 "정확도"가 그대로 적용되지 않습니다.
그래서 세 지표로 수치화합니다.

| 지표 | 의미 | 왜 필요한가 |
|---|---|---|
| **L1 Loss** | 예측 오차의 절댓값 평균 | 최적화가 실제로 진행되는지 |
| **관절별 MAE (deg)** | 관절마다 평균 몇 도 틀리는지 | 어느 관절이 학습이 안 되는지 진단 |
| **Success@τ** | 모든 관절이 τ도 이내인 타임스텝 비율 | 분류의 "정확도"에 대응 — "이 정도면 집을 수 있나" |

Success@τ는 **모든 관절이 동시에** 임계값 이내일 때만 성공으로 셉니다. 관절 하나라도 크게
틀리면 그 동작은 실패라는 게 현실적이기 때문입니다.

**DEMO 모드**는 GPU·데이터셋 없이 실제 학습 곡선(loss 지수 감소 + success 상승)을 재현해
대시보드를 즉시 확인할 수 있습니다. 이때도 값은 진짜 `compute_metrics()` 를 통과하므로
차트 로직이 실제와 동일하게 검증됩니다. **LEROBOT 모드**는 실제 lerobot ACT 학습에
연결하는 지점(`act_trainer.py`의 `LeRobotTrainer.run`)만 환경의 lerobot 버전에 맞춰
교체하면 됩니다.

실시간 전송은 **SSE**(Server-Sent Events)를 씁니다. 폴링은 간격만큼 늘 뒤처지지만, SSE는
에폭이 끝나는 즉시 차트가 자랍니다.

## 실제 LeRobot ACT 학습 · rollout (신규, Windows 검증용)

기존 `DEMO` 모드(가짜 학습곡선)와 별개로, 실제 `lerobot` 패키지로 학습하고
그 결과로 팔로워를 자율 제어(rollout)하는 경로를 추가했다. 관련 코드는
`backend/core/lerobot_bridge.py`(변환+학습세션+추론)와 `backend/core/rollout.py`
(자율 제어 루프) 두 파일에 격리돼 있다 — 이 두 파일이 없어도(=lerobot 미설치)
나머지 대시보드는 그대로 기동된다.

### 설치

```bash
pip install -r requirements.txt              # 코어 (전원 필수)
pip install -r requirements-lerobot.txt       # 학습/rollout 담당자만 추가 설치
```

Windows에서 자주 걸리는 지점(GitHub 이슈 번호 기준):

| 증상 | 원인/해결 |
|---|---|
| GPU 있는데 CPU 전용 torch가 깔림 | `uv sync` 대신 [pytorch.org](https://pytorch.org/get-started/locally/)에서 CUDA 버전 맞는 인덱스로 먼저 설치 ([lerobot#4093](https://github.com/huggingface/lerobot/issues/4093)) |
| 카메라가 인식 안 됨 | `config.yaml`의 `camera.device`를 정수 인덱스 대신 실제 경로/이름으로 ([lerobot#1456](https://github.com/huggingface/lerobot/issues/1456)) |
| `usb-port` 설정 오류 | COM 포트 문자열 표기 재확인 ([lerobot#1094](https://github.com/huggingface/lerobot/issues/1094)) — omx-factory-system 자체 텔레옵이 이미 같은 포트로 동작 중이라면 그 값 그대로 쓰면 된다 |

### 절차 (teleop → 녹화는 이미 되는 상태에서 이어서)

1. **에피소드 녹화** — 지금 있는 대시보드 그대로: 텔레옵 켜고 `/api/recording/start`
   (label=`good`)로 정상 파지 시연을 여러 번 녹화한다. 소규모 검증이면 10~20개면 충분.
2. **데이터셋 변환** (선택 — 학습 시작 시 자동으로도 됨, 미리 확인하고 싶으면):
   ```bash
   python scripts/convert_dataset.py --output lerobot_data/lerobot_datasets/local__omx_pick_and_place
   ```
3. **학습** — 대시보드의 `ACT 학습·실시간` 패널에서 `demo=false`로 시작하거나:
   ```bash
   python scripts/train.py --epochs 30 --lerobot
   ```
   진행 상황(loss/MAE/success@τ)은 DEMO 모드와 동일한 SSE 스트림·차트로 그대로 보인다.
4. **Rollout** — 학습이 끝나면 체크포인트가 `lerobot_data/checkpoints/<repo_id>/`에 저장된다.
   ```
   POST /api/rollout/start   {}                         ← 최신 체크포인트 자동 사용
   POST /api/rollout/start   {"checkpoint": "<경로>"}    ← 특정 체크포인트 지정
   POST /api/rollout/stop
   GET  /api/rollout/status
   ```
   teleop과 rollout은 둘 다 팔로워를 직접 제어하므로 **상호 배타적**이다 — 하나가
   실행 중이면 다른 하나는 시작을 거부한다(안전을 위해 `SafetyGuard` 클램프도
   teleop과 동일하게 적용됨).

### 주의

- 자체 저장 포맷(`lerobot_data/episodes/`)은 공식 `LeRobotDataset` 스키마가
  아니다. 변환 단계(`convert_episodes_to_dataset`)가 그 간극을 메운다 — 원본
  에피소드 폴더는 그대로 두고, 변환 결과만 별도 폴더(`lerobot_datasets/`,
  git 추적 제외)에 만든다.
- `lerobot`의 Python API는 버전마다 모듈 경로가 바뀐다. `lerobot_bridge.py`의
  `_import_lerobot_modules()`가 신/구 경로를 순서대로 시도하지만, 그래도 안 맞으면
  `python -c "from lerobot.datasets.lerobot_dataset import LeRobotDataset; help(LeRobotDataset.create)"`
  로 실제 시그니처를 먼저 확인할 것.
- `lerobot_bridge.quick_eval()`이 계산하는 val 지표는 held-out validation
  split이 아니라 매 에폭 마지막 학습 배치로 추정한 값이다(대략적 추이 확인용).
  엄밀한 검증이 필요하면 별도 val set을 분리하도록 바꿀 것.

## 3D 스캔 검사

5뷰 스캔 이미지에서 3D 형상을 만들어 마우스로 돌려가며 검사합니다.

**왜 포토그래메트리 대신 Visual Hull인가.** front/back/left/right/top 5장은 90도씩 벌어져
있어 COLMAP 같은 SfM의 특징점 매칭이 거의 실패합니다(보통 겹침 큰 수십~수백 장 필요).
대신 **실루엣 기반 space carving**을 씁니다:

1. 각 뷰에서 물체 실루엣(전경 마스크)을 뽑고
2. 3D 복셀 격자의 각 복셀을 5개 뷰로 투영해
3. **모든 뷰에서 실루엣 안**인 복셀만 남깁니다 → 물체의 볼록 근사 형상

오목한 안쪽(구멍)은 복원 못 하지만, 형상 확인·불량 위치 표시에는 충분하고 무엇보다
**5뷰만으로 항상 동작**합니다. 복셀은 marching cubes로 표면 메시가 됩니다
(scikit-image 없으면 복셀 큐브 폴백).

**검사 표시.** CNN 판정 결과(good/defect + 신뢰도)를 KPI로 보여주고, 불량 부위는 메시
표면에 **선셋 오렌지 정점 색상**으로 오버레이합니다. 별도 마커 없이 표면에서 바로 보입니다.
현재 불량 위치는 데모값이며, 실제로는 CNN Grad-CAM이나 이상탐지 히트맵의 3D 역투영
위치를 `defect_regions`로 넘기면 됩니다.

**뷰어 조작**: 드래그=회전, 스크롤=확대. three.js r128 기반이며, 의존성을 줄이려
OrbitControls 대신 최소 궤도 컨트롤을 직접 구현했습니다.

> **데이터 없이 바로 확인**: 3D SCAN INSPECTION 섹션의 **DEMO** 버튼을 누르면 카메라·물체
> 없이 합성 형상 + 오렌지 하이라이트를 확인할 수 있습니다.

---

## 알려진 제약

**브라우저 동시 연결 한도.** HTTP/1.1 은 호스트당 6개 연결로 제한됩니다(MDN).
MJPEG 스트림 5개가 5개를 점유하면 폴링 API 가 굶습니다. 프리뷰를 12fps 로 낮춰
완화했지만, 더 필요하면 HTTP/2 리버스 프록시를 앞에 두거나 스트림을 별도 호스트명으로
분리하세요.

**Flask 개발 서버.** 스트림은 연결당 워커를 점유합니다. `use_reloader=False` 는 필수입니다
(리로더가 프로세스를 두 번 띄워 카메라를 두 번 열고 장치를 잠급니다). 실사용은
`scripts/serve.sh` 의 gunicorn + gevent 구성을 쓰세요.

**소프트웨어 동기화.** 하드웨어 트리거가 아니므로 skew 를 0으로 만들 수는 없습니다.
정밀도가 더 필요하면 외부 트리거를 지원하는 산업용 카메라가 필요합니다.

**USB 대역폭.** 5대를 한 호스트 컨트롤러에 물리면 640x480@30 도 MJPG 없이는 부족합니다.
`fourcc: MJPG` 를 유지하고, 가능하면 별도 PCIe USB 카드로 분산하세요.

---

## 문제 해결

| 증상 | 확인 |
|---|---|
| arm 이 계속 OFFLINE | `groups $USER` 에 `dialout` 이 있는지. 없으면 `sudo usermod -aG dialout $USER` 후 재로그인 |
| 카메라 NO SIGNAL | `v4l2-ctl --list-devices` 로 경로 확인. 다른 프로세스가 점유 중인지 `fuser /dev/video0` |
| fps 가 목표치의 절반 | USB 대역폭 부족. `fourcc: MJPG` 확인, 허브 분산 |
| skew 경고 다발 | `sample_rate_hz` 를 카메라 fps 보다 낮게 (예: 카메라 30 → 샘플링 20) |
| 관절 ID 응답 없음 | Dynamixel Wizard 로 ID/보드레이트 확인 |

---

## 테스트

```bash
pytest
```

하드웨어 없이 35개 테스트가 돕니다 — 안전 클램프, 경로 조작 방어, 상태 전이, 동기화 회귀,
ACT 회귀 지표(success@τ 경계), 3D 재구성, 불량 오버레이 검증.
