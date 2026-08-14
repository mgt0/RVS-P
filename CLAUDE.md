# CLAUDE.md — OMX Scan Control 작업 지침

이 저장소로 작업할 Claude(와 사람)를 위한 요약. **자세한 설계 이유·API·데이터 구조는
[README.md](README.md)가 1차 출처**다 — 여기서는 README에 없는 "당장 사고 안 나게
지켜야 할 것"과 "지금 상태가 뭔지"만 압축해 둔다. README와 내용이 갈리면 README를
갱신하고 이 파일은 포인터만 유지할 것 (같은 사실을 두 곳에 다른 말로 적어두면 반드시
어긋난다).

## 이게 뭔가

ROBOTIS OMX-L(리더)/OMX-F(팔로워) 텔레오퍼레이션 + 5축 카메라 스캔 + 컨베이어 3축
제어 + LeRobot ACT 모방학습(녹화→변환→학습→rollout)을 한 Flask 서버(`run.py`)가
전부 관장한다. **실물 로봇 팔·컨베이어를 직접 구동하는 프로젝트다.** 시뮬레이션 없이
API를 호출하면 진짜 모터가 움직인다.

## 하드웨어 안전 — 예외 없이

- **teleop/rollout 시작은 사용자가 로봇 옆에 물리적으로 있을 때만.** 자리를 비우거나
  대화가 끊길 가능성이 있으면 먼저 teleop/rollout 정지 → 홈 복귀 → 서버 종료까지
  하고 나서 대기한다. "일단 띄워두고 나중에 확인"은 하지 않는다.
- rollout은 매번 새 코드일 수 있다 — 처음 실행은 모터 구동 전에 에러가 나는지부터
  확인한 뒤 진행한다(`/api/rollout/status`로 fault 확인).
- 그리퍼(6번 관절) 관련 안전값(`safety.joint_limits`, `arms.*.home_position`의
  6번째 값)은 각도를 정밀 실측한 게 아니라 넉넉한 폴백/전류 리밋에 기대고 있다.
  그리퍼가 걸린 동작(홈 복귀, rollout)을 처음 돌릴 때는 저속·근접 관찰 필수.
  자세한 내막은 README의 "`feature/integration` 병합 메모" 절 참고.
- `/api/emergency_stop`이 텔레오퍼·전 벨트·녹화를 한 번에 세운다(팔로워 토크는
  유지 — 풀면 중력 낙하로 오히려 위험하다). 뭔가 이상하면 이거 먼저.

## 환경 — Windows 전용, 순서에 민감한 함정들

- 서버 실행은 반드시 `.venv\Scripts\python.exe`(**Python 3.12**, 3.13 아님 — numpy
  wheel 호환 문제로 3.13에서 깨진다). `python run.py`를 VS Code 터미널에서 그냥
  치면 시스템 conda python(3.13, 프로젝트 의존성 없음)이 잡혀 즉시
  `ModuleNotFoundError`가 난다 — venv activate 여부와 실행 위치(레포 루트인지)부터
  확인할 것.
- **`opencv-python-headless`만 쓴다. `opencv-python`(GUI판)을 같이 깔지 말 것.**
  `requirements.txt`에 이유가 적혀 있다 — 두 패키지가 섞이면 site-packages의 cv2
  파일이 뒤엉켜 DLL 충돌이 실측됐다(2026-08-11). `cv2.imshow` 등 GUI 함수는 이
  프로젝트 어디서도 안 쓴다 — 카메라를 눈으로 확인하려면 대신
  `/api/stream/<name>`(MJPEG, 브라우저)이나 `scripts/find_cameras.py`(정적 썸네일
  저장)를 쓴다.
- 진입점(`run.py`, `scripts/to_lerobot.py`)마다 파일 맨 위에 아무 것도 안 하는
  `import torch`(실패해도 무시)가 있다 — cv2/pandas가 먼저 로드되면 torch DLL
  초기화가 깨지는 문제(`WinError 1114`)를 슬롯 선점으로 피하는 것. 새 진입점을
  추가하면 같은 패턴을 넣을 것. 지우면 안 된다.
- `lerobot` 패키지는 기본 `requirements.txt`에 없다(선택 설치,
  `requirements-lerobot.txt`) — 없어도 서버 자체와 teleop/녹화는 정상 동작해야
  한다. rollout(`/api/rollout/*`)만 지연 임포트로 lerobot을 찾는다.

## 모방학습 파이프라인 — 뭐가 웹이고 뭐가 CLI인지

| 단계 | 방법 |
|---|---|
| 에피소드 녹화(teleop) | ✅ 웹 대시보드 (`/api/recording/*`) |
| LeRobotDataset 변환 | ❌ CLI (`python scripts/to_lerobot.py`) |
| ACT 학습 | ❌ CLI (`lerobot-train`) — `/api/train/*`는 DEMO 모드(합성 곡선)만 실행되고, 실제 학습은 대시보드에서 트리거되지 않는다(`backend/core/act_trainer.py`의 `LeRobotTrainer.run`이 안내 메시지만 반환하도록 의도적으로 그렇게 짜여 있다). GPU를 몇 시간 점유하는 작업을 카메라·시리얼 포트 쥔 서버 프로세스 안에서 돌리지 않기 위해서다. |
| rollout(자율 실행) | ✅ 웹 대시보드 (`/api/rollout/*`) — 팔로워/카메라를 이미 서버가 쥐고 있어서 이 프로세스 안에서 돈다. |

## 지금 상태

`feature/integration` 브랜치가 최신 통합 지점(3axis 컨베이어 + 모방학습, 2026-08-14).
병합 세부·미검증 항목은 README의 "`feature/integration` 병합 메모" 절 참고. 그 절의
체크리스트가 이 저장소의 실질적인 다음 작업 목록이다.

## 테스트

```powershell
$env:OMX_SIMULATE="1"; .venv\Scripts\python.exe -m pytest -q
```

하드웨어 없이 도는 회귀 테스트. 코드를 바꿨으면 커밋 전에 반드시 돌릴 것.
