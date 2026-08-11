"""5뷰 스캔 → 3D 형상 재구성.

정직한 한계
-----------
cam1~cam5 의 5장(각 90° 간격)만으로 완전한 포토그래메트리
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

from ..utils.errors import EmptyHullError
from .storage import imread

logger = logging.getLogger(__name__)

# 각 카메라 슬롯이 물체를 바라보는 방향(정규화). space carving 의 투영 축으로 쓴다.
# 슬롯 이름(cam1~cam5)에는 방향 정보가 없으므로 대응을 여기서 정한다.
# 카메라를 물리적으로 옮겼다면 config.yaml 의 nth 가 아니라 이 표를 고쳐야 한다.
# 물체가 있다고 가정하는 화면 중앙 영역의 비율(가로/세로 각각).
# 0.5 = 가운데 50% 사각형. 리그에서 물체가 화면을 크게 차지하면 키우고,
# 배경이 자주 잡히면 줄인다.
CENTER_ROI = 0.5

# 배경 차분 임계(0~255). 낮추면 미세한 차이도 물체로 잡아 그림자·조명 흔들림이
# 섞이고, 높이면 배경과 색이 비슷한 부품을 놓친다. 30 은 JPEG 노이즈(±10 안팎)와
# 실제 물체를 가르는 중간값.
BACKGROUND_DIFF_THRESHOLD = 30

# 배경 차분이 화면의 이 비율을 넘게 잡으면 배경 참조가 무효라고 보고 채도 기반으로
# 전환한다. 스캔대 위 부품이 화면의 1/4 을 넘을 일은 없다(실측: 정상 뷰 5~10%).
BACKGROUND_DIFF_FALLBACK = 0.25


# 실루엣이 화면의 이 비율보다 작으면 '물체를 못 찾았다'고 본다.
# 스캔대 위 부품은 어느 뷰에서든 화면의 몇 % 는 차지한다. 0.5% 는 그보다 훨씬 작다.
TINY_SILHOUETTE = 0.005

# 메시 평활화 강도(복셀 단위 가우시안 시그마). 0 이면 평활화 없음.
#
# 매끄러움과 형상 보존은 정면으로 충돌한다. res 96 실측(거칠기 / 원본 형상 보존):
#   0.0 → 40.7° / 0.926      0.6 → 38.1° / 0.917
#   0.8 → 36.6° / 0.885      1.0 → 36.5° / 0.850
# 1.0 은 거칠기를 0.1도 더 줄이자고 형상을 7% 더 깎는다 — 남는 장사가 아니다.
# 0.6 이 무릎이다. 더 매끄럽게 하려면 시그마가 아니라 뷰를 늘려야 한다.
MESH_SMOOTH_SIGMA = 0.6

VIEW_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    # 좌표계: +X 오른쪽, +Y 위, +Z 앞. '물체 → 카메라' 방향이며 크기는 무관(정규화한다).
    # 실제 리그는 직교 5뷰가 아니라 대각선 4대 + 하단 1대이고, 전부 45° 기준이다.
    #
    # 주의: 고도 45° 를 (1,1,1) 로 쓰면 안 된다. 정규화하면 y = 1/sqrt(3) 이라
    # 고도가 asin(1/sqrt(3)) = 35.3° 로, 의도보다 10° 낮게 들어간다.
    # 방위 a, 고도 e 의 정확한 벡터는 (sin a cos e, sin e, cos a cos e) 다.
    "cam1": (0.5, 0.7071, 0.5),        # 오른쪽 대각선 위 — 방위 +45°, 고도 +45°
    "cam2": (-0.5, 0.7071, 0.5),       # 왼쪽 대각선 위   — 방위 -45°, 고도 +45°
    "cam3": (-0.7071, 0.0, 0.7071),    # 왼쪽 대각선      — 방위 -45°, 고도   0°
    "cam4": (0.7071, 0.0, 0.7071),     # 오른쪽 대각선    — 방위 +45°, 고도   0°
    "cam5": (0.0, -1.0, 0.0),          # 하단 뷰          — 바로 아래에서 위로
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


def _deband(image: np.ndarray) -> np.ndarray:
    """조명 플리커로 생긴 가로 줄무늬를 제거한다.

    형광등·LED 는 전원 주파수의 2배(한국 60Hz → 120Hz)로 밝기가 오르내린다.
    노출 시간이 그 주기의 정수배가 아니면 프레임마다 '행 단위 밝기 변조'가 남아
    가로 줄무늬로 보인다. 롤링 셔터라 한 프레임 안에서 행마다 노출 시점이 다르기
    때문이다.

    이게 왜 3D 를 망가뜨리나: 어두운 줄에 걸린 물체 픽셀은 배경 차분 임계값을
    넘지 못해 배경으로 판정된다. 그러면 실루엣에 가로 방향 틈이 생기고,
    space carving 이 그 틈만큼 형상을 잘라낸다 — "스캔이 잘려 나온다".

    변조가 행 전체에 균일하게 걸리므로, 각 행의 밝기를 전체 평균에 맞추면
    줄무늬만 사라지고 물체의 공간 구조는 그대로 남는다. 카메라 노출을 맞추는
    것이 근본 해결이지만, 그건 조명·하드웨어에 달려 있고 이건 항상 동작한다.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # 행 대표값은 평균이 아니라 중앙값. 물체가 그 행을 크게 가리면 평균이 끌려간다.
    row_level = np.median(gray, axis=1, keepdims=True)
    target = float(np.median(row_level))

    # 0 에 가까운 행(완전 암부)에서 폭주하지 않게 하한을 둔다.
    gain = target / np.maximum(row_level, 1.0)
    gain = np.clip(gain, 0.5, 2.0)[:, :, None]

    return np.clip(image.astype(np.float32) * gain, 0, 255).astype(np.uint8)


