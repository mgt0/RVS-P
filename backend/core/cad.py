"""CAD 기준 모델 로딩 (STL).

왜 CAD 를 쓰는가
---------------
5뷰 visual hull 은 원리적 한계가 있다. 오목한 부분은 어떤 실루엣에도 나타나지
않아 복원이 불가능하고, 캘리브레이션이 없어 절대 치수도 나오지 않는다.
양품의 설계 형상을 이미 알고 있다면 그걸 기준으로 쓰는 편이 모든 면에서 낫다.
스캔은 '형상을 만드는' 데가 아니라 '설계와 다른 곳을 찾는' 데 쓴다.

STL 만 직접 지원하는 이유
------------------------
STL 은 삼각형 목록이라 의존성 없이 40줄로 읽는다. STEP/STP 는 B-rep 이라
곡면을 삼각형으로 쪼개는 커널(OpenCASCADE)이 필요한데, 수백 MB 짜리 의존성을
붙이느니 CAD 툴에서 STL 로 내보내는 편이 싸다(어느 툴이든 클릭 한 번이다).
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 이 크기를 넘는 메시는 브라우저로 보내기 전에 줄인다. 정점 하나가 JSON 에서
# 30바이트 안팎이라, 20만개면 6MB 다.
MAX_FACES = 200_000

# z-버퍼 허용 오차([-1,1] 좌표 기준). 같은 면 위의 정점들이 미세한 깊이 차이로
# 서로를 가리지 않도록 둔다. 너무 키우면 뒷면까지 통과한다.
DEPTH_TOLERANCE = 0.05

# 이 폭(화면 대비 비율)보다 얇은 불일치는 정합 오차로 보고 무시한다.
# 키우면 작은 결함을 놓치고, 줄이면 자세 오차가 불량으로 잡힌다.
DEVIATION_MIN_WIDTH = 0.08

# 이 값보다 정합 일치도가 낮으면 비교 결과를 신뢰하지 않고 색칠을 건너뛴다.
# 합성 실측: 같은 형상끼리 0.86, 결손품 0.84. 실제 촬영은 실루엣 잡음 때문에
# 더 낮게 나오므로 여유를 뒀다. 정상품이 통째로 칠해지면 이 값을 올릴 것.
MIN_ALIGNMENT_SCORE = 0.55


def load_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """STL 파일 → (vertices, faces)."""
    return parse_stl(path.read_bytes())


def parse_stl(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    """STL 바이트 → (vertices, faces). 바이너리·ASCII 둘 다 지원한다.

    경로가 아니라 바이트를 받는 이유: 업로드된 파일을 디스크에 쓰기 *전에*
    파싱해보고 진짜 STL 인지 확인하기 위해서다. 저장한 뒤에 검증하면 깨진
    파일이 남아 다음 조회에서 터진다.

    바이너리 판별은 확장자나 첫 5바이트("solid")로 하지 않는다. 일부 CAD 는
    바이너리 STL 의 헤더에도 "solid" 를 써넣어서 그 방법이 틀린다.
    파일 크기가 삼각형 개수와 정확히 맞아떨어지는지로 판정하는 것이 확실하다.
    """
    if len(raw) < 84:
        raise ValueError("STL 로 보기에 너무 작은 파일입니다.")

    n_tri = struct.unpack("<I", raw[80:84])[0]
    if len(raw) == 84 + n_tri * 50:
        tris = _parse_binary(raw, n_tri)
    else:
        tris = _parse_ascii(raw)

    if not len(tris):
        raise ValueError("삼각형을 하나도 읽지 못했습니다. STL 파일이 맞는지 확인하세요.")

    # STL 은 삼각형마다 정점을 중복 저장한다. 같은 좌표를 합쳐 정점 수를 줄이고
    # (보통 1/3 로 준다) 인접 정보가 생겨 음영이 매끄러워진다.
    flat = tris.reshape(-1, 3)
    verts, inverse = np.unique(flat, axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    return verts.astype(np.float32), faces.astype(np.int32)


def _parse_binary(raw: bytes, n_tri: int) -> np.ndarray:
    # 삼각형 하나 = 법선 3 + 정점 9 = float 12개(48B) + 속성 2B
    data = np.frombuffer(raw, dtype=np.uint8, count=n_tri * 50, offset=84)
    data = data.reshape(n_tri, 50)[:, :48].copy()          # 속성 2바이트 버림
    values = data.view("<f4").reshape(n_tri, 12)
    return values[:, 3:].reshape(n_tri, 3, 3)              # 법선은 다시 계산하므로 버린다


def _parse_ascii(raw: bytes) -> np.ndarray:
    verts = []
    for line in raw.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "vertex":
            verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
    arr = np.array(verts, dtype=np.float32)
    if len(arr) % 3:
        arr = arr[: len(arr) - len(arr) % 3]
    return arr.reshape(-1, 3, 3)


def normalize(verts: np.ndarray) -> tuple[np.ndarray, dict]:
    """뷰어용으로 중심 정렬 + 단위 크기 정규화. 원본 치수는 stats 로 돌려준다.

    visual hull 결과와 같은 좌표계로 맞춰야 뷰어와 나중의 비교가 그대로 동작한다.
    """
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    size = hi - lo
    centered = verts - (lo + hi) / 2.0
    scale = float(np.abs(centered).max()) or 1.0
    return centered / scale, {
        "size_mm": [round(float(v), 3) for v in size],   # STL 은 단위가 없다. mm 가정.
        "scale_divisor": round(scale, 6),
    }


def save_model(root: Path, object_id: str, raw: bytes) -> dict:
    """업로드된 STL 을 검증하고 저장한다.

    저장 전에 파싱한다. 신뢰 경계이므로 '확장자가 .stl 이니 STL 이겠지' 로
    넘기지 않는다 — 깨진 파일을 받아두면 나중에 조회할 때마다 터진다.
    """
    verts, faces = parse_stl(raw)          # 여기서 실패하면 저장하지 않는다

    models = root / "models"
    models.mkdir(parents=True, exist_ok=True)
    # 파일명은 object_id 로 강제한다. 업로드된 이름은 신뢰하지 않는다
    # (경로 조작과 확장자 위장을 한 번에 막는다).
    target = models / f"{object_id}.stl"
    target.write_bytes(raw)

    _, info = normalize(verts)
    logger.info("CAD 모델 저장: %s (면 %d개)", target, len(faces))
    return {"file": target.name, "num_vertices": int(len(verts)),
            "num_faces": int(len(faces)), **info}


def voxelize(verts: np.ndarray, faces: np.ndarray, res: int) -> np.ndarray:
    """삼각형 메시 → 복셀 볼륨.

    삼각형 표면을 조밀하게 샘플해 껍질을 만들고 내부를 채운다. 정식 래스터화보다
    거칠지만, 어차피 비교 상대(스캔 실루엣)가 복셀 해상도라 더 정밀할 이유가 없다.

    verts 는 normalize() 를 거친 [-1,1] 좌표여야 한다.
    """
    from scipy import ndimage

    tri = verts[faces]                                   # (F, 3, 3)
    # 삼각형마다 무게중심 좌표로 표면점을 뿌린다. 가장 긴 변 기준으로 촘촘하게.
    edge = np.linalg.norm(tri[:, 1] - tri[:, 0], axis=1)
    edge = np.maximum(edge, np.linalg.norm(tri[:, 2] - tri[:, 0], axis=1))
    n_samples = int(np.clip(edge.max() * res * 2, 4, 64))

    u = np.random.default_rng(0).random((n_samples, 2))
    u = u[u.sum(axis=1) <= 1.0]                          # 삼각형 내부만
    w = np.stack([1 - u[:, 0] - u[:, 1], u[:, 0], u[:, 1]], axis=1)   # (S, 3)
    pts = np.einsum("sk,fkd->fsd", w, tri).reshape(-1, 3)

    idx = np.clip(((pts + 1.0) / 2.0 * (res - 1)).astype(np.int32), 0, res - 1)
    shell = np.zeros((res, res, res), bool)
    shell[idx[:, 0], idx[:, 1], idx[:, 2]] = True

    # 껍질에 구멍이 있으면 fill 이 새어나간다. 한 번 닫고 채운다.
    shell = ndimage.binary_closing(shell, iterations=1)
    return ndimage.binary_fill_holes(shell)


def find_model(root: Path, object_id: str) -> Path | None:
    """models/<object_id>.stl 을 찾는다. 대소문자와 확장자 표기를 가리지 않는다."""
    models = root / "models"
    if not models.exists():
        return None
    for path in models.iterdir():
        if path.stem.lower() == object_id.lower() and path.suffix.lower() == ".stl":
            return path
    return None


def _project_fitted(points: np.ndarray, direction, size: int, fit=None):
    """점들을 시선 방향 평면에 정사영하고, 그 결과가 화면을 꽉 채우도록 맞춘다.

    관측 실루엣은 _normalize_silhouette 로 바운딩박스 정규화(물체가 정사각형을
    가득 채움)를 거친다. CAD 투영을 그냥 두면 좌표계가 서로 달라 비교가 성립하지
    않는다 — 실제로 그래서 정합 일치도가 0.12 까지 떨어졌다.

    fit 을 넘기면 그 변환을 재사용한다. 복셀로 구한 변환을 정점에도 똑같이
    적용해야 두 결과가 같은 픽셀 격자 위에 놓인다.
    """
    from .reconstruct3d import _view_basis

    right, up = _view_basis(direction)
    pu = points @ right
    pv = points @ up

    if fit is None:
        cu, cv = (pu.min() + pu.max()) / 2, (pv.min() + pv.max()) / 2
        # 종횡비 유지: 긴 쪽에 맞춘다(_normalize_silhouette 의 정사각 패딩과 동일)
        half = max(pu.max() - pu.min(), pv.max() - pv.min()) / 2 or 1e-6
        fit = (cu, cv, half)

    cu, cv, half = fit
    px = np.clip((((pu - cu) / half * 0.5 + 0.5) * (size - 1)).astype(np.int32), 0, size - 1)
    py = np.clip(((0.5 - (pv - cv) / half * 0.5) * (size - 1)).astype(np.int32), 0, size - 1)
    return px, py, fit


def _voxel_points(volume: np.ndarray) -> np.ndarray:
    """채워진 복셀의 중심 좌표를 [-1,1] 로. 정점과 같은 좌표계여야 한다."""
    idx = np.argwhere(volume)
    return idx / (volume.shape[0] - 1) * 2.0 - 1.0


def _rotation(rx: float, ry: float, rz: float) -> np.ndarray:
    """Z→Y→X 순 오일러 회전 행렬(라디안)."""
    cx, sx, cy, sy, cz, sz = (np.cos(rx), np.sin(rx), np.cos(ry),
                              np.sin(ry), np.cos(rz), np.sin(rz))
    return (np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
            @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            @ np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]))


def align_to_scan(
    volume: np.ndarray,
    silhouettes: dict[str, np.ndarray],
    directions: dict[str, tuple[float, float, float]],
    *,
    steps: int = 6,
) -> tuple[np.ndarray, float]:
    """CAD 모델을 실제 놓인 자세에 맞춘다. (회전행렬, 일치도) 반환.

    물체를 컨베이어에 올릴 때마다 방향이 다르므로, 비교 전에 반드시 맞춰야 한다.
    맞추지 않으면 자세 차이가 전부 '불량'으로 칠해진다.

    물체를 회전시키는 대신 카메라 방향을 반대로 회전시킨다. 결과는 같은데
    복셀을 다시 샘플링하지 않아도 되고(보간 손실 없음) 훨씬 빠르다.

    형상을 이미 알고 있어 미지수가 회전 3개뿐이라, 카메라 각도까지 추정하려
    했을 때(미지수 10개, 실패)와 달리 충분히 구속된다.
    """
    from .reconstruct3d import _normalize_silhouette

    res = volume.shape[0]
    masks = {n: _normalize_silhouette(s, res) > 0 for n, s in silhouettes.items()}
    points = _voxel_points(volume)

    def score(rot: np.ndarray) -> float:
        total = []
        for name, mask in masks.items():
            d = rot.T @ np.asarray(directions.get(name, (0.0, 0.0, 1.0)), float)
            px, py, _ = _project_fitted(points, d, res)
            shadow = np.zeros((res, res), bool)
            shadow[py, px] = True
            union = (shadow | mask).sum()
            total.append((shadow & mask).sum() / union if union else 0.0)
        return float(np.mean(total))

    best_rot, best = np.eye(3), -1.0
    angles = np.linspace(0, 2 * np.pi, steps, endpoint=False)
    for rx in angles:
        for ry in angles:
            for rz in angles:
                rot = _rotation(rx, ry, rz)
                s = score(rot)
                if s > best:
                    best, best_rot = s, rot

    # 거친 격자로 찾은 뒤 주변을 촘촘히 다시 본다.
    fine = np.linspace(-np.pi / steps, np.pi / steps, 5)
    base = best_rot
    for rx in fine:
        for ry in fine:
            for rz in fine:
                rot = base @ _rotation(rx, ry, rz)
                s = score(rot)
                if s > best:
                    best, best_rot = s, rot
    return best_rot, best


def vertex_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """면 법선을 정점에 누적한 정규화 법선. 어느 면이 어느 카메라를 향하는지 판정용."""
    tri = verts[faces]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    out = np.zeros_like(verts, dtype=np.float64)
    for k in range(3):
        np.add.at(out, faces[:, k], fn)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(norms, 1e-9)


def deviation_colors(
    verts: np.ndarray,
    faces: np.ndarray,
    volume: np.ndarray,
    silhouettes: dict[str, np.ndarray],
    directions: dict[str, tuple[float, float, float]],
    rotation: np.ndarray,
) -> tuple[list[list[float]], dict]:
    """설계와 다른 곳을 정점 색으로 표시한다.

    3D 재구성을 거치지 않는다. CAD 를 각 카메라로 렌더링해 실제 실루엣과
    2D 에서 비교하고, 어긋난 픽셀에 투영되는 CAD 정점만 칠한다.
    스캔 형상의 품질(오목부 복원 불가, 스케일 미상)에 기대지 않는 것이 요점이다.

    표시 대상은 '실제에는 없는데 설계에는 있는' 영역이다 — 깨짐·결손처럼
    재료가 빠진 불량이 여기 잡힌다. 반대(설계엔 없는데 실제에 있는) 영역은
    CAD 표면 위에 대응점이 없어 색으로 표현할 수 없다.
    """
    from .reconstruct3d import _normalize_silhouette

    res = volume.shape[0]
    neutral, alert = np.array([0.78, 0.78, 0.80]), np.array([1.0, 0.48, 0.09])
    points = _voxel_points(volume)
    normals = vertex_normals(verts, faces)

    # 정점별 '불량 정도' 0~1. 이진 플래그가 아니라 연속값이라 색 농도로 쓴다.
    score = np.zeros(len(verts), np.float32)
    per_view = {}

    for name, raw in silhouettes.items():
        mask = _normalize_silhouette(raw, res) > 0
        d = rotation.T @ np.asarray(directions.get(name, (0.0, 0.0, 1.0)), float)
        d = d / (np.linalg.norm(d) or 1.0)

        # 복셀로 변환을 정하고, 정점에 '같은' 변환을 적용한다.
        # 각자 따로 맞추면 두 결과가 서로 다른 격자에 놓여 비교가 어긋난다.
        px, py, fit = _project_fitted(points, d, res)
        cad_shadow = np.zeros((res, res), bool)
        cad_shadow[py, px] = True

        # 설계에는 있는데 실제 촬영에는 없는 영역 = 빠진 부분
        missing = cad_shadow & ~mask

        # 얼마나 '깊이' 어긋났는지를 거리 변환으로 잰다.
        #
        # 이진 판정(어긋남/아님)은 두 가지를 구분하지 못한다: 자세가 1~2도 틀어져
        # 실루엣 외곽에 생긴 한두 픽셀 폭의 띠와, 실제로 크게 떨어져 나간 결손.
        # 거리 변환은 영역 안쪽으로 들어갈수록 값이 커지므로 얇은 띠는 저절로
        # 작은 값이 되고, 덩어리진 결손만 큰 값을 갖는다.
        # 그 값이 그대로 '불량의 정도'가 되어 색 농도로 쓰인다.
        import cv2
        dist = cv2.distanceTransform(missing.astype(np.uint8), cv2.DIST_L2, 3)

        # 정합 오차 폭(화면 대비 DEVIATION_MIN_WIDTH)을 0 으로 놓고 그 위를 편다.
        floor = res * DEVIATION_MIN_WIDTH / 2.0
        severity = np.clip((dist - floor) / max(floor, 1.0), 0.0, 1.0)

        per_view[name] = round(float((severity > 0).sum() / max(cad_shadow.sum(), 1)), 4)
        if not severity.any():
            continue

        vx, vy, _ = _project_fitted(verts, d, res, fit=fit)
        hit = severity[vy, vx]

        # 이 카메라를 향한 면만 남긴다. 2D 차이를 그냥 역투영하면 광선을 따라
        # 반대편 뒷면까지 칠해진다(실측: 정점의 53.7% 가 칠해졌고 그중 실제 결손
        # 위치는 22% 뿐이었다). 보이지 않는 면은 그 카메라가 판단할 근거가 없다.
        facing = normals @ d > 0.1

        # 앞을 향해도 같은 픽셀에 앞뒤로 겹칠 수 있다. z-버퍼로 '그 픽셀에서
        # 실제로 보이는' 최전면 정점만 남긴다. d 가 물체→카메라 방향이므로
        # 투영 성분이 클수록 카메라에 가깝다.
        depth = verts @ d
        zbuf = np.full((res, res), -np.inf)
        np.maximum.at(zbuf, (vy[facing], vx[facing]), depth[facing])
        visible = facing & (depth >= zbuf[vy, vx] - DEPTH_TOLERANCE)

        # 여러 카메라가 같은 곳을 지적하면 가장 심하게 본 값을 남긴다.
        # 평균을 내면 그 부위를 못 보는 카메라(뒤쪽) 때문에 희석된다.
        np.maximum(score, np.where(visible, hit, 0.0), out=score)

    # 정도에 비례해 중립색 → 경고색으로 섞는다. 살짝 어긋난 곳은 옅게,
    # 크게 떨어져 나간 곳은 진하게 나와 심각도가 한눈에 보인다.
    colors = neutral + score[:, None] * (alert - neutral)
    flagged = score > 0
    return (
        np.round(colors, 3).tolist(),
        {"deviation_ratio_per_view": per_view,
         "flagged_vertices": int(flagged.sum()),
         "flagged_ratio": round(float(flagged.mean()), 4),
         "max_severity": round(float(score.max()), 3),
         "mean_severity": round(float(score[flagged].mean()) if flagged.any() else 0.0, 3)},
    )


def compare_with_scan(
    mesh: dict,
    silhouettes: dict[str, np.ndarray],
    directions: dict[str, tuple[float, float, float]],
    *,
    resolution: int = 48,
) -> dict:
    """기준 모델에 '설계와 다른 곳'을 색으로 얹는다. mesh 를 갱신해 돌려준다.

    3D 재구성을 거치지 않고 2D 에서 비교한다 — 형상은 CAD(정확)를 쓰고
    이미지는 증거로만 쓰므로 스캔 재구성 품질에 기대지 않는다.
    """
    verts = np.asarray(mesh["vertices"], dtype=np.float64)
    faces = np.asarray(mesh["faces"], dtype=np.int32)

    volume = voxelize(verts, faces, resolution)
    if not volume.any():
        logger.warning("CAD 복셀화 결과가 비었습니다. 비교를 건너뜁니다.")
        return mesh

    rotation, fit = align_to_scan(volume, silhouettes, directions)

    # 정합이 나쁘면 색칠하지 않는다.
    #
    # CAD 를 실제 자세에 못 맞추면 CAD 실루엣과 촬영 실루엣이 거의 안 겹치고,
    # 그러면 모델 '전체'가 빠진 부분으로 잡혀 정상품도 통째로 주황색이 된다.
    # 그 상태의 색은 불량 정보가 아니라 정합 실패의 표시일 뿐이라, 보여주면
    # 사용자가 멀쩡한 물건을 불량으로 판정하게 된다. 차라리 아무것도 칠하지 않고
    # 왜 비교를 못 했는지 알려주는 편이 낫다.
    if fit < MIN_ALIGNMENT_SCORE:
        mesh["stats"] = {
            **mesh["stats"],
            "alignment_score": round(fit, 4),
            "comparison_skipped": (
                f"CAD 모델을 실제 자세에 맞추지 못했습니다(일치도 {fit:.2f}, "
                f"기준 {MIN_ALIGNMENT_SCORE}). 이 상태로 비교하면 정상품도 전부 "
                "불량으로 표시됩니다. 배경 캡처가 최신인지, config 의 카메라 "
                "view_dir 이 실제 배치와 맞는지 확인하세요."
            ),
            "num_defect_regions": 0,
        }
        return mesh

    colors, stats = deviation_colors(verts, faces, volume, silhouettes, directions, rotation)

    mesh["vertex_colors"] = colors
    mesh["stats"] = {
        **mesh["stats"],
        "alignment_score": round(fit, 4),
        "num_defect_regions": 1 if stats["flagged_vertices"] else 0,
        **stats,
    }
    return mesh


def load_reference(root: Path, object_id: str) -> dict | None:
    """뷰어가 그대로 먹는 형태로 기준 모델을 돌려준다. 없으면 None."""
    path = find_model(root, object_id)
    if path is None:
        return None

    verts, faces = load_stl(path)
    if len(faces) > MAX_FACES:
        logger.warning("%s 는 면이 %d개라 브라우저에 부담이 큽니다.", path.name, len(faces))

    verts, info = normalize(verts)
    return {
        "vertices": np.round(verts.astype(np.float64), 4).tolist(),
        "faces": faces.tolist(),
        "vertex_colors": [[0.78, 0.78, 0.80]] * len(verts),   # 중립 그레이
        "stats": {
            "source": "cad_stl",
            "file": path.name,
            "num_vertices": int(len(verts)),
            "num_faces": int(len(faces)),
            **info,
        },
    }
