"""시연 녹화 세션 — 녹화 → 리셋 → 다음 에피소드를 서버 스레드가 반복한다(LeRobot record 루프).

왜 서버가 도는가
---------------
시간 제한을 브라우저 타이머에 맡기면 탭이 백그라운드로 가거나 노트북이 잠들 때 타이머가 늦어지거나
멈춰, 녹화가 끝나지 않은 채 디스크를 먹는다. 비상 정지도 서버가 직접 세션을 끊어야 새지 않는다.

이 모듈은 팔을 움직이지 않는다. 사람이 리더를 잡고 시연하고(텔레옵), 세션은 Recorder 의
시작·정지를 타이머와 사람의 입력(next/redo/end)에 맞춰 부를 뿐이다.

상태: IDLE → RECORDING ⇄ RESET → IDLE
  start : 첫 에피소드를 바로 녹화한다. 가드·입력 오류는 스레드를 띄우기 전에 예외로 돌려준다.
  next  : RECORDING 이면 지금 저장하고 RESET 으로, RESET 이면 남은 리셋을 건너뛰고 다음 에피소드 시작.
  redo  : RECORDING 에서만. 지금 에피소드를 버리고 리셋 뒤 같은 ID 로 다시 녹화한다.
  end   : RECORDING 이면 저장하고 종료(LeRobot 의 Esc). RESET 이면 그냥 종료.
  abort : 허브가 부른다(비상 정지·서버 종료). 세션 스레드만 세운다 — 녹화기는 허브가 이어서 중단한다.
  (시간이 다 되면 next 와 같다 — 저장하고 RESET.)

스레드 소유 규칙: 첫 start() 이후 Recorder 의 start/stop 은 세션 스레드만 부른다. 요청(next/redo/end)은
API 스레드가 슬롯에 적어 두고 세션 스레드가 처리한 뒤 알린다. abort() 는 락을 짧게만 잡고 바로
돌아온다 — 비상 정지가 이 모듈 때문에 지연되면 안 된다.

녹화기를 누가 중단하나: 허브가 부른 abort 때는 허브다(emergency_stop 이 뒤이어 recorder.abort 를 하고
'recording: aborted' 로 보고한다 — 여기서 먼저 중단하면 그 보고가 'idle' 로 바뀐다). 그 밖의 사유
(텔레옵 정지·팔 끊김·녹화기 중단·예외)는 허브가 모르는 일이라 세션 스레드가 직접 중단한다.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum

from ..utils.errors import RecordingStateError, ValidationError
from .cycle import CycleBusyError
from .storage import validate_id

logger = logging.getLogger(__name__)

POLL_SEC = 0.1              # 세션 스레드가 중단 사유·텔레옵·녹화기 상태를 확인하는 간격
REQUEST_TIMEOUT_SEC = 45.0  # next/redo/end 응답 대기 상한. Recorder.stop() 의 join 상한(5+30초)보다 길다
MAX_TASK_LEN = 200
# Recorder.start 가 라벨을 요구해 채우는 자리표시다. 분류 라벨이 아니다 — 그 뜻은 kind=demo 가 갖는다.
# 이 값으로 classification/ 을 만지지 않도록 classify=False 로 시작한다.
PLACEHOLDER_LABEL = "good"


class DemoState(str, Enum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    RESET = "RESET"


class _Abort(Exception):
    """세션을 끝내야 하는 사유. external=True 는 허브가 abort() 로 건 것(녹화기 중단도 허브 몫)."""

    def __init__(self, reason: str, *, external: bool = False) -> None:
        super().__init__(reason)
        self.external = external


def _seconds(value, name: str, lo: int, hi: int):
    # bool 은 int 의 하위 타입이라 따로 거른다. NaN 은 범위 비교가 거짓이라 함께 걸러진다.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ValidationError(f"{name} 는 {lo}~{hi} 사이의 숫자(초)여야 합니다: {value!r}")
    return value


def _task(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= MAX_TASK_LEN:
        raise ValidationError(f"task 는 1~{MAX_TASK_LEN}자의 문장이어야 합니다.")
    return text


def _begin(recorder, episode_id: str, object_id: str, task: str) -> None:
    recorder.start(
        episode_id, object_id, PLACEHOLDER_LABEL, None,
        extra={"kind": "demo", "task": task}, classify=False,
    )


class DemoSession:
    def __init__(self, hub) -> None:
        self._hub = hub                       # recorder, teleop_status, leader/follower, autorun_status, cfg
        self._cv = threading.Condition()      # 아래 필드 전부를 지키는 락 겸 대기 지점
        self._state = DemoState.IDLE
        self._starting = False                # start() 진행 중(동시 시작·수동 녹화 시작을 막는다)
        self._object_id: str | None = None
        self._task: str | None = None
        self._episode_sec = None
        self._reset_sec = None
        self._episode_id: str | None = None   # 녹화 중이거나 다음에 녹화할 에피소드
        self._phase_started = 0.0
        self._phase_total = 0
        self._saved = 0
        self._last_saved: str | None = None
        self._fault: str | None = None        # 마지막 세션의 비정상 종료 사유. 다음 start() 가 지운다
        self._abort_reason: str | None = None
        self._request: str | None = None      # 처리 대기 중인 next/redo/end
        self._claimed = False                 # 세션 스레드가 그 요청을 집어 처리 중이다(요청자가 시간 초과로 거두면 안 된다)
        self._reply: Exception | None = None  # 그 요청을 거절한 사유

    # ------------------------------------------------------------------ 상태
    @property
    def active(self) -> bool:
        with self._cv:
            return self._state != DemoState.IDLE or self._starting

    def status(self) -> dict:
        with self._cv:
            active = self._state != DemoState.IDLE
            total = self._phase_total if active else 0
            elapsed = min(time.monotonic() - self._phase_started, total) if active else 0.0
            return {
                "state": self._state.value,
                "episode_id": self._episode_id if active else None,
                "episode_sec": self._episode_sec,
                "reset_sec": self._reset_sec,
                "phase_elapsed_sec": round(elapsed, 1),
                "phase_total_sec": total,
                "saved": self._saved,
                "task": self._task,
                "object_id": self._object_id,
                "last_saved": self._last_saved,
                "fault": self._fault,
            }

    # ------------------------------------------------------------------ 제어(API 스레드)
    def start(self, object_id, task, episode_sec=20, reset_sec=10) -> dict:
        object_id = validate_id(object_id, "object_id")
        task = _task(task)
        episode_sec = _seconds(episode_sec, "episode_sec", 1, 600)
        reset_sec = _seconds(reset_sec, "reset_sec", 0, 120)

        with self._cv:
            if self._state != DemoState.IDLE or self._starting:
                raise RecordingStateError("이미 시연 세션이 진행 중입니다.")
            self._starting = True
        try:
            self._guard()
            recorder = self._hub.recorder
            episode_id = recorder.index.next_episode_id()
            _begin(recorder, episode_id, object_id, task)    # 실패하면 세션도 스레드도 만들지 않는다
            with self._cv:
                self._object_id, self._task = object_id, task
                self._episode_sec, self._reset_sec = episode_sec, reset_sec
                self._saved, self._last_saved, self._fault = 0, None, None
                self._abort_reason = self._request = self._reply = None
                self._claimed = False
                self._enter(DemoState.RECORDING, episode_sec, episode_id)
            threading.Thread(target=self._run, name="demo-session", daemon=True).start()
        finally:
            with self._cv:
                self._starting = False
        logger.info("시연 세션 시작: object=%s episode=%s (%s초 녹화 / %s초 리셋)", object_id, episode_id, episode_sec, reset_sec)
        return self.status()

    def next(self) -> dict:
        return self._ask("next")

    def redo(self) -> dict:
        return self._ask("redo")

    def end(self) -> dict:
        return self._ask("end")

    def abort(self, reason: str) -> None:
        """허브(비상 정지·서버 종료)용. 기다리지 않고 녹화기도 건드리지 않는다 — 세션 스레드가 멈추고,
        녹화기는 허브가 이어서 중단한다."""
        with self._cv:
            if self._state == DemoState.IDLE or self._abort_reason:
                return
            self._abort_reason = reason
            self._cv.notify_all()

    def _guard(self) -> None:
        hub = self._hub
        if hub.cycle.active:    # 텔레옵 가드보다 먼저 — 사이클 중에는 이게 진짜 이유다
            raise CycleBusyError("시연 세션을 시작")
        if not hub.teleop_status["running"]:
            raise RecordingStateError("텔레옵이 꺼져 있습니다. 장비 제어에서 텔레옵을 먼저 켜세요.")
        if not (hub.leader.connected and hub.follower.connected):
            raise RecordingStateError("리더·팔로워 팔이 모두 연결되어야 시연 세션을 시작할 수 있습니다.")
        if hub.recorder.state.value != "IDLE":
            raise RecordingStateError("녹화가 이미 진행 중입니다. 먼저 정지하세요.")
        if hub.autorun_status.get("running"):
            raise RecordingStateError("자율 실행(rollout)이 실행 중입니다. 먼저 정지하세요.")

    def _ask(self, name: str) -> dict:
        with self._cv:
            if self._state == DemoState.IDLE:
                raise RecordingStateError("진행 중인 시연 세션이 없습니다.")
            if name == "redo" and self._state != DemoState.RECORDING:
                raise RecordingStateError(
                    "리셋 중에는 버릴 에피소드가 없습니다. 이미 저장된 에피소드는 에피소드 표에서 삭제하세요."
                )
            if self._request:
                raise RecordingStateError("이전 요청을 처리하는 중입니다. 잠시 뒤 다시 시도하세요.")
            self._request, self._reply, self._claimed = name, None, False   # 시간 초과로 버려진 지난 거절 사유가 남지 않게 비운다
            self._cv.notify_all()
            if not self._cv.wait_for(lambda: self._request is None, REQUEST_TIMEOUT_SEC):
                if not self._claimed:
                    # 세션 스레드가 아직 집지 않았다 — 거두지 않으면 나중에 그 오래된 요청을 실행하고(예: end 로 세션이
                    # 끝난다), 그동안 새 요청은 전부 '이전 요청 처리 중' 으로 거부된다. 이미 집어 갔으면 전환이 끝나야
                    # 풀리므로 그대로 둔다.
                    self._request = None
                raise RecordingStateError("시연 세션이 응답하지 않습니다. 서버 로그를 확인하세요.")
            error, self._reply = self._reply, None
            if error:
                raise error
            if self._state == DemoState.IDLE and self._fault:    # 처리 중에 세션이 끊겼다
                raise RecordingStateError(f"시연 세션이 종료되었습니다: {self._fault}")
        return self.status()

    # ------------------------------------------------------------------ 세션 스레드
    def _run(self) -> None:
        reason: str | None = None   # None = end 로 정상 종료
        external = False
        try:
            while True:
                ev = self._wait({"next", "redo", "end"})          # timeout 이면 저장하고 리셋
                self._finish(self._episode_id, discard=(ev == "redo"))
                if ev == "end":
                    break
                # 저장했으면 다음 번호, 버렸으면 같은 번호(폴더가 지워졌다).
                episode_id = self._episode_id if ev == "redo" else self._hub.recorder.index.next_episode_id()
                if self._reset_sec > 0:
                    self._enter(DemoState.RESET, self._reset_sec, episode_id)
                    self._answer(ev)
                    ev = self._wait({"next", "end"})
                    if ev == "end":
                        break
                self._begin_episode(episode_id)
                self._answer(ev)
        except _Abort as stop:
            reason, external = str(stop), stop.external
        except Exception as exc:  # noqa: BLE001
            logger.exception("시연 세션 오류")
            reason = self._abort_reason      # 허브가 이미 중단을 걸었다면 그 사유가 먼저다(중단이 낳은 오류일 수 있다)
            external = reason is not None
            reason = reason or f"시연 세션 오류: {exc}"
        self._close(reason, external)

    def _begin_episode(self, episode_id: str) -> None:
        self._raise_if_aborted()
        _begin(self._hub.recorder, episode_id, self._object_id, self._task)
        self._enter(DemoState.RECORDING, self._episode_sec, episode_id)
        with self._cv:
            reason = self._abort_reason
        if reason:
            # 시작하는 사이에 허브가 중단을 걸었다. 허브의 녹화기 정리는 이미 지나갔을 수 있어 방금 시작한
            # 이 녹화만큼은 직접 버린다(허브가 아직이었다면 이쪽이 먼저 중단해 보고가 'idle' 이 될 뿐이다).
            self._hub.recorder.abort(reason)
            raise _Abort(reason, external=True)

    def _wait(self, allowed: set[str]) -> str:
        """현재 구간이 끝나거나(timeout) 사람이 요청할 때까지 기다린다.

        기다리는 동안 중단 사유·텔레옵·팔·녹화기 상태를 계속 본다. 하나라도 어긋나면 _Abort.
        """
        while True:
            with self._cv:
                self._raise_if_aborted()
                request = self._request
                if request in allowed:
                    self._claimed = True    # 이 요청은 이제 이 스레드가 처리한다 — _answer 가 끝났음을 알린다
                    return request
                if request:   # 구간이 막 바뀌어 더는 못 쓰는 요청(예: 리셋이 시작된 직후의 redo)
                    self._answer(request, RecordingStateError(f"지금 구간에서는 '{request}' 를 쓸 수 없습니다."))
                remaining = self._phase_started + self._phase_total - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                self._cv.wait(min(remaining, POLL_SEC))
            self._check_hardware()

    def _check_hardware(self) -> None:
        hub = self._hub
        teleop = hub.teleop_status
        if not teleop["running"]:
            detail = f": {teleop['fault']}" if teleop["fault"] else ""
            raise self._abort_for(f"텔레옵이 정지되어 세션을 종료했습니다{detail}")
        if not (hub.leader.connected and hub.follower.connected):
            raise self._abort_for("리더·팔로워 연결이 끊겨 세션을 종료했습니다")
        if self._state == DemoState.RECORDING and hub.recorder.state.value != "RECORDING":
            error = hub.recorder.status()["last_error"]
            raise self._abort_for("녹화가 중단되어 세션을 종료했습니다" + (f": {error}" if error else ""))

    def _abort_for(self, reason: str) -> _Abort:
        """내부 사유로 끝내기 전에 허브의 중단이 이미 걸렸는지 본다. 비상 정지는 중단을 먼저 걸고 텔레옵을
        세우므로, 멈춘 텔레옵이 보인다면 그 중단도 보인다 — 그쪽이 진짜 원인이다."""
        self._raise_if_aborted()
        return _Abort(reason)

    def _finish(self, episode_id: str, discard: bool) -> None:
        """진행 중인 에피소드를 저장하거나(discard=False) 버린다."""
        self._hub.recorder.stop(discard=discard)
        if discard:
            return
        # stop() 이 돌아왔다고 저장됐다고 단정하지 않는다 — 저장 중에 비상 정지가 오면
        # Recorder.abort() 가 폴더를 지운다. metadata.json 이 있어야 저장된 것이다(storage.py 레이아웃).
        meta = self._hub.cfg.recording.root / "episodes" / f"episode_{episode_id}" / "metadata.json"
        if not meta.exists():
            self._raise_if_aborted()
            raise RuntimeError(f"에피소드 {episode_id} 가 저장되지 않았습니다.")
        with self._cv:
            self._saved += 1
            self._last_saved = episode_id

    def _raise_if_aborted(self) -> None:
        with self._cv:
            if self._abort_reason:
                raise _Abort(self._abort_reason, external=True)

    def _enter(self, state: DemoState, total, episode_id: str) -> None:
        with self._cv:
            self._state, self._phase_total, self._episode_id = state, total, episode_id
            self._phase_started = time.monotonic()

    def _answer(self, ev: str, error: Exception | None = None) -> None:
        """요청이 만든 전환이 끝났음을 요청자에게 알린다.

        시간 초과로 생긴 전환에는 답할 요청이 없다. 다만 저장하는 동안(_finish 가 도는 몇 초, state 는 아직 이전 구간)
        들어온 요청이 있을 수 있다 — 이미 지나간 구간의 요청이라 '이미 넘어갔다'고 거절해 비운다. 비우지 않으면
        다음 구간이 그 요청을 자기 것으로 집어(예: 리셋 중 next = 리셋 건너뛰기) 엉뚱한 전환을 만든다.
        """
        with self._cv:
            if ev == "timeout":
                if not self._request:
                    return
                error = RecordingStateError("시간이 다 되어 저장하고 이미 다음 구간으로 넘어갔습니다. 지금 구간에서 다시 시도하세요.")
            self._reply, self._request, self._claimed = error, None, False
            self._cv.notify_all()

    def _close(self, reason: str | None, external: bool = False) -> None:
        recorder = self._hub.recorder
        # 사유가 있으면 진행 중이던 에피소드는 버린다. 허브가 건 중단(external)은 허브가 녹화기도 정리하므로
        # 여기서 먼저 중단하지 않는다(비상 정지의 'recording: aborted' 보고를 지킨다).
        if reason and not external and recorder.state.value != "IDLE":
            try:
                recorder.abort(reason)
            except Exception:  # noqa: BLE001
                logger.exception("시연 세션: 녹화 중단 실패")
        with self._cv:
            self._state, self._episode_id, self._fault = DemoState.IDLE, None, reason
            self._request, self._claimed = None, False    # 대기 중인 요청자를 깨운다(end 는 정상, 그 밖에는 _fault 로 오류)
            self._cv.notify_all()
            saved = self._saved
        if reason:
            logger.warning("시연 세션 중단: %s (저장 %d개)", reason, saved)
        else:
            logger.info("시연 세션 종료: 저장 %d개", saved)
