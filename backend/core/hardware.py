"""하드웨어 리소스 컨테이너 (Facade).

라우트가 CameraManager / ArmController / PortRegistry / Recorder 를 각각
전역으로 들고 있으면 종료 순서가 꼬여 포트가 잠긴 채 남는다.
소유권을 이 클래스 하나로 모으고, shutdown() 에서 역순으로 정리한다.
"""

from __future__ import annotations

import logging
import threading

from ..config import AppConfig
from ..utils.errors import ArmConnectionError, ValidationError
from .arm_controller import (
    ArmController,
    DynamixelBackend,
    SafetyGuard,
    SimulatedBackend,
    TeleopLoop,
)
from .act_trainer import TrainingManager
from .camera_manager import CameraManager
from .port_finder import IdentifySession, PortRegistry
from .recorder import Recorder
from .rollout import AutoRunLoop

logger = logging.getLogger(__name__)


class HardwareHub:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.guard = SafetyGuard(cfg.safety)
        self.ports = PortRegistry(cfg.arms)
        self.identify = IdentifySession(self.ports)
        self.cameras = CameraManager(cfg.cameras, simulate=cfg.server.simulate)

        self.leader = self._build_arm("leader")
        self.follower = self._build_arm("follower")

        self.recorder = Recorder(cfg.recording, self.cameras, self.leader, self.follower)
        self.training = TrainingManager(cfg.recording.root)
        self._teleop: TeleopLoop | None = None
        self._autorun: AutoRunLoop | None = None
        self._checkpoint_root = cfg.recording.root / "checkpoints"
        self._lock = threading.RLock()

    def _build_arm(self, role: str) -> ArmController:
        arm_cfg = self.cfg.arms[role]
        if self.cfg.server.simulate:
            backend = SimulatedBackend(arm_cfg)
        else:
            # 포트는 connect() 시점에 해석한다. 기동 시점에 USB 가 안 꽂혀 있어도
            # 서버는 떠야 하고, 대시보드에서 상태를 보며 연결할 수 있어야 한다.
            backend = _LazyDynamixelBackend(arm_cfg, self.ports)
        return ArmController(arm_cfg, backend, self.guard)

    # ---------------------------------------------------------------- 시작/종료
    def start(self) -> None:
        self.cameras.start()
        logger.info("카메라 매니저 시작 (simulate=%s)", self.cfg.server.simulate)

        if self.cfg.server.simulate:
            # 시뮬레이션에서는 물리적 위험이 없으므로 즉시 연결한다.
            # 실기에서는 사용자가 대시보드에서 상태를 확인한 뒤 연결하게 두는 것이
            # 안전하다 — 서버가 뜨자마자 토크가 걸리면 사람이 놀란다.
            self.leader.connect()
            self.follower.connect()

    def shutdown(self) -> None:
        with self._lock:
            # 순서가 중요하다: 녹화 중단 → 텔레오퍼 정지 → 홈 복귀/토크 해제 → 카메라 해제
            try:
                if self.recorder.state.value != "IDLE":
                    self.recorder.abort("서버 종료")
            finally:
                self.stop_teleop()
                self.stop_autorun()
                for arm in (self.follower, self.leader):
                    try:
                        arm.disconnect(home_first=True)
                    except Exception:  # noqa: BLE001
                        logger.exception("%s arm 종료 중 오류", arm.role)
                self.cameras.stop()
        logger.info("하드웨어 정리 완료")

    # ---------------------------------------------------------------- 텔레오퍼
    def start_teleop(self) -> dict:
        with self._lock:
            if self._autorun and self._autorun.is_alive():
                raise ValidationError("autorun이 실행 중입니다. 먼저 ROLLOUT STOP을 누르세요.")
            if self._teleop and self._teleop.is_alive():
                return {"running": True, "fault": self._teleop.fault}
            self.leader.connect()
            self.follower.connect()
            # 리더는 사람이 손으로 움직여야 하므로 토크를 끄고(백드라이버블),
            # 팔로워는 명령을 물리적으로 따라가야 하므로 토크를 켠다.
            self.leader.set_torque(False)
            self.follower.set_torque(True)
            self._teleop = TeleopLoop(self.leader, self.follower, self.cfg.safety)
            self._teleop.start()
            return {"running": True, "fault": None}

    def stop_teleop(self) -> dict:
        with self._lock:
            if self._teleop:
                self._teleop.stop()
                self._teleop.join(timeout=2.0)
                fault = self._teleop.fault
                self._teleop = None
                return {"running": False, "fault": fault}
            return {"running": False, "fault": None}

    @property
    def teleop_status(self) -> dict:
        running = bool(self._teleop and self._teleop.is_alive())
        return {"running": running, "fault": self._teleop.fault if self._teleop else None}

    # ---------------------------------------------------------------- 자율실행(rollout)
    def start_autorun(self, checkpoint: str | None = None, rate_hz: int = 10) -> dict:
        """학습된 ACT 정책으로 팔로워를 자율 제어한다.

        teleop과는 상호 배타적이다 — 둘 다 follower.write()를 호출하는
        루프라 동시에 돌면 명령이 뒤섞인다. 반드시 하나만 실행되게 막는다.
        """
        with self._lock:
            if self._teleop and self._teleop.is_alive():
                raise ValidationError("teleop이 실행 중입니다. 먼저 TELEOP STOP을 누르세요.")
            if self._autorun and self._autorun.is_alive():
                return self._autorun.status()

            # 지연 임포트: lerobot/torch 미설치 상태에서도 서버 자체는 기동돼야 한다.
            from .lerobot_bridge import ActInferenceRunner, LeRobotUnavailableError

            ckpt_path = checkpoint or str(self._default_checkpoint_dir())
            try:
                runner = ActInferenceRunner(ckpt_path)
            except LeRobotUnavailableError as exc:
                raise ValidationError(str(exc)) from exc

            self.follower.connect()
            self.follower.set_torque(True)
            self._autorun = AutoRunLoop(runner, self.follower, self.cameras, self.cfg.safety, rate_hz=rate_hz)
            self._autorun.start()
            logger.info("autorun 시작: checkpoint=%s", ckpt_path)
            return self._autorun.status()

    def stop_autorun(self) -> dict:
        with self._lock:
            if self._autorun:
                self._autorun.stop()
                self._autorun.join(timeout=5.0)
                status = self._autorun.status()
                self._autorun = None
                return status
            return {"running": False, "fault": None, "tick_count": 0}

    @property
    def autorun_status(self) -> dict:
        if self._autorun:
            return self._autorun.status()
        return {"running": False, "fault": None, "tick_count": 0}

    def _default_checkpoint_dir(self):
        """checkpoint 인자를 안 주면 가장 최근에 학습된 체크포인트를 쓴다."""
        if not self._checkpoint_root.exists():
            raise ValidationError(
                f"체크포인트가 없습니다 ({self._checkpoint_root}). "
                "checkpoint 경로를 직접 지정하거나 먼저 학습을 완료하세요."
            )
        candidates = sorted(self._checkpoint_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise ValidationError(f"{self._checkpoint_root} 안에 체크포인트 폴더가 없습니다.")
        return candidates[0]


class _LazyDynamixelBackend:
    """connect() 시점에 포트를 해석하는 어댑터.

    Adapter 패턴: ArmBackend 인터페이스를 유지하면서 포트 해석 시점만 늦춘다.
    """

    def __init__(self, arm_cfg, ports: PortRegistry) -> None:
        self._arm_cfg = arm_cfg
        self._ports = ports
        self._delegate: DynamixelBackend | None = None

    def connect(self) -> None:
        port = self._ports.resolve(self._arm_cfg.role)   # 실패 시 role 포함 예외
        self._delegate = DynamixelBackend(self._arm_cfg, port)
        self._delegate.connect()

    def disconnect(self) -> None:
        if self._delegate:
            self._delegate.disconnect()
            self._delegate = None

    def read_positions(self):
        if self._delegate is None:
            raise ArmConnectionError(self._arm_cfg.role, "arm 이 연결되지 않았습니다.")
        return self._delegate.read_positions()

    def write_positions(self, degrees) -> None:
        if self._delegate is None:
            raise ArmConnectionError(self._arm_cfg.role, "arm 이 연결되지 않았습니다.")
        self._delegate.write_positions(degrees)

    def set_torque(self, enabled: bool) -> None:
        if self._delegate:
            self._delegate.set_torque(enabled)
