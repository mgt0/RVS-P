"""자동 사이클 — 시작 버튼 한 번으로 집기 → 벨트 이송 → 카메라 검사 → 분류를 한 번 돈다(제작보고서 3.1 플로우차트).

실물 로봇팔과 벨트를 구동하므로 안전 규칙이 기능보다 앞선다.
  * 모든 대기는 Event.wait(timeout) 이다. 단계 사이, 하드웨어 명령 직전, 대기 중에 중단을 확인하고
    중단됐으면 다음 단계·명령에 들어가지 않는다(sleep 으로 기다리면 정지 요청에 늦게 반응한다).
  * 중단·오류가 나면 벨트 정지와 autorun 정지만 한다. 게이트와 팔은 더 움직이지 않는다 — 멈추는 중에
    새 동작을 만들지 않는다. 게이트는 다음 사이클이 시작할 때 닫힘으로 되돌린다.
  * 사이클이 도는 동안 수동 조작(텔레옵·rollout·녹화·시연·수동 촬영·벨트 구동·홈 복귀)은 서버가 CycleBusyError 로
    거부한다. 정지는 언제나 허용하되, 사람이 어떤 벨트든 세우면 사이클도 함께 멈춘다(서보 게이트와 COM 포트를
    세 축이 공유하므로 사이클 중 수동 조작은 보수적으로 전부 막는다).
  * 비상 정지·서버 종료는 abort() 로 이 객체를 가장 먼저 끊는다. 플래그를 세운 뒤, 이미 점검을 통과해 나가는
    중인 벨트 명령이 끝나길 잠깐 기다리고 돌아온다(ABORT_WAIT_SEC). 비상 정지는 그 뒤에 벨트를 STOP·DISABLE 하므로
    DISABLE 뒤에 START 가 끼어 드라이버를 다시 켤 수 없다. 모터는 사이클 스레드가 깨어나 세운다(허브의 정지
    절차도 따로 같은 일을 한다).
  * 팔을 움직이는 autorun 시작은 허브가 자기 락 안에서 중단 여부를 다시 확인해 정지 뒤의 시작을 거부한다.

스레드: 사이클 한 번 = 스레드 하나. 상태 필드는 _lock 이 지킨다. 이 락을 쥔 채 허브를 부르지 않는다 —
허브 락(텔레옵·rollout 시작이 쥔다)과 순서가 꼬인다. 허브 락 → 이 락 순서만 허용한다.
벨트 명령은 _act 를 쥔 채 낸다(_do). 이 락을 쥔 채에도 허브를 부르지 않는다 — 서버 종료는 허브 락을 쥔 채
abort() 로 _act 를 기다리므로, 반대 순서면 서로 기다린다.

바스켓 개수는 메모리에만 있다. 서버를 다시 띄우면 0 으로 돌아간다(실물 바스켓은 그대로다).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ..config import PROJECT_ROOT, CycleConfig
from ..utils.errors import OmxError, ValidationError

logger = logging.getLogger(__name__)

STEPS = (("pick", "집기"), ("to_scan", "이송"), ("inspect", "검사"), ("sort", "분류"))
BASKET_KO = {"good": "양품", "defect": "불량"}
POLL_SEC = 0.1          # 집기 중 autorun 상태를 보는 간격(중단은 Event 가 즉시 깨우므로 반응 속도와 무관하다)
STOP_JOIN_SEC = 5.0     # stop() 이 스레드 종료를 기다리는 상한. 끊을 수 없는 호출(촬영·시리얼 명령)이 끝나길 기다린다
ABORT_WAIT_SEC = 1.0    # abort() 가 이미 나간 벨트 명령이 끝나길 기다리는 상한(명령 하나는 ms 단위). 넘기면 기다리지 않고 계속한다
# 비상 정지가 벨트 드라이버를 DISABLE 한 채다. 펌웨어 beginRun() 의 START 가 드라이버를 다시 켜므로, 사람이 명시적으로
# 리셋('드라이버 켜기' = /api/emergency_reset)해야 자동 운전을 재개하게 한다.
DRIVER_OFF = "비상 정지 뒤 벨트 드라이버가 꺼져 있습니다. 장비 제어에서 '드라이버 켜기'를 누르세요."


def _short(path: Path) -> str:
    """레포 안의 경로는 레포 기준 상대 경로로 줄인다(이유 문구가 상단바에서 잘리지 않게). 레포 밖은 그대로."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


