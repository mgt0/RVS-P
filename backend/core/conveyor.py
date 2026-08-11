"""컨베이어(ESP32+TB6600+NEMA17) 제어. USB-UART로 한 줄 텍스트 명령을 주고받는다.

프로토콜은 firmware/conveyor_stepper/conveyor_stepper.ino 와 1:1로 맞춰져 있다.
보내는 것: START / STOP / RPM <값> / DIR CW|CCW / ENABLE / DISABLE / STATUS
받는 것: "OK: ...", "ERR: ...", 또는 STATUS 에 대한 JSON 한 줄.

두 가지가 이 클래스의 존재 이유다.

1) 연결을 명령마다 새로 열지 않는다. 대부분의 ESP32 보드는 시리얼 포트를 열 때
   DTR 토글로 재부팅되므로, 매번 열고 닫으면 명령이 재부팅 타이밍과 겹쳐 씹힌다.
2) 그래서 '처음 여는 순간'만은 반드시 부팅을 기다려야 한다. 포트를 열자마자
   START 를 쓰면 부트로더가 그 바이트를 버리고, 서버는 성공했다고 응답하는데
   컨베이어는 조용히 멈춰 있다. _wait_boot() 가 READY 배너를 기다리는 이유다.
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
# ESP32 리셋 후 READY 까지 실측 1초 남짓. 부트로더 지연을 감안해 여유를 둔다.
_BOOT_TIMEOUT_SEC = 5.0
# 모터축 RPM 상한. 여기서는 명백한 오입력만 막고, 실제 범위 판정은 펌웨어에 맡긴다
# (마이크로스텝 값이 DIP 스위치와 함께 펌웨어에 박혀 있다).
# 2000RPM = 마이크로스텝 8 기준 53,333 sps 로 펌웨어의 MAX_SPS(60000) 안에 들어간다.
# 주의: 스텝모터는 1000RPM 부근부터 토크가 급격히 떨어진다. 상한을 열어둔 것이
# 그 속도에서 탈조하지 않는다는 뜻은 아니다. 실제 한계는 부하를 걸고 찾아야 한다.
_RPM_HARD_MAX = 10000.0


class ConveyorController:
    def __init__(self, cfg: ConveyorConfig, *, simulate: bool = False) -> None:
        self._cfg = cfg
        self._simulate = simulate
        self._ser: serial.Serial | None = None
        # 중첩 획득이 있다(_command → _refresh → _command). RLock 이어야 한다.
        self._lock = threading.RLock()
        self._state: dict = {
            "running": False,
            "enabled": True,
            "dir": "cw",
            "rpm": 0.0,
            "target_rpm": None,
            "sps": 0.0,
            "target_sps": None,
        }

    # ------------------------------------------------------------------ 연결
    @property
    def connected(self) -> bool:
        if self._simulate:
            return True
        return self._ser is not None and self._ser.is_open

    def _ensure_open(self) -> serial.Serial | None:
        if self._simulate:
            return None
        if not self._cfg.port:
            raise ConveyorError("config.yaml 의 conveyor.port 가 설정되지 않았습니다.")
        if self._ser is None or not self._ser.is_open:
            try:
                ser = serial.Serial(self._cfg.port, self._cfg.baudrate, timeout=self._cfg.timeout_sec)
            except (serial.SerialException, OSError) as exc:
                raise ConveyorError(f"{self._cfg.port} 포트를 열 수 없습니다: {exc}") from exc
            self._ser = ser
            # 방금 리셋됐으므로 캐시된 상태는 전부 무효다.
            self._state.update(running=False, enabled=True, rpm=0.0, sps=0.0)
            self._wait_boot(ser)
        return self._ser

    def _wait_boot(self, ser: serial.Serial) -> None:
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
            if line.startswith(_READY_PREFIX):
                logger.info("컨베이어 펌웨어 준비됨 (%s)", self._cfg.port)
                return
        # 배너를 못 봤어도 계속 간다. 이미 부팅이 끝난 뒤 포트를 다시 연 경우
        # (리셋이 안 걸리는 보드/USB 허브) 배너가 안 오는 게 정상이다.
        logger.warning(
            "컨베이어 READY 배너를 %.0f초 안에 받지 못했습니다. 그대로 진행합니다.", _BOOT_TIMEOUT_SEC
        )
        ser.reset_input_buffer()

    def disconnect(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except (serial.SerialException, OSError):
                    logger.exception("컨베이어 포트 닫기 실패")
                self._ser = None
            self._state.update(running=False, rpm=0.0, sps=0.0)

    # ------------------------------------------------------------------ 통신
    def _command(self, command: str) -> str:
        """한 줄 보내고 한 줄 받는다. "ERR: " 응답은 예외로 올린다."""
        with self._lock:
            if self._simulate:
                return self._simulated(command)

            ser = self._ensure_open()
            assert ser is not None
            try:
                # 이전 명령의 늦은 응답이 남아 있으면 이번 응답으로 오해한다.
                ser.reset_input_buffer()
                ser.write(f"{command}\n".encode("ascii"))
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
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
            self._ser = None
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
        self._command("START")
        self._state["running"] = True
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

    # ------------------------------------------------------------------ 상태
    @property
    def state(self) -> dict:
        return {"connected": self.connected, "port": self._cfg.port, **self._state}

    def status(self) -> dict:
        """대시보드 폴링용. 연결돼 있지 않으면 포트를 열지 않는다.

        여기서 _ensure_open() 을 부르면 /release 로 놓아준 포트를 상태 폴링이
        다시 붙잡아 ESP32 를 리셋시킨다(= 돌던 모터가 멈춘다). 열지 않는 게 맞다.
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
        if command == "PING":
            return "PONG"
        return f"ERR: UNKNOWN COMMAND: {command}"