def _saturation_mask(image: np.ndarray) -> np.ndarray:
    """채도로 물체를 찾는다. 배경 차분이 못 쓰게 됐을 때의 대안.

    이 리그의 배경(스테인리스 벨트, 회색 프레임, 흰 조명, 실험실 집기)은 채도가
    낮고, 검사 대상인 수지 부품은 채도가 높다. 채도는 밝기와 달리 조명이 어두워져도
    비교적 유지되므로, 배경 캡처 시점과 조명이 달라져도 동작한다.

    배경 차분보다 일반성이 떨어진다 — 무채색 부품(검은 플라스틱, 알루미늄)은
    못 잡는다. 어디까지나 배경 참조가 무효화됐을 때의 차선책이고, 제대로 하려면
    조명을 고정하고 빈 벨트를 다시 캡처하는 것이 맞다.
    """
    hsv = cv2.cvtColor(cv2.GaussianBlur(image, (5, 5), 0), cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]

    # Otsu 로 채도 임계를 자동 결정한다. 조명·부품 색이 바뀌어도 따라간다.
    _, mask = cv2.threshold(sat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def _match_exposure(image: np.ndarray, background: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """두 이미지의 전역 밝기를 서로 맞춘다.

    채널별 중앙값을 기준으로 배경을 촬영본 쪽으로 끌어온다. 중앙값을 쓰는 이유:
    물체가 화면 일부를 차지하므로 평균은 물체 색에 끌려가지만, 중앙값은 화면
    대부분인 배경이 결정한다. 채널별로 하면 자동 화이트밸런스가 만든 색조 차이도
    같이 없앤다.

    이걸 하지 않으면 자동 노출이 바뀐 것만으로 화면 전체가 '다른 곳'이 되어
    실루엣이 프레임 전체로 번진다.
    """
    img_med = np.median(image.reshape(-1, 3), axis=0)
    bg_med = np.median(background.reshape(-1, 3), axis=0)

    # 0 에 가까운 채널에서 폭주하지 않도록 하한과 배율 상한을 둔다.
    gain = np.clip(img_med / np.maximum(bg_med, 1.0), 0.4, 2.5)
    matched = np.clip(background.astype(np.float32) * gain, 0, 255).astype(np.uint8)
    return image, matched


def _extract_silhouette(image: np.ndarray, background: np.ndarray | None = None) -> np.ndarray:
    """전경(물체) 마스크 추출.

    background 가 있으면 배경 차분, 없으면 Otsu 이진화로 폴백한다.

    Otsu 만으로는 이 리그에서 동작하지 않는다. 스캔대가 실험실 한가운데 있어
    선반·창문·현수막이 화면의 대부분을 차지하는데, 흑백 밝기만으로는 노란 부품과
    배경이 구분되지 않는다(실측: 마스크가 현수막 글자에 붙고 물체는 통째로 누락).
    빈 컨베이어를 한 번 찍어두고 그것과의 차이를 보면 배경이 무엇이든 상관없어진다.
    """
    if background is not None and background.shape == image.shape:
        # 촬영본과 배경은 서로 다른 시각에 찍혀 플리커 위상이 다르다. 그대로 빼면
        # 줄무늬 차이가 그대로 남아 실루엣에 가로 틈이 생긴다. 양쪽 다 편다.
        image = _deband(image)
        background = _deband(background)
        # 두 이미지의 전역 밝기를 맞춘다.
        #
        # _deband 는 '한 이미지 안의' 행 간 편차만 없앤다. 각 이미지를 자기
        # 중앙값에 맞추므로 이미지끼리의 밝기 차이는 그대로 남는다.
        # 카메라 자동 노출은 물체가 들어오면 화면 전체 밝기를 다시 잡기 때문에
        # 배경 캡처 때와 촬영 때의 밝기가 통째로 달라지고, 그러면 화면 전부가
        # '배경과 다른 곳'이 된다(실측: 전역 차이 중앙값이 임계값 30 을 넘는 뷰가 있었다).
        image, background = _match_exposure(image, background)
        # 조명 흔들림과 JPEG 노이즈를 흡수하기 위해 블러 후 차분한다.
        diff = cv2.absdiff(
            cv2.GaussianBlur(image, (5, 5), 0), cv2.GaussianBlur(background, (5, 5), 0)
        )
        # 채널 최대값: 색만 다르고 밝기는 비슷한 물체(노란 부품 위 흰 조명)도 잡는다.
        diff = diff.max(axis=2)
        _, mask = cv2.threshold(diff, BACKGROUND_DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)

        # 배경 차분이 화면 절반 이상을 물체로 잡으면 그 결과는 못 쓴다.
        # 배경 캡처 이후 조명이 바뀌었거나 배경에 다른 물체가 있었다는 뜻이고,
        # 이 상태로 두면 실루엣이 화면 전체가 되어 정합도 비교도 무의미해진다.
        # 조명 변화에 둔감한 채도 기반으로 갈아탄다(실측: 배경 차분 54~80% → 채도 기반).
        if (mask > 0).mean() > BACKGROUND_DIFF_FALLBACK:
            logger.warning(
                "배경 차분이 화면의 %.0f%% 를 물체로 잡았습니다. 배경 캡처가 현재 조명과 "
                "맞지 않는 것으로 보여 채도 기반으로 전환합니다.", 100 * (mask > 0).mean()
            )
            mask = _saturation_mask(image)
    else:
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

    # 화면 가운데에 있는 덩어리를 물체로 고른다.
    #
    # 예전에는 '가장 큰 덩어리'를 골랐는데, 스캔 리그가 실험실 한가운데 있으면
    # 배경(선반·창문·케이블)이 물체보다 훨씬 커서 배경을 물체로 착각한다.
    # 그러면 뷰마다 엉뚱한 영역이 잡혀 5뷰 교집합이 비고 3D 형상이 통째로 사라진다
    # (실측: 뷰별 실루엣 11~41% 인데 교집합 0복셀).
    #
    # 물체는 리그 중앙에 놓이는 것이 전제이므로, 중앙 ROI 와 겹치는 면적이
    # 가장 큰 덩어리를 고른다. 중앙 픽셀 하나만 보면 물체에 구멍이 있거나
    # 살짝 치우쳤을 때 놓친다.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n > 1:
        h, w = mask.shape
        roi = np.zeros_like(mask, dtype=bool)
        y0, y1 = int(h * (1 - CENTER_ROI) / 2), int(h * (1 + CENTER_ROI) / 2)
        x0, x1 = int(w * (1 - CENTER_ROI) / 2), int(w * (1 + CENTER_ROI) / 2)
        roi[y0:y1, x0:x1] = True

        # 화면 가장자리에 닿는 덩어리는 물체가 아니라 리그 구조물이다.
        #
        # 스캔대 위 부품은 카메라가 겨냥한 중앙에 있고 프레임 밖으로 이어지지 않는다.
        # 반면 투명 아크릴 커버·벨트·프레임은 화면을 가로질러 양 끝까지 뻗는다.
        # 특히 아크릴은 반사와 그림자가 촬영마다 달라져 배경 차분에 크게 잡히는데,
        # 그 띠가 중앙을 통과하면 '중앙 겹침' 기준만으로는 물체를 이긴다(실측:
        # 물체 대신 벨트 띠가 선택돼 정합이 0.45 로 떨어졌다).
        overlaps = [int(np.count_nonzero((labels == i) & roi)) for i in range(1, n)]
        best = 1 + int(np.argmax(overlaps))
        if overlaps[best - 1] == 0:
            # 중앙 ROI 에 걸치는 덩어리가 하나도 없다 = 물체가 화면 밖이거나
            # 배경과 구분이 안 된다. 넓이 기준으로 되돌아가되 그대로 두면
            # 교집합이 비어 상위에서 EmptyHullError 로 잡힌다.
            best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == best, 255, 0).astype(np.uint8)

    return mask


def _view_basis(direction) -> tuple[np.ndarray, np.ndarray]:
    """시선 방향에 수직인 이미지 평면의 (right, up) 정규직교 기저.

    direction 은 '물체에서 카메라를 향하는' 단위 벡터다. 카메라가 어느 대각선에
    있든 이 두 축으로 정사영하면 그 카메라가 보는 그림이 된다.
    """
    d = np.asarray(direction, dtype=np.float64)
    n = np.linalg.norm(d)
    d = d / n if n > 1e-9 else np.array([0.0, 0.0, 1.0])

    # 월드 up 과 시선이 거의 나란하면(하단/상단 뷰) up 을 기준으로 삼을 수 없다.
    world_up = np.array([0.0, 1.0, 0.0])
    if abs(float(d @ world_up)) > 0.99:
        world_up = np.array([0.0, 0.0, 1.0])

    right = np.cross(world_up, d)
    right /= np.linalg.norm(right)
    up = np.cross(d, right)
    up /= np.linalg.norm(up)
    return right, up


def _normalize_silhouette(mask: np.ndarray, size: int) -> np.ndarray:
    """실루엣을 자기 바운딩박스 기준으로 정규화한 정사각 마스크로 만든다.

    왜 필요한가: space carving 은 복셀을 각 뷰의 이미지 평면에 투영해 교집합을
    구하는데, 원래 구현은 복셀 격자를 '이미지 전체'에 매핑했다. 그건 5대 카메라가
    물체를 전부 같은 크기·같은 위치로 담고 있을 때만 맞는 가정이다.

    실제 리그는 카메라마다 거리와 각도가 달라 물체가 화면의 8%~54% 로 제각각이고
    위치도 다르다(실측). 그러면 한 뷰에서라도 복셀이 배경에 떨어져 전부 깎이고
    교집합이 빈다 — 실루엣이 멀쩡히 잡혀도 형상이 안 나오는 이유였다.

    캘리브레이션 정보가 없으므로 각 뷰의 물체를 같은 크기로 맞춰 비교한다.
    ponytail: 절대 크기와 뷰 간 상대 비율은 잃는다. 형상만 얻는다.
    실측 치수가 필요해지면 카메라 캘리브레이션(내부/외부 파라미터)이 있어야 한다.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return np.zeros((size, size), np.uint8)

    y0, y1 = ys.min(), ys.max() + 1
    x0, x1 = xs.min(), xs.max() + 1
    crop = mask[y0:y1, x0:x1]

    # 종횡비를 유지한 채 정사각으로 패딩한다. 늘려버리면 길쭉한 부품이
    # 뷰마다 다른 비율로 찌그러져 교집합이 실제 형상과 멀어진다.
    h, w = crop.shape
    side = max(h, w)
    square = np.zeros((side, side), np.uint8)
    oy, ox = (side - h) // 2, (side - w) // 2
    square[oy:oy + h, ox:ox + w] = crop

    # INTER_AREA(면적 평균) 후 이진화한다. INTER_NEAREST 로 줄이면 원본 640x480 에서
    # 픽셀을 띄엄띄엄 집어와, 같은 실루엣인데 복셀 해상도만 바꿔도 형상이 달라진다
    # (실측: res48 과 res96 결과의 IoU 가 0.57 밖에 안 됐다).
    # 면적 평균은 축소 배율이 변해도 실루엣의 '넓이'를 보존해 해상도에 수렴한다.
    small = cv2.resize(square, (size, size), interpolation=cv2.INTER_AREA)
    return np.where(small >= 128, 255, 0).astype(np.uint8)


def _carve_visual_hull(
    silhouettes: dict[str, np.ndarray],
    resolution: int = 64,
    directions: dict[str, tuple[float, float, float]] | None = None,
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

    # 복셀 중심을 [-0.5, 0.5] 정규 좌표로. 원점이 물체 중심이다.
    cx = gx / (res - 1) - 0.5
    cy = gy / (res - 1) - 0.5
    cz = gz / (res - 1) - 0.5

    for name, raw in silhouettes.items():
        # 뷰마다 물체 크기·위치가 다르므로 바운딩박스 기준으로 맞춘 뒤 투영한다.
        mask = _normalize_silhouette(raw, res)
        h, w = mask.shape

        # 시선 방향에 수직인 평면의 정규직교 기저(right, up)를 만들어 그 위에 정사영한다.
        # 예전 구현은 X·Y·Z 세 축 중 하나로만 투영해서, 대각선에 달린 카메라를
        # 가장 가까운 축으로 스냅해 버렸다. 실제 리그는 대각선 4대 + 하단 1대라
        # 어느 것도 축에 정렬돼 있지 않아 형상이 물체와 전혀 다르게 나왔다.
        table = directions or VIEW_DIRECTIONS
        right, up = _view_basis(table.get(name, VIEW_DIRECTIONS.get(name, (0.0, 0.0, 1.0))))

        pu = cx * right[0] + cy * right[1] + cz * right[2]
        pv = cx * up[0] + cy * up[1] + cz * up[2]

        # 정규 좌표 → 픽셀.
        # 스케일은 '이 방향에서 본 복셀 정육면체의 투영 폭'이다. 실루엣을 바운딩박스로
        # 정규화해 물체가 정사각형을 꽉 채우게 만들었으므로, 정육면체의 투영도 같은
        # 정사각형을 꽉 채워야 짝이 맞는다. 외접 반경(sqrt(3)/2)으로 나누면 투영이
        # 마스크 안쪽으로 쪼그라들어 아무것도 안 깎인다(실측: 점유율 100%).
        # 정육면체 꼭짓점의 최대 투영값 = 0.5 * (|ax|+|ay|+|az|).
        su = 0.5 * float(np.abs(right).sum())
        sv = 0.5 * float(np.abs(up).sum())

        px = np.clip(((pu / su * 0.5 + 0.5) * (w - 1)).astype(np.int32), 0, w - 1)
        # 이미지 y 는 아래로 증가하므로 up 을 뒤집는다.
        py = np.clip(((0.5 - pv / sv * 0.5) * (h - 1)).astype(np.int32), 0, h - 1)

        volume &= mask[py, px] > 0

    return volume.astype(np.float32)


def _clean_volume(volume: np.ndarray, smooth_sigma: float = MESH_SMOOTH_SIGMA) -> np.ndarray:
    """복셀 덩어리를 메시로 만들기 전에 다듬는다.

    space carving 결과를 그대로 marching cubes 에 넣으면 세 가지가 그대로 보인다:
      1. 공중에 뜬 파편 — 실루엣 노이즈가 우연히 5뷰에서 겹친 자리
      2. 표면의 잔 구멍 — 한 뷰에서만 살짝 어긋나도 복셀이 뚫린다
      3. 계단 모양 — 복셀은 정육면체라 경계가 각진다

    순서대로 처리한다. 순서가 중요하다: 파편을 먼저 지워야 닫힘 연산이
    파편과 본체를 엉뚱하게 이어붙이지 않는다.
    """
    from scipy import ndimage

    binary = volume > 0.5
    if not binary.any():
        return volume

    # 1) 가장 큰 연결 덩어리만 남긴다. 물체는 하나이므로 나머지는 전부 노이즈다.
    labels, n = ndimage.label(binary)
    if n > 1:
        sizes = ndimage.sum(binary, labels, range(1, n + 1))
        binary = labels == (int(np.argmax(sizes)) + 1)

    # 2) 닫힘(팽창 후 침식)으로 잔 구멍과 실금을 메운다. 반경 1 복셀만.
    #    크게 잡으면 진짜 구멍(부품의 관통 홀)까지 막아버린다.
    binary = ndimage.binary_closing(binary, iterations=1)

    # 3) 이진값을 흐려 실수 볼륨으로 만든 뒤 marching cubes 를 0.5 등고면에 태운다.
    #    이러면 표면이 복셀 격자가 아니라 보간된 위치를 지나 매끄러워진다.
    smoothed = ndimage.gaussian_filter(binary.astype(np.float32), sigma=smooth_sigma)

    # 4) 평활화가 얇은 연결부를 끊어 새 파편을 만들 수 있으므로 마지막에 한 번 더
    #    가장 큰 덩어리만 남긴다(실측: 이 단계가 없으면 파편이 7개까지 되살아난다).
    final = smoothed > 0.5
    labels, n = ndimage.label(final)
    if n > 1:
        sizes = ndimage.sum(final, labels, range(1, n + 1))
        smoothed = np.where(labels == (int(np.argmax(sizes)) + 1), smoothed, 0.0)

    return smoothed


def _project_pixels(res: int, direction, size: int):
    """복셀 격자를 주어진 방향의 이미지 평면에 정사영한 픽셀 좌표 (px, py).

    carve 와 완전히 같은 식을 써야 한다. 여기서 어긋나면 되쏘기 검증이
    자기 자신과도 안 맞는다.
    """
    idx = np.arange(res)
    gx, gy, gz = np.meshgrid(idx, idx, idx, indexing="ij")
    cx, cy, cz = (g / (res - 1) - 0.5 for g in (gx, gy, gz))

    right, up = _view_basis(direction)
    pu = cx * right[0] + cy * right[1] + cz * right[2]
    pv = cx * up[0] + cy * up[1] + cz * up[2]
    su = 0.5 * float(np.abs(right).sum())
    sv = 0.5 * float(np.abs(up).sum())

    px = np.clip(((pu / su * 0.5 + 0.5) * (size - 1)).astype(np.int32), 0, size - 1)
    py = np.clip(((0.5 - pv / sv * 0.5) * (size - 1)).astype(np.int32), 0, size - 1)
    return px, py


def silhouette_consistency(
    silhouettes: dict[str, np.ndarray],
    directions: dict[str, tuple[float, float, float]],
    resolution: int = 32,
) -> float:
    """카메라 방향이 얼마나 맞는지 0~1 로 점수화한다 (leave-one-out).

    카메라를 하나씩 빼고 나머지로 형상을 깎은 뒤, **빠진 카메라의 실루엣을
    예측**하게 한다. 방향이 맞으면 예측이 실제 실루엣과 겹치고, 틀리면 어긋난다.

    왜 leave-one-out 인가: 전체 뷰로 깎은 형상을 그대로 되쏘면 정의상 항상
    실루엣 '안에' 들어간다(carve 가 AND 이므로). 그러면 덜 깎일수록 점수가 올라
    각도가 틀려 뭉툭해진 형상이 정답보다 높은 점수를 받는다. 실제로 그렇게 됐다
    (합성 검증: 정답 0.863 < 틀린 각도 0.922). 빼놓은 뷰는 깎기에 참여하지
    않으므로 뭉툭한 형상은 예측이 넘쳐 벌점을 받는다 — 이 편향이 사라진다.

    체커보드 없이 데이터만으로 각도를 맞출 수 있는 근거다.
    """
    names = list(silhouettes)
    if len(names) < 3:
        return 0.0

    scores = []
    for held_out in names:
        rest = {n: s for n, s in silhouettes.items() if n != held_out}
        volume = _carve_visual_hull(rest, resolution, directions) > 0.5
        if not volume.any():
            scores.append(0.0)
            continue

        mask = _normalize_silhouette(silhouettes[held_out], resolution) > 0
        px, py = _project_pixels(
            resolution, directions.get(held_out, (0.0, 0.0, 1.0)), resolution
        )
        shadow = np.zeros((resolution, resolution), bool)
        shadow[py[volume], px[volume]] = True
        union = (shadow | mask).sum()
        scores.append((shadow & mask).sum() / union if union else 0.0)
    return float(np.mean(scores))


def _spherical(azimuth_deg: float, elevation_deg: float) -> tuple[float, float, float]:
    """방위각·고도각 → 방향 벡터. 방위 0° = +Z(정면), 고도 +90° = 바로 위."""
    a, e = np.radians(azimuth_deg), np.radians(elevation_deg)
    return (float(np.sin(a) * np.cos(e)), float(np.sin(e)), float(np.cos(a) * np.cos(e)))


def _volume_to_mesh(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """복셀 볼륨 → 표면 메시(marching cubes).

    scikit-image 가 있으면 그걸 쓰고, 없으면 복셀 표면 큐브를 직접 생성하는
    폴백을 쓴다(의존성 하나라도 줄이는 게 배포에 유리).
    """
    try:
        from skimage import measure

        verts, faces, normals, _ = measure.marching_cubes(_clean_volume(volume), level=0.5)
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
    backgrounds: dict[str, np.ndarray] | None = None,
    directions: dict[str, tuple[float, float, float]] | None = None,
) -> ReconstructionResult:
    """5뷰 이미지 → 3D 메시 + 불량 오버레이.

    view_images: {"cam1": BGR np.ndarray, ...}. 최소 3뷰 이상 권장.
    backgrounds: 같은 키의 빈 스캔대 이미지. 있으면 배경 차분으로 실루엣을 뽑는다.
    classification: {"label": "good"|"defect", "confidence": 0~1, ...}
      good 이면 defect_regions 를 무시하고 전체를 중립색으로 둔다.
    """
    if len(view_images) < 2:
        raise ValueError("재구성에는 최소 2개 이상의 뷰가 필요합니다.")

    backgrounds = backgrounds or {}
    silhouettes = {
        name: _extract_silhouette(img, backgrounds.get(name))
        for name, img in view_images.items()
    }
    coverage = {name: float((m > 0).mean()) for name, m in silhouettes.items()}

    volume = _carve_visual_hull(silhouettes, resolution=resolution, directions=directions)
    occupancy = float(volume.mean())

    # 실루엣이 전 뷰에서 극히 작으면 배경 참조에 물체가 이미 찍혀 있다는 뜻이다.
    # (배경 == 촬영본 이라 차이가 거의 없다) 가장 흔한 실수라 따로 짚어준다.
    # 한 뷰만 작은 것은 그 뷰가 가려졌을 수 있으니 넘어가고, '절반 이상'이 작을 때만
    # 배경 오염으로 본다(max 로 보면 한 뷰만 커도 통과해 버려 이 실수를 못 잡는다).
    tiny = [k for k, v in coverage.items() if v < TINY_SILHOUETTE]
    if backgrounds and len(tiny) * 2 >= len(coverage):
        raise EmptyHullError(
            "물체가 배경과 거의 구분되지 않습니다. '배경 캡처' 를 누를 때 "
            "스캔대에 물체가 올려져 있지 않았는지 확인하세요 — 배경에 물체가 찍혀 있으면 "
            "차이가 0 이 되어 형상이 만들어지지 않습니다. 물체를 치우고 다시 캡처한 뒤 재시도하세요. "
            f"뷰별 실루엣 비율: "
            f"{', '.join(f'{k} {v * 100:.1f}%' for k, v in sorted(coverage.items()))}",
            detail={"silhouette_coverage": {k: round(v, 4) for k, v in coverage.items()}},
        )

    if occupancy == 0.0:
        # 조용히 빈 메시를 반환하면 화면에 아무것도 안 뜨고 이유도 알 수 없다.
        # visual hull 은 '모든 뷰에서 물체 안쪽'인 복셀만 남기는 AND 라,
        # 한 뷰라도 물체를 놓치면 전부 깎인다. 어느 뷰가 이상한지 같이 알려준다.
        worst = min(coverage, key=coverage.get)
        raise EmptyHullError(
            "5뷰의 실루엣 교집합이 비어 3D 형상을 만들 수 없습니다. "
            "물체가 모든 카메라에 온전히 담기고 배경이 균일해야 합니다. "
            f"뷰별 실루엣 비율: "
            f"{', '.join(f'{k} {v * 100:.0f}%' for k, v in sorted(coverage.items()))}"
            f" (가장 의심스러운 뷰: {worst})",
            detail={"silhouette_coverage": {k: round(v, 4) for k, v in coverage.items()}},
        )

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
        # 카메라 방향(config 의 view_dir)이 얼마나 맞는지. 뷰 하나를 빼고 깎은 형상이
        # 그 뷰의 실루엣을 얼마나 잘 예측하는가. config 를 고쳤을 때 오르면 개선이다.
        # 주의: 절대값의 의미는 약하다. 같은 물체에서 설정끼리 비교할 때만 쓸 것.
        "view_dir_consistency": round(
            silhouette_consistency(silhouettes, directions or VIEW_DIRECTIONS, 32), 4
        ),
    }

    logger.info(
        "재구성 완료: %d뷰 → %d verts / %d faces (occupancy %.3f)",
        len(view_images), len(verts), len(faces), occupancy,
    )

    return ReconstructionResult(
        # 좌표를 소수점 4자리로 줄인다. 메시는 단위 크기로 정규화돼 있으므로
        # 1/10000 정밀도이고 화면에서 차이를 볼 수 없다. 반면 JSON 은 브라우저로
        # 통째로 가므로 문자열 길이가 그대로 전송량이 된다(res 96 에서 8.8MB).
        # float32 상태로 반올림하면 tolist() 에서 이진 변환 오차가 되살아나
        # 0.12340000271797180 같은 긴 소수가 다시 나온다. float64 로 올린 뒤 반올림한다.
        vertices=np.round(verts.astype(np.float64), 4).tolist(),
        faces=faces.tolist(),
        vertex_colors=np.round(colors.astype(np.float64), 3).tolist(),   # 색은 3자리면 8비트보다 곱다
        stats=stats,
    )


def load_backgrounds(root: Path) -> dict[str, np.ndarray]:
    """빈 스캔대 참조 이미지. 없으면 빈 dict → Otsu 폴백으로 동작한다."""
    bg_dir = root / "background"
    if not bg_dir.exists():
        return {}
    out: dict[str, np.ndarray] = {}
    for path in bg_dir.glob("*.jpg"):
        img = imread(path)
        if img is not None:
            out[path.stem] = img
    return out


def load_object_views(root: Path, label: str, object_id: str) -> dict[str, np.ndarray]:
    """classification/{good,defect}/<object_id>/ 에서 5뷰 이미지를 로드."""
    obj_dir = root / "classification" / label / object_id
    if not obj_dir.exists():
        raise FileNotFoundError(f"스캔 이미지를 찾을 수 없습니다: {obj_dir}")

    images: dict[str, np.ndarray] = {}
    for name in VIEW_DIRECTIONS:
        # cam1.jpg 또는 cam1_<episode>.jpg 형태 모두 허용, 가장 최근 것 사용
        candidates = sorted(obj_dir.glob(f"{name}*.jpg"))
        if candidates:
            img = imread(candidates[-1])
            if img is not None:
                images[name] = img

    if not images:
        raise FileNotFoundError(f"{obj_dir} 에 유효한 뷰 이미지가 없습니다.")
    return images
