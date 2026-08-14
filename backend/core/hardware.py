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
from .conveyor import ConveyorController
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
        self.conveyors = {
            name: ConveyorController(c, simulate=cfg.server.simulate)
            for name, c in cfg.conveyors.items()
        }
        self._teleop: TeleopLoop | None = None
        self._autorun: AutoRunLoop | None = None
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
                for conveyor in self.conveyors.values():
                    conveyor.disconnect()
        logger.info("하드웨어 정리 완료")

    # ---------------------------------------------------------------- 비상 정지
    def emergency_stop(self) -> dict:
        """움직이는 모든 것을 즉시 세운다.

        한 장치가 실패해도 나머지는 반드시 계속 시도한다 — 비상 정지에서
        예외로 중단하는 것은 최악의 동작이다.

        팔로워 토크는 켜둔다. 여기서 토크를 풀면 팔이 중력으로 낙하해
        '정지'가 오히려 충돌을 만든다. 멈춘 자세를 유지하는 것이 정지다.
        """
        result: dict = {}
        try:
            result["teleop"] = self.stop_teleop()
        except Exception as exc:  # noqa: BLE001
            logger.exception("비상 정지: 텔레오퍼 정지 실패")
            result["teleop"] = {"error": str(exc)}

        belts: dict = {}
        for name, ctrl in self.conveyors.items():
            try:
                ctrl.stop()
                belts[name] = "stopped"
            except Exception as exc:  # noqa: BLE001
                logger.exception("비상 정지: 벨트 %s 정지 실패", name)
                belts[name] = f"error: {exc}"
        result["conveyors"] = belts

        if self.recorder.state.value != "IDLE":
            self.recorder.abort("비상 정지")
            result["recording"] = "aborted"
        else:
            result["recording"] = "idle"

        logger.warning("비상 정지 실행: %s", result)
        return result

    # ---------------------------------------------------------------- 텔레오퍼
    def start_teleop(self) -> dict:
        with self._lock:
            if self._autorun and self._autorun.is_alive():
                raise ValidationError("autorun이 실행 중입니다. 먼저 ROLLOUT STOP을 누르세요.")
            if self._teleop and self._teleop.is_alive():
                return {"running": True, "fault": self._teleop.fault}
            self.leader.connect()
            self.follower.connect()
            # 팔로워 좌표계를 리더의 현재 자세에 정렬한다. 실기 모터는 extended
            # position mode 라 절대각이 리더와 어긋나(실측 베이스 ~181° 차이),
            # 정렬 없이 절대각을 미러링하면 시작 순간 크게 휘두른다.
            # 사용자는 텔레오퍼 시작 전에 두 팔을 비슷한 자세로 맞춰두면 된다.
            self.follower.align_to(self.leader.read())
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

        checkpoint는 필수다. 학습이 `lerobot-train` 별도 프로세스에서 도는
        구조라(act_trainer.LeRobotTrainer 참고), 이 서버는 어느 체크포인트가
        최신인지 알 방법이 없다 — `outputs/train/<job_name>/checkpoints/<step>/
        pretrained_model/` 처럼 학습 로그 마지막 줄에 찍힌 경로를 그대로 넘겨라.
        """
        with self._lock:
            if not checkpoint:
                raise ValidationError(
                    "checkpoint 경로가 필요합니다. lerobot-train 학습 로그에 출력된 "
                    "pretrained_model 폴더 경로를 넘기세요."
                )
            if self._teleop and self._teleop.is_alive():
                raise ValidationError("teleop이 실행 중입니다. 먼저 TELEOP STOP을 누르세요.")
            if self._autorun and self._autorun.is_alive():
                return self._autorun.status()

            # 지연 임포트: lerobot/torch 미설치 상태에서도 서버 자체는 기동돼야 한다.
            from .lerobot_bridge import ActInferenceRunner, LeRobotUnavailableError

            try:
                runner = ActInferenceRunner(checkpoint)
            except LeRobotUnavailableError as exc:
                raise ValidationError(str(exc)) from exc

            self.follower.connect()
            self.follower.set_torque(True)
            self._autorun = AutoRunLoop(runner, self.follower, self.cameras, self.cfg.safety, rate_hz=rate_hz)
            self._autorun.start()
            logger.info("autorun 시작: checkpoint=%s", checkpoint)
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
