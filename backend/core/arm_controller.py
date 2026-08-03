"""OMX 리더/팔로워 제어.

패턴
----
* Strategy: `ArmBackend` 인터페이스 뒤에 DynamixelBackend / SimulatedBackend 를
  둔다. 상위 로직(안전 클램프, 홈 복귀, 텔레오퍼레이션 루프)은 하드웨어 유무와
  무관하게 동일하게 테스트된다. dynamixel_sdk 미설치 환경에서도 서버가 뜬다.
* Template Method: `TeleopLoop.run()` 이 골격을 잡고 clamp 정책만 주입받는다.

안전 원칙
--------
1. 팔로워에 쓰기 전 항상 (a) 절대 소프트리밋 (b) 스텝당 상대변화량 순서로 클램프.
2. 리더 응답이 watchdog 시간 이상 끊기면 즉시 정지 — 마지막 목표값을 유지한다.
3. 서버 종료/에러 시 반드시 홈 복귀 후 토크 해제. finally 블록으로 보장.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Protocol, Sequence

from ..config import ArmConfig, SafetyConfig
from ..utils.errors import ArmConnectionError, SafetyLimitError

logger = logging.getLogger(__name__)

# Dynamixel 프로토콜 2.0 / X-시리즈 컨트롤 테이블 주소
ADDR_TORQUE_ENABLE = 64
ADDR_GOAL_POSITION = 116
ADDR_PRESENT_POSITION = 132
LEN_GOAL_POSITION = 4
DXL_RESOLUTION = 4096          # 1회전 = 4096 tick
DXL_CENTER = 2048


def ticks_to_deg(ticks: int) -> float:
    return (ticks - DXL_CENTER) * 360.0 / DXL_RESOLUTION


def deg_to_ticks(deg: float) -> int:
    return int(round(deg * DXL_RESOLUTION / 360.0)) + DXL_CENTER


class ArmBackend(Protocol):
    """하드웨어 추상화. 상위 로직은 이 4개 메서드만 안다."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def read_positions(self) -> list[float]: ...
    def write_positions(self, degrees: Sequence[float]) -> None: ...
    def set_torque(self, enabled: bool) -> None: ...


class SimulatedBackend:
    """하드웨어 없이 UI/수집 파이프라인을 검증하기 위한 대역.

    리더는 사인파로 스스로 움직이고, 팔로워는 명령을 그대로 따른다.
    """

    def __init__(self, cfg: ArmConfig) -> None:
        self._cfg = cfg
        self._positions = list(cfg.home_position)
        self._t0 = time.monotonic()
        self._is_leader = cfg.role == "leader"

    def connect(self) -> None:
        logger.info("[SIM] %s arm connected", self._cfg.role)

    def disconnect(self) -> None:
        logger.info("[SIM] %s arm disconnected", self._cfg.role)

    def read_positions(self) -> list[float]:
        if self._is_leader:
            t = time.monotonic() - self._t0
            return [
                home + 18.0 * math.sin(t * 0.6 + i * 0.9)
                for i, home in enumerate(self._cfg.home_position)
            ]
        return list(self._positions)

    def write_positions(self, degrees: Sequence[float]) -> None:
        self._positions = list(degrees)

    def set_torque(self, enabled: bool) -> None:  # noqa: D102
        pass


