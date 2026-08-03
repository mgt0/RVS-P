"""도메인 예외 정의.

API 계층은 이 예외들만 잡아서 일관된 JSON 에러 바디로 변환한다.
(Flask의 errorhandler 한 곳에서 처리 → 각 라우트에서 try/except 반복 제거)
"""

from __future__ import annotations


class OmxError(Exception):
    """모든 도메인 예외의 루트. HTTP 상태코드와 머신리더블 코드를 갖는다."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.message, "detail": self.detail},
        }


class ArmPortNotFoundError(OmxError):
    """리더/팔로워 중 '어느 쪽'을 못 찾았는지 반드시 담는다.

    요구사항: "실패 시 어느 arm인지 명시하는 에러 표시".
    role 필드를 detail에 넣어 프론트가 해당 카드만 붉게 표시할 수 있게 한다.
    """

    status_code = 404
    code = "arm_port_not_found"

    def __init__(self, role: str, message: str | None = None, **kw) -> None:
        super().__init__(
            message or f"{role.upper()} arm 포트를 찾지 못했습니다. USB 연결과 udev 권한을 확인하세요.",
            detail={"role": role, **kw},
        )
        self.role = role


class ArmConnectionError(OmxError):
    status_code = 503
    code = "arm_connection_failed"

    def __init__(self, role: str, message: str, **kw) -> None:
        super().__init__(message, detail={"role": role, **kw})
        self.role = role


class CameraUnavailableError(OmxError):
    status_code = 503
    code = "camera_unavailable"

    def __init__(self, name: str, message: str | None = None, **kw) -> None:
        super().__init__(
            message or f"카메라 '{name}' 를 열 수 없습니다. 장치 경로와 사용 중인 프로세스를 확인하세요.",
            detail={"camera": name, **kw},
        )


class UnknownCameraError(OmxError):
    """설정에 없는 카메라 이름. 재시도해도 소용없으므로 404."""

    status_code = 404
    code = "unknown_camera"


class SafetyLimitError(OmxError):
    """max_relative_target 초과 등 안전 제한 위반."""

    status_code = 409
    code = "safety_limit_exceeded"


class RecordingStateError(OmxError):
    """IDLE 상태에서 stop, RECORDING 중 start 같은 잘못된 상태 전이."""

    status_code = 409
    code = "invalid_recording_state"


class ValidationError(OmxError):
    status_code = 400
    code = "validation_failed"
