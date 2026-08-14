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
# cam1~cam5 는 물리 배치가 아니라 슬롯 번호다. 각 슬롯이 물체를 어느 방향에서
# 보는지는 reconstruct3d.VIEW_DIRECTIONS 가 정한다(3D 재구성이 그 값을 쓴다).
CAMERA_ORDER: tuple[str, ...] = ("cam1", "cam2", "cam3", "cam4", "cam5", "global")
DEFECT_TYPES: tuple[str, ...] = ("scratch", "dent", "discoloration", "crack", "other")


@dataclass(frozen=True)
class CameraConfig:
    name: str
    device: str | int          # /dev/v4l/by-id/... 또는 인덱스 정수
    # 화면에 보여줄 이름("하단 뷰"). 슬롯 키(cam1..cam5)는 녹화 폴더명·parquet
    # 컬럼명이라 바꾸지 않는다 — 표시만 이 값으로 한다. 비면 슬롯 키를 쓴다.
    label: str = ""
    # 장치 이름으로 지목한다(권장). USB 를 다시 꽂거나 재부팅하면 인덱스는 밀리지만
    # 이름은 그대로다 — arm 의 serial_number 와 같은 역할이다.
    # 같은 이름이 여러 대면 nth 로 몇 번째인지 고른다(0-based).
    # 이름이 지정되면 device 는 무시되고 DirectShow 로 연다(이름의 출처가 DirectShow 라
    # MSMF 인덱스와 순서가 다르다 — 실측: DSHOW 0=ACER, MSMF 1=ACER).
    match_name: str | None = None
    nth: int = 0
    # 카메라를 거꾸로 매달았을 때 상하 반전. 프리뷰·녹화·분류 이미지 전부에 적용된다
    # (한 곳에서 뒤집어야 학습 데이터와 눈으로 본 화면이 어긋나지 않는다).
    vflip: bool = False
    # 3D 재구성용: '물체 → 카메라' 방향 벡터 [x, y, z]. +X 오른쪽, +Y 위, +Z 앞.
    # 크기는 무관(정규화한다). None 이면 reconstruct3d 의 기본값을 쓴다.
    view_dir: tuple[float, float, float] | None = None
    # 자동 화이트밸런스를 끈다. 배경 차분을 쓰려면 사실상 필수 —
    # 물체가 들어올 때마다 색온도가 바뀌면 배경 참조가 곧바로 무효가 된다.
    lock_white_balance: bool = False
    # 고정할 색온도(K). None 이면 자동 WB 만 끄고 현재 값을 유지한다.
    white_balance: float | None = None
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"       # YUYV 는 640x480@30 이상에서 USB 대역폭 부족
    # 수동 노출(log2 초: -6 = 1/64초). None 이면 카메라 자동 노출에 맡긴다.
    # 노출이 짧을수록 조명 플리커가 가로 줄무늬로 드러난다 — 실측(60Hz 형광등):
    #   자동 2.73%, -8 11.22%, -7 0.86%, -6 0.65%, -5 0.31%, -4 0.07%
    # 길게 할수록 좋지만 밝은 곳이 날아가므로 brightness 로 되돌려 균형을 잡는다.
    exposure: float | None = None
    # 센서 밝기(UVC BRIGHTNESS, 대략 -64~64). 노출을 길게 잡아 플리커를 없앤 뒤
    # 여기서 밝기를 낮춰 포화를 막는다. None 이면 카메라 기본값을 그대로 둔다.
    brightness: float | None = None

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
    queue_maxsize: int = 512


