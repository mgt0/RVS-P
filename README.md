# RVS-P (Robot-Vision Scan Platform)

[![한국어](https://img.shields.io/badge/lang-한국어-e11d48)](README.md)
[![English](https://img.shields.io/badge/lang-English-2563eb)](README.en.md)
[![中文](https://img.shields.io/badge/lang-中文-f59e0b)](README.zh.md)

![license](https://img.shields.io/badge/license-MIT-22c55e)
![python](https://img.shields.io/badge/python-3.12-3776AB)
![platform](https://img.shields.io/badge/platform-Windows-0078D6)
![tests](https://img.shields.io/badge/tests-passing-16a34a)
![last commit](https://img.shields.io/github/last-commit/mgt0/RVS-P?color=f59e0b)
![repo size](https://img.shields.io/github/repo-size/mgt0/RVS-P?color=eab308)
![issues](https://img.shields.io/github/issues/mgt0/RVS-P?color=8b5cf6)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-ec4899)

**로봇·비전 통합 제품 검사 플랫폼.** 모방학습(LeRobot ACT)으로 동작을 배운 로봇팔이 제품을
컨베이어에 올리고, 벨트가 멈추면 카메라 4대가 옆면·윗면까지 촬영해 3D 형상을 복원한 뒤
정상품 CAD(STL) 모델과 비교해 어긋난 부위를 표시합니다. 로봇·카메라·컨베이어는 웹 대시보드
하나에서 다룹니다.

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

## 대시보드 화면

`python run.py` 후 http://localhost:5000 을 엽니다. 고성능 HMI(ISA-101) 원칙을 따릅니다.

- 정상 상태는 무채색이고, 색은 이상과 조작에만 씁니다.
- 상태는 색, 기호(● ○ ■ ▲ ⓘ), 글자를 함께 표시합니다.
- 데스크톱(1280px 이상)을 기준으로 만들었습니다.

| 화면 | 하는 일 |
|---|---|
| 개요 | 스테이션 지도, 알림, 카메라 fps, 팔 관절, 동기 오차, 데이터 준비도, 최근 검사 촬영 |
| 장비 제어 | 컨베이어 2축(속도 %·방향·게이트), 텔레오퍼레이션, 홈 복귀, 카메라 실시간 영상 |
| 수집 | 시연 녹화 세션(ACT 학습용), 검사 샘플 촬영과 라벨, 에피소드 관리 |
| 학습 | LeRobot 학습 로그 따라 읽기(loss·l1·kld·grdn·lr·처리량·GPU), 데모 곡선 |
| 검사 | 5캠 촬영 → 3D 형상과 표면 이상(주황), 판정 근거와 신뢰도, CAD 기준 모델 |
| 진단 | 실행 환경, 시리얼 포트 식별, 카메라 설정, 안전 한계, 리그 캘리브레이션 |

- **상단바**
  - 오른쪽 위에 **비상 정지**가 있습니다. Vue 나 CDN 이 뜨지 않아도 동작합니다.
  - 자동 사이클의 시작·정지 버튼과 바스켓 상태를 보여 줍니다([자동 사이클](#자동-사이클--버튼-한-번--집기--이송--검사--분류) 절).
  - 알림 칩이 있습니다. 알림을 누르면 무엇이 문제인지, 영향, 원인, 해결 방법을 담은 상세 창이 열립니다.
- **프런트엔드 구조**
  - 빌드 없는 Vue 3(CDN, 버전 고정)와 `frontend/static/ui/` 의 정적 파일로 되어 있습니다.
  - 화면마다 `js/screens/<이름>.js` 파일이 하나씩 있고, 공용 상태와 컴포넌트는 `js/core/` 에 있습니다.
- **하드웨어 없이 화면을 확인하려면** 격리 시뮬레이션 서버를 씁니다.

  ```bash
  python scripts/dev_sim.py --port 5100 --data .dev_data/demo
  ```

  - 5000·5001 포트는 거부합니다.
  - 실제 `lerobot_data` 와 픽 궤적 파일은 건드리지 않습니다.

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

카메라 4대를 각각 스레드로 캡처하면 위상이 제각각입니다. 각 카메라의 *최신* 프레임을 그냥
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

### 컨베이어 구동부 — DC 모터 + HW-095(L298N 듀얼 H-브리지 모듈)

STM32F411 BlackPill 1보드가 벨트 2개(M1 공급 = A 채널, M2 작업 = B 채널)와 분류 게이트 서보(PB3)를 쥡니다.
엔코더가 없어 RPM·위치는 모르고, 속도는 PWM 듀티(%)로만 정합니다. LM 가이드(M3)는 없어졌습니다.

| L298N | M1 (공급, A) | M2 (작업, B) |
|---|---|---|
| ENA/ENB (PWM) | **PB0** (펌웨어 상수 `PB0_ALT1`, TIM3_CH3) | PA3 (TIM5_CH4) |
| IN1/IN3 | PA1 | PA6 |
| IN2/IN4 | PA2 | PA7 |

- **PA0 은 쓰지 않습니다.** 온보드 KEY 버튼이라 출력으로 쓰면 안 됩니다(그래서 M1 ENA 가 PB0 입니다).
- 모터 전원 12V 는 L298N 12V 단자에, GND 는 BlackPill 과 공통으로 묶습니다.
- **ENA/ENB 점퍼를 뽑습니다.** 꽂혀 있으면 항상 전속이라 PWM 이 안 먹습니다.
- EN·IN 핀에 10kΩ 풀다운을 권장합니다 — 리셋·DFU 중에는 핀이 떠 있어 모터가 튈 수 있습니다.
- 펌웨어 상수(전부 현장 실측 필요): `MIN_RUN_DUTY` 20(모터가 실제로 도는 최저 듀티, 임시값), PWM 2kHz/10bit, `RAMP_MS` 300.
- **호스트 워치독.** 펌웨어는 모터가 달리는 중 2초(`HOST_TIMEOUT_MS`) 동안 아무 줄도 못 받으면 모든 모터를 세우고,
  포트가 닫혀도(DTR 하강) 즉시 세웁니다. 서버는 벨트가 하나라도 달리는 동안 포트당 스레드 하나로 0.5초마다 `PING`
  을 보냅니다(명령과 같은 락). 서버가 죽거나 `release` 하면 벨트가 멈춥니다.
- `STOP` 은 브레이크, `DISABLE` 은 프리런 정지입니다. `DISABLE`(비상 정지 포함) 뒤에는 `ENABLE`('드라이버 켜기')
  전까지 `START` 를 거부합니다. `DUTY 0` 은 `STOP` 과 같고 설정한 속도는 남습니다. 속도의 기본값은
  `config.yaml` 의 `conveyors.*.default_duty`(기본 40)이며 연결 뒤 첫 `START` 전에 한 번 보냅니다.
- 옛 `config.yaml` 의 `conveyors.lm` 절, `stroke_mm`·`mm_per_rev`, `cycle.belt_rpm` 은 경고만 남기고 무시합니다.

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
│       ├── camera_cam2/frame_000000.jpg …
│       ├── camera_cam2/ … camera_cam5/
│       ├── joint_states.parquet
│       └── metadata.json
└── classification/
    ├── good/<object_id>/{cam2..cam5}.jpg + labels.json
    └── defect/<object_id>/{cam2..cam5}.jpg + labels.json
├── background/{cam2..cam5}.jpg   ← '배경 캡처' 가 저장하는 빈 스캔대 참조
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
| `ts_{cam2..cam5}` | 카메라별 캡처 시각 (monotonic) |

`metadata.json` 의 `monotonic_offset` 을 더하면 monotonic → epoch 로 복원됩니다.
(monotonic 을 쓰는 이유: wall clock 은 NTP 보정으로 역행할 수 있어 프레임 정렬에 부적합)

---

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/meta` | 카메라 순서, 불량 유형, 다음 에피소드 ID |
| GET | `/api/status` | 녹화·arm·텔레오퍼·카메라 통합 상태 (메인 폴링). `sync_skew_ms`(카메라 간 동기 오차 ms, 2대 미만이면 null)와 `demo`(시연 세션 상태), `cycle`(자동 사이클 상태) 포함 |
| GET | `/api/ports` | 포트 + 카메라 상태 스냅샷 |
| POST | `/api/ports/refresh` | 오버라이드 초기화 후 재스캔 |
| POST | `/api/ports/test` | 실제 open + ping 검증 |
| POST | `/api/ports/identify/{start,confirm}` | 언플러그 diff 2-phase |
| GET | `/api/alerts[?refresh=1]` | 지금 사람이 알아야 할 항목(원인·영향·해결 방법 포함, 심각도 fault/warn/info). 5초 캐시, `refresh=1` 은 우회 |
| GET | `/api/diagnostics[?refresh=1]` | 환경·직렬 포트·카메라 설정·안전 한계(관절 한계·최대 상대 이동·홈 복귀 속도 `homing_speed_deg_per_s` 등) 진단. 30초 캐시, `refresh=1` 은 우회 (실시간 연결 상태는 `/api/ports`) |
| GET | `/api/stream/<name>?fps=&q=` | MJPEG 스트림 |
| GET | `/api/snapshot/<name>` | 단일 JPEG |
| POST | `/api/recording/{start,stop}` | 수집 제어 (시연 세션 중 `start`·`stop`, 자동 사이클 중 `start` 는 409 — 세션 스레드가 녹화를 쥐고 있어 수동 정지가 끼면 에피소드를 잃는다. 비상 정지는 이 제한과 무관) |
| POST | `/api/demo/start` | 시연 녹화 세션 시작 — body `object_id`, `task`, `episode_sec`(1~600), `reset_sec`(0~120). 텔레오퍼 켜짐·팔 연결·녹화/rollout 없음이어야 함 |
| POST | `/api/demo/{next,redo,end}` | 저장하고 다음 / 버리고 다시(녹화 중에만) / 저장하고 세션 종료. 진행 상태는 `/api/status` 의 `demo`. 시연 에피소드는 `kind=demo` + `task` 로 저장되고 분류 이미지는 만들지 않음 |
| GET | `/api/episodes` | 저장된 에피소드 목록 (metadata.json) |
| PATCH | `/api/episodes/<id>` | 라벨·물체 ID 수정 (분류 폴더도 함께 이동. `kind=demo` 시연 에피소드는 분류 폴더를 건드리지 않음) |
| DELETE | `/api/episodes/<id>` | 에피소드 삭제 (마지막 참조면 분류 이미지도 정리. 시연 에피소드는 참조로 세지 않음) |
| GET | `/api/inspections?limit=` | 3D 검사 촬영 목록(최신순)과 라벨 현황 요약. `capture_id` 는 `YYYYmmdd_HHMMSS` 이고 같은 초의 촬영은 `_02`, `_03` … 이 붙는다 |
| GET | `/api/inspections/<capture_id>/<cam>.jpg` | 촬영 원본 이미지 |
| POST | `/api/inspections/<capture_id>/label` | 촬영 라벨 저장 — `inspections/<id>/label.json` 에만 기록 (`classification/` 은 건드리지 않음) |
| DELETE | `/api/inspections/<capture_id>` | 촬영 삭제 (라벨 없는 것만, 있으면 400) |
| GET | `/api/objects` | 물체 목록(에피소드·라벨된 촬영·CAD 모델)과 권장 촬영 자세 수 |
| GET | `/api/readiness` | 데이터 준비도 (ACT 에피소드 수, 양품·불량 물체 수, 목표값) |
| POST | `/api/emergency_stop` | 비상 정지 — 자동 사이클·텔레오퍼·전 벨트·녹화·시연 세션 일괄 정지 |
| POST | `/api/emergency_reset` | 비상 정지가 끈 벨트 드라이버만 다시 켠다('드라이버 켜기', 아무것도 움직이지 않는다). 연결된 벨트만 대상 — 끊기거나 release 한 벨트는 포트를 열지 않고 `not_connected` 로 알린다 |
| GET/POST | `/api/conveyor/status`, `/api/conveyor/<name>/{start,stop,speed,dir,enable,servo,release}` | 컨베이어(DC 모터 2벨트) 제어 (자동 사이클 중에는 `stop`·`enable(false)` 외 구동 명령이 409). `speed` 본문은 `{"duty": 0~100}`(PWM 듀티 %, 범위 밖이면 400). `dir` 은 달리는 중이면, `start` 는 드라이버가 꺼져 있으면 펌웨어가 거부해 400. 상태 키: `running`(구동 중)·`duty`(설정한 속도)·`direction`·`enabled`·`servo_deg`(M1 만) |
| GET | `/api/cycle/status` | 자동 사이클 상태 — 단계·진행 메시지·판정 결과·바스켓 개수·시작 가능 여부(`ready`: 막는 이유 `reasons`, 막지는 않는 안내 `notes`). `/api/status` 의 `cycle` 키와 같다 |
| POST | `/api/cycle/start` | 사이클 한 번 시작(집기 → 벨트 이송 → 검사 → 분류). 준비가 안 됐거나 이미 돌고 있으면 400 + 이유. 점검을 통과하면 팔이 움직이기 전에 벨트 포트를 열고, 못 열면 400 |
| POST | `/api/cycle/stop` | 즉시 중단 — 벨트와 자율 실행만 세운다(게이트·팔은 그 자리). state 는 `aborted` |
| POST | `/api/cycle/baskets/empty` | 바스켓을 비운 뒤 개수를 0 으로 — body `{"basket": "good"}`, `"defect"`, `"all"` 중 하나 |
| GET/POST | `/api/scan/model/<object_id>` | CAD 기준 모델(STL) 조회/업로드 |
| POST | `/api/scan/background` | 빈 스캔대 배경 캡처 (배경 차분용. 자동 사이클 중 409 — 벨트 위 물체가 배경에 박힌다) |
| POST | `/api/scan/capture` | 컨베이어가 멈춘 지금 한 세트를 촬영해 3D 판정 — body `settle_sec`(0~3), `resolution`(16~96). 숫자가 아니면 400, 자동 사이클 중 409. 촬영본은 `inspections/<capture_id>/` |
| POST | `/api/teleop/{start,stop}` | 텔레오퍼레이션 (자동 사이클 중 `start` 는 409) |
| POST | `/api/arm/home` | 홈 복귀 (녹화·자동 사이클 중 거부) |
| POST | `/api/train/{start,stop}` | ACT 학습 제어 (demo/lerobot 모드) |
| GET | `/api/train/status` | 학습 상태 + 메트릭 시계열 (폴링) |
| GET | `/api/train/stream` | 학습 메트릭 SSE 실시간 스트림 |
| GET | `/api/train/runs/latest` | `logs/train` 의 가장 최근 학습 실행 요약 (없으면 `run: null`, 따라 읽기를 시작하지 않음) |
| GET | `/api/scan/objects` | 재구성 가능한 스캔 물체 목록 |
| POST | `/api/scan/reconstruct` | 5뷰 → 3D 메시 (visual hull). body 는 저장된 물체(`object_id`, `label`) 또는 저장된 검사 촬영(`capture_id` — `inspections/<id>/<cam>.jpg` 를 읽어 `/api/scan/capture` 와 같은 방식으로 다시 만든다). 계산만 하므로 자동 사이클 중에도 된다 |
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
python scripts/train_act.py --repo-id omx/scan --steps 20000
```

`scripts/train_act.py` 는 `lerobot-train` 을 하위 프로세스로 띄우는 래퍼다
(`--policy.type=act`, 필수인 `--policy.repo_id`, `--policy.push_to_hub=false` 를 알아서 붙인다 —
아래 "실제로 겪은 문제" 3번). 학습 인자 중 래퍼가 모르는 것은 `--` 뒤에 그대로 쓴다
(`... --repo-id omx/scan -- --policy.device=cuda --save_freq=5000`). `--log-freq`(기본 50)는
lerobot 에 명시적으로 넘기며, 대시보드 곡선의 점 간격이다. 래퍼는 실행마다
`logs/train/<YYYYmmdd_HHMMSS>/` 에 `run.json`(인자·상태 RUNNING/DONE/FAILED·returncode·
output_dir), `train.log`(lerobot 출력 원본), `metrics.jsonl`(파싱한 지표), `gpu.jsonl`(GPU
상태, 아래), `heartbeat`(10초마다 수정 시각만 갱신하는 빈 파일 — 서버가 래퍼의 생존을 판단하는
근거, 아래)를 남기고 콘솔에도 그대로 흘린다. 같은 초에 시작한 실행은 폴더 이름에 `_02`, `_03` …
이 붙는다(두 자리라 이름순 = 시간순). 학습을 멈추려면 래퍼를 띄운 터미널에서 Ctrl+C.
스키마와 로그 파서는 `backend/core/train_log.py`. lerobot 이 없으면 설치 안내를 내고 즉시 실패한다.

**GPU 활용.** 노트북 GPU 한 대(RTX 5070 Laptop, VRAM 8GB)로 돌릴 때 손댈 것은 이 셋이다.

- `--precision {bf16,fp16,no}`(기본 `bf16`) — 혼합정밀도. 텐서코어를 쓰고 메모리를 아낀다. bf16 은
  RTX 30 시리즈 이상이 지원하니 그보다 오래된 GPU 면 `--precision no`. 이 값은 lerobot 의
  `--accelerator.mixed_precision` 으로 넘어가는데, 이 인자는 **lerobot main 에만 있고 릴리스(0.6.1
  까지 확인)에는 없다.** 모르는 인자를 주면 `lerobot-train` 이 시작도 못 하고 죽으므로 래퍼가 설치된
  lerobot 을 먼저 보고, 없으면 인자를 빼고(fp32) 안내만 한다(`run.json` 의 `precision` 이 `null`).
  릴리스에서 AMP 가 필요하면 `-- --policy.use_amp=true` 를 직접 준다(autocast dtype 은 torch 기본값을 따른다).
- `--batch-size`·`--steps` — 안 주면 lerobot 기본값(batch 8, steps 100000). 8GB VRAM 에 카메라가
  5~6대면 배치마다 이미지가 많이 실려 OOM 이 쉽게 나니 OOM 직전까지 batch 를 직접 올려 보고 정한다.
  batch 를 바꾸면 한 스텝이 보는 샘플 수가 달라지므로 `--steps` 도 같이 맞춘다.
- `--num-workers` — DataLoader 워커 수. 안 주면 lerobot 기본값(4)이고 `0`(메인 프로세스 로딩)도 유효하다.

래퍼는 학습이 도는 동안 5초마다 `nvidia-smi` 로 GPU 상태를 재서 `gpu.jsonl` 에 남기고(torch 를 쓰지
않고 하위 프로세스로만 잰다), LEROBOT 모드 대시보드가 그 최근 요약(약 30초)을 'GPU' 카드로 보여 준다.
`nvidia-smi` 가 없으면 한 번 알리고 바로 계측을 멈추고, 일시적인 실패는 다음 주기에 다시 시도하며,
연속 5번 실패하면 멈춘다. 어느 쪽이든 학습은 그대로 돈다.

| GPU 카드 | 읽는 법 |
|---|---|
| 사용률 | 90% 이상 유지가 정상. 들쭉날쭉하면 GPU 가 데이터를 기다리는 로딩 병목이니 `--num-workers` 를 조정한다 |
| 전력 / 한계 · 온도 | 최근 샘플 중 전력 제한(`sw_power_cap`)이나 열 제한(`sw/hw_thermal_slowdown`)이 한 번이라도 걸렸으면 주황색 경고 — 클럭이 깎여 느려진 상태라 전원 연결·성능 모드, 냉각을 확인한다 |
| VRAM | 사용 / 전체. batch 를 올릴 때 OOM 까지 얼마나 남았는지 보는 용도 |

ACT 에는 `torch.compile` 옵션이 아직 없다. lerobot main 의 `--accelerator.compile` 은 켜면 "placeholder
and not wired yet" 오류를 내는 자리표시자이고 `ACTConfig` 에도 `compile_model` 필드가 없어서 래퍼도
옵션을 만들지 않았다. 또 `--policy.path=lerobot/smolvla_base` 는 ACT 이어학습이 아니라 다른 정책
(SmolVLA)으로 갈아타는 것이라, `ACTPolicy` 전용인 rollout 코드(`backend/core/lerobot_bridge.py`)와
맞지 않는다.

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
차트 로직이 실제와 동일하게 검증됩니다. **LEROBOT 모드**는 학습을 시작하지 않고, 위
`scripts/train_act.py` 가 남긴 가장 최근 `logs/train/*` 실행을 따라 읽어 실제 loss 곡선을
같은 차트에 그립니다(`LeRobotTrainer`, 0.5초 폴링). 카메라 4대와 시리얼 포트를 쥔 서버 안에서
GPU 학습을 돌릴 이유가 없어서 학습은 계속 서버 밖에서 돌고, 서버를 재시작해도 살아남습니다.
실행 기록이 없으면 래퍼 사용법을 안내하며 FAILED 로 끝납니다.

LEROBOT 모드에서 달라지는 점:
- x축은 EPOCH 가 아니라 **STEP** 이다. lerobot 로그의 `step:1K` 는 손실 압축이라(1000·1200·1400
  이 모두 `1K`) 파싱값을 쓰지 않고 'n 번째 지표 줄 × log_freq' 로 복원한다. `--resume` 으로
  이어받은 실행은 체크포인트 스텝만큼 x 값이 어긋난다.
- lerobot 은 val MAE·성공률을 로그에 남기지 않아 해당 KPI·차트는 '—'/숨김이다. VAL loss 는
  `step N: eval_loss=…` 줄이 있을 때만 그려진다. 같은 스텝의 평가 줄은 그 스텝의 점에 붙고(두 번 와도
  한 점), 지표 줄이 없는 스텝은 별도 점이 된다 — 직전 train loss 를 이어 붙이고, 첫 지표 줄보다 먼저 온
  평가 줄은 train loss 를 비운 점으로 남긴다. 그래서 x축에 같은 스텝이 두 번 나오지 않는다.
- **STOP 은 '따라 읽기' 중단**일 뿐 학습 프로세스를 죽이지 않는다(서버는 학습의 부모가 아니다).
  래퍼가 강제 종료돼도 `run.json` 은 RUNNING 으로 남지만, heartbeat 파일이 60초 넘게 멈추면 서버가
  FAILED 로 본다(heartbeat 가 없는 옛 실행은 STOP 으로 끊는다).

실시간 전송은 **SSE**(Server-Sent Events)를 씁니다. 폴링은 간격만큼 늘 뒤처지지만, SSE는
에폭이 끝나는 즉시 차트가 자랍니다.

---

## Rollout — 학습된 ACT 정책으로 자율 실행 (신규)

위 절차(`to_lerobot.py` → `train_act.py`)로 학습이 끝나면, 그 체크포인트로
팔로워를 자율 제어할 수 있다. 학습(`lerobot-train`)은 의도적으로 이 서버
프로세스 밖에서 돈다 — 카메라 4대와 시리얼 포트를 쥔 채로 GPU를 몇 시간
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
- `frames`로 넘기는 카메라 이름은 `CAMERA_ORDER`(`cam2`~`cam5`)와 일치해야
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
      2축(DC 모터로 교체됨) + 모방학습 rollout을 같은 서버 프로세스에서 동시에 띄워보는
      건 아직 실물로 확인 안 됨. `hardware.py`가 둘의 공통 접점이니 이상 있으면
      거기부터 볼 것.
- [ ] **CAD 기반 불량검사(`cad.py`)까지 3갈래 완전 통합** — 컨베이어·모방학습은 이번에
      합쳤고, CAD 검사(`feature/cad-inspection-and-fixes` 브랜치, 2026-08-11 12:05
      마지막 푸시로 3axis보다 오래됨)는 아직 이 통합에 안 들어갔다.

---

## 3D 스캔 검사

5뷰 스캔 이미지에서 3D 형상을 만들어 마우스로 돌려가며 검사합니다.

**왜 포토그래메트리 대신 Visual Hull인가.** cam2~cam5 의 4뷰는 서로 크게 벌어져
있어 COLMAP 같은 SfM의 특징점 매칭이 거의 실패합니다(보통 겹침 큰 수십~수백 장 필요).
대신 **실루엣 기반 space carving**을 씁니다:

1. 각 뷰에서 물체 실루엣(전경 마스크)을 뽑고
2. 3D 복셀 격자의 각 복셀을 5개 뷰로 투영해
3. **모든 뷰에서 실루엣 안**인 복셀만 남깁니다 → 물체의 볼록 근사 형상

오목한 안쪽(구멍)은 복원 못 하지만, 형상 확인·불량 위치 표시에는 충분하고 무엇보다
**5뷰만으로 항상 동작**합니다. 복셀은 marching cubes로 표면 메시가 됩니다
(scikit-image 없으면 복셀 큐브 폴백). visual hull 은 물체를 **감싸는** 형상이라 이 배치
(대각 4 + 하단 1)에서는 모서리 바깥을 다 깎지 못합니다 — 합성 50mm 큐브가 X·Z 61mm,
Y 55mm 로 나옵니다. 화면의 치수는 그래서 '외접 치수'입니다.

**실루엣이 형상을 결정한다.** visual hull 은 '모든 뷰에서 안쪽'인 AND 라 실루엣 한 장이
틀리면 그만큼 형상이 깨집니다. 이 리그에서 실제로 형상이 덩어리로 나오던 원인은 대부분
실루엣이었습니다(배경 캡처 뒤 모니터·로봇팔·사람이 움직임, 흰 큐브 앞의 흰 벽, 유리 반사).
그래서 세 겹으로 막습니다.

1. **검사 ROI** — `config.yaml` 의 `cameras.*.roi` 안에서만 실루엣을 뽑습니다. 물체가 멈추는
   자리만 보면 화면 가장자리의 변화가 섞이지 않습니다. 캘리브레이션 후에는 작업 공간(정지
   위치 ±45mm) 투영으로 자동 계산되므로 줄을 지워도 됩니다.
2. **GrabCut 보정** — 배경 차분은 씨앗으로만 쓰고(확실히 물체/아마 물체/ROI 테두리 = 배경)
   경계는 색 분포로 다시 정합니다. 배경 사진이 오래돼 차분이 엉망이어도 쓸 만한 경계가 나옵니다.
   난수 시드를 고정해 같은 사진은 늘 같은 실루엣이 나옵니다.
3. **어긋난 뷰 자동 제외** — 뷰마다 '나머지 뷰로 깎은 형상이 이 뷰의 실루엣을 얼마나
   맞히는가'(leave-one-out IoU)를 재고, 최대 2대까지 빼 본 모든 조합 중 평균이 0.04 이상
   오르는 조합을 고릅니다(최소 3대 유지). 나쁜 뷰가 둘이어도 둘 다 빠집니다. 뺀 뷰와 점수는
   `stats.dropped_views`·`view_scores` 로, 화면에는 '제외' 로 나옵니다.

저장된 9세트(2026-10-12 촬영) 기준으로 카메라 일치도(leave-one-out 평균)가 0.08~0.32 →
0.73~0.79 로 올랐고, 볼록도(형상이 파였는지)는 0.86~0.95 로 무너진 세트가 없습니다. 유리 너머
하단 C1 과 반사가 섞이는 C5 는 대부분 세트에서 자동으로 빠집니다. '관용 깎기'(N−1 대에서
안쪽이면 남김)는 시험해 봤지만 점유율이 1.0 근처까지 부풀어 버렸습니다. 근본적으로는
**검사 자리 뒤에 단색 배경판**(수평 C2·C4 시야)을 두고, **촬영 직전에 배경 캡처**를 새로
하는 것이 가장 효과가 큽니다 — 소프트웨어는 뒤에서 움직인 것을 완전히 구분하지 못합니다.

**주황 = 표면 이상(실제 증거).** 균열·긁힘·펜 자국은 윤곽을 바꾸지 않아 실루엣 비교로는
원리적으로 못 잡습니다. 그래서 원본 이미지에서 '부품 표면 위의 얇고 어두운 자국'을
블랙햇(닫힘 − 원본)으로 찾고(`anomaly_map`), 그 뷰에서 실제로 보이는 정점에 옮겨
칠합니다(`surface_anomaly`). 면 사이 밝기 차이·완만한 음영에는 반응하지 않습니다.
자국은 '부품 표면에 사방이 둘러싸인' 곳에서만 봅니다(`_part_surface`: 실루엣 중심부와 이어진
밝은 덩어리, 구멍은 메우고 가장자리는 3px 깎음). 깨끗한 큐브의 오탐은 전부 실루엣에 덧붙은
배경(큐브 테두리 바로 바깥의 틈) 위였기 때문입니다. 같은 영역에서만 텍스처 색도 뽑아,
매트·뒤쪽 물체 색이 큐브 옆면에 묻지 않습니다(못 본 곳은 중립 회색).
저장된 데이터에서 깨끗한 큐브 6세트는 표면의 0.00~0.22%, 펜 자국 큐브 3세트는 3.0~6.8% 가
표시되고 주황은 실제 자국 위치('+', 사선, 점)에 모입니다. 같은 큐브·리그 데이터로 맞췄으니
부품·조명이 바뀌면 다시 확인할 것. 넓은 얼룩·밝아진 변색·윤곽에 걸친 자국은 못 잡습니다.

**신뢰도(품질 게이트).** 카메라 일치도 < 0.5, 볼록도 < 0.8, 쓸 수 있는 카메라 < 3대 중
하나라도 걸리면 `stats.quality.level = "low"` 로, 텍스처·주황을 끄고 중립 회색 형상만
보내며 `reasons`·`actions`(이유·할 일)를 함께 담습니다. 화면은 검사 KPI 위에 한 줄로 보입니다.

**환경 교란 시험**(저장된 9세트, 해상도 64, 2026-10-28 — 배경판·캘리브레이션 없음):

| 교란 | 일치도 | 볼록도 최저 | CAD 정합 | 신뢰도 낮음 | 깨끗 주황 최대 | 자국 주황 최저 |
|---|---|---|---|---|---|---|
| 없음 | 0.75 | 0.86 | 0.84 | 0/9 | 0.20% | 3.1% |
| 밝기 +30% / −30% | 0.74 / 0.74 | 0.88 / 0.88 | 0.83 / 0.83 | 0/9 | 0.47% / 0.00% | 3.7% / 1.9% |
| 감마 0.7 / 1.3 | 0.74 / 0.74 | 0.86 / 0.84 | 0.84 / 0.81 | 0/9 | 0.10% / 0.21% | 3.4% / 3.2% |
| 화이트밸런스 차갑게 / 따뜻하게 | 0.75 / 0.75 | 0.84 / 0.86 | 0.84 / 0.83 | 0/9 | 0.00% / 0.28% | 3.0% / 3.2% |
| C4 뒤 사람 크기 물체 합성 | 0.75 | 0.86 | 0.85 | 0/9 | 0.22% | 1.5% |
| 카메라 ±3px 흔들림 | 0.75 | 0.85 | 0.84 | 0/9 | 0.19% | 3.1% |
| 낡은 배경(배경만 +20% 밝게) | 0.75 | 0.88 | 0.84 | 0/9 | 0.20% | 3.1% |

합성 원근 장면(`tests/test_scan3d.py` 의 렌더러)에서도 위 조명·흔들림 교란 전부에서 깨끗한
큐브 주황 0%, 그은 선은 제 면에 표시, 외접 치수 60~61mm 로 유지됩니다. 사람 합성은 그
카메라의 자국 검출을 줄입니다(자국 주황 최저 1.5%) — 검사 자리 뒤를 비우는 것이 여전히 낫습니다.

예전에는 불량 라벨이면 고정 좌표 하나를 칠하는 데모값이 들어가 있어 '주황이 엉뚱한 곳에
뜬다'의 원인이었습니다 — 제거했습니다(외부 검출기 위치는 `defect_regions` 로 계속 넘길 수 있음).

**CAD 기준 모델 비교.** 기준 모델(STL/STEP)이 있으면 자세를 맞춘 뒤 윤곽 결손과 표면 이상을
CAD 위에 칠합니다. 자세를 못 맞추면(일치도 < 0.55) 회색 CAD 대신 스캔 형상을 보여주고
이유를 알립니다. CAD 복셀화가 격자 끝 면을 깎아 속이 빈 껍질이 되던 버그를 고쳐(50mm 큐브
STL 점유 0.005 → 0.999) 정합 일치도가 0.12~0.14 → 0.70~0.84 로 올랐습니다.

**리그 캘리브레이션(선택, 권장).** 캘리브레이션이 없으면 공칭 시선 방향 + 정사영 + 뷰별
바운딩박스 정규화라는 근사로 깎습니다(치수 없음). ArUco 마커 큐브로 카메라별 렌즈·자세를
재면 실제 원근 투영으로 mm 단위 형상이 나옵니다. 원근 경로는 보정된 뷰만 쓰므로, 보정된 카메라가
3대 미만이면 쓸 뷰가 모자라 항상 신뢰도 낮음이 된다 — 그때는 모든 뷰로 정사영 근사를 돌리고
이유를 `stats.calibration_note` 에 남긴다(`quality` 와 별개라 색·주황은 꺼지지 않는다).

1. `python scripts/make_calib_cube.py` → `calib_cube.png` 를 **실제 크기(100%)** 로 인쇄,
   눈금이 50mm 인지 자로 확인.
2. 바깥선을 잘라 50mm 큐브(검사용 큐브 그대로)에 감싸 붙인다. 전개도를 접으면 면·방향이
   코드의 관례와 맞도록 인쇄돼 있다(`tests/test_scan3d.py` 가 접기를 계산해 확인).
3. 벨트를 멈추고 큐브를 정지 위치에 **'앞(ID2)' 면이 C2·C4 사이를 향하게** 놓고
   [캘리브레이션 촬영] — 첫 샷이 좌표계 기준이다. 모든 카메라에 마커가 보여야 한다.
4. 큐브를 조금씩 돌리고 **한두 장은 기울여**(쐐기 등) 2~4장 더 찍고 [계산].
   하단 C1 은 밑면 하나만 보므로, 돌리기만 하면 초점거리를 풀 수 없어 화각 70° 가정값을
   쓴다(화면에 '초점 가정값' 표시). 합성 검증: 기울인 샷 포함 4장 → 카메라 위치 오차
   0.7~6.3mm(거리 250mm), 돌리기만 한 3장 → C1 은 가정값 고정으로 약 12mm.
5. 결과는 `lerobot_data/rig_calibration.json`. 카메라를 건드리면 다시 한다. 왜곡 계수는
   풀지 않는다(큐브가 화면 가운데 작게 찍혀 근거가 없다).

**뷰어 조작**: 드래그=회전, 스크롤=확대. three.js r128 기반이며, 의존성을 줄이려
OrbitControls 대신 최소 궤도 컨트롤을 직접 구현했습니다.

> **데이터 없이 바로 확인**: 3D SCAN INSPECTION 섹션의 **DEMO** 버튼을 누르면 카메라·물체
> 없이 합성 형상 + 오렌지 하이라이트를 확인할 수 있습니다(DEMO 의 주황은 고정 좌표 — 검출 결과가 아님).

---

## 자동 사이클 — 버튼 한 번 = 집기 → 이송 → 검사 → 분류

상단 [▶ 사이클 시작] 한 번이 한 사이클이다(제작보고서 3.1 플로우차트). 서버 스레드가 아래 순서로 진행하고,
진행은 `/api/status` 의 `cycle` 키(또는 `GET /api/cycle/status`)로 폴링한다. 구현은 `backend/core/cycle.py`.

1. **집기** — 게이트를 닫힘 각도로 두고 벨트 방향·속도를 정한 뒤, 웨이포인트 재생(`/api/rollout/start` 와 같은
   autorun)으로 물체를 집어 벨트에 놓는다. `pick_timeout_sec` 안에 안 끝나거나 fault 면 실패. 궤적이 끝나면
   `pick_settle_sec`(기본 1초)을 더 기다린 뒤 이송으로 넘어간다 — 그리퍼가 놓는 동작이 끝나기 전에 벨트가
   움직이지 않게 하는 값이다(현장에서 재서 맞춘다).
2. **이송** — 벨트를 `to_scan_sec` 동안 돌린 뒤 멈춘다.
3. **검사** — `/api/scan/capture` 와 같은 함수(`run_scan_capture`)로 정지 확인 → 촬영 → 3D 판정. 촬영본은
   `inspections/<capture_id>/`. 촬영이 실패하면 사이클도 실패.
4. **분류** — 양품이면 벨트를 `good_run_sec` 돌리고 멈춘다. 불량이면 `defect_to_branch_sec` 돌려 멈춘 뒤 게이트를
   열고 `gate_hold_sec` 기다렸다 닫는다. 그다음 해당 바스켓 개수를 +1 한다.

판정: `stats.quality.level == "low"` 면 불량(판정 불가 물체를 양품 라인으로 보내지 않는다),
`anomaly.flagged_ratio >= defect_ratio` 면 불량, 그 밖에는 양품.

**픽 웨이포인트 만들기(처음 한 번)**

1. 장비 제어에서 텔레오퍼를 켜고, '집어서 벨트에 놓는' 시연을 한 에피소드로 녹화한다. 같은 대기 자세에서 시작해
   그 자세로 돌아오게 한다 — 사이클 사이에 홈 복귀가 없어 다음 사이클은 끝난 자세에서 시작한다.
2. `python scripts/to_waypoints.py --episode <ID> --out lerobot_data/waypoints/pick_place.json`
3. `config.yaml` 의 `cycle.pick_waypoints` 가 그 파일을 가리키게 한다(기본값이 위 경로). 같은 `cycle:` 절의 집기 뒤
   대기(`pick_settle_sec`, 0~60초)·벨트 시간·속도·게이트 각도·판정 기준·바스켓 용량은 전부 출발점이다 — 줄마다
   '현장 실측 필요' 가 붙어 있다. 값이 잘못되면(범위 밖·숫자 아님) 서버가 기동할 때 바로 거부한다.

**안전 규칙**

- 시작 전 점검(`ready`)에 하나라도 걸리면 시작하지 않고 이유(`reasons`)를 준다: 픽 동작 파일(레포 안 경로는 레포
  기준 상대 경로로 보인다), 사이클 벨트가 설정에 있고 포트가 정해져 있을 것·드라이버가 켜져 있을 것·서 있을 것,
  팔로워 연결, 재구성용 카메라 2대 이상, 텔레오퍼·rollout·녹화·시연이 없을 것, 바스켓 여유.
  `pick_checkpoint`(ACT)를 설정하면 시작을 거부한다 — 완료 신호가 없어 집기가 항상 시간 초과로 끝난다(웨이포인트를
  쓴다. 설정 검증이 아니라 점검에서 막으므로 서버 기동은 실패하지 않는다).
  - 실기에서는 서버를 켠 직후 벨트 포트가 닫혀 있다(첫 명령 때 연다). `ready` 는 포트를 열지 않고 막지도 않는다 —
    `notes` 에 '시작하면 벨트 포트를 엽니다' 라고 안내만 한다. `POST /api/cycle/start` 가 점검을 통과한 뒤 **팔이
    움직이기 전에** 포트를 열고, 못 열면 400 으로 이유를 주며 팔은 움직이지 않는다.
  - 비상 정지는 벨트 드라이버를 끈다(DISABLE). 펌웨어가 DISABLE 상태의 START 를 거부하므로, 비상 정지 뒤에는 사람이 장비
    제어의 '드라이버 켜기'(`POST /api/emergency_reset`)로 명시적으로 풀어야 시작할 수 있다. 포트가 닫혀 있던 동안
    꺼진 드라이버는 시작이 포트를 연 뒤에 확인해 같은 이유로 거부한다.
- 사이클이 도는 동안 서버가 텔레오퍼·rollout·녹화·시연 시작, 수동 촬영(`/api/scan/capture`·`background`·
  `calibrate/shot`), 벨트 구동 명령(start·speed·dir·servo·release), 홈 복귀를 409(`cycle_busy`)로
  거부한다. 사이클 도중에 배경을 찍으면 벨트 위 물체가 배경에 박혀 이후 판정이 모두 틀어지기 때문이다. 계산만
  하는 `reconstruct`·`calibrate/solve` 는 막지 않는다. 벨트 `stop`·비상 정지·`/api/cycle/stop` 은 언제나 된다.
  **사람이 어떤 벨트든 세우거나 드라이버를 해제하면 사이클도 함께 중단된다**(사이클이 다음 단계에서 벨트를 다시
  돌리지 않게). 서보 게이트와 COM 포트를 두 축이 공유하므로 사이클 중 수동 조작은 보수적으로 전부 막고, 다른
  벨트를 세워도 사이클을 멈추는 쪽이 안전하다.
- 중단·오류가 나면 벨트 정지와 autorun 정지만 한다(벨트가 먼저 — autorun 정지는 허브 락을 기다릴 수 있다). 게이트와
  팔은 멈춘 자리에 그대로 있다(게이트는 다음 사이클을 시작할 때 닫힘으로 되돌린다). 비상 정지는 가장 먼저 사이클을
  끊고, 이미 나간 벨트 명령이 끝나길 잠깐(최대 1초) 기다린 뒤 벨트를 정지·비활성한다 — 비활성 뒤에 점검을 통과한
  START 가 끼어 드라이버가 다시 켜지는 일이 없다. 팔을 움직이는 autorun 시작은 허브가 자기 락 안에서 중단·서버
  종료 여부를 다시 확인해 정지가 끝난 뒤에는 시작하지 않는다. 서버 종료가 끝난 뒤에는 닫은 벨트 포트를 다시 열지
  않는다. 모든 대기가 `Event.wait` 라 정지가 대기 도중에도 즉시 먹는다.
- 바스켓이 차면(`basket_capacity`) 시작하지 않는다. 비운 뒤 [비웠습니다](`POST /api/cycle/baskets/empty`)를 누르면
  개수가 0 으로 돌아간다. 개수는 서버 메모리에만 있어 서버를 다시 켜면 0 이다.
- **처음 돌릴 때는 `pick_speed` 를 낮추고 로봇 옆에서 지켜본다.**

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
