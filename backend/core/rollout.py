"""ACT 정책 기반 자율 실행 루프 (rollout, 신규).

`arm_controller.py`의 `TeleopLoop`과 구조가 완전히 대칭이다. 유일한 차이는
매 tick의 목표값을 "리더 읽기"가 아니라 "ACTPolicy 예측"으로 만든다는
것뿐이고, `follower.write()`를 그대로 호출하기 때문에 `SafetyGuard`의
소프트리밋·상대변화량 클램프가 자율 모드에서도 100% 동일하게 적용된다.
학생이 텔레옵용으로 만든 안전장치를 rollout에서도 재사용하는 게 핵심 의도다.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from ..config import SafetyConfig
from .arm_controller import ArmController
from .camera_manager import CameraManager

if TYPE_CHECKING:
    from .lerobot_bridge import ActInferenceRunner

logger = logging.getLogger(__name__)


class AutoRunLoop(threading.Thread):
    """학습된 ACT 정책으로 팔로워를 자율 제어하는 루프.

    watchdog: 예측/카메라 캡처가 timeout(safety.watchdog_timeout_ms)을 넘기면
    루프를 정지시키고 fault 플래그를 세운다. TeleopLoop와 동일한 정책이다
    — "무슨 이유로든 안전한 판단을 못 하게 되면 즉시 멈춘다".

    rate_hz 기본값이 TeleopLoop(50Hz)보다 훨씬 낮은 이유: ACT 추론(신경망
    forward pass)은 사람이 관절을 읽는 것보다 훨씬 느리다. CPU 추론 기준
    10Hz 정도가 현실적인 상한이라 이를 기본값으로 둔다. GPU가 있으면 올려도 된다.
    """

    def __init__(
        self,
        runner: "ActInferenceRunner",
        follower: ArmController,
        cameras: CameraManager,
        safety: SafetyConfig,
        rate_hz: int = 10,
    ) -> None:
        super().__init__(name="autorun", daemon=True)
        self._runner = runner
        self._follower = follower
        self._cameras = cameras
        self._safety = safety
        self._period = 1.0 / rate_hz
        self._stop_event = threading.Event()
        self.fault: str | None = None
        self.tick_count = 0

    def run(self) -> None:
        watchdog_sec = self._safety.watchdog_timeout_ms / 1000.0
        # rollout은 teleop보다 한 tick이 느리므로(추론 시간 포함), watchdog을
        # 너무 빡빡하게 잡으면 정상 상황에서도 오탐이 난다. 최소 여유를 둔다.
        watchdog_sec = max(watchdog_sec, self._period * 3)
        last_ok = time.monotonic()

        try:
            while not self._stop_event.is_set():
                tick_started = time.monotonic()
                try:
                    frames, _skew_ms = self._cameras.snapshot()
                    if not frames:
                        raise RuntimeError("카메라 프레임을 하나도 받지 못했습니다.")

                    follower_pos = self._follower.read()
                    target = self._runner.predict(
                        follower_joints=follower_pos,
                        frames={name: frame.image for name, frame in frames.items()},
                    )
                    # SafetyGuard.clamp()는 ArmController.write() 내부에서 자동 적용된다
                    # (TeleopLoop이 follower.write(leader_pos)를 쓰는 것과 동일한 경로).
                    self._follower.write(target)
                    self.tick_count += 1
                    last_ok = tick_started
                except Exception as exc:  # 추론/통신 실패는 루프를 죽이지 않고 watchdog에 맡긴다
                    logger.warning("autorun tick 실패: %s", exc)
                    if tick_started - last_ok > watchdog_sec:
                        self.fault = f"추론/통신이 {watchdog_sec * 1000:.0f}ms 이상 끊겼습니다: {exc}"
                        break

                elapsed = time.monotonic() - tick_started
                self._stop_event.wait(max(0.0, self._period - elapsed))
        finally:
            # 어떤 경로로 빠져나가든 팔로워는 현재 자세에서 멈춘다 (TeleopLoop와 동일).
            logger.info("autorun 루프 종료 (fault=%s, ticks=%d)", self.fault, self.tick_count)

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict:
        return {
            "running": self.is_alive(),
            "fault": self.fault,
            "tick_count": self.tick_count,
        }
