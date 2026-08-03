"""설정 로딩.

config.yaml 을 dataclass 로 역직렬화한다. dict 를 그대로 들고 다니면
오타(`camera.fps` vs `camera.FPS`)가 런타임까지 살아남기 때문에,
경계에서 한 번만 검증하고 이후로는 타입이 보장된 객체만 쓴다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 카메라 순서는 저장 폴더/파케이 컬럼 순서와 직결되므로 상수로 고정한다.
CAMERA_ORDER: tuple[str, ...] = ("front", "back", "left", "right", "top", "global")
DEFECT_TYPES: tuple[str, ...] = ("scratch", "dent", "discoloration", "crack", "other")


@dataclass(frozen=True)
class CameraConfig:
    name: str
    device: str | int          # /dev/v4l/by-id/... 또는 인덱스 정수
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"       # YUYV 는 640x480@30 이상에서 USB 대역폭 부족

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True)
class ArmConfig:
    role: str                  # "leader" | "follower"
    model: str = "OMX"
    baudrate: int = 1_000_000
    serial_number: str | None = None   # USB iSerial. 있으면 포트 자동 매칭의 1순위 키
    port_hint: str | None = None       # 수동 지정 fallback (/dev/ttyACM0 등)
    joint_ids: tuple[int, ...] = (1, 2, 3, 4, 5)
    home_position: tuple[float, ...] = (0.0, -90.0, 90.0, 0.0, 0.0)


@dataclass(frozen=True)
class SafetyConfig:
    # 1 tick 당 허용되는 최대 관절 변화량(deg). 리더를 갑자기 크게 움직였을 때
    # 팔로워가 튀는 것을 막는다. LeRobot 의 max_relative_target 과 같은 역할.
    max_relative_target: float = 5.0
    # 절대 각도 소프트 리밋(deg). 하드웨어 리밋보다 안쪽으로 잡는다.
    joint_limits: tuple[tuple[float, float], ...] = (
        (-180.0, 180.0), (-120.0, 120.0), (-120.0, 120.0), (-180.0, 180.0), (-100.0, 100.0),
    )
    homing_speed_deg_per_s: float = 30.0
    # 이 시간(ms) 이상 리더 응답이 없으면 텔레오퍼레이션을 정지시킨다.
    watchdog_timeout_ms: int = 500


@dataclass(frozen=True)
class RecordingConfig:
    root: Path = PROJECT_ROOT / "lerobot_data"
    sample_rate_hz: int = 30
    # 프레임 간 타임스탬프 편차가 이 값을 넘으면 metadata 에 경고로 기록
    max_sync_skew_ms: float = 15.0
    jpeg_quality: int = 90
    # 분류용 정지 이미지를 뽑을 시점(에피소드 진행률 0.0~1.0)
    classification_capture_at: float = 0.5
    queue_maxsize: int = 512


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False
    # 하드웨어 없이 UI/파이프라인만 확인할 때 True
    simulate: bool = False


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    arms: dict[str, ArmConfig] = field(default_factory=dict)
    cameras: dict[str, CameraConfig] = field(default_factory=dict)


def _as_tuple_of_pairs(raw) -> tuple[tuple[float, float], ...]:
    return tuple((float(lo), float(hi)) for lo, hi in raw)


def load_config(path: str | Path | None = None) -> AppConfig:
    """YAML 로드. 파일이 없으면 기본값으로 동작한다(데모/CI 편의)."""
    cfg_path = Path(path or os.getenv("OMX_CONFIG", DEFAULT_CONFIG_PATH))
    raw: dict = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    server_raw = raw.get("server", {})
    server = ServerConfig(
        host=server_raw.get("host", "0.0.0.0"),
        port=int(server_raw.get("port", 5000)),
        debug=bool(server_raw.get("debug", False)),
        # 환경변수가 YAML 을 이긴다: 같은 config.yaml 로 데모/실기를 전환하기 위함
        simulate=_env_bool("OMX_SIMULATE", server_raw.get("simulate", False)),
    )

    safety_raw = raw.get("safety", {})
    safety = SafetyConfig(
        max_relative_target=float(safety_raw.get("max_relative_target", 5.0)),
        joint_limits=_as_tuple_of_pairs(
            safety_raw.get("joint_limits", SafetyConfig().joint_limits)
        ),
        homing_speed_deg_per_s=float(safety_raw.get("homing_speed_deg_per_s", 30.0)),
        watchdog_timeout_ms=int(safety_raw.get("watchdog_timeout_ms", 500)),
    )

    rec_raw = raw.get("recording", {})
    recording = RecordingConfig(
        root=Path(rec_raw.get("root", PROJECT_ROOT / "lerobot_data")).expanduser(),
        sample_rate_hz=int(rec_raw.get("sample_rate_hz", 30)),
        max_sync_skew_ms=float(rec_raw.get("max_sync_skew_ms", 15.0)),
        jpeg_quality=int(rec_raw.get("jpeg_quality", 90)),
        classification_capture_at=float(rec_raw.get("classification_capture_at", 0.5)),
        queue_maxsize=int(rec_raw.get("queue_maxsize", 512)),
    )

    arms: dict[str, ArmConfig] = {}
    for role, node in (raw.get("arms") or {}).items():
        node = node or {}
        arms[role] = ArmConfig(
            role=role,
            model=node.get("model", "OMX"),
            baudrate=int(node.get("baudrate", 1_000_000)),
            serial_number=node.get("serial_number"),
            port_hint=node.get("port_hint"),
            joint_ids=tuple(node.get("joint_ids", (1, 2, 3, 4, 5))),
            home_position=tuple(float(v) for v in node.get("home_position", (0, -90, 90, 0, 0))),
        )
    for role in ("leader", "follower"):
        arms.setdefault(role, ArmConfig(role=role))

    cameras: dict[str, CameraConfig] = {}
    cam_raw = raw.get("cameras") or {}
    for idx, name in enumerate(CAMERA_ORDER):
        node = cam_raw.get(name) or {}
        cameras[name] = CameraConfig(
            name=name,
            device=node.get("device", idx),
            width=int(node.get("width", 640)),
            height=int(node.get("height", 480)),
            fps=int(node.get("fps", 30)),
            fourcc=node.get("fourcc", "MJPG"),
        )

    return AppConfig(server=server, safety=safety, recording=recording, arms=arms, cameras=cameras)


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return bool(default)
    return val.strip().lower() in {"1", "true", "yes", "on"}