class CycleBusyError(OmxError):
    """사이클이 도는 동안의 수동 조작 거부. 사이클이 모터를 쥐고 있어 끼어들면 안 된다."""

    status_code = 409
    code = "cycle_busy"

    def __init__(self, what: str) -> None:
        super().__init__(f"자동 사이클이 진행 중이라 {what}할 수 없습니다. 끝난 뒤(또는 사이클 정지 뒤) 다시 시도하세요.")


class _Stopped(Exception):
    """사이클을 끝내야 하는 사유(사용자 정지·비상 정지·서버 종료·벨트 수동 정지). 오류가 아니라 중단이다."""


def judge(stats: dict, defect_ratio: float) -> dict:
    """3D 재구성 stats 로 양품/불량을 가른다.

    신뢰도가 낮은 형상은 표면 이상(주황) 계산을 건너뛰어 flagged_ratio 가 0 이 된다 — 그 0 을 '깨끗함'으로 읽으면
    판정 불가 물체가 양품 라인으로 간다. 그래서 신뢰도를 먼저 본다.
    """
    quality = stats.get("quality") or {}
    level = quality.get("level") or "ok"
    flagged = float((stats.get("anomaly") or {}).get("flagged_ratio") or 0.0)
    if level == "low":
        why = " · ".join(quality.get("reasons") or []) or "형상을 믿을 수 없음"
        verdict, reason = "defect", f"신뢰도 낮음: {why}"
    elif flagged >= defect_ratio:
        verdict, reason = "defect", f"표면 이상 {flagged:.2%} (기준 {defect_ratio:.2%} 이상)"
    else:
        verdict, reason = "good", f"표면 이상 {flagged:.2%} (기준 {defect_ratio:.2%} 미만)"
    return {"verdict": verdict, "reason": reason, "flagged_ratio": flagged, "quality": level}