class DynamixelBackend:
    """실제 OMX 하드웨어. dynamixel_sdk 는 지연 임포트한다.

    지연 임포트 이유: 이 패키지는 시뮬레이션 모드에서 필요 없고,
    설치 실패가 서버 기동 전체를 막으면 안 되기 때문.
    """

    def __init__(self, cfg: ArmConfig, port: str) -> None:
        self._cfg = cfg
        self._port = port
        self._handler = None
        self._packet = None

    def connect(self) -> None:
        try:
            from dynamixel_sdk import PacketHandler, PortHandler
        except ImportError as exc:
            raise ArmConnectionError(
                self._cfg.role,
                "dynamixel-sdk 가 설치되어 있지 않습니다. "
                "`pip install dynamixel-sdk` 후 다시 시도하거나 시뮬레이션 모드로 실행하세요.",
            ) from exc

        handler = PortHandler(self._port)
        if not handler.openPort():
            raise ArmConnectionError(
                self._cfg.role, f"{self._port} 포트를 열 수 없습니다. 권한(dialout 그룹)을 확인하세요."
            )
        if not handler.setBaudRate(self._cfg.baudrate):
            handler.closePort()
            raise ArmConnectionError(
                self._cfg.role, f"보드레이트 {self._cfg.baudrate} 설정 실패."
            )

        self._handler = handler
        self._packet = PacketHandler(2.0)

        # 연결 직후 전 관절 ping 으로 실제 통신을 확인한다.
        # openPort 성공만으로는 모터 응답을 보장하지 못한다.
        for dxl_id in self._cfg.joint_ids:
            _, comm, err = self._packet.ping(self._handler, dxl_id)
            if comm != 0 or err != 0:
                self.disconnect()
                raise ArmConnectionError(
                    self._cfg.role,
                    f"{self._cfg.role.upper()} arm 의 관절 ID {dxl_id} 가 응답하지 않습니다. "
                    "ID 설정과 전원을 확인하세요.",
                    joint_id=dxl_id,
                )

    def disconnect(self) -> None:
        if self._handler is not None:
            self._handler.closePort()
            self._handler = None
            self._packet = None

    def read_positions(self) -> list[float]:
        if self._packet is None:
            raise ArmConnectionError(self._cfg.role, "연결되지 않은 arm 에서 읽기를 시도했습니다.")
        out: list[float] = []
        for dxl_id in self._cfg.joint_ids:
            value, comm, err = self._packet.read4ByteTxRx(
                self._handler, dxl_id, ADDR_PRESENT_POSITION
            )
            if comm != 0 or err != 0:
                raise ArmConnectionError(
                    self._cfg.role, f"관절 {dxl_id} 위치 읽기 실패", joint_id=dxl_id
                )
            out.append(ticks_to_deg(value))
        return out

    def write_positions(self, degrees: Sequence[float]) -> None:
        if self._packet is None:
            raise ArmConnectionError(self._cfg.role, "연결되지 않은 arm 에 쓰기를 시도했습니다.")
        for dxl_id, deg in zip(self._cfg.joint_ids, degrees):
            comm, err = self._packet.write4ByteTxRx(
                self._handler, dxl_id, ADDR_GOAL_POSITION, deg_to_ticks(deg)
            )
            if comm != 0 or err != 0:
                raise ArmConnectionError(
                    self._cfg.role, f"관절 {dxl_id} 목표위치 쓰기 실패", joint_id=dxl_id
                )

    def set_torque(self, enabled: bool) -> None:
        if self._packet is None:
            return
        for dxl_id in self._cfg.joint_ids:
            self._packet.write1ByteTxRx(
                self._handler, dxl_id, ADDR_TORQUE_ENABLE, 1 if enabled else 0
            )


class SafetyGuard:
    """관절 명령 클램프. 순수 함수에 가까워 단위 테스트가 쉽다."""

    def __init__(self, safety: SafetyConfig) -> None:
        self._safety = safety

    def clamp(self, target: Sequence[float], current: Sequence[float]) -> tuple[list[float], bool]:
        """(클램프된 목표값, 클램프가 실제로 발생했는지) 반환."""
        limits = self._safety.joint_limits
        max_delta = self._safety.max_relative_target
        clamped: list[float] = []
        touched = False

        for i, (tgt, cur) in enumerate(zip(target, current)):
            lo, hi = limits[i] if i < len(limits) else (-180.0, 180.0)

            # 1) 절대 소프트리밋
            bounded = min(max(tgt, lo), hi)
            # 2) 스텝당 상대 변화량 — 리더가 튀어도 팔로워는 서서히 따라간다
            delta = bounded - cur
            if abs(delta) > max_delta:
                bounded = cur + math.copysign(max_delta, delta)
                touched = True
            elif bounded != tgt:
                touched = True

            clamped.append(bounded)

        return clamped, touched

    def assert_within_limits(self, positions: Sequence[float]) -> None:
        for i, pos in enumerate(positions):
            lo, hi = self._safety.joint_limits[i] if i < len(self._safety.joint_limits) else (-180.0, 180.0)
            if not (lo <= pos <= hi):
                raise SafetyLimitError(
                    f"관절 {i} 가 소프트리밋을 벗어났습니다: {pos:.1f}° (허용 {lo}~{hi}°)"
                )


