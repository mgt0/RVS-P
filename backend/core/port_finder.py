"""시리얼 포트 탐색 및 리더/팔로워 식별.

핵심 문제: /dev/ttyACM* 의 번호는 '연결 순서'에 따라 부여되므로 재부팅하거나
USB 를 다시 꽂으면 리더와 팔로워가 뒤바뀐다. 이 상태로 텔레오퍼레이션을
켜면 팔로워가 리더처럼 동작해 하드웨어가 손상될 수 있다.

3단계 폴백 전략:
  1) serial_number 매칭  — 가장 견고. config.yaml 에 iSerial 을 적어두면 끝.
  2) 대화형 언플러그 diff — lerobot-find-port 와 동일한 방식. 시리얼이 없거나
                            중복될 때 사용. 결과를 config 에 저장해 1) 로 승격.
  3) port_hint 수동 지정  — 최후 수단.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass

from serial.tools import list_ports

from ..config import ArmConfig
from ..utils.errors import ArmPortNotFoundError, ValidationError

# ROBOTIS U2D2 / OpenRB-150 계열에서 관측되는 VID (참고용 우선순위 힌트).
KNOWN_ROBOTIS_VIDS = {0x0403, 0x2F5D, 0x1A86}


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    manufacturer: str | None

    @property
    def looks_like_robotis(self) -> bool:
        return self.vid in KNOWN_ROBOTIS_VIDS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vid"] = f"{self.vid:04x}" if self.vid is not None else None
        d["pid"] = f"{self.pid:04x}" if self.pid is not None else None
        d["looks_like_robotis"] = self.looks_like_robotis
        return d


def scan_serial_ports() -> list[SerialPortInfo]:
    """현재 시스템의 시리얼 포트 목록. ROBOTIS 로 보이는 포트를 앞으로 정렬."""
    ports = [
        SerialPortInfo(
            device=p.device,
            description=p.description or "",
            hwid=p.hwid or "",
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number,
            manufacturer=p.manufacturer,
        )
        for p in list_ports.comports()
    ]
    ports.sort(key=lambda p: (not p.looks_like_robotis, p.device))
    return ports


class PortRegistry:
    """role → device path 해석기.

    Facade 패턴: 라우트는 `resolve("leader")` 하나만 알면 되고,
    시리얼 매칭/수동 오버라이드/캐시 무효화는 전부 이 안에서 처리한다.
    """

    def __init__(self, arms: dict[str, ArmConfig]) -> None:
        self._arms = arms
        self._overrides: dict[str, str] = {}   # 언플러그 diff 나 수동 지정 결과
        self._lock = threading.RLock()

    # ---------- 조회 ----------
    def resolve(self, role: str) -> str:
        """해당 role 의 포트 경로를 반환. 실패 시 role 을 담은 예외."""
        with self._lock:
            arm = self._arms.get(role)
            if arm is None:
                raise ValidationError(f"알 수 없는 arm role: {role}")

            available = {p.device: p for p in scan_serial_ports()}

            # 1) 세션 중 확정된 오버라이드
            override = self._overrides.get(role)
            if override and override in available:
                return override

            # 2) 시리얼 넘버 매칭
            if arm.serial_number:
                for dev, info in available.items():
                    if info.serial_number == arm.serial_number:
                        return dev
                raise ArmPortNotFoundError(
                    role,
                    f"{role.upper()} arm (S/N {arm.serial_number}) 가 연결된 포트에 없습니다. "
                    f"현재 감지된 포트: {', '.join(available) or '없음'}",
                    expected_serial=arm.serial_number,
                    available=list(available),
                )

            # 3) 수동 힌트
            if arm.port_hint and arm.port_hint in available:
                return arm.port_hint

            raise ArmPortNotFoundError(
                role,
                f"{role.upper()} arm 을 식별할 기준이 없습니다. "
                "IDENTIFY 를 실행해 포트를 확정하거나 config.yaml 에 serial_number 를 지정하세요.",
                available=list(available),
            )

    def status(self, role: str) -> dict:
        """예외를 던지지 않는 조회. 대시보드 폴링용."""
        try:
            device = self.resolve(role)
            info = next((p for p in scan_serial_ports() if p.device == device), None)
            return {
                "role": role,
                "connected": True,
                "device": device,
                "serial_number": info.serial_number if info else None,
                "error": None,
            }
        except ArmPortNotFoundError as exc:
            return {
                "role": role,
                "connected": False,
                "device": None,
                "serial_number": None,
                "error": exc.message,
            }

    # ---------- 오버라이드 ----------
    def set_override(self, role: str, device: str) -> None:
        with self._lock:
            if role not in self._arms:
                raise ValidationError(f"알 수 없는 arm role: {role}")
            self._overrides[role] = device

    def clear_overrides(self) -> None:
        with self._lock:
            self._overrides.clear()


class IdentifySession:
    """lerobot-find-port 의 언플러그 diff 를 웹에서 재현.

    흐름:
      POST /api/ports/identify/start  {role}  → 현재 포트 스냅샷 저장
      (사용자가 해당 arm 의 USB 를 뽑음)
      POST /api/ports/identify/confirm {role} → 사라진 포트 = 그 arm

    CLI 처럼 blocking input() 을 쓸 수 없으므로 2-phase 로 쪼갰다.
    타임아웃을 둬서 세션이 영원히 열려 있지 않게 한다.
    """

    TIMEOUT_SEC = 120

    def __init__(self, registry: PortRegistry) -> None:
        self._registry = registry
        self._snapshots: dict[str, tuple[float, set[str]]] = {}
        self._lock = threading.Lock()

    def start(self, role: str) -> dict:
        devices = {p.device for p in scan_serial_ports()}
        if not devices:
            raise ArmPortNotFoundError(
                role, "감지된 시리얼 포트가 하나도 없습니다. USB 케이블과 전원을 먼저 확인하세요."
            )
        with self._lock:
            self._snapshots[role] = (time.monotonic(), devices)
        return {"role": role, "before": sorted(devices), "timeout_sec": self.TIMEOUT_SEC}

    def confirm(self, role: str) -> dict:
        with self._lock:
            entry = self._snapshots.pop(role, None)

        if entry is None:
            raise ValidationError(f"{role.upper()} 식별 세션이 없습니다. START 를 먼저 실행하세요.")

        started_at, before = entry
        if time.monotonic() - started_at > self.TIMEOUT_SEC:
            raise ValidationError(f"{role.upper()} 식별 세션이 만료되었습니다. 다시 시작하세요.")

        after = {p.device for p in scan_serial_ports()}
        removed = before - after

        if len(removed) == 1:
            device = removed.pop()
            self._registry.set_override(role, device)
            return {"role": role, "device": device, "confirmed": True}

        if not removed:
            raise ArmPortNotFoundError(
                role,
                f"사라진 포트가 없습니다. {role.upper()} arm 의 USB 를 뽑았는지 확인하세요.",
                before=sorted(before),
            )

        # 허브 전체가 빠지는 등 2개 이상이 동시에 사라진 경우 — 추측하지 않고 실패시킨다.
        raise ArmPortNotFoundError(
            role,
            f"{len(removed)}개 포트가 동시에 사라져 {role.upper()} arm 을 특정할 수 없습니다: "
            f"{', '.join(sorted(removed))}. 하나씩 다시 시도하세요.",
            removed=sorted(removed),
        )
