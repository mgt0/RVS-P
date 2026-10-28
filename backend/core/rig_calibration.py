"""리그 캘리브레이션 — ArUco 마커 큐브 하나로 카메라 4대의 내부·외부 파라미터를 잡는다.

왜 필요한가
-----------
캘리브레이션 없는 visual hull 은 '공칭 시선 방향 + 정사영 + 뷰마다 바운딩박스 정규화'
라는 가정 위에 서 있다. 실제 리그는 광각 렌즈(원근 왜곡이 크다)이고 카메라 위치도
설계값과 다르다. 그 차이가 그대로 형상 오차가 된다(실측 2026-10-12: 50mm 정육면체가
덩어리로 나오고 CAD 정합 0.12~0.14).

카메라마다 K(렌즈)·왜곡·R·t(자세)를 알면 복셀을 실제 이미지 픽셀로 정확히 투영할 수
있어 형상이 mm 단위로 나오고, 정규화 가정이 필요 없다.

왜 체커보드가 아니라 마커 큐브인가
---------------------------------
수평 카메라(C2·C4, 고도 0°)는 벨트 위에 눕힌 평판을 모서리로만 본다. 하단 카메라(C1)는
유리 아래에서 위를 본다. 모든 카메라가 한 번에 볼 수 있는 건 '면마다 마커를 붙인
큐브'다. 카메라마다 한 면만 보여도 전부 같은 좌표계(큐브 좌표계)로 묶인다.

좌표계
------
월드 = 샷 0 에서 정지 위치에 놓인 큐브의 좌표계. 원점은 큐브 중심, +X 오른쪽, +Y 위,
+Z 앞(C2·C4 수평 카메라 사이 방향) — reconstruct3d.VIEW_DIRECTIONS 와 같은 관례다.
큐브 밑면이 벨트에 닿아 있으므로 벨트 면은 y = -cube_mm/2.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .storage import imread, imwrite

logger = logging.getLogger(__name__)

ARUCO_DICT = cv2.aruco.DICT_4X4_50
CUBE_MM = 50.0
MARKER_MM = 40.0

# 면 정의: 마커 id → (바깥 법선 n, 마커 오른쪽 u, 마커 위 v). u × v = n.
# '밖에서 그 면을 바라볼 때' 마커가 바로 서 보이는 방향이다. scripts/make_calib_cube.py 의
# 전개도는 접었을 때 이 방향이 되도록 인쇄한다(tests/test_scan3d.py 가 접기를 계산해 확인).
FACES: dict[int, tuple[tuple[float, float, float], ...]] = {
    0: ((0, 1, 0), (1, 0, 0), (0, 0, -1)),    # 위   (+Y) — 앞이 아래쪽으로 보이게 내려다본다
    1: ((0, -1, 0), (1, 0, 0), (0, 0, 1)),    # 아래 (-Y) — 앞이 위쪽으로 보이게 올려다본다
    2: ((0, 0, 1), (1, 0, 0), (0, 1, 0)),     # 앞   (+Z)
    3: ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),   # 뒤   (-Z)
    4: ((1, 0, 0), (0, 0, -1), (0, 1, 0)),    # 오른쪽 (+X)
    5: ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),    # 왼쪽 (-X)
}

# 작업 공간(복셀 격자) 반변. 물체는 정지 위치 중심 ±45mm, 벨트에서 90mm 높이까지.
# 이 리그의 부품(50mm 큐브)에 여유를 둔 값이다. 큰 부품을 올리면 키울 것.
WORK_HALF_MM = 45.0

# 초기 수평 화각 추정. Arducam 광각 모듈의 대략값이고, calibrateCamera 가 다시 맞춘다.
INITIAL_HFOV_DEG = 70.0

# 이보다 재투영 오차가 크면 캘리브레이션을 믿기 어렵다(픽셀).
RMS_WARN_PX = 2.0

RIG_FILE = "rig_calibration.json"
SHOTS_DIR = "rig_calib_shots"


def marker_corners_3d(marker_id: int, cube_mm: float = CUBE_MM, marker_mm: float = MARKER_MM) -> np.ndarray:
    """마커 네 꼭짓점의 월드 좌표. ArUco 검출 순서(TL, TR, BR, BL)와 같다."""
    n, u, v = (np.asarray(a, np.float64) for a in FACES[marker_id])
    c, h = n * cube_mm / 2.0, marker_mm / 2.0
    return np.array([c + (-u + v) * h, c + (u + v) * h, c + (u - v) * h, c + (-u - v) * h], np.float32)


def detect_markers(image: np.ndarray) -> dict[int, np.ndarray]:
    """큐브 마커만 골라 {id: (4,2) 코너}. 다른 id 는 버린다."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(ARUCO_DICT), params)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None:
        return {}
    return {int(i): c.reshape(4, 2) for c, i in zip(corners, ids.ravel()) if int(i) in FACES}