class ArmController:
    """단일 arm 의 연결 상태와 최근 관절값을 관리."""

    def __init__(self, cfg: ArmConfig, backend: ArmBackend, guard: SafetyGuard) -> None:
        self.cfg = cfg
        self._backend = backend
        self._guard = guard
        self._connected = False
        self._last_positions: list[float] = list(cfg.home_position)
        self._last_read_at = 0.0
        self._lock = threading.RLock()

    @property
    def role(self) -> str:
        return self.cfg.role

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._backend.connect()
            self._connected = True
            self._last_positions = self._backend.read_positions()
            self._last_read_at = time.monotonic()

    def set_torque(self, enabled: bool) -> None:
        with self._lock:
            if self._connected:
                self._backend.set_torque(enabled)

    def disconnect(self, *, home_first: bool = True) -> None:
        with self._lock:
            if not self._connected:
                return
            try:
                if home_first and self.cfg.role == "follower":
                    self.go_home()
            finally:
                # 홈 복귀가 실패해도 토크 해제와 포트 닫기는 반드시 수행한다.
                self._backend.set_torque(False)
                self._backend.disconnect()
                self._connected = False

    def read(self) -> list[float]:
        with self._lock:
            if not self._connected:
                return list(self._last_positions)
            self._last_positions = self._backend.read_positions()
            self._last_read_at = time.monotonic()
            return list(self._last_positions)

    def write(self, target: Sequence[float]) -> list[float]:
        """안전 클램프를 거쳐 기록. 실제 적용된 값을 반환."""
        with self._lock:
            current = self._last_positions
            clamped, touched = self._guard.clamp(target, current)
            if self._connected:
                self._backend.write_positions(clamped)
            self._last_positions = clamped
            if touched:
                logger.debug("%s: safety clamp applied", self.cfg.role)
            return clamped

    def go_home(self) -> list[float]:
        """홈 포지션까지 등속 보간으로 복귀.

        목표값을 한 번에 쓰면 모터가 최대 속도로 튄다. max_relative_target
        단위로 쪼개 보내 사람이 손을 뗄 시간을 준다.
        """
        with self._lock:
            self.set_torque(True)
            home = list(self.cfg.home_position)
            step_hz = 50.0
            step_deg = self._guard._safety.homing_speed_deg_per_s / step_hz

            for _ in range(int(step_hz * 20)):  # 최대 20초 안전장치
                current = self._last_positions
                remaining = [h - c for h, c in zip(home, current)]
                if max(abs(r) for r in remaining) < 0.5:
                    break
                nxt = [
                    c + math.copysign(min(step_deg, abs(r)), r) if r else c
                    for c, r in zip(current, remaining)
                ]
                if self._connected:
                    self._backend.write_positions(nxt)
                self._last_positions = nxt
                time.sleep(1.0 / step_hz)

            return list(self._last_positions)

    def status(self) -> dict:
        return {
            "role": self.cfg.role,
            "connected": self._connected,
            "joints": [round(v, 2) for v in self._last_positions],
            "joint_ids": list(self.cfg.joint_ids),
            "stale_ms": round((time.monotonic() - self._last_read_at) * 1000, 1)
            if self._last_read_at
            else None,
        }


class TeleopLoop(threading.Thread):
    """리더 → (클램프) → 팔로워 미러링 루프.

    watchdog: 리더 읽기가 timeout 을 넘기면 루프를 정지시키고 플래그를 세운다.
    UI 는 이 플래그를 보고 사용자에게 재연결을 요구한다.
    """

    def __init__(
        self,
        leader: ArmController,
        follower: ArmController,
        safety: SafetyConfig,
        rate_hz: int = 50,
    ) -> None:
        super().__init__(name="teleop", daemon=True)
        self._leader = leader
        self._follower = follower
        self._safety = safety
        self._period = 1.0 / rate_hz
        self._stop_event = threading.Event()
        self.fault: str | None = None

    def run(self) -> None:
        watchdog_sec = self._safety.watchdog_timeout_ms / 1000.0
        last_ok = time.monotonic()
        try:
            while not self._stop_event.is_set():
                tick_started = time.monotonic()
                try:
                    leader_pos = self._leader.read()
                    self._follower.write(leader_pos)
                    last_ok = tick_started
                except Exception as exc:  # 통신 실패는 루프를 죽이지 않고 watchdog 에 맡긴다
                    logger.warning("teleop tick 실패: %s", exc)
                    if tick_started - last_ok > watchdog_sec:
                        self.fault = f"리더 통신이 {self._safety.watchdog_timeout_ms}ms 이상 끊겼습니다: {exc}"
                        break

                elapsed = time.monotonic() - tick_started
                self._stop_event.wait(max(0.0, self._period - elapsed))
        finally:
            # 어떤 경로로 빠져나가든 팔로워는 현재 자세에서 멈춘다.
            logger.info("teleop 루프 종료 (fault=%s)", self.fault)

    def stop(self) -> None:
        self._stop_event.set()