@dataclass(frozen=True)
class ConveyorConfig:
    # 컨베이어 제어 보드(STM32F411 BlackPill)와 USB CDC 로 연결.
    # 비어있으면 API가 503을 반환한다.
    port: str = ""
    baudrate: int = 115200
    timeout_sec: float = 2.0
    # 대시보드에 보여줄 사람용 이름 ("공급 벨트" 등). 비면 키 이름을 쓴다.
    label: str = ""
    # 보드 하나(BlackPill 3축 펌웨어)가 스텝모터 3개(M1/M2/M3)를 물고 있다. 같은 port 를
    # 쓰는 항목들이 이 번호로 자기 모터를 지정한다(1~3). 0 = 프리픽스 없음(단일 모터 호환).
    motor: int = 0
    # 소프트 리밋(LM 가이드처럼 왕복하는 축용). 사람은 mm 로 생각하므로 mm 로 적고,
    # 펌웨어에 보낼 회전수는 limit_rev 가 환산한다.
    #   stroke_mm  : 원점부터 갈 수 있는 거리(mm). 0 이면 리밋 없음(벨트는 무한 회전).
    #   mm_per_rev : 모터 1회전당 이송(mm). 볼스크류면 리드, 벨트 구동이면 풀리 원주.
    # 리밋 스위치가 없어 위치는 펌웨어가 보낸 펄스로 세므로, 전원을 켤 때마다
    # 축을 한쪽 끝에 두고 '원점 설정'을 눌러 기준을 잡아야 한다.
    stroke_mm: float = 0.0
    mm_per_rev: float = 0.0

    @property
    def limit_rev(self) -> float:
        """펌웨어가 받는 단위(회전). 둘 중 하나라도 0 이면 리밋을 걸지 않는다."""
        if not (self.stroke_mm and self.mm_per_rev):
            return 0.0
        return self.stroke_mm / self.mm_per_rev


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False
    # 하드웨어 없이 UI/파이프라인만 확인할 때 True
    simulate: bool = False
    # MJPEG 프리뷰 전용 포트. 브라우저는 '오리진당' 동시 연결이 6개로 묶여 있는데,
    # 카메라 6대 스트림이 연결을 하나씩 영구 점유하면 그 6개를 다 써버려 API 요청이
    # 큐에서 무한 대기한다(실측: 버튼을 눌러도 요청이 나가지 않고 화면이 굳는다).
    # 포트가 다르면 별개 오리진이라 연결 예산도 따로 잡힌다. 0 이면 분리하지 않는다.
    stream_port: int = 5001


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    arms: dict[str, ArmConfig] = field(default_factory=dict)
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    # 벨트가 여러 대일 수 있다(공급용 + 로봇 작업 영역용). 키가 API 경로의 이름이 된다.
    conveyors: dict[str, ConveyorConfig] = field(default_factory=dict)


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
        stream_port=int(server_raw.get("stream_port", 5001)),
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
        node = cam_raw.get(name)
        # cameras 블록이 아예 없으면(테스트/CI) 기본 인덱스로 전부 만든다.
        # 블록이 있는데 이 슬롯이 없거나 enabled: false 면 그 슬롯은 쓰지 않는다.
        # 있지도 않은 카메라를 2초마다 재연결 시도하는 것은 로그만 더럽힌다.
        if cam_raw and (node is None or node.get("enabled") is False):
            continue
        node = node or {}
        cameras[name] = CameraConfig(
            name=name,
            device=node.get("device", idx),
            label=node.get("label") or "",
            match_name=node.get("match_name"),
            nth=int(node.get("nth", 0)),
            vflip=bool(node.get("vflip", False)),
            view_dir=(
                tuple(float(v) for v in node["view_dir"])
                if node.get("view_dir") else None
            ),
            width=int(node.get("width", 640)),
            height=int(node.get("height", 480)),
            fps=int(node.get("fps", 30)),
            fourcc=node.get("fourcc", "MJPG"),
            exposure=None if node.get("exposure") is None else float(node["exposure"]),
            brightness=None if node.get("brightness") is None else float(node["brightness"]),
            lock_white_balance=bool(node.get("lock_white_balance", False)),
            white_balance=(
                None if node.get("white_balance") is None else float(node["white_balance"])
            ),
        )

    # conveyors(복수) 가 우선. 없으면 예전 단일 conveyor 키를 "main" 으로 승격 —
    # 기존 config.yaml 이 수정 없이 계속 동작해야 한다.
    def _conveyor(node: dict) -> ConveyorConfig:
        return ConveyorConfig(
            port=node.get("port") or "",
            baudrate=int(node.get("baudrate", 115200)),
            timeout_sec=float(node.get("timeout_sec", 2.0)),
            label=node.get("label") or "",
            motor=int(node.get("motor", 0)),
            stroke_mm=float(node.get("stroke_mm", 0.0)),
            mm_per_rev=float(node.get("mm_per_rev", 0.0)),
        )

    conveyors: dict[str, ConveyorConfig] = {}
    if raw.get("conveyors"):
        for name, node in raw["conveyors"].items():
            conveyors[name] = _conveyor(node or {})
    else:
        conveyors["main"] = _conveyor(raw.get("conveyor") or {})

    return AppConfig(
        server=server, safety=safety, recording=recording,
        arms=arms, cameras=cameras, conveyors=conveyors,
    )


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return bool(default)
    return val.strip().lower() in {"1", "true", "yes", "on"}
