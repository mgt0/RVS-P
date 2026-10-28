"""하드웨어 리소스 컨테이너 (Facade).

라우트가 CameraManager / ArmController / PortRegistry / Recorder 를 각각
전역으로 들고 있으면 종료 순서가 꼬여 포트가 잠긴 채 남는다.
소유권을 이 클래스 하나로 모으고, shutdown() 에서 역순으로 정리한다.
"""

from __future__ import annotations

import logging
import threading

from ..config import PROJECT_ROOT, AppConfig
from ..utils.errors import ArmConnectionError, ValidationError
from .arm_controller import (
    ArmController,
    DynamixelBackend,
    SafetyGuard,
    SimulatedBackend,
    TeleopLoop,
)
from .act_trainer import TrainingManager
from .calibration import Calibration
from .camera_manager import CameraManager
from .conveyor import ConveyorController
from .cycle import CycleBusyError, CycleRunner
from .demo_session import DemoSession
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
        # 사람이 직접 찍은 끝점 저장소. config 옆에 두어 설정과 함께 다니게 한다.
        self.calibration = Calibration(PROJECT_ROOT / "calibration.json")
        self.conveyors = {
            name: ConveyorController(
                c, simulate=cfg.server.simulate, calibration=self.calibration, name=name
            )
            for name, c in cfg.conveyors.items()
        }
        self._teleop: TeleopLoop | None = None
        self._autorun: AutoRunLoop | None = None
        self._lock = threading.RLock()
        self._closed = False    # shutdown() 이 끝나 포트를 놓았다 — 그 뒤의 정지 명령이 끊긴 포트를 다시 열지 않게 한다
        # 시연 녹화 세션(녹화→리셋 반복). 비상 정지·종료가 이 객체를 먼저 끊는다.
        self.demo = DemoSession(self)
        # 자동 사이클(집기→이송→검사→분류). 비상 정지·종료가 이 객체를 가장 먼저 끊는다.
        self.cycle = CycleRunner(self)

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
                self.cycle.abort("서버 종료")   # 사이클이 아래에서 정리하는 벨트·autorun 을 다시 구동하지 못하게 먼저 끊는다
                self.demo.abort("서버 종료")    # 세션이 녹화를 다시 시작하지 못하게 먼저 끊는다
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
                # 락을 쥔 채 세운다. 이 락을 기다리던 사이클 스레드의 start_autorun 은 이걸 보고 물러난다(아래).
                self._closed = True
        logger.info("하드웨어 정리 완료")

    @property
    def closed(self) -> bool:
        return self._closed

    # ---------------------------------------------------------------- 비상 정지
    def emergency_stop(self) -> dict:
        """움직이는 모든 것을 즉시 세운다.

        한 장치가 실패해도 나머지는 반드시 계속 시도한다 — 비상 정지에서
        예외로 중단하는 것은 최악의 동작이다.

        팔로워 토크는 켜둔다. 여기서 토크를 풀면 팔이 중력으로 낙하해
        '정지'가 오히려 충돌을 만든다. 멈춘 자세를 유지하는 것이 정지다.

        상태를 걸어 잠그지 않는다(래치 없음). 스텝 드라이버는 DISABLE 로 꺼두지만
        펌웨어의 beginRun() 이 START 를 받으면 스스로 다시 ENABLE 하므로, 누른 뒤
        그냥 평소처럼 시작하면 원래대로 동작한다 — 별도 해제 절차가 필요 없다.
        """
        result: dict = {}
        # 자동 사이클을 가장 먼저 끊는다. 중단 플래그만 세우고 바로 돌아오므로 아래 정지를 늦추지 않는다 —
        # 사이클 스레드가 깨어 벨트와 autorun 을 세우고(아래 정지도 같은 일을 한다) 다음 단계에 들어가지 않는다.
        # 이 호출이 어떻게 실패하든 아래 정지 절차는 반드시 그대로 진행한다.
        try:
            self.cycle.abort("비상 정지")
        except Exception:  # noqa: BLE001
            logger.exception("비상 정지: 자동 사이클 중단 실패")

        # 이어서 시연 세션을 끊는다. 중단 플래그만 세우고 바로 돌아오므로 아래 정지를 늦추지 않는다 —
        # 세션 스레드가 알아서 진행 중이던 에피소드를 버리고 끝나며, 리셋이 끝나도 새 녹화를 시작하지
        # 않는다. 이 호출이 어떻게 실패하든 아래 정지 절차는 반드시 그대로 진행한다.
        try:
            self.demo.abort("비상 정지")
        except Exception:  # noqa: BLE001
            logger.exception("비상 정지: 시연 세션 중단 실패")

        # rollout(autorun)을 가장 먼저 세운다. 이게 살아 있으면 팔로워에 계속
        # 목표 각도를 밀어넣기 때문에, 나중에 멈추면 그 사이 팔이 더 움직인다.
        try:
            result["autorun"] = self.stop_autorun()
        except Exception as exc:  # noqa: BLE001
            logger.exception("비상 정지: rollout 정지 실패")
            result["autorun"] = {"error": str(exc)}

        try:
            result["teleop"] = self.stop_teleop()
        except Exception as exc:  # noqa: BLE001
            logger.exception("비상 정지: 텔레오퍼 정지 실패")
            result["teleop"] = {"error": str(exc)}

        belts: dict = {}
        for name, ctrl in self.conveyors.items():
            # STOP(펄스 중단) 뒤 DISABLE(드라이버 차단)까지 간다. STOP 만 하면
            # 여자 상태가 남아 홀딩 토크로 축을 계속 붙잡고 발열도 이어진다 —
            # '전부 off' 가 아니다. 앞 단계가 실패해도 뒤 단계는 반드시 시도한다.
            outcome = []
            for step, action in (("stopped", ctrl.stop), ("disabled", lambda c=ctrl: c.set_enabled(False))):
                try:
                    action()
                    outcome.append(step)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("비상 정지: 모터 %s %s 실패", name, step)
                    outcome.append(f"{step} 실패: {exc}")
            belts[name] = " + ".join(outcome)
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
            if self.cycle.active:    # 사이클이 팔로워를 쥐고 있다. 사이클 자신은 이 경로로 오지 않는다
                raise CycleBusyError("텔레오퍼레이션을 시작")
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
    def start_autorun(
        self,
        checkpoint: str | None = None,
        rate_hz: int = 10,
        waypoints: str | None = None,
        speed: float = 1.0,
        *,
        by_cycle: bool = False,
    ) -> dict:
        """학습된 ACT 정책으로 팔로워를 자율 제어한다.

        teleop과는 상호 배타적이다 — 둘 다 follower.write()를 호출하는
        루프라 동시에 돌면 명령이 뒤섞인다. 반드시 하나만 실행되게 막는다.

        by_cycle 은 자동 사이클(core/cycle.py)이 자기 집기 단계에서만 True 로 부른다. 사이클이 도는 동안의
        수동 시작은 거부한다(API 라우트는 이 인자를 받지 않는다).

        checkpoint는 필수다. 학습이 `lerobot-train` 별도 프로세스에서 도는
        구조라(act_trainer.LeRobotTrainer 참고), 이 서버는 어느 체크포인트가
        최신인지 알 방법이 없다 — `outputs/train/<job_name>/checkpoints/<step>/
        pretrained_model/` 처럼 학습 로그 마지막 줄에 찍힌 경로를 그대로 넘겨라.
        """
        with self._lock:
            if self.cycle.active and not by_cycle:
                raise CycleBusyError("자율 실행(rollout)을 시작")
            if by_cycle and (self.cycle.aborted or self._closed):
                # 사이클 스레드는 점검(_check)을 통과한 뒤 이 락을 기다리는데, 그 사이에 비상 정지(cycle.abort →
                # stop_autorun)나 서버 종료가 이미 끝났을 수 있다. 그대로 이어가면 정지 뒤에 팔로워를 연결하고
                # 토크를 걸어 움직인다 — 락 안에서 다시 확인해 거부한다.
                raise ValidationError("사이클이 중단되어 자율 실행을 시작하지 않았습니다.")
            if not checkpoint and not waypoints:
                raise ValidationError(
                    "checkpoint 또는 waypoints 경로가 필요합니다. checkpoint 는 "
                    "lerobot-train 로그에 출력된 pretrained_model 폴더, waypoints 는 "
                    "scripts/to_waypoints.py 가 만든 JSON 경로입니다."
                )
            if self._teleop and self._teleop.is_alive():
                raise ValidationError("teleop이 실행 중입니다. 먼저 TELEOP STOP을 누르세요.")
            if self._autorun and self._autorun.is_alive():
                return self._autorun.status()

            if waypoints:
                from .waypoints import WaypointRunner

                runner = WaypointRunner(waypoints, speed=speed)
                expected = len(self.follower.cfg.joint_ids)
                if runner.num_joints != expected:
                    raise ValidationError(
                        f"웨이포인트 관절 수({runner.num_joints})가 팔로워({expected})와 다릅니다."
                    )
            else:
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
            logger.info("autorun 시작: checkpoint=%s waypoints=%s", checkpoint, waypoints)
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
