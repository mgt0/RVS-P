"""컨베이어(STM32F411 BlackPill + TB6600 + NEMA17) 제어. USB CDC 로 한 줄 텍스트 명령을 주고받는다.

프로토콜은 firmware/conveyor_blackpill/conveyor_blackpill.ino 와 1:1로 맞춰져 있다.
보내는 것: START / STOP / RPM <값> / DIR CW|CCW / ENABLE / DISABLE / STATUS
받는 것: "OK: ...", "ERR: ...", 또는 STATUS 에 대한 JSON 한 줄.

보드 하나가 스텝모터 3개(M1=컨베이어1, M2=컨베이어2, M3=LM가이드)를 물고 있다.
config 의 motor 가 1 이상이면 모든 명령 앞에 "M<n> " 를 붙여 모터를 지정하고,
같은 port 를 쓰는 컨트롤러들은 시리얼 핸들과 락을 공유한다(포트는 한 번만 연다).

두 가지가 이 클래스의 존재 이유다.

1) 연결을 명령마다 새로 열지 않는다. 열고 닫기가 잦으면 명령이 유실될 틈이
   생기고, CH340 계열 보드(구 ESP32 리그)는 열 때마다 리셋까지 된다.
2) 처음 열 때 _wait_boot() 로 READY 배너를 잠깐 기다린다. BlackPill 은 네이티브
   USB 라 포트를 열어도 리셋되지 않으므로 배너는 '보드가 방금 부팅했고 호스트가
   이미 듣고 있던' 경우에만 온다 — 그래서 배너가 없어도 그대로 진행한다.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import serial

from ..config import ConveyorConfig
from ..utils.errors import ConveyorError, ValidationError

logger = logging.getLogger(__name__)

# 펌웨어 부팅 배너. 이 접두사를 바꾸면 펌웨어의 setup() 도 같이 고쳐야 한다.
_READY_PREFIX = "READY"
# 보드가 리셋된 직후라면 READY 까지 1초 남짓(ESP32 실측). BlackPill 은 보통
# 배너가 안 오고 그게 정상이다 — 타임아웃 후 그대로 진행한다.
_BOOT_TIMEOUT_SEC = 5.0
# 모터축 RPM 상한. 여기서는 명백한 오입력만 막고, 실제 범위 판정은 펌웨어에 맡긴다
# (마이크로스텝 값이 DIP 스위치와 함께 펌웨어에 박혀 있다).
# 펌웨어 MAX_SPS 는 1,200,000(타이머 PWM 이라 CPU 부담 없음). 호스트 상한은
# 주의: 스텝모터는 1000RPM 부근부터 토크가 급격히 떨어진다. 상한을 열어둔 것이
# 그 속도에서 탈조하지 않는다는 뜻은 아니다. 실제 한계는 부하를 걸고 찾아야 한다.
_RPM_HARD_MAX = 10000.0

# 한 보드(포트)에 모터가 여럿이라 컨트롤러들이 같은 시리얼을 공유한다.
# 포트당 핸들/락을 하나만 두고, 끊기면 dict 에서 지워 다음 명령이 다시 열게 한다.
_ports: dict[str, serial.Serial] = {}
_port_locks: dict[str, threading.RLock] = {}


class ConveyorController:
    def __init__(self, cfg: ConveyorConfig, *, simulate: bool = False) -> None:
        self._cfg = cfg
        self._simulate = simulate
        # 중첩 획득이 있다(_command → _refresh → _command). RLock 이어야 한다.
        # 같은 포트의 다른 모터 컨트롤러와도 한 락을 나눠 쓴다(한 선로 = 한 락).
        self._lock = (
            _port_locks.setdefault(cfg.port, threading.RLock())
            if cfg.port else threading.RLock()
        )
        self._state: dict = {
            "running": False,
            "enabled": True,
            "dir": "cw",
            "rpm": 0.0,
            "target_rpm": None,
            "sps": 0.0,
            "target_sps": None,
        }

    @property
    def label(self) -> str:
        """대시보드 표시용 이름. config 에 없으면 포트라도 보여준다."""
        return self._cfg.label or self._cfg.port or "conveyor"

    # ------------------------------------------------------------------ 연결
    @property
    def connected(self) -> bool:
        if self._simulate:
            return True
        ser = _ports.get(self._cfg.port)
        return ser is not None and ser.is_open

    def _ensure_open(self) -> serial.Serial | None:
        if self._simulate:
            return None
        if not self._cfg.port:
            raise ConveyorError("config.yaml 의 conveyor.port 가 설정되지 않았습니다.")
        ser = _ports.get(self._cfg.port)
        if ser is None or not ser.is_open:
            try:
                ser = serial.Serial(self._cfg.port, self._cfg.baudrate, timeout=self._cfg.timeout_sec)
            except (serial.SerialException, OSError) as exc:
                raise ConveyorError(f"{self._cfg.port} 포트를 열 수 없습니다: {exc}") from exc
            _ports[self._cfg.port] = ser
            # 방금 리셋됐으므로 캐시된 상태는 전부 무효다.
            self._state.update(running=False, enabled=True, rpm=0.0, sps=0.0)
            self._wait_boot(ser)
        return ser

    def _wait_boot(self, ser: serial.Serial) -> None:
        # 수동 대기 대신 PING 을 먼저 쏜다. BlackPill(네이티브 USB)은 리셋이 없어
        # READY 배너가 영영 안 오는데, 그때 타임아웃까지 통째로 기다리면 첫 명령이
        # 항상 5초 늦는다(실측 6.1초). PONG 이 오면 즉시 준비 완료로 판정한다.
        # 리셋형 보드라면 부팅 중이라 PING 이 버려지고 READY 배너가 대신 온다 —
        # 어느 쪽이든 한 경로로 처리된다.
        try:
            ser.write(b"PING\n")
            ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise ConveyorError(f"부팅 확인 PING 전송 실패: {exc}") from exc

        deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                raw = ser.readline()
            except (serial.SerialException, OSError) as exc:
                raise ConveyorError(f"부팅 대기 중 포트가 끊겼습니다: {exc}") from exc
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                # timeout_sec 을 0 으로 둔 설정에서 readline() 이 즉시 돌아와
                # CPU 를 태우는 것만 막는다(정상 설정이면 여기 거의 안 온다).
                time.sleep(0.02)
                continue
            logger.debug("컨베이어 부팅 로그: %s", line)
            if line.startswith(_READY_PREFIX) or line == "PONG":
                logger.info("컨베이어 펌웨어 준비됨 (%s)", self._cfg.port)
                return
        # 배너를 못 봤어도 계속 간다. 이미 부팅이 끝난 뒤 포트를 다시 연 경우
        # (리셋이 안 걸리는 보드/USB 허브) 배너가 안 오는 게 정상이다.
        logger.warning(
            "컨베이어 READY 배너를 %.0f초 안에 받지 못했습니다. 그대로 진행합니다.", _BOOT_TIMEOUT_SEC
        )
        ser.reset_input_buffer()

    def disconnect(self) -> None:
        # 포트는 보드 단위다 — 같은 보드에 물린 다른 모터들도 함께 끊긴다
        # (펌웨어 업로드용이므로 그게 맞다). 각자 다음 명령에서 자동 재연결한다.
        with self._lock:
            ser = _ports.pop(self._cfg.port, None)
            if ser is not None:
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    logger.exception("컨베이어 포트 닫기 실패")
            self._state.update(running=False, rpm=0.0, sps=0.0)

    # ------------------------------------------------------------------ 통신
    def _command(self, command: str) -> str:
        """한 줄 보내고 한 줄 받는다. "ERR: " 응답은 예외로 올린다."""
        with self._lock:
            if self._simulate:
                return self._simulated(command)

            ser = self._ensure_open()
            assert ser is not None
            # 3축 펌웨어는 "M<n> " 접두사로 모터를 고른다. motor=0 이면 접두사
            # 없이 보낸다(단일 모터 펌웨어 호환; 펌웨어 쪽 기본값은 M1).
            wire = f"M{self._cfg.motor} {command}" if self._cfg.motor else command
            try:
                # 이전 명령의 늦은 응답이 남아 있으면 이번 응답으로 오해한다.
                ser.reset_input_buffer()
                ser.write(f"{wire}\n".encode("ascii"))
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                self._invalidate()
                raise ConveyorError(f"명령 전송 실패: {exc}") from exc

            reply = self._read_reply(ser, command)
            if reply.startswith("ERR"):
                raise ConveyorError(f"펌웨어가 명령을 거부했습니다: {reply}", detail={"command": command})
            return reply

    def _read_reply(self, ser: serial.Serial, command: str) -> str:
        """의미 있는 첫 줄을 돌려준다. 부팅 로그 같은 잡음은 건너뛴다."""
        deadline = time.monotonic() + max(self._cfg.timeout_sec, 0.5)
        while time.monotonic() < deadline:
            try:
                line = ser.readline().decode("utf-8", "replace").strip()
            except (serial.SerialException, OSError) as exc:
                self._invalidate()
                raise ConveyorError(f"응답 수신 실패: {exc}") from exc
            if not line:
                continue
            if line.startswith(("OK", "ERR", "PONG", "{")):
                return line
            logger.debug("컨베이어 기타 출력: %s", line)
        raise ConveyorError(
            f"'{command}' 에 대한 응답이 없습니다. 포트({self._cfg.port})와 펌웨어를 확인하세요.",
            detail={"command": command},
        )

    def _invalidate(self) -> None:
        """포트가 죽었다. 핸들을 버려 다음 명령에서 재연결하게 한다."""
        ser = _ports.pop(self._cfg.port, None)
        if ser is not None:
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass
        self._state.update(running=False, rpm=0.0, sps=0.0)

    def _refresh(self) -> dict:
        """펌웨어의 STATUS 로 캐시를 갱신한다. 구형 펌웨어면 캐시를 유지한다."""
        try:
            reply = self._command("STATUS")
        except ConveyorError:
            # STATUS 를 모르는 펌웨어(ERR: UNKNOWN COMMAND)도 구동 자체는 된다.
            logger.debug("STATUS 미지원 펌웨어로 판단, 캐시 상태를 유지합니다.")
            return self.state
        if reply.startswith("{"):
            try:
                self._state.update(json.loads(reply))
            except json.JSONDecodeError:
                logger.warning("STATUS 응답을 파싱할 수 없습니다: %s", reply)
        return self.state

    # ------------------------------------------------------------------ 명령
    def start(self) -> dict:
        # 소프트 리밋은 보드가 재부팅되면 사라진다(전원/DFU). 매번 같이 보내면
        # '언제 연결이 새로 열렸는지' 를 따로 추적하지 않아도 항상 적용돼 있다.
        # START 는 빈번한 명령이 아니라 왕복 한 번이 더 붙는 비용은 무시할 만하다.
        if self._cfg.limit_rev:
            self._command(f"LIMIT {self._cfg.limit_rev:.3f}")
        self._command("START")
        self._state["running"] = True
        return self._refresh()

    def set_zero(self) -> dict:
        """현재 위치를 원점으로. 리밋 스위치가 없는 축(LM 가이드)의 기준점이다."""
        self._command("ZERO")
        # 가동 범위는 원점부터 +방향(cw)으로 limit_rev 까지다. 방향이 ccw 인 채로
        # 원점을 잡으면 곧바로 하한에 막혀 START 가 거부된다 — 눌러도 아무 일이
        # 없는 것처럼 보이는 함정이라 여기서 진행 방향을 함께 맞춰준다.
        if self._cfg.limit_rev:
            self._command("DIR CW")
            self._state["dir"] = "cw"
        return self._refresh()

    def stop(self) -> dict:
        self._command("STOP")
        self._state.update(running=False, rpm=0.0, sps=0.0)
        return self._refresh()

    def set_speed_rpm(self, rpm: float) -> dict:
        try:
            value = float(rpm)
        except (TypeError, ValueError):
            raise ValidationError(f"rpm 은 숫자여야 합니다: {rpm!r}") from None
        if not 0.0 < value <= _RPM_HARD_MAX:
            raise ValidationError(f"rpm 은 0 초과 {_RPM_HARD_MAX:.0f} 이하여야 합니다: {value}")
        self._command(f"RPM {value:.3f}")
        self._state["target_rpm"] = value
        return self._refresh()

    def set_direction(self, direction: str) -> dict:
        key = str(direction).strip().lower()
        if key not in ("cw", "ccw"):
            raise ValidationError(f"direction 은 'cw' 또는 'ccw' 여야 합니다: {direction!r}")
        self._command(f"DIR {key.upper()}")
        self._state["dir"] = key
        return self._refresh()

    def set_enabled(self, enabled: bool) -> dict:
        self._command("ENABLE" if enabled else "DISABLE")
        self._state["enabled"] = bool(enabled)
        if not enabled:
            self._state.update(running=False, rpm=0.0, sps=0.0)
        return self._refresh()

    def set_servo_deg(self, deg) -> dict:
        """분류 게이트 서보 각도(0~180). 벨트 위 물체를 양품/불량 라인으로 가른다."""
        try:
            value = int(deg)
        except (TypeError, ValueError):
            raise ValidationError(f"deg 는 정수여야 합니다: {deg!r}") from None
        if not 0 <= value <= 180:
            raise ValidationError(f"deg 는 0~180 이어야 합니다: {value}")
        self._command(f"SERVO {value}")
        self._state["servo_deg"] = value
        return self._refresh()

    # ------------------------------------------------------------------ 상태
    @property
    def state(self) -> dict:
        return {
            "connected": self.connected,
            "port": self._cfg.port,
            "motor": self._cfg.motor,
            # 설정값을 항상 실어 보낸다. 펌웨어 STATUS 의 실제 적용값이 오면 아래
            # _state 가 덮어쓴다 — 연결 전에도 대시보드가 '리밋 있는 축'을 알아야
            # 원점 버튼을 그릴 수 있다.
            "limit_rev": self._cfg.limit_rev,
            # 사람은 mm 로 생각한다. 회전수를 mm 로 바꿔 쓰라고 환산 계수를 같이 준다
            # (펌웨어는 회전수만 알고, mm 는 기구 제원이라 config 에만 있다).
            "mm_per_rev": self._cfg.mm_per_rev,
            "stroke_mm": self._cfg.stroke_mm,
            **self._state,
        }

    def status(self) -> dict:
        """대시보드 폴링용. 연결돼 있지 않으면 포트를 열지 않는다.

        여기서 _ensure_open() 을 부르면 /release 로 놓아준 포트를 상태 폴링이
        다시 붙잡는다. 리셋형 보드(CH340 계열)라면 돌던 모터까지 멈추고,
        BlackPill 이라도 DFU 업로드 중이면 방해가 된다. 열지 않는 게 맞다.
        """
        with self._lock:
            if not self.connected:
                return self.state
            return self._refresh()

    # ------------------------------------------------------------- 시뮬레이션
    def _simulated(self, command: str) -> str:
        """하드웨어 없이 UI/파이프라인만 확인할 때의 응답(simulate=True)."""
        if command == "START":
            self._state.update(running=True, rpm=self._state.get("target_rpm") or 0.0)
            return "OK: STARTED"
        if command == "STOP":
            self._state.update(running=False, rpm=0.0)
            return "OK: STOPPED"
        if command.startswith("RPM "):
            self._state["target_rpm"] = float(command[4:])
            if self._state["running"]:
                self._state["rpm"] = self._state["target_rpm"]
            return f"OK: TARGET_RPM={self._state['target_rpm']}"
        if command.startswith("DIR "):
            self._state["dir"] = command[4:].strip().lower()
            return f"OK: DIR={command[4:].strip().upper()}"
        if command in ("ENABLE", "DISABLE"):
            self._state["enabled"] = command == "ENABLE"
            return f"OK: {command}D"
        if command == "STATUS":
            return json.dumps(self._state)
        if command == "ZERO":
            self._state["pos_rev"] = 0.0
            return "OK: ZERO"
        if command.startswith("LIMIT "):
            self._state["limit_rev"] = float(command[6:])
            self._state.setdefault("pos_rev", 0.0)
            return f"OK: LIMIT={self._state['limit_rev']}"
        if command.startswith("SERVO "):
            self._state["servo_deg"] = int(command[6:])
            return f"OK: SERVO={self._state['servo_deg']}"
        if command == "PING":
            return "PONG"
        return f"ERR: UNKNOWN COMMAND: {command}"
