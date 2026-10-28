"""설정 로딩.

config.yaml 을 dataclass 로 역직렬화한다. dict 를 그대로 들고 다니면
오타(`camera.fps` vs `camera.FPS`)가 런타임까지 살아남기 때문에,
경계에서 한 번만 검증하고 이후로는 타입이 보장된 객체만 쓴다.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 카메라 순서는 저장 폴더/파케이 컬럼 순서와 직결되므로 상수로 고정한다.
# cam2~cam5 는 물리 배치가 아니라 슬롯 번호다. 각 슬롯이 물체를 어느 방향에서
# 보는지는 reconstruct3d.VIEW_DIRECTIONS 가 정한다(3D 재구성이 그 값을 쓴다).
CAMERA_ORDER: tuple[str, ...] = ("cam2", "cam3", "cam4", "cam5")
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
    # 좌우(거울) 반전. 카메라가 뒤에서 비추거나 거울을 거쳐 보는 배치면 필요하다.
    # 주의: 좌우 반전은 형상의 좌우 방향(카이랄리티)을 바꾼다 — 3D 재구성이
    # view_dir 로 세운 좌표계와 어긋나면 형상이 거울상으로 나온다. 실제로 화면이
    # 뒤집혀 보일 때만 켤 것.
    hflip: bool = False
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
    # 조명 플리커 방지 주파수(50/60Hz). 한국은 60. 자동 노출일 때만 효과가 있다.
    # 이 Arducam 은 드라이버 기본값이 50Hz 이고, 열 때마다 50Hz 로 돌아가므로(실측
    # 2026-10-12) 매번 다시 넣는다. None 이면 건드리지 않는다.
    power_line_hz: int | None = None
    # 열린 직후 화면 평균 밝기(0~255)를 이 값으로 맞추고 잠근다. 조명이 바뀌어도
    # 고정 노출처럼 날아가지 않게 하려는 것. None 이면 exposure/brightness 그대로.
    target_mean: float | None = None
    # UVC 대비(0~64, 기본 32)·감마(72~500, 기본 100). brightness 를 많이 내리면 흰색 상한도
    # 같이 내려가(-64 에서 약 183) 화면이 뿌옇게 된다 — 대비를 올리고 감마를 낮춰 어둡게 하면
    # 선명함이 유지된다(실측 2026-10-12, Arducam). None 이면 건드리지 않는다.
    contrast: float | None = None
    gamma: float | None = None
    # 3D 검사 ROI: 물체가 멈추는 자리의 화면 영역 [x0, y0, x1, y1] (0~1, 플립 적용 후 좌표).
    # 실루엣은 이 안에서만 뽑는다 — 배경 캡처 이후 뒤에서 사람·로봇팔이 움직인 것까지
    # 물체로 잡히지 않게. None 이면 리그 캘리브레이션이 있으면 그 작업 공간 투영, 없으면 화면 전체.
    roi: tuple[float, float, float, float] | None = None

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
    # 보드 하나(BlackPill + HW-095 L298N)가 DC 모터 2개(M1=A 채널, M2=B 채널)를 물고 있다. 같은 port 를
    # 쓰는 항목들이 이 번호로 자기 모터를 지정한다(1~2). 0 = 프리픽스 없음(단일 모터 호환).
    motor: int = 0
    # 속도의 기본값(PWM 듀티 %, 0 초과 100 이하). 엔코더가 없어 RPM 을 모르므로 속도는 듀티로만 정한다.
    # 연결을 새로 연 뒤 첫 START 전에 한 번 펌웨어에 보낸다(core/conveyor.py _sync_duty).
    # 0 은 안 된다 — 펌웨어에서 DUTY 0 은 STOP 이라 속도를 정하지 못한다.
    default_duty: float = 40.0


@dataclass(frozen=True)
class CycleConfig:
    """자동 사이클(집기 → 이송 → 검사 → 분류) 손잡이. 기본값은 출발점일 뿐이다 — 현장에서 재서 맞춘다.

    시간은 전부 초다. 벨트 이송 거리는 벨트 속도(belt_duty)와 시간의 곱이라 둘이 한 쌍으로 움직인다.
    """

    # 픽 동작 = scripts/to_waypoints.py 산출 JSON. pick_checkpoint 가 있으면 그쪽이 이긴다.
    pick_waypoints: Path = PROJECT_ROOT / "lerobot_data" / "waypoints" / "pick_place.json"
    pick_checkpoint: Path | None = None
    pick_speed: float = 1.0
    pick_timeout_sec: float = 90.0
    # 집기 궤적이 끝난 뒤 이송(벨트 구동)을 시작하기 전에 기다리는 시간. 그리퍼가 물체를 놓는 동작이 끝나게 한다.
    pick_settle_sec: float = 1.0
    belt: str = "work"            # 물체를 싣는 벨트 키(config conveyors 의 키)
    belt_dir: str = "cw"
    belt_duty: float = 40.0       # 벨트 속도(PWM 듀티 %, 0 초과 100 이하). 사이클이 시작할 때 set_duty 로 정한다
    to_scan_sec: float = 3.0
    settle_sec: float = 0.5
    resolution: int = 96
    # 표면 이상 정점 비율이 이 값 이상이면 불량. 근거는 config.yaml 주석(깨끗한 큐브 0.47% 이하, 펜 자국 1.5% 이상).
    defect_ratio: float = 0.01
    good_run_sec: float = 4.0
    defect_to_branch_sec: float = 1.5
    gate_open_deg: int = 15
    gate_close_deg: int = 130
    gate_hold_sec: float = 2.0
    basket_capacity: int = 2


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
    cycle: CycleConfig = field(default_factory=CycleConfig)


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
            hflip=bool(node.get("hflip", False)),
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
            power_line_hz=None if node.get("power_line_hz") is None else int(node["power_line_hz"]),
            target_mean=None if node.get("target_mean") is None else float(node["target_mean"]),
            contrast=None if node.get("contrast") is None else float(node["contrast"]),
            gamma=None if node.get("gamma") is None else float(node["gamma"]),
            lock_white_balance=bool(node.get("lock_white_balance", False)),
            white_balance=(
                None if node.get("white_balance") is None else float(node["white_balance"])
            ),
            roi=_roi(name, node.get("roi")),
        )

    # conveyors(복수) 가 우선. 없으면 예전 단일 conveyor 키를 "main" 으로 승격 —
    # 기존 config.yaml 이 수정 없이 계속 동작해야 한다.
    def _conveyor(name: str, node: dict) -> ConveyorConfig:
        # 스텝모터·LM 가이드 시절의 키. 사용자 로컬 config.yaml 에 남아 있어도 서버는 떠야 하므로 경고만 남기고 무시한다.
        for old in ("stroke_mm", "mm_per_rev"):
            if old in node:
                logger.warning("config.yaml conveyors.%s.%s: LM 가이드와 함께 사라진 키라 무시합니다. 지워도 됩니다.", name, old)
        raw_duty = node.get("default_duty", ConveyorConfig.default_duty)
        try:
            default_duty = float(raw_duty)
        except (TypeError, ValueError):
            raise ValueError(f"conveyors.{name}.default_duty 는 숫자여야 합니다: {raw_duty!r}") from None
        if not (math.isfinite(default_duty) and 0.0 < default_duty <= 100.0):
            raise ValueError(f"conveyors.{name}.default_duty 는 0 초과 100 이하(%)만 허용합니다: {raw_duty!r}")
        return ConveyorConfig(
            port=node.get("port") or "",
            baudrate=int(node.get("baudrate", 115200)),
            timeout_sec=float(node.get("timeout_sec", 2.0)),
            label=node.get("label") or "",
            motor=int(node.get("motor", 0)),
            default_duty=default_duty,
        )

    conveyors: dict[str, ConveyorConfig] = {}
    if raw.get("conveyors"):
        for name, node in raw["conveyors"].items():
            if name == "lm":
                # LM 가이드(M3)는 하드웨어에서 사라졌다. 옛 절이 남아 있어도 서버는 떠야 한다.
                logger.warning("config.yaml conveyors.lm: LM 가이드가 제거되어 이 절을 무시합니다. 지워도 됩니다.")
                continue
            conveyors[name] = _conveyor(name, node or {})
    else:
        conveyors["main"] = _conveyor("main", raw.get("conveyor") or {})

    return AppConfig(
        server=server, safety=safety, recording=recording,
        arms=arms, cameras=cameras, conveyors=conveyors,
        cycle=_cycle(raw.get("cycle") or {}),
    )


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return bool(default)
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _cycle(node: dict) -> CycleConfig:
    """cycle.* 검증. 잘못된 값을 조용히 쓰면 벨트가 엉뚱한 시간만큼 돌거나 게이트가 범위 밖 각도로 간다 —
    사이클이 도는 중에 알게 되는 것보다 시작할 때 바로 알리는 편이 안전하다."""
    base = CycleConfig()

    def num(key: str, lo: float, hi: float | None = None, *, whole: bool = False, lo_open: bool = False):
        raw = node.get(key, getattr(base, key))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"cycle.{key} 는 숫자여야 합니다: {raw!r}") from None
        ok = math.isfinite(value) and (value > lo if lo_open else value >= lo) and (hi is None or value <= hi)
        if not ok or (whole and value != int(value)):
            bound = f"{lo:g}{' 초과' if lo_open else ' 이상'}" + ("" if hi is None else f" {hi:g} 이하")
            raise ValueError(f"cycle.{key} 는 {'정수이고 ' if whole else ''}{bound}만 허용합니다: {raw!r}")
        return int(value) if whole else value

    def path(key: str) -> Path | None:
        raw = node.get(key, getattr(base, key))
        if raw is None:
            return None
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p     # 상대 경로는 실행 위치가 아니라 레포 루트 기준

    belt = str(node.get("belt", base.belt)).strip()
    belt_dir = str(node.get("belt_dir", base.belt_dir)).strip().lower()
    if not belt:
        raise ValueError("cycle.belt 는 config 의 conveyors 키(예: work)여야 합니다.")
    if belt_dir not in ("cw", "ccw"):
        raise ValueError(f"cycle.belt_dir 는 'cw' 또는 'ccw' 여야 합니다: {node.get('belt_dir')!r}")
    if "belt_rpm" in node:      # 엔코더 없는 DC 모터가 되면서 사라진 키. 옛 config.yaml 이 서버를 죽이지 않게 경고만 남긴다
        logger.warning("config.yaml cycle.belt_rpm: DC 모터로 바뀌어 무시합니다. cycle.belt_duty(PWM 듀티 %%, 기본 %g)를 쓰세요.", base.belt_duty)
    return CycleConfig(
        pick_waypoints=path("pick_waypoints") or base.pick_waypoints,
        pick_checkpoint=path("pick_checkpoint"),
        pick_speed=num("pick_speed", 0.0, 5.0, lo_open=True),
        pick_timeout_sec=num("pick_timeout_sec", 0.0, 600.0, lo_open=True),
        pick_settle_sec=num("pick_settle_sec", 0.0, 60.0),      # 0 이면 기다리지 않는다
        belt=belt,
        belt_dir=belt_dir,
        belt_duty=num("belt_duty", 0.0, 100.0, lo_open=True),     # 0 은 DUTY 0 = STOP 이라 벨트가 서 있다
        to_scan_sec=num("to_scan_sec", 0.0, 300.0, lo_open=True),     # 상한은 단위를 잘못 적은 값(ms 로 3000)을 거르려는 것
        settle_sec=num("settle_sec", 0.0, 3.0),         # /api/scan/capture 와 같은 상한
        resolution=num("resolution", 16, 96, whole=True),   # /api/scan/capture 와 같은 범위
        defect_ratio=num("defect_ratio", 0.0, 1.0, lo_open=True),   # 0 이면 모든 물체가 불량이 된다
        good_run_sec=num("good_run_sec", 0.0, 300.0, lo_open=True),
        defect_to_branch_sec=num("defect_to_branch_sec", 0.0, 300.0, lo_open=True),
        gate_open_deg=num("gate_open_deg", 0, 180, whole=True),     # SERVO 명령 범위
        gate_close_deg=num("gate_close_deg", 0, 180, whole=True),
        gate_hold_sec=num("gate_hold_sec", 0.0, 60.0),
        basket_capacity=num("basket_capacity", 1, whole=True),
    )


def _roi(camera: str, value) -> tuple[float, float, float, float] | None:
    """cameras.*.roi 검증. 잘못된 ROI 를 조용히 쓰면 실루엣이 통째로 사라지거나 엉뚱한 곳을
    잡는데, 그 원인을 3D 화면에서 거꾸로 찾기는 어렵다 — 시작할 때 바로 알린다."""
    if value is None:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        raise ValueError(f"cameras.{camera}.roi 는 [x0, y0, x1, y1] 숫자 4개여야 합니다: {value!r}") from None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError(f"cameras.{camera}.roi 는 0~1 범위에서 x0<x1, y0<y1 이어야 합니다: {value!r}")
    return x0, y0, x1, y1