@dataclass
class CameraModel:
    K: np.ndarray
    dist: np.ndarray
    rvec: np.ndarray
    tvec: np.ndarray
    size: tuple[int, int]            # (w, h)
    rms: float = 0.0
    markers_seen: list[int] = field(default_factory=list)
    shots_used: int = 0
    # 초점거리를 풀 근거가 없어(_focal_unobservable) 화각 가정값(INITIAL_HFOV_DEG)으로 고정했는지.
    # True 면 이 카메라의 거리(=치수)는 가정값만큼만 맞다 — 기울인 샷을 더 찍어 다시 계산할 것.
    focal_fixed: bool = False

    def __post_init__(self) -> None:
        self.K = np.asarray(self.K, np.float64).reshape(3, 3)
        self.dist = np.asarray(self.dist, np.float64).ravel()
        self.rvec = np.asarray(self.rvec, np.float64).ravel()
        self.tvec = np.asarray(self.tvec, np.float64).ravel()
        self.R = cv2.Rodrigues(self.rvec)[0]
        # 왜곡 다항식은 시야 밖에서 되접혀 엉뚱한 픽셀로 간다. 이미지 모서리의 정규화
        # 반경보다 먼 점은 투영 결과를 믿지 않는다.
        w, h = self.size
        corners = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float64)
        und = cv2.undistortPoints(corners.reshape(-1, 1, 2), self.K, self.dist).reshape(-1, 2)
        self._r2_max = float((und ** 2).sum(axis=1).max()) * 1.1

    @property
    def center(self) -> np.ndarray:
        """카메라 위치(월드, mm)."""
        return -self.R.T @ self.tvec

    def depth(self, pts: np.ndarray) -> np.ndarray:
        """카메라 광축 방향 거리(mm). 작을수록 가깝다."""
        return np.asarray(pts, np.float64).reshape(-1, 3) @ self.R[2] + self.tvec[2]

    def project(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """월드 점 → (픽셀 uv (N,2), 유효 여부). 카메라 뒤·화면 밖·왜곡 신뢰 범위 밖은 무효."""
        pts = np.asarray(pts, np.float64).reshape(-1, 3)
        cam = pts @ self.R.T + self.tvec
        z = cam[:, 2]
        front = z > 1e-3
        zs = np.where(front, z, 1.0)
        ok = front & (((cam[:, 0] / zs) ** 2 + (cam[:, 1] / zs) ** 2) <= self._r2_max)
        uv = cv2.projectPoints(pts.reshape(-1, 1, 3), self.rvec, self.tvec, self.K, self.dist)[0]
        uv = uv.reshape(-1, 2)
        w, h = self.size
        ok &= (uv[:, 0] >= 0) & (uv[:, 0] <= w - 1) & (uv[:, 1] >= 0) & (uv[:, 1] <= h - 1)
        return uv, ok

    def to_dict(self) -> dict:
        return {
            "K": self.K.round(4).tolist(), "dist": self.dist.round(6).tolist(),
            "rvec": self.rvec.round(6).tolist(), "tvec": self.tvec.round(4).tolist(),
            "size": list(self.size), "rms": round(float(self.rms), 4),
            "markers_seen": self.markers_seen, "shots_used": self.shots_used,
            "focal_fixed": self.focal_fixed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CameraModel":
        return cls(d["K"], d["dist"], d["rvec"], d["tvec"], tuple(d["size"]),
                   d.get("rms", 0.0), d.get("markers_seen", []), d.get("shots_used", 0),
                   bool(d.get("focal_fixed", False)))


@dataclass
class Rig:
    cameras: dict[str, CameraModel]
    cube_mm: float = CUBE_MM

    def work_box(self) -> tuple[np.ndarray, np.ndarray]:
        """작업 공간 (lo, hi) mm. 벨트 아래는 넣지 않는다 — 유리에 비친 물체 반사가
        수평 카메라 실루엣에 섞여 들어와도 벨트 밑으로 형상이 자라지 않게."""
        belt = -self.cube_mm / 2.0
        lo = np.array([-WORK_HALF_MM, belt, -WORK_HALF_MM])
        return lo, lo + 2 * WORK_HALF_MM

    def grid(self, res: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """복셀 중심 (res³,3) mm, lo, 간격."""
        lo, hi = self.work_box()
        step = (hi - lo) / (res - 1)
        idx = np.arange(res)
        g = np.stack(np.meshgrid(idx, idx, idx, indexing="ij"), axis=-1).reshape(-1, 3)
        return lo + g * step, lo, step

    def auto_roi(self, name: str, margin: float = 0.05) -> tuple[float, float, float, float] | None:
        """작업 공간 박스가 이 카메라 화면에서 차지하는 영역(정규화). config 에 roi 가 없을 때 쓴다."""
        cam = self.cameras[name]
        lo, hi = self.work_box()
        t = np.linspace(0, 1, 9)
        a, b = np.meshgrid(t, t)
        a, b = a.ravel(), b.ravel()
        pts = []
        for axis in range(3):
            for side in (0.0, 1.0):
                p = np.empty((a.size, 3))
                p[:, axis] = side
                p[:, (axis + 1) % 3], p[:, (axis + 2) % 3] = a, b
                pts.append(lo + p * (hi - lo))
        uv, ok = cam.project(np.concatenate(pts))
        if ok.sum() < 4:
            return None
        w, h = cam.size
        (x0, y0), (x1, y1) = uv[ok].min(axis=0), uv[ok].max(axis=0)
        mx, my = (x1 - x0) * margin, (y1 - y0) * margin
        return (max(0.0, (x0 - mx) / w), max(0.0, (y0 - my) / h),
                min(1.0, (x1 + mx) / w), min(1.0, (y1 + my) / h))


def _pose(obj: np.ndarray, img: np.ndarray, K, dist, nominal) -> tuple[np.ndarray, np.ndarray]:
    """샷 0 의 카메라 자세. 마커 한 장(평면 4점)만 보이면 해가 둘 나올 수 있다(평면 자세
    모호성 — 기울기 방향이 뒤집힌 해). 재투영 오차로 못 가르면 config 의 공칭 시선
    방향(view_dir)에 가까운 쪽을 고른다. 하단 C1 처럼 한 면만 보는 카메라에서 생긴다."""
    if len(obj) > 4:
        ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_ITERATIVE)
        return rvec.ravel(), tvec.ravel()
    _, rvecs, tvecs, errs = cv2.solvePnPGeneric(obj, img, K, dist, flags=cv2.SOLVEPNP_IPPE)
    cands = sorted(zip(rvecs, tvecs, np.ravel(errs)), key=lambda c: c[2])
    if len(cands) > 1 and nominal is not None and cands[1][2] < 1.5 * max(cands[0][2], 0.5):
        nd = np.asarray(nominal, float) / (np.linalg.norm(nominal) or 1.0)

        def agree(c):
            R = cv2.Rodrigues(c[0])[0]
            center = -R.T @ c[1].ravel()
            return float(center @ nd / (np.linalg.norm(center) or 1.0))
        cands.sort(key=agree, reverse=True)
    return cands[0][0].ravel(), cands[0][1].ravel()


def _focal_unobservable(objs: list[np.ndarray], imgs: list[np.ndarray], K0: np.ndarray) -> bool:
    """초점거리를 풀 근거가 없는지: 매 샷 마커 한 장(평면 하나)만 보였고, 그 면이 카메라에
    대해 거의 같은 각도(10° 이내)였으면 그렇다.

    정면으로 본 평면에서는 초점거리와 거리가 서로 상쇄돼 어느 쪽도 정해지지 않는다. 하단 C1 은
    큐브 밑면만 보는데, 큐브를 벨트 위에서 돌리기만 하면 밑면은 늘 같은 각도다 — 합성 실측:
    돌리기만 한 3샷에서 C1 위치 오차 768mm(초점 2129px, 참값 480). 기울인 샷이 하나라도
    있으면 풀린다(같은 조건 2.2mm).
    """
    normals = []
    for obj, img in zip(objs, imgs):
        if len(obj) > 4:
            return False                    # 한 샷에서라도 두 면 이상(비평면)이 보였다
        _, rvecs, _, _ = cv2.solvePnPGeneric(obj, img, K0, None, flags=cv2.SOLVEPNP_IPPE)
        n = np.cross(obj[1] - obj[0], obj[3] - obj[0]).astype(np.float64)
        normals.append(cv2.Rodrigues(rvecs[0])[0] @ (n / np.linalg.norm(n)))
    cos_min = min(abs(float(a @ b)) for a in normals for b in normals)
    return cos_min > math.cos(math.radians(10))


def calibrate(
    shots: list[dict[str, np.ndarray]],
    *,
    cube_mm: float = CUBE_MM,
    marker_mm: float = MARKER_MM,
    nominal_dirs: dict[str, tuple[float, float, float]] | None = None,
) -> tuple[Rig, dict[str, str]]:
    """샷들 → (Rig, 실패한 카메라{이름: 이유}).

    shots[0] 은 정지 위치에 '앞(ID 2) 면을 앞으로' 놓고 찍은 기준 샷이다. 외부 파라미터
    (월드 좌표계)는 이 샷에서 정한다. 나머지 샷은 큐브를 조금씩 돌리고 한두 장은 기울여 찍어
    초점거리를 더 정확히 맞추는 데만 쓴다(기울인 샷이 없으면 하단 C1 은 초점거리를 못 푼다 —
    _focal_unobservable).
    """
    names = sorted({n for s in shots for n in s})
    cams: dict[str, CameraModel] = {}
    failed: dict[str, str] = {}
    for name in names:
        objs, imgs, idx, seen0, size = [], [], [], [], None
        for i, shot in enumerate(shots):
            img = shot.get(name)
            if img is None:
                continue
            size = (img.shape[1], img.shape[0])
            det = detect_markers(img)
            if not det:
                continue
            ids = sorted(det)
            objs.append(np.concatenate([marker_corners_3d(k, cube_mm, marker_mm) for k in ids]))
            imgs.append(np.concatenate([det[k] for k in ids]).astype(np.float32))
            idx.append(i)
            if i == 0:
                seen0 = ids
        if 0 not in idx:
            failed[name] = "기준 샷(첫 번째)에서 마커를 찾지 못했습니다 — 큐브가 이 카메라에 보이게 놓으세요"
            continue

        w, h = size
        f0 = w / (2 * math.tan(math.radians(INITIAL_HFOV_DEG) / 2))
        K0 = np.array([[f0, 0, (w - 1) / 2], [0, f0, (h - 1) / 2], [0, 0, 1]], np.float64)
        # 왜곡 계수는 풀지 않는다. 큐브가 화면 가운데 작게 찍혀 왜곡을 가를 근거가 없고, 풀게
        # 두면 초점거리와 서로 상쇄하며 튄다(합성 실측: 왜곡 0 렌즈인데 C2 위치 오차 37mm →
        # 고정하면 6mm). 실루엣도 화면 가운데라 왜곡의 영향은 작다.
        flags = (cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_FIX_PRINCIPAL_POINT
                 | cv2.CALIB_FIX_ASPECT_RATIO | cv2.CALIB_ZERO_TANGENT_DIST
                 | cv2.CALIB_FIX_K1 | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3)
        try:
            focal_fixed = _focal_unobservable(objs, imgs, K0)
            if focal_fixed:
                flags |= cv2.CALIB_FIX_FOCAL_LENGTH
            rms, K, dist, _, _ = cv2.calibrateCamera(objs, imgs, (w, h), K0, np.zeros(5), flags=flags)
            rvec, tvec = _pose(objs[0], imgs[0], K, dist, (nominal_dirs or {}).get(name))
        except cv2.error as exc:
            failed[name] = f"계산 실패: {exc.err}"
            continue
        cams[name] = CameraModel(K, dist, rvec, tvec, (w, h), float(rms), seen0, len(imgs), focal_fixed)
        if focal_fixed:
            logger.warning("%s 는 초점거리를 풀 근거가 없어 화각 %.0f° 가정값을 썼습니다 — "
                           "큐브를 기울인 샷을 추가하세요", name, INITIAL_HFOV_DEG)
        if rms > RMS_WARN_PX:
            logger.warning("%s 재투영 오차 %.2fpx — 마커가 흐리거나 너무 작게 찍혔을 수 있습니다", name, rms)
    return Rig(cams, cube_mm), failed


# ------------------------------------------------------------------ 저장/불러오기
def save_rig(root: Path, rig: Rig, failed: dict[str, str], *, marker_mm: float, shots: int) -> dict:
    data = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cube_mm": rig.cube_mm, "marker_mm": marker_mm, "shots": shots,
        "cameras": {n: c.to_dict() for n, c in rig.cameras.items()},
        "failed": failed,
    }
    (root / RIG_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_rig(root: Path) -> Rig | None:
    """없거나 깨졌으면 None — 그러면 재구성은 기존 정사영 경로로 돈다."""
    path = root / RIG_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cams = {n: CameraModel.from_dict(d) for n, d in data.get("cameras", {}).items()}
        cube_mm = float(data.get("cube_mm", CUBE_MM))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, cv2.error):
        # null 필드(TypeError)와 객체가 아닌 JSON(AttributeError)도 '깨진 파일'이다. 새면 load_rig 를 부르는
        # 모든 재구성이 500 이 된다. cube_mm 도 try 안에서 읽어 거기서 나는 오류가 새지 않게 한다.
        logger.warning("리그 캘리브레이션 파일을 읽을 수 없습니다: %s", path, exc_info=True)
        return None
    return Rig(cams, cube_mm) if cams else None


def rig_summary(root: Path) -> dict:
    path = root / RIG_FILE
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):      # 객체가 아닌 JSON 은 load_rig 처럼 '없음' — 여기서 새면 상태 조회가 500 이다
        data = {}
    cams = data.get("cameras")
    cams = {n: c for n, c in cams.items() if isinstance(c, dict)} if isinstance(cams, dict) else {}
    return {
        "calibrated": bool(cams),
        "created_at": data.get("created_at"),
        "cameras": {n: {"rms": c.get("rms"), "markers_seen": c.get("markers_seen"),
                        "focal_fixed": bool(c.get("focal_fixed", False))}
                    for n, c in cams.items()},
        "failed": data.get("failed") or {},
        "pending_shots": len(list_shots(root)),
    }


def list_shots(root: Path) -> list[Path]:
    d = root / SHOTS_DIR
    return sorted(p for p in d.iterdir() if p.is_dir()) if d.exists() else []


def save_shot(root: Path, frames: dict[str, np.ndarray]) -> tuple[int, dict[str, list[int]]]:
    """샷을 저장하고 (번호, 카메라별 검출 마커). 첫 샷이 기준(월드 좌표계)이 된다."""
    n = len(list_shots(root))
    out = root / SHOTS_DIR / f"{n:02d}"
    out.mkdir(parents=True, exist_ok=True)
    for name, img in frames.items():
        imwrite(out / f"{name}.jpg", img, 95)
    return n, {name: sorted(detect_markers(img)) for name, img in frames.items()}


def load_shots(root: Path) -> list[dict[str, np.ndarray]]:
    shots = []
    for d in list_shots(root):
        shot = {p.stem: img for p in sorted(d.glob("*.jpg")) if (img := imread(p)) is not None}
        shots.append(shot)
    return shots


def clear_shots(root: Path) -> None:
    shutil.rmtree(root / SHOTS_DIR, ignore_errors=True)
