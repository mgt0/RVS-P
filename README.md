# OMX SCAN CONTROL

[![한국어](https://img.shields.io/badge/lang-한국어-blue)](README.md)
[![English](https://img.shields.io/badge/lang-English-lightgrey)](README.en.md)
[![中文](https://img.shields.io/badge/lang-中文-lightgrey)](README.zh.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.12-blue.svg)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
![tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)
![last commit](https://img.shields.io/github/last-commit/mgt0/OMX-FACTORY-SYSTEM)
![repo size](https://img.shields.io/github/repo-size/mgt0/OMX-FACTORY-SYSTEM)
![issues](https://img.shields.io/github/issues/mgt0/OMX-FACTORY-SYSTEM)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

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

> **Windows**: 위 명령들은 Linux 기준입니다. 가상환경은 `.venv\Scripts\activate`,
> 환경변수는 `$env:OMX_SIMULATE="0"` 로 씁니다. `serve.sh` 의 gunicorn 은
> Windows 에서 동작하지 않으므로 `python run.py` 를 그대로 쓰세요.
> 포트 이름도 `/dev/ttyACM*` 대신 `COM*` 입니다(식별 전략은 동일).

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
│       ├── camera_cam1/frame_000000.jpg …
│       ├── camera_cam2/ … camera_cam5/
│       ├── joint_states.parquet
│       └── metadata.json
└── classification/
    ├── good/<object_id>/{cam1..cam5}.jpg + labels.json
    └── defect/<object_id>/{cam1..cam5}.jpg + labels.json
├── background/{cam1..cam5}.jpg   ← '배경 캡처' 가 저장하는 빈 스캔대 참조
└── models/<object_id>.stl        ← '기준 모델 등록' 이 저장하는 CAD
```

`joint_states.parquet` 컬럼:

| 컬럼 | 설명 |
|---|---|
| `frame_index` | 0부터 증가 |
| `timestamp` | 에피소드 시작 기준 경과 초 |
| `sync_skew_ms` | 해당 프레임 세트의 카메라 간 시간 편차 |
| `leader_joint_{0..4}` | 리더 관절 각도 (deg) |
| `follower_joint_{0..4}` | 팔로워 관절 각도 (deg) |
| `ts_{cam1..cam5}` | 카메라별 캡처 시각 (monotonic) |

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
| GET | `/api/episodes` | 저장된 에피소드 목록 (metadata.json) |
| PATCH | `/api/episodes/<id>` | 라벨·물체 ID 수정 (분류 폴더도 함께 이동) |
| DELETE | `/api/episodes/<id>` | 에피소드 삭제 (마지막 참조면 분류 이미지도 정리) |
| POST | `/api/emergency_stop` | 비상 정지 — 텔레오퍼·전 벨트·녹화 일괄 정지 |
| GET/POST | `/api/conveyor/status`, `/api/conveyor/<name>/{start,stop,speed,dir,release}` | 컨베이어(다중 벨트) 제어 |
| GET/POST | `/api/scan/model/<object_id>` | CAD 기준 모델(STL) 조회/업로드 |
| POST | `/api/scan/background` | 빈 스캔대 배경 캡처 (배경 차분용) |
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

## 실제 ACT 학습 — LeRobot 변환

이 저장소의 폴더 구조는 사람이 읽기 위한 것이라 LeRobot 표준 데이터셋이 아닙니다.
학습 전에 한 번 변환합니다.

```bash
python scripts/to_lerobot.py --repo-id omx/scan
lerobot-train --dataset.repo_id=omx/scan --policy.type=act
```

변환의 핵심은 관측/행동 대응입니다.

| LeRobot 필드 | 우리 컬럼 | 의미 |
|---|---|---|
| `observation.state` | `follower_joint_*` | 지금 로봇이 있는 자세 |
| `action` | `leader_joint_*` | 사람이 시킨 자세 |
| `observation.images.<cam>` | `camera_<cam>/frame_*.jpg` | 같은 타임스텝의 프레임 |

둘이 뒤바뀌면 정책은 "현재 자세 → 현재 자세"를 배우고, 손실은 예쁘게 떨어지지만
로봇은 아무것도 하지 않습니다. `tests/test_episodes.py` 가 이 대응을 고정합니다.

meta/*.jsonl·청킹·stats 집계는 직접 쓰지 않고 `LeRobotDataset.create/add_frame/
save_episode` 에 맡깁니다. 어차피 학습에 lerobot 이 필요하고, 손으로 쓴 포맷은
lerobot 버전이 오르면 조용히 어긋납니다.

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
차트 로직이 실제와 동일하게 검증됩니다. **LEROBOT 모드**를 선택하면 실제 학습은 별도
프로세스로 안내합니다 — `scripts/to_lerobot.py` 로 변환 후 `lerobot-train` CLI 를 쓰는
것이 정석입니다(위 "실제 ACT 학습" 절 참조). 카메라 5대와 시리얼 포트를 쥔 서버 안에서
GPU 학습을 돌릴 이유가 없습니다.

실시간 전송은 **SSE**(Server-Sent Events)를 씁니다. 폴링은 간격만큼 늘 뒤처지지만, SSE는
에폭이 끝나는 즉시 차트가 자랍니다.

---

## Rollout — 학습된 ACT 정책으로 자율 실행 (신규)

위 절차(`to_lerobot.py` → `lerobot-train`)로 학습이 끝나면, 그 체크포인트로
팔로워를 자율 제어할 수 있다. 학습(`lerobot-train`)은 의도적으로 이 서버
프로세스 밖에서 돈다 — 카메라 5대와 시리얼 포트를 쥔 채로 GPU를 몇 시간
점유할 수는 없기 때문이다(`backend/core/act_trainer.py`의 `LeRobotTrainer`
참고). 반대로 **rollout(추론)은 이 프로세스 안에서** 돈다 — 팔로워/카메라를
이미 `HardwareHub`가 쥐고 있어서다. 관련 코드는 `backend/core/lerobot_bridge.py`
(`ActInferenceRunner`, 추론 전용)와 `backend/core/rollout.py`(`AutoRunLoop`,
`TeleopLoop`과 대칭 구조) — 이 두 파일이 없어도(=lerobot 미설치) 나머지
대시보드는 그대로 기동된다.

```bash
pip install -r requirements.txt -r requirements-lerobot.txt   # rollout 담당자만
```

```
POST /api/rollout/start   {"checkpoint": "outputs/train/<job>/checkpoints/<step>/pretrained_model"}
POST /api/rollout/start   {"waypoints": "lerobot_data/waypoints/episode_000001.json", "speed": 1.0}
POST /api/rollout/stop
GET  /api/rollout/status
```

**웨이포인트 재생 (학습 없이)** — 같은 동작을 반복하기만 하면 되는 작업이면 ACT
학습·GPU가 필요 없다. 녹화한 에피소드를 각도 목록으로 뽑아 그대로 되돌린다:

```bash
python scripts/to_waypoints.py --episode 000001          # --tol 로 성김 조절(기본 1.5도)
```

출력은 사람이 읽고 텍스트 편집기로 고칠 수 있는 JSON이고, 재생은 결정적이다.
`WaypointRunner`(`backend/core/waypoints.py`)가 `ActInferenceRunner` 와 같은
`predict()` 시그니처라 `AutoRunLoop`(watchdog · SafetyGuard · 정지 경로)를 그대로
쓴다 — 재생 전용 제어 루프는 없다. 카메라를 보지 않으며(`needs_frames = False`),
궤적 끝에 닿으면 스스로 멈춘다. `/api/rollout/status` 에 `progress_sec`/`total_sec`
가 붙는다. 팔이 목표에서 5도 이상 뒤처지면 재생 시계를 멈춰(도착 대기) 첫
웨이포인트가 멀 때 궤적을 통째로 건너뛰는 걸 막는다.

- `checkpoint` 또는 `waypoints` 중 하나는 필수다. `checkpoint` 는 학습이 별도 프로세스라 이 서버는 어떤 체크포인트가
  최신인지 알 방법이 없다 — `lerobot-train` 로그 마지막 줄에 찍힌 경로를 그대로 쓴다.
- teleop과 rollout은 둘 다 `follower.write()`를 호출하는 루프라 **상호 배타적**이다.
  하나가 실행 중이면 다른 하나는 시작을 거부한다. `SafetyGuard`(소프트리밋 +
  상대변화량 클램프)는 teleop과 완전히 동일하게 rollout에도 적용된다.
- `frames`로 넘기는 카메라 이름은 `CAMERA_ORDER`(`cam1`~`cam5`)와 일치해야
  한다 — 학습 때 못 본 카메라 이름을 주면 정책이 조용히 무시하거나 에러를 낸다.

### Windows에서 실제로 겪은 문제 4가지 (전부 재현·해결함)

teleop → 녹화 → `to_lerobot.py` 변환 → `lerobot-train` → rollout까지 이 저장소를
Windows에서 실물 하드웨어로 끝까지 돌려서 검증하는 과정에서 아래 4가지를 실제로
만났다. lerobot/torch 버전이 바뀌면 다시 겪을 수 있으니 진단 순서를 남겨둔다.

1. **torch DLL 초기화 실패** (`WinError 1114`, `c10.dll` 로드 실패)
   cv2/pandas가 torch보다 먼저 로드되면 동명의 런타임 DLL(OpenMP 계열)을 먼저
   점유해 torch 쪽 DLL 초기화가 깨진다. `run.py`와 `scripts/to_lerobot.py` 맨
   위에 아무 것도 안 하는 `import torch`(실패해도 무시)를 넣어 슬롯을 선점하게
   해뒀다 — 새 진입점(스크립트)을 추가한다면 같은 패턴을 넣을 것.
   - 증상이 이거면서 `import torch` 단독으로는 되는데 스크립트에서만 깨진다면
     100% 이 문제다. `python -c "import cv2,pandas; import torch"` 로 재현 확인.
2. **lerobot extras 누락**: `pip install lerobot` 만으로는 `lerobot.datasets`
   (`pip install 'lerobot[dataset]'` 필요, 내부적으로 HuggingFace `datasets`
   패키지를 씀)와 `lerobot-train`(`pip install 'lerobot[training]'`, `accelerate`
   필요)이 둘 다 빠진다. `requirements-lerobot.txt`는 `lerobot[dataset,training]`
   으로 이미 반영해뒀다.
3. **`--policy.repo_id` 누락 시 `lerobot-train`이 즉시 에러** (`ValueError:
   'repo_id' argument missing`). 허브에 올릴 생각이 없어도 이 인자는 필수다.
   `--policy.push_to_hub=false`와 같이 주면 로컬에만 저장된다.
4. **관절 수 불일치로 rollout 추론이 즉시 실패** (`tensor a(6) must match
   tensor b(5)`). 리더/팔로워가 물리적으로 관절 수가 같아도 `config.yaml`의
   `arms.*.joint_ids` 개수가 다르면(우리 경우 리더만 5개, 팔로워는 그리퍼 포함
   6개 읽음) `to_lerobot.py`가 만든 데이터셋의 `observation.state` 차원과 실제
   `follower.read()` 길이가 어긋난다. `ActInferenceRunner`가 체크포인트의
   `config.json`에서 `input_features["observation.state"]["shape"]`를 직접 읽어
   자동으로 맞춰 자르지만, 애초에 픽업(그리퍼 조작)까지 학습시키려면
   `arms.leader.joint_ids`에 그리퍼 축도 포함시켜 재수집해야 한다 — 지금
   설정으로는 팔 위치만 배우고 그리퍼 개폐는 데이터에 아예 없다.
5. **`PolicyProcessorPipeline.from_pretrained()`를 직접 호출하면 postprocessor가
   깨진다** (`EnvTransition must be a dictionary. Got Tensor`). 이 클래스의
   `to_transition` 기본값이 딕셔너리 전용(`batch_to_transition`)으로 고정돼
   있어서, 액션(텐서)을 받아야 하는 후처리기에도 그게 적용된다. 반드시
   `lerobot.policies.make_pre_post_processors(policy_cfg=policy.config,
   pretrained_path=checkpoint_path)`(공식 `lerobot_eval.py`가 쓰는 것과 동일)로
   불러올 것 — 이 함수가 후처리기에는 `to_transition=policy_action_to_transition`
   을 명시적으로 넣어준다.

> 위 4번은 2026-08-11에 `arms.leader.joint_ids`에 그리퍼(ID 6)를 추가해서
> 해소했다 — 이제 리더/팔로워 둘 다 6관절을 읽는다. 다만 안전 설정
> (`safety.joint_limits`, `arms.follower.home_position`)이 아직 5개 값뿐이라
> 그리퍼 축은 사실상 무제한으로 움직일 수 있다 — 아래 "다음 작업" 참고.

### `feature/integration` 병합 메모 (2026-08-14)

`feature/3axis-stepper-soft-pulse`(컨베이어 3축 스텝모터 통합, STM32 BlackPill 1보드
+ TB6600 3개, 소프트 펄스 전환)와 `feature/lerobot-rollout`(이 절 전체 — 모방학습
파이프라인)을 `feature/integration` 브랜치로 병합했다. **base는 3axis 쪽** — 사용자
지시로 포트 번호·카메라 매칭·컨베이어 설정 등 "장비 배선에 관한 값"은 전부 3axis
브랜치 값을 그대로 썼다. 모방학습 관련 코드(`lerobot_bridge.py`, `rollout.py`,
`act_trainer.py`, `to_lerobot.py`, `requirements-lerobot.txt`)는 lerobot-rollout
쪽에서 그대로 가져왔다 — 이 부분은 3axis가 손대지 않아 충돌 없음.

충돌은 `config.yaml`과 `run.py` 단 두 곳뿐이었다:
- `config.yaml`의 `arms.leader/follower`: **port_hint는 3axis 값(COM6/COM7)을
  유지**하고, lerobot-rollout 쪽에서 `scripts/find_ports.py`로 확인한
  `serial_number`를 추가로 얹었다 — serial_number가 port_hint보다 매칭 우선순위가
  높아 포트가 바뀌어도 안전하고, port_hint 정책과 충돌하지 않는다.
- `run.py`: torch 우선 import(DLL 순서 문제 회피, lerobot-rollout)와 단일 인스턴스
  락 + 스트림 포트 분리(3axis)는 서로 다른 관심사라 순서만 맞춰 이어붙였다.

**병합으로 저절로 해결된 것** (3axis 브랜치가 자체적으로 이미 채워둔 값):
- `safety.joint_limits`의 그리퍼(6번째) 범위 — `[-180, 180]`으로 넉넉히 열어두고
  current 기반 모드의 전류 리밋으로 과도 조임을 막는 전략. **각도 자체를 실측한 건
  아니라서**, 그리퍼가 실제로 안전한지는 처음 구동할 때 반드시 저속/근접 관찰로
  확인할 것.
- `arms.follower.home_position`의 그리퍼(6번째) 값 — 3axis 쪽에서 `0.0`으로 채워짐.
  **이 값이 이 개체의 실제 그리퍼에서 안전한 홈 각도인지는 검증되지 않았다** —
  통합 브랜치에서 처음 홈 복귀를 시킬 때 사람이 옆에서 지켜볼 것.

**아직 안 된 것 (병합과 무관하게 그대로 남은 TODO)**:
- [ ] **그리퍼 포함 모방학습 첫 검증** — 에피소드 1~2개 녹화(그리퍼 개폐 동작 포함,
      `--repo-id`는 기존 `omx/scan`과 겹치지 않게 새로 지정할 것 — 관절 수가 달라
      섞으면 `to_lerobot.py`가 깨짐) → 변환 → `lerobot-train` → `/api/rollout/start`
      로 확인. 아직 5관절(그리퍼 제외) 데이터로만 전체 파이프라인이 검증돼 있다.
- [ ] **rollout `rate_hz` 실측 재확인** — 5Hz 요청 시 관측된 tick 수가 예상보다
      많았음(도구 호출 지연으로 인한 측정 오차일 가능성 높음, 코드 자체는 문제
      없어 보임) — 실제 벽시계 기준으로 다시 재볼 것
- [ ] **병합 후 실물 통합 테스트** — `pytest`(시뮬레이션)는 전부 통과했지만, 컨베이어
      3축(`lm` 포함) + 모방학습 rollout을 같은 서버 프로세스에서 동시에 띄워보는
      건 아직 실물로 확인 안 됨. `hardware.py`가 둘의 공통 접점이니 이상 있으면
      거기부터 볼 것.
- [ ] **CAD 기반 불량검사(`cad.py`)까지 3갈래 완전 통합** — 컨베이어·모방학습은 이번에
      합쳤고, CAD 검사(`feature/cad-inspection-and-fixes` 브랜치, 2026-08-11 12:05
      마지막 푸시로 3axis보다 오래됨)는 아직 이 통합에 안 들어갔다.

---

## 3D 스캔 검사

5뷰 스캔 이미지에서 3D 형상을 만들어 마우스로 돌려가며 검사합니다.

**왜 포토그래메트리 대신 Visual Hull인가.** cam1~cam5 의 5뷰는 서로 크게 벌어져
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

---

## 라이선스 / License

MIT — [LICENSE](LICENSE) 참고. / See [LICENSE](LICENSE).
