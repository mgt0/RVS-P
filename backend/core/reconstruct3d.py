"""5뷰 스캔 → 3D 형상 재구성.

정직한 한계
-----------
front/back/left/right/top 5장(각 90° 간격)만으로 완전한 포토그래메트리
메시를 만드는 건 어렵다. COLMAP 같은 SfM 은 보통 겹침이 큰 수십~수백 장을
요구하고, 90° 간격은 특징점 매칭이 거의 안 된다.

그래서 5뷰에 실제로 강건한 방식인 '실루엣 기반 Visual Hull(space carving)'을
기본으로 쓴다. 원리:
  1. 각 뷰에서 물체 실루엣(전경 마스크)을 뽑는다.
  2. 3D 복셀 격자를 만든다.
  3. 각 복셀을 5개 카메라 방향으로 투영해, '모든 뷰에서 실루엣 안'인 복셀만 남긴다.
     → 남은 복셀 덩어리가 물체의 볼록 근사 형상(visual hull)이다.

이 방식은 오목한 부분(구멍 안쪽)은 복원 못 하지만, 5뷰 산업 스캔에서
형상 확인·불량 위치 표시 용도로는 충분하고 무엇보다 '항상 동작'한다.

출력은 웹(three.js)이 바로 먹는 형태:
  - vertices, faces (marching cubes 로 복셀→표면 메시)
  - vertex_colors (양품=중립 그레이, 불량=선셋 오렌지 오버레이)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 카메라가 물체를 바라보는 방향(정규화). space carving 의 투영 축으로 쓴다.
# 실제 리그의 카메라 배치에 맞춰 조정하면 형상이 더 정확해진다.
VIEW_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "front": (0.0, 0.0, 1.0),
    "back": (0.0, 0.0, -1.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "top": (0.0, 1.0, 0.0),
}


@dataclass
class ReconstructionResult:
    vertices: list[list[float]]
    faces: list[list[int]]
    vertex_colors: list[list[float]]
    stats: dict

    def to_dict(self) -> dict:
        return {
            "vertices": self.vertices,
            "faces": self.faces,
            "vertex_colors": self.vertex_colors,
            "stats": self.stats,
        }


def _extract_silhouette(image: np.ndarray) -> np.ndarray:
    """전경(물체) 마스크 추출.

    스캔 환경은 배경이 비교적 균일하다고 가정하고 Otsu + 형태학 연산으로
    실루엣을 뽑는다. 배경이 복잡하면 크로마키나 배경 차분으로 교체하면 된다.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # Otsu 자동 임계. 물체가 배경보다 어둡거나 밝은 양쪽 모두 처리하기 위해
    # 전경 비율이 절반을 넘으면 반전한다.
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if mask.mean() > 127:
        mask = cv2.bitwise_not(mask)

    # 잔 구멍 메우기 + 노이즈 제거
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    # 가장 큰 연결 요소만 물체로 간주
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == largest, 255, 0).astype(np.uint8)

    return mask


def _carve_visual_hull(
    silhouettes: dict[str, np.ndarray], resolution: int = 64
) -> np.ndarray:
    """실루엣들로 복셀 격자를 조각.

    각 복셀 (x,y,z) ∈ [0,res)^3 을 각 뷰의 2D 평면에 정사영해서,
    해당 픽셀이 실루엣 내부이면 유지, 하나라도 밖이면 제거한다.
    반환: (res,res,res) bool/float 볼륨. 1=물체, 0=공기.
    """
    res = resolution
    volume = np.ones((res, res, res), dtype=bool)

    # 복셀 인덱스 격자 (정규화 좌표 0~1)
    idx = np.arange(res)
    gx, gy, gz = np.meshgrid(idx, idx, idx, indexing="ij")

    for name, mask in silhouettes.items():
        h, w = mask.shape
        # 뷰별로 어떤 2축을 이미지 평면에 쓰는지 매핑.
        # (리그 기하에 맞춰 축/부호를 조정하는 지점)
        if name in ("front", "back"):
            u, v = gx, gy           # z축으로 투영
        elif name in ("left", "right"):
            u, v = gz, gy           # x축으로 투영
        else:  # top
            u, v = gx, gz           # y축으로 투영

        # 정규화 → 픽셀 좌표
        px = np.clip((u / (res - 1) * (w - 1)).astype(np.int32), 0, w - 1)
        py = np.clip((v / (res - 1) * (h - 1)).astype(np.int32), 0, h - 1)

        inside = mask[py, px] > 0
        volume &= inside

    return volume.astype(np.float32)