class CycleRunner:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._lock = threading.Lock()
        # 벨트 명령 하나(점검 + 전송)를 한 덩어리로 묶는 락. abort() 가 이걸 기다려 이미 나간 명령이 끝난 뒤에 돌아온다.
        # 재진입 락이다 — 사이클 스레드 안에서 abort() 를 부르는 경로(시험·오류 처리)가 스스로를 기다리지 않게.
        self._act = threading.RLock()
        self._abort = threading.Event()         # 모든 대기가 이걸로 한다 — 세워지면 기다리던 도중에도 즉시 깬다
        self._thread: threading.Thread | None = None
        self._run_cfg: CycleConfig | None = None    # 돌고 있는(마지막) 사이클의 설정. 시작할 때 고정한다
        self._baskets = {"good": 0, "defect": 0}
        self._completed = 0                     # 서버가 켜진 뒤 정상으로 끝난 사이클 수
        self._reset()

    def _reset(self) -> None:
        """상태 필드를 대기 상태로. 호출자가 _lock 을 쥐고 있어야 한다."""
        self._state = "idle"                    # idle | running | done | failed | aborted
        self._step: str | None = None
        self._steps = [{"id": i, "label": label, "state": "pending"} for i, label in STEPS]
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._message = "대기 중"
        self._error: str | None = None
        self._result: dict | None = None
        self._abort_reason: str | None = None

    # ------------------------------------------------------------------ 상태
    @property
    def active(self) -> bool:
        """사이클이 모터를 쥐고 있는 동안 True. 수동 조작 거부 가드가 읽는다."""
        with self._lock:
            return self._state == "running"

    def status(self, include_ready: bool = True) -> dict:
        """include_ready=False 면 시작 전 점검(ready)을 건너뛴다 — 알림처럼 상태·바스켓만 읽는 호출이 매번 장치를 훑지 않게."""
        with self._lock:
            running = self._state == "running"
            snap = {
                "state": self._state,
                "step": self._step,
                "steps": [dict(s) for s in self._steps],
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "message": self._message,
                "error": self._error,
                "result": dict(self._result) if self._result else None,
            }
            counts, completed = dict(self._baskets), self._completed
        capacity = self._hub.cfg.cycle.basket_capacity
        snap["baskets"] = {k: {"count": n, "capacity": capacity} for k, n in counts.items()}
        if include_ready:
            if running:
                snap["ready"] = {"ok": False, "reasons": ["사이클이 진행 중입니다."], "notes": []}
            else:
                try:
                    snap["ready"] = self.ready()
                except Exception as exc:  # noqa: BLE001 — 점검 실패가 메인 상태 폴링 전체를 죽이면 안 된다
                    logger.warning("사이클 준비 점검 실패: %s", exc)
                    snap["ready"] = {"ok": False, "reasons": [f"준비 상태를 확인하지 못했습니다: {exc}"], "notes": []}
        snap["completed"] = completed
        return snap

    def ready(self) -> dict:
        """지금 시작해도 되는지. 하드웨어를 건드리지 않고(포트도 열지 않는다) 현재 상태만 본다 — 상태 폴링이 매번 부른다.

        reasons 는 시작을 막는 이유, notes 는 막지는 않지만 알려 둘 것(벨트 포트가 닫혀 있으면 시작할 때 연다)이다.
        """
        hub, cfg = self._hub, self._hub.cfg.cycle
        reasons: list[str] = []
        notes: list[str] = []

        if cfg.pick_checkpoint:
            # ACT 정책에는 '끝났다'는 신호가 없어 집기가 늘 pick_timeout_sec 에 걸려 실패한다 — 시작하기 전에 막는다.
            reasons.append("ACT 체크포인트 집기는 아직 지원하지 않습니다(완료 신호 없음). pick_waypoints 를 쓰세요 "
                           "— config.yaml 의 cycle.pick_checkpoint 는 null 로 둡니다.")
        elif not cfg.pick_waypoints.exists():
            reasons.append(f"픽 동작 파일이 없습니다: {_short(cfg.pick_waypoints)}")

        belt = hub.conveyors.get(cfg.belt)
        if belt is None:
            reasons.append(f"사이클 벨트 '{cfg.belt}' 가 config.yaml 의 conveyors 에 없습니다(있는 벨트: {', '.join(hub.conveyors)}).")
        elif not belt.connected:
            # 서버를 켠 직후에는 포트가 닫혀 있다(첫 명령 때 연다). 막지 않는다 — start() 가 팔이 움직이기 전에 연다.
            # 다만 포트가 설정에 아예 없으면 열 수도 없다.
            port = belt.state.get("port")
            if port:
                notes.append(f"{belt.label} 포트({port})가 닫혀 있습니다. 시작하면 벨트 포트를 엽니다.")
            else:
                reasons.append(f"사이클 벨트 '{cfg.belt}'({belt.label})의 포트가 config.yaml 에 없습니다(conveyors.{cfg.belt}.port). 벨트를 구동할 수 없습니다.")
        elif belt.state.get("enabled") is False:
            reasons.append(DRIVER_OFF)
        # 벨트가 도는 채로 시작하면 팔이 움직이는 벨트에 물체를 올리고, 검사는 벨트가 서야 찍는다.
        running = [c.label for c in hub.conveyors.values() if c.state.get("running")]
        if running:
            reasons.append(f"벨트가 돌고 있습니다({', '.join(running)}). 먼저 정지하세요.")

        if not hub.follower.connected:
            reasons.append("팔로워 팔이 연결되어 있지 않습니다.")

        live = {c["name"] for c in hub.cameras.status_all() if c["connected"]}
        cams = sum(1 for name, cam in hub.cfg.cameras.items() if cam.view_dir and name in live)
        if cams < 2:
            reasons.append(f"재구성용 카메라가 2대 미만입니다(신호가 있는 카메라 {cams}대).")

        if hub.teleop_status["running"]:
            reasons.append("텔레오퍼레이션이 실행 중입니다. 먼저 정지하세요.")
        if hub.autorun_status.get("running"):
            reasons.append("자율 실행(rollout)이 실행 중입니다. 먼저 정지하세요.")
        if hub.recorder.state.value != "IDLE":
            reasons.append("녹화가 진행 중입니다. 먼저 정지하세요.")
        if hub.demo.active:
            reasons.append("시연 세션이 진행 중입니다. 먼저 끝내세요.")

        with self._lock:
            counts = dict(self._baskets)
        for key, n in counts.items():
            if n >= cfg.basket_capacity:
                reasons.append(f"{BASKET_KO[key]} 바스켓이 가득 찼습니다({n}/{cfg.basket_capacity}). 비운 뒤 '비웠습니다'를 누르세요.")
        return {"ok": not reasons, "reasons": reasons, "notes": notes}

    # ------------------------------------------------------------------ 제어(API 스레드)
    def start(self) -> dict:
        hub = self._hub
        # 허브 락 안에서 점검과 상태 전환을 한다. 텔레옵·rollout 시작도 같은 락 안에서 사이클이 도는지 보므로,
        # 점검을 통과한 직후 다른 쪽이 끼어들어 팔로워를 두 루프가 쥐는 일이 없다.
        with hub._lock:
            if self.active:
                raise ValidationError("이미 사이클이 진행 중입니다.")
            ready = self.ready()
            if not ready["ok"]:
                raise ValidationError("사이클을 시작할 수 없습니다. " + " ".join(ready["reasons"]))
            cfg = hub.cfg.cycle
            self._open_belt(cfg)        # 상태를 바꾸기 전에 — 실패하면 아무것도 시작하지 않고 팔도 움직이지 않는다
            with self._lock:
                self._reset()
                self._state, self._started_at = "running", time.time()
                self._message = "시작합니다."
                self._run_cfg = cfg
                self._abort.clear()
            self._thread = threading.Thread(target=self._run, args=(cfg,), name="cycle", daemon=True)
            self._thread.start()
        logger.info("자동 사이클 시작")
        return self.status()

    def _open_belt(self, cfg: CycleConfig) -> None:
        """점검을 통과한 뒤, 팔이 움직이기 전에 사이클 벨트 포트를 연다.

        서버를 켠 직후에는 포트가 닫혀 있다(첫 명령 때 연다). 사이클의 첫 벨트 명령이 열게 두면 열기에 실패했을 때
        이미 시작한 뒤라 사이클이 실패로 끝난다 — 시작 전에 열어 보고 안 되면 거부한다. 연 뒤 펌웨어의 실제
        드라이버 상태를 다시 본다: 포트가 닫혀 있던 동안(서버를 다시 켜는 사이 포함) 꺼진 드라이버는 ready() 가 알 수 없었다.
        허브 락을 쥔 채 호출된다(점검과 시작을 한 덩어리로 묶으려고) — 포트 열기는 보통 수십 ms 다.
        """
        belt = self._belt(cfg)
        try:
            belt.connect()
            enabled = belt.status().get("enabled")
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"{belt.label} 포트를 열지 못해 사이클을 시작하지 않았습니다(팔은 움직이지 않았습니다): {exc}") from exc
        if enabled is False:
            raise ValidationError(f"사이클을 시작할 수 없습니다. {DRIVER_OFF}")

    def stop(self) -> dict:
        """사용자 정지. 모터를 지금 세우고 스레드가 끝나길 기다려 state 가 aborted 가 된 채 돌려준다."""
        if self.active:
            self.abort("사용자가 사이클을 정지했습니다.")
            self._halt()        # 스레드가 깨기 전에도 모터는 지금 세운다
            thread = self._thread
            if thread is not None:
                thread.join(STOP_JOIN_SEC)
        return self.status()

    @property
    def aborted(self) -> bool:
        """중단 요청이 서 있는지(비상 정지·서버 종료·사용자 정지·벨트 수동 정지). 허브가 팔을 움직이기 직전에 자기 락 안에서 다시 본다."""
        return self._abort.is_set()

    def abort(self, reason: str) -> None:
        """허브(비상 정지·서버 종료)와 벨트 수동 정지용. 중단 플래그를 세운 뒤, 이미 점검을 통과해 나가는 중인 벨트 명령이
        끝나길 잠깐(ABORT_WAIT_SEC) 기다리고 돌아온다 — 비상 정지는 이 호출이 돌아온 뒤에 벨트를 STOP·DISABLE 하므로
        DISABLE 뒤에 START 가 끼어 드라이버를 다시 켜는 일이 없다. 모터를 세우는 건 아니다(스레드와 허브가 한다).

        이미 중단된 사이클에 또 불러도 같은 대기를 한다 — 먼저 부른 쪽이 아직 기다리는 중일 수 있다.
        """
        with self._lock:
            if self._state != "running":
                return
            self._abort_reason = self._abort_reason or reason       # 먼저 건 사유가 이긴다
        self._abort.set()
        if self._act.acquire(timeout=ABORT_WAIT_SEC):
            self._act.release()
        else:
            logger.warning("사이클 중단: 진행 중인 벨트 명령이 %.1f초 안에 끝나지 않았습니다 — 기다리지 않고 계속합니다.", ABORT_WAIT_SEC)

    def empty_baskets(self, which) -> dict:
        if which not in ("good", "defect", "all"):
            raise ValidationError(f"basket 은 'good', 'defect', 'all' 중 하나여야 합니다: {which!r}")
        with self._lock:
            for key in self._baskets if which == "all" else (which,):
                self._baskets[key] = 0
        return self.status()

    # ------------------------------------------------------------------ 사이클 스레드
    def _run(self, cfg: CycleConfig) -> None:
        # 어떤 경로로 빠져도 finally 가 상태를 끝낸다 — running 으로 굳으면 서버가 수동 조작을 영영 거부한다.
        state, error = "failed", "사이클 스레드가 예기치 않게 끝났습니다."
        try:
            self._run_step("pick", "집기: 로봇팔이 물체를 집어 벨트에 놓는 중입니다.", lambda: self._pick(cfg))
            self._run_step("to_scan", "이송: 벨트가 물체를 검사 자리로 옮기는 중입니다.",
                           lambda: self._belt_run(cfg, cfg.to_scan_sec))
            verdict = self._run_step("inspect", "검사: 촬영하고 3D 형상으로 판정하는 중입니다.", lambda: self._inspect(cfg))
            self._run_step("sort", "분류: 벨트가 물체를 보내는 중입니다.", lambda: self._sort(cfg, verdict))
            state, error = "done", None
        except _Stopped as stop:
            state, error = "aborted", str(stop)
        except Exception as exc:  # noqa: BLE001
            state, error = "failed", str(exc)
            # 우리가 던진 오류·도메인 오류는 메시지로 충분하다. 예상 밖 예외만 원인의 traceback 을 남긴다.
            cause = exc.__cause__ or exc
            logger.warning("자동 사이클 실패: %s", exc, exc_info=None if isinstance(cause, (OmxError, RuntimeError)) else cause)
        finally:
            self._finish(state, error)

    def _finish(self, state: str, error: str | None) -> None:
        if state != "done":
            # 중단·오류: 이것만 한다. 게이트·팔은 건드리지 않는다. 상태를 바꾸기 전에 세운다 —
            # 그동안 state 가 running 이라 서버가 수동 조작을 계속 거부한다.
            self._halt()
        with self._lock:
            for s in self._steps:
                if s["state"] == "pending":
                    s["state"] = "skipped"
            self._state, self._step, self._error = state, None, error
            self._ended_at = time.time()
            if state == "done":
                self._completed += 1
                self._message = f"완료: {BASKET_KO[self._result['verdict']]}"
            else:
                self._message = error
        if state == "done":
            logger.info("자동 사이클 완료: %s", self._result["verdict"])
        elif state == "aborted":
            logger.warning("자동 사이클 중단: %s", error)

    def _halt(self) -> None:
        """벨트 정지 + autorun 정지. 중단·오류 때 하는 일은 이것뿐이다. 각각 따로 시도한다 — 하나가 실패해도 다음은 간다.

        벨트가 먼저다. autorun 정지는 허브 락을 기다리는데, 서버 종료는 그 락을 쥔 채 팔을 홈으로 보낸다 — 벨트 정지가
        그 뒤에 서면 그 동안 벨트가 돈다. 서버 종료가 끝나 포트를 놓은 뒤에는 벨트를 건드리지 않는다 — 정지 명령이
        끊긴 포트를 다시 여는 것을 막는다(포트가 닫혔으면 서버가 내려가는 중이다).
        """
        cfg = self._run_cfg
        belt = self._hub.conveyors.get(cfg.belt) if cfg else None
        actions = []
        if belt is not None and not self._hub.closed:
            actions.append(("벨트 정지", belt.stop))
        actions.append(("자율 실행 정지", self._hub.stop_autorun))
        for what, action in actions:
            try:
                action()
            except Exception:  # noqa: BLE001
                logger.exception("사이클 중단: %s 실패", what)

    # ------------------------------------------------------------------ 단계 공통
    def _run_step(self, step_id: str, message: str, work):
        self._check()       # 단계에 들어가기 전에 중단을 본다
        label = self._mark(step_id, "active", message)
        try:
            out = work()
        except _Stopped:
            self._mark(step_id, "failed")
            raise
        except Exception as exc:
            self._mark(step_id, "failed")
            self._check()   # 중단이 걸린 뒤의 오류는 중단이 낳은 것이다(예: 정지가 autorun 을 치웠다) — 중단으로 끝낸다
            raise RuntimeError(f"{label} 단계 실패: {exc}") from exc
        self._mark(step_id, "done")
        return out

    def _mark(self, step_id: str, state: str, message: str | None = None) -> str:
        with self._lock:
            label = ""
            for s in self._steps:
                if s["id"] == step_id:
                    s["state"], label = state, s["label"]
            self._step = step_id if state == "active" else None
            if message:
                self._message = message
        return label

    def _say(self, message: str) -> None:
        with self._lock:
            self._message = message

    def _check(self) -> None:
        if self._abort.is_set():
            with self._lock:
                raise _Stopped(self._abort_reason or "사이클이 중단되었습니다.")

    def _wait(self, sec: float) -> None:
        """모든 대기는 이것만 쓴다. 중단되면 기다리던 도중에도 즉시 깨어 _Stopped 를 던진다."""
        self._abort.wait(sec)
        self._check()

    def _do(self, action, *args, **kwargs):
        """벨트 명령은 전부 이걸 거친다 — 중단된 뒤에는 새 명령을 내지 않는다.

        점검과 명령을 _act 한 덩어리로 묶는다. 락 없이 점검만 하면, 점검을 통과한 뒤 비상 정지가 벨트를 DISABLE 하고 난
        다음에 이미 나가던 START 가 끼어든다 — 펌웨어 beginRun() 은 START 를 받으면 드라이버를 다시 켜서 비상 정지가
        무효가 된다. abort() 가 이 락을 기다려 주므로(이미 나간 명령이 끝난 뒤에 돌아온다) 순서가 보장된다.
        이 락을 쥔 채 허브를 부르지 않는다(모듈 docstring). 팔을 움직이는 autorun 시작과 촬영은 이걸 거치지 않는다.
        """
        with self._act:
            self._check()
            return action(*args, **kwargs)

    def _belt(self, cfg: CycleConfig):
        try:
            return self._hub.conveyors[cfg.belt]
        except KeyError:
            raise ValidationError(f"사이클 벨트 '{cfg.belt}' 가 config.yaml 의 conveyors 에 없습니다.") from None

    # ------------------------------------------------------------------ 단계
    def _pick(self, cfg: CycleConfig) -> None:
        hub, belt = self._hub, self._belt(cfg)
        # 방향·속도는 사이클이 명시적으로 정한다(직전에 사람이 어떻게 만졌든 상관없이). 벨트가 서 있을 때만 바꾼다.
        self._do(belt.set_direction, cfg.belt_dir)
        self._do(belt.set_speed_rpm, cfg.belt_rpm)
        self._do(belt.set_servo_deg, cfg.gate_close_deg)     # 게이트는 닫힌 채로 물체를 받는다
        # 팔을 움직이는 start_autorun 은 _do(_act)를 거치지 않는다 — 허브 락을 기다리므로 abort() 와 락 순서가 꼬인다.
        # 대신 허브가 자기 락 안에서 중단 여부를 다시 확인해, 이 점검을 통과한 뒤에 비상 정지가 끝났다면 거부한다.
        self._check()
        if cfg.pick_checkpoint:
            hub.start_autorun(checkpoint=str(cfg.pick_checkpoint), by_cycle=True)
        else:
            hub.start_autorun(waypoints=str(cfg.pick_waypoints), speed=cfg.pick_speed, by_cycle=True)

        deadline = time.monotonic() + cfg.pick_timeout_sec
        while True:
            status = hub.autorun_status
            if status.get("fault"):
                raise RuntimeError(f"자율 실행이 안전 정지했습니다: {status['fault']}")
            if not status.get("running"):
                if status.get("done"):
                    break
                raise RuntimeError("자율 실행이 끝나기 전에 멈췄습니다.")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"집기가 {cfg.pick_timeout_sec:g}초 안에 끝나지 않았습니다.")
            self._wait(POLL_SEC)
        hub.stop_autorun()      # 끝난 루프를 치운다. 팔을 움직이는 일이 아니다
        # 궤적이 끝난 직후에는 그리퍼가 물체를 놓는 중일 수 있다 — 벨트를 바로 돌리면 그 전에 물체가 끌려간다. 기다린다.
        self._wait(cfg.pick_settle_sec)

    def _belt_run(self, cfg: CycleConfig, sec: float) -> None:
        belt = self._belt(cfg)
        self._do(belt.start)
        self._wait(sec)
        belt.stop()     # 중단 확인 없이 — 정지는 늘 허용한다. 중단됐다면 위 _wait 에서 이미 빠져나갔다

    def _inspect(self, cfg: CycleConfig) -> str:
        # api → core 방향이 아닌 역방향 임포트라 지연한다. 계약상 /api/scan/capture 와 같은 함수를 써야 한다.
        from ..api import analysis

        # 진동이 가라앉기를 기다리는 일은 여기서 한다 — 촬영 함수 안의 settle 은 끊을 수 없는 sleep 이라 0 으로 넘긴다.
        # 촬영 함수는 그래도 벨트가 서 있는지 확인하고 거부한다(정지 확인은 그대로다).
        self._wait(cfg.settle_sec)      # 끝에서 중단을 확인한다
        # 촬영은 모터를 움직이지 않는다 — _do(_act)를 쥐지 않는다. 재구성이 몇 초 걸려 abort() 가 그만큼 막히기 때문이다.
        capture = analysis.run_scan_capture(self._hub, 0.0, cfg.resolution)
        judged = judge(capture["mesh"]["stats"], cfg.defect_ratio)
        with self._lock:
            self._result = {"verdict": judged["verdict"], "reason": judged["reason"],
                            "capture_id": capture["capture_id"],
                            "flagged_ratio": judged["flagged_ratio"], "quality": judged["quality"]}
        return judged["verdict"]

    def _sort(self, cfg: CycleConfig, verdict: str) -> None:
        belt = self._belt(cfg)
        if verdict == "good":
            self._say("분류: 양품 바스켓 쪽으로 벨트를 돌립니다.")
            self._belt_run(cfg, cfg.good_run_sec)
        else:
            self._say("분류: 분기점까지 벨트를 돌립니다.")
            self._belt_run(cfg, cfg.defect_to_branch_sec)
            self._say("분류: 게이트를 열어 불량 쪽으로 보냅니다.")
            self._do(belt.set_servo_deg, cfg.gate_open_deg)
            self._wait(cfg.gate_hold_sec)
            self._do(belt.set_servo_deg, cfg.gate_close_deg)
        with self._lock:    # 물리적 분류가 끝까지 간 뒤에만 센다(위에서 중단되면 여기까지 오지 않는다)
            self._baskets[verdict] += 1
