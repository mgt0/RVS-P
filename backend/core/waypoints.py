"""웨이포인트 파일 재생 — 학습 없이 시연을 그대로 되돌린다.

`scripts/to_waypoints.py` 가 뽑은 JSON을 시간축 선형보간으로 따라간다.
`ActInferenceRunner` 와 같은 `predict()` 시그니처를 가지므로 `AutoRunLoop`
(watchdog · SafetyGuard 클램프 · 정지 경로)를 그대로 재사용한다 — 재생 전용
제어 루프를 따로 만들지 않는 게 핵심이다.

카메라를 보지 않으므로 `needs_frames = False`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..utils.errors import ValidationError

logger = logging.getLogger(__name__)


class WaypointRunner:
    """시간 기반 웨이포인트 보간기.

    도착 대기(lag gating): 팔이 목표에서 `lag_tol_deg` 이상 뒤처져 있으면
    재생 시계를 멈춘다. SafetyGuard 가 tick 당 변화량을 클램프하기 때문에
    첫 웨이포인트가 멀면 팔이 따라오기 전에 시계만 흘러 궤적을 통째로
    건너뛴다 — 그걸 막는다.
    """

    needs_frames = False

    def __init__(self, path: str | Path, speed: float = 1.0, lag_tol_deg: float = 5.0) -> None:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.waypoints = data["waypoints"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise ValidationError(f"웨이포인트 파일을 읽을 수 없습니다: {path} ({exc})") from exc
        if not self.waypoints:
            raise ValidationError(f"웨이포인트가 비어 있습니다: {path}")
        if speed <= 0:
            raise ValidationError("speed 는 0보다 커야 합니다.")

        self.source = data.get("source_episode", str(path))
        self.num_joints = len(self.waypoints[0]["joints"])
        self._speed = speed
        self._lag_tol = lag_tol_deg
        self._clock = 0.0
        self._last_tick: float | None = None
        self._target: list[float] | None = None
        self.done = False

    def _at(self, t: float) -> list[float]:
        wps = self.waypoints
        if t <= wps[0]["t"]:
            return list(wps[0]["joints"])
        for a, b in zip(wps, wps[1:]):
            if t <= b["t"]:
                span = b["t"] - a["t"]
                r = 0.0 if span <= 0 else (t - a["t"]) / span
                return [pa + (pb - pa) * r for pa, pb in zip(a["joints"], b["joints"])]
        return list(wps[-1]["joints"])

    def predict(self, follower_joints, frames=None) -> list[float]:  # noqa: ARG002
        now = time.monotonic()
        dt = 0.0 if self._last_tick is None else now - self._last_tick
        self._last_tick = now

        arrived = self._target is None or max(
            abs(a - b) for a, b in zip(follower_joints, self._target)
        ) <= self._lag_tol
        if arrived:
            self._clock += dt * self._speed
            if self._clock >= self.waypoints[-1]["t"]:
                self.done = True

        self._target = self._at(self._clock)
        return self._target

    def status(self) -> dict:
        return {
            "source": self.source,
            "progress_sec": round(self._clock, 2),
            "total_sec": self.waypoints[-1]["t"],
            "done": self.done,
        }


def demo() -> None:
    """assert 자체검사: 보간 · 도착 대기 · 종료 플래그.

    실행: python -c "from backend.core.waypoints import demo; demo()"
    (상대 임포트 때문에 파일을 직접 실행할 수는 없다.)
    """
    import tempfile

    path = Path(tempfile.gettempdir()) / "_wp_demo.json"
    path.write_text(
        json.dumps({"waypoints": [{"t": 0, "joints": [0.0]}, {"t": 2, "joints": [10.0]}]}),
        encoding="utf-8",
    )
    r = WaypointRunner(path)
    assert r._at(1.0) == [5.0], r._at(1.0)      # 중간점 보간
    assert r._at(-1) == [0.0] and r._at(99) == [10.0]   # 범위 밖은 양 끝 고정

    r.predict([0.0])                            # 첫 tick: dt=0, 시계 그대로
    assert r._clock == 0.0 and not r.done
    r._last_tick -= 1.0                         # 1초 흐른 것으로 위장
    r.predict([0.0])
    assert 0.9 < r._clock < 1.1, r._clock

    r._last_tick -= 1.0
    r.predict([99.0])                           # 팔이 크게 뒤처짐 → 시계 정지
    assert 0.9 < r._clock < 1.1, r._clock

    r._last_tick -= 2.0
    r.predict(r._target)
    assert r.done
    path.unlink()
    print("ok")