def _volume_to_mesh(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """복셀 볼륨 → 표면 메시(marching cubes).

    scikit-image 가 있으면 그걸 쓰고, 없으면 복셀 표면 큐브를 직접 생성하는
    폴백을 쓴다(의존성 하나라도 줄이는 게 배포에 유리).
    """
    try:
        from skimage import measure

        verts, faces, normals, _ = measure.marching_cubes(volume, level=0.5)
        # 중심 정렬 + 단위 정규화
        verts = verts - verts.mean(axis=0)
        scale = np.abs(verts).max() or 1.0
        verts = verts / scale
        return verts.astype(np.float32), faces.astype(np.int32), normals.astype(np.float32)
    except ImportError:
        logger.info("scikit-image 없음 → 복셀 큐브 폴백 사용")
        return _voxel_cube_mesh(volume)


def _voxel_cube_mesh(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """표면 복셀만 작은 큐브로 렌더(폴백).

    marching cubes 보다 거칠지만 형상 확인엔 충분하고 의존성이 없다.
    """
    res = volume.shape[0]
    occupied = volume > 0.5
    # 표면 복셀 = 채워졌지만 이웃 하나라도 비어있는 것
    padded = np.pad(occupied, 1)
    neighbors = (
        padded[:-2, 1:-1, 1:-1] & padded[2:, 1:-1, 1:-1] &
        padded[1:-1, :-2, 1:-1] & padded[1:-1, 2:, 1:-1] &
        padded[1:-1, 1:-1, :-2] & padded[1:-1, 1:-1, 2:]
    )
    surface = occupied & ~neighbors
    coords = np.argwhere(surface)

    if len(coords) == 0:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int32), np.zeros((0, 3), np.float32)

    # 정규화
    center = coords.mean(axis=0)
    scale = np.abs(coords - center).max() or 1.0

    cube_v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                       [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=np.float32)
    cube_f = np.array([[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6],
                       [0, 4, 5], [0, 5, 1], [1, 5, 6], [1, 6, 2],
                       [2, 6, 7], [2, 7, 3], [3, 7, 4], [3, 4, 0]], dtype=np.int32)

    all_v, all_f = [], []
    vsize = 1.0 / res
    for i, c in enumerate(coords):
        v = (c + cube_v * 0.9 - center) / scale * vsize * res * 0.5
        all_v.append(v)
        all_f.append(cube_f + i * 8)

    verts = np.vstack(all_v).astype(np.float32)
    faces = np.vstack(all_f).astype(np.int32)
    normals = verts / (np.linalg.norm(verts, axis=1, keepdims=True) + 1e-9)
    return verts, faces, normals


def _apply_defect_overlay(
    verts: np.ndarray, defect_regions: list[dict] | None
) -> np.ndarray:
    """정점 색상 생성. 불량 영역 근처 정점을 선셋 오렌지로 물들인다.

    defect_regions: [{"center": [x,y,z], "radius": r, "severity": 0~1}, ...]
      CNN/검출 결과에서 넘어오는 불량 위치. 정규화 좌표 기준.
    """
    neutral = np.array([0.72, 0.73, 0.75])   # 중립 그레이
    sunset = np.array([1.0, 0.478, 0.09])    # #ff7a17

    colors = np.tile(neutral, (len(verts), 1)).astype(np.float32)
    if not defect_regions:
        return colors

    for region in defect_regions:
        center = np.array(region.get("center", [0, 0, 0]), dtype=np.float32)
        radius = float(region.get("radius", 0.2))
        severity = float(region.get("severity", 1.0))

        dist = np.linalg.norm(verts - center, axis=1)
        # 반경 안에서 중심에 가까울수록 진하게(가우시안 감쇠)
        weight = np.clip(np.exp(-(dist / max(radius, 1e-3)) ** 2) * severity, 0, 1)
        colors = colors * (1 - weight[:, None]) + sunset * weight[:, None]

    return colors.astype(np.float32)


def reconstruct_from_views(
    view_images: dict[str, np.ndarray],
    *,
    resolution: int = 64,
    defect_regions: list[dict] | None = None,
    classification: dict | None = None,
) -> ReconstructionResult:
    """5뷰 이미지 → 3D 메시 + 불량 오버레이.

    view_images: {"front": BGR np.ndarray, ...}. 최소 3뷰 이상 권장.
    classification: {"label": "good"|"defect", "confidence": 0~1, ...}
      good 이면 defect_regions 를 무시하고 전체를 중립색으로 둔다.
    """
    if len(view_images) < 2:
        raise ValueError("재구성에는 최소 2개 이상의 뷰가 필요합니다.")

    silhouettes = {name: _extract_silhouette(img) for name, img in view_images.items()}
    coverage = {name: float((m > 0).mean()) for name, m in silhouettes.items()}

    volume = _carve_visual_hull(silhouettes, resolution=resolution)
    occupancy = float(volume.mean())

    verts, faces, _ = _volume_to_mesh(volume)

    # 양품이면 하이라이트를 끈다.
    label = (classification or {}).get("label", "unknown")
    regions = defect_regions if label != "good" else None
    colors = _apply_defect_overlay(verts, regions)

    stats = {
        "num_views": len(view_images),
        "resolution": resolution,
        "num_vertices": int(len(verts)),
        "num_faces": int(len(faces)),
        "voxel_occupancy": round(occupancy, 4),
        "silhouette_coverage": {k: round(v, 4) for k, v in coverage.items()},
        "classification": classification or {"label": "unknown"},
        "num_defect_regions": len(regions) if regions else 0,
        "mesh_method": "visual_hull",
    }

    logger.info(
        "재구성 완료: %d뷰 → %d verts / %d faces (occupancy %.3f)",
        len(view_images), len(verts), len(faces), occupancy,
    )

    return ReconstructionResult(
        vertices=verts.tolist(),
        faces=faces.tolist(),
        vertex_colors=colors.tolist(),
        stats=stats,
    )


def load_object_views(root: Path, label: str, object_id: str) -> dict[str, np.ndarray]:
    """classification/{good,defect}/<object_id>/ 에서 5뷰 이미지를 로드."""
    obj_dir = root / "classification" / label / object_id
    if not obj_dir.exists():
        raise FileNotFoundError(f"스캔 이미지를 찾을 수 없습니다: {obj_dir}")

    images: dict[str, np.ndarray] = {}
    for name in VIEW_DIRECTIONS:
        # front.jpg 또는 front_<episode>.jpg 형태 모두 허용, 가장 최근 것 사용
        candidates = sorted(obj_dir.glob(f"{name}*.jpg"))
        if candidates:
            img = cv2.imread(str(candidates[-1]))
            if img is not None:
                images[name] = img

    if not images:
        raise FileNotFoundError(f"{obj_dir} 에 유효한 뷰 이미지가 없습니다.")
    return images
