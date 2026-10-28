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
  - vertex_colors (촬영 색 텍스처, 표면 이상 = 선셋 오렌지 오버레이)
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


# 검사 ROI 를 쓸 때의 배경 무효 판정. ROI 안에서는 물체가 25% 를 넘는 게 정상이라
# 화면 비율로는 판정할 수 없다. 대신 ROI 바깥 테두리 띠(폭 ROI_RING)만 본다 — 물체는
# 가운데 있으니 띠가 대부분 '변했다'면 조명·노출이 바뀐 것이다.
ROI_RING = 0.1
ROI_RING_STALE = 0.5

# ROI 안 GrabCut 보정. 배경 차분은 '배경 캡처와 달라진 곳'을 다 잡아서, 흰 큐브 뒤에서
# 모니터·로봇팔·사람이 움직이면 그걸 물체로, 흰 벽 앞의 흰 큐브는 배경으로 판정했다.
# 차분은 씨앗으로만 쓰고(LO 초과 = 아마 물체, HI 초과를 깎은 것 = 확실히 물체, ROI 테두리 =
# 확실히 배경) 경계는 색 분포로 다시 정한다. 실측(2026-10-28, 9세트): 뷰 leave-one-out
# 평균 0.09~0.52 → 0.50~0.69, 유리 너머 하단 C1 은 이후 뷰 선택에서 거의 항상 빠진다.
GRABCUT_LO = 12
GRABCUT_HI = 40
GRABCUT_ITERS = 5
GRABCUT_RING = 0.04

# 실루엣 안 구멍 중 채운 면적 대비 이 비율보다 작은 것은 메운다. 흰 부품이 흰 배경 앞에
# 서면 면 일부의 차분이 임계 아래로 떨어져 구멍이 나는데, visual hull 은 그 구멍을 관통
# 터널로 깎는다(실측 2026-10-12: cam2/cam3 큐브 면에 구멍). 진짜 관통 홀은 보통 이보다 크다.
MAX_HOLE_RATIO = 0.15

# 실루엣이 화면의 이 비율보다 작으면 '물체를 못 찾았다'고 본다.
# 스캔대 위 부품은 어느 뷰에서든 화면의 몇 % 는 차지한다. 0.5% 는 그보다 훨씬 작다.
TINY_SILHOUETTE = 0.005

# 뷰 하나가 나머지와 안 맞으면(leave-one-out IoU) 깎기에서 뺀다. visual hull 은 AND 라
# 실루엣 한 장이 반쯤 비면(노출 과다로 흰 큐브가 배경에 묻힘 — 실측 cam2) 그만큼 형상이
# 잘려 나간다. 나쁜 뷰 하나를 빼는 게 그 뷰를 믿는 것보다 낫다. 최소 MIN_VIEWS 대는 남긴다.
MIN_VIEWS = 3
VIEW_MAX_DROP = 2          # 한 번에 빼 보는 최대 뷰 수(5대 → 최소 3대 남김)
# 평균이 이만큼 이상 올라야 뷰를 뺀다 — 같으면 뷰가 많은 쪽. 하단 C1 은 멀쩡해도 구조적으로
# 점수가 낮아(합성 0.64) 빼면 평균이 0.02 쯤 오르는데, 그 C1 이 X·Z 폭을 잡는 유일한 뷰라
# 빠지면 외접 치수가 60 → 85mm 로 부풀었다(합성 화이트밸런스 교란). 실데이터에서 실제로 나쁜
# 뷰를 뺄 때는 +0.05~+0.33 이었다(2026-10-28, 9세트).
VIEW_SUBSET_MARGIN = 0.04
CONSISTENCY_RES = 32

# 메시 평활화 강도(복셀 단위 가우시안 시그마). 0 이면 평활화 없음.
#
# 매끄러움과 형상 보존은 정면으로 충돌한다. res 96 실측(거칠기 / 원본 형상 보존):
#   0.0 → 40.7° / 0.926      0.6 → 38.1° / 0.917
#   0.8 → 36.6° / 0.885      1.0 → 36.5° / 0.850
# 1.0 은 거칠기를 0.1도 더 줄이자고 형상을 7% 더 깎는다 — 남는 장사가 아니다.
# 0.6 이 무릎이다. 더 매끄럽게 하려면 시그마가 아니라 뷰를 늘려야 한다.
MESH_SMOOTH_SIGMA = 0.6

# 어느 카메라에서도 보이지 않는 정점의 색. 대시보드 canvas 와 같은 톤의 중립 회색 —
# 여기에 색을 지어내면 '찍히지 않은 면'과 '실제로 회색인 면'을 구분할 수 없다.
NEUTRAL_GRAY = 0.72
# 한 정점의 색에 섞을 카메라 수(내적 상위 K개). 1 이면 담당이 바뀌는 경계에서
# 색이 뚝 끊겨 이음새가 보이고, 크게 잡으면 비스듬한 뷰까지 섞여 흐려진다.
TEXTURE_BLEND_VIEWS = 3
# 가림 판정 허용오차(복셀 칸 단위). 표면은 복셀 한 칸 두께로 요철이 있어
# 1 이하로 잡으면 멀쩡한 표면이 얼룩덜룩 빠지고, 크게 잡으면 뒷면이 새어 들어온다.
OCCLUSION_DEPTH_TOL = 1.5
# 정사영 경로에서 색을 뽑는 텍스처 해상도. 복셀 해상도(≤96)로 줄이면 원본에서 1~2px
# 굵기인 균열 선이 평균에 묻혀 표면 이상 검출이 못 본다.
TEXTURE_RES = 256

# 표면 이상(균열·긁힘·펜 자국) 판정은 정점이 아니라 '원본 이미지'에서 한다(anomaly_map).
# 블랙햇(닫힘 - 원본)은 커널보다 얇은 어두운 자국에만 반응하고, 면과 면 사이의 밝기 계단·
# 완만한 음영에는 반응하지 않는다. 정점 색끼리 비교하던 방식은 정사영 근사의 몇 px 어긋남과
# 면 경계 음영을 결함으로 읽었다(실측 2026-10-28: 깨끗한 큐브 정점의 15~20% 표시).
ANOMALY_KERNEL_FRAC = 0.12   # 블랙햇 커널 = 실루엣 크기의 이 비율(이보다 굵은 자국은 못 본다)
ANOMALY_BLACKHAT = 40.0      # 주변보다 이만큼(회색조) 어두우면 시작, 두 배에서 심각도 1
# 자국은 '부품 표면에 사방이 둘러싸인' 곳에서만 본다(_part_surface). 표면 = 실루엣 밝기
# 상위 25% 지점의 이 비율 이상인 픽셀 중 실루엣 중심부와 이어진 덩어리, 그 안의 구멍(=자국)은
# 메우고 가장자리는 ANOMALY_SURFACE_ERODE_PX 만큼 깎는다.
ANOMALY_SURFACE_FRAC = 0.75
ANOMALY_SURFACE_ERODE_PX = 3
# 왜: 깨끗한 큐브의 오탐은 전부 큐브 면이 아니라 실루엣에 덧붙은 배경 위였다 — 큐브 테두리
# 바로 바깥의 얇은 틈(큐브와 뒤쪽 밝은 물체 사이)이 블랙햇에 '자국'처럼 걸렸다. 실측
# (2026-10-28, 저장된 9세트, 표시 정점 비율): 이전 방식(닫힘 밝기 ≥ 0.8×기준) 깨끗 1.8~3.6% ·
# 자국 3.6~8.8% → 이 방식 깨끗 0.00~0.27% · 자국 3.2~6.8%. 같은 큐브·리그 데이터라 부품·
# 조명이 바뀌면 다시 볼 것(합성 장면·교란 시험은 tests/test_scan3d.py 와 README).
# 품질 게이트(_quality). 하나라도 걸리면 '신뢰도 낮음' — 텍스처·주황을 끄고 이유와 할 일을 단다.
# 형상·색 대응이 틀린 상태의 주황은 결함 위치가 아니라 엉뚱한 곳을 가리킨다.
#   일치도: 남은 뷰의 leave-one-out 평균. 실측(2026-10-28) 현재 9세트 0.73~0.79, 실루엣이
#           망가졌던 이전 파이프라인 0.08~0.32.
#   볼록도: 깎은 형상 복셀 부피 / 볼록 껍질 부피(_solidity). 볼록한 부품의 visual hull 은 볼록하다 —
#           실루엣 일부가 빠지면 형상이 파여 떨어진다. 실측: 현재 9세트 0.86~0.95, 1차 방식(C1 만
#           뺌)에서 무너졌던 2세트 0.71·0.72, 들쭉날쭉했던 defect/cube 0.78.
#           오목한 부품(ㄱ자 브래킷 등)은 정상이어도 낮게 나온다 — 이유 문구에 그렇게 적는다.
QUALITY_MIN_CONSISTENCY = 0.5
QUALITY_MIN_SOLIDITY = 0.8
# 이 내적보다 비스듬히 보이는 정점은 판정에서 뺀다. 스치듯 보면 면 색이 어둡게 찍혀
# 멀쩡한 테두리가 결함으로 잡힌다.
ANOMALY_MIN_FACING = 0.2
ANOMALY_MIN_REGION = 6     # 이보다 작은 표시 덩어리(정점 수)는 노이즈로 지운다
# 불량 표시색 = 대시보드 --accent-sunset(#ff7a17)
SUNSET = np.array([1.0, 0.478, 0.09], dtype=np.float32)

# 각 카메라 슬롯이 물체를 바라보는 방향. space carving 의 투영 기준이다.
# 여기 값은 기본값일 뿐이고, 실제 리그는 config.yaml 의 cameras.*.view_dir 로 덮어쓴다
# (기하는 물리적 사실이라 코드가 아니라 설정이 가져야 한다).
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


def _roi_box(shape, roi) -> tuple[int, int, int, int] | None:
    """정규화 ROI [x0, y0, x1, y1] → 픽셀 박스. roi 가 None 이면 None(화면 전체)."""
    if roi is None:
        return None
    h, w = shape[:2]
    x0, y0, x1, y1 = (float(np.clip(v, 0.0, 1.0)) for v in roi)
    box = (int(round(x0 * w)), int(round(y0 * h)), int(round(x1 * w)), int(round(y1 * h)))
    # 폭이 0 인 ROI 로 실루엣이 통째로 사라지는 것보다 화면 전체를 쓰는 편이 낫다.
    # (config 는 로딩할 때 검증하므로 여기 오는 건 계산된 ROI 의 극단적인 경우뿐)
    return box if box[2] - box[0] >= 8 and box[3] - box[1] >= 8 else None


def _in_roi(fn, image: np.ndarray, box) -> np.ndarray:
    """fn(image) → 마스크를 ROI 안에서만 계산해 원래 크기로 돌려준다.

    Otsu·채도 임계는 ROI 안에서 정해야 한다. 화면 전체로 정하면 물체가 작아
    분포가 배경에 끌려간다.
    """
    if box is None:
        return fn(image)
    x0, y0, x1, y1 = box
    out = np.zeros(image.shape[:2], np.uint8)
    out[y0:y1, x0:x1] = fn(image[y0:y1, x0:x1])
    return out


def _diff_is_stale(mask: np.ndarray, box) -> bool:
    """배경 차분 결과를 못 쓰는지(배경 캡처 이후 조명·노출이 바뀌었는지)."""
    if box is None:
        return (mask > 0).mean() > BACKGROUND_DIFF_FALLBACK
    x0, y0, x1, y1 = box
    roi = mask[y0:y1, x0:x1] > 0
    h, w = roi.shape
    by, bx = max(1, int(h * ROI_RING)), max(1, int(w * ROI_RING))
    ring = np.ones_like(roi)
    ring[by:h - by, bx:w - bx] = False
    return roi[ring].mean() > ROI_RING_STALE


def extract_silhouette(
    image: np.ndarray, background: np.ndarray | None = None, roi=None
) -> np.ndarray:
    """전경(물체) 마스크 추출.

    background 가 있으면 배경 차분, 없으면 Otsu 이진화로 폴백한다.

    Otsu 만으로는 이 리그에서 동작하지 않는다. 스캔대가 실험실 한가운데 있어
    선반·창문·현수막이 화면의 대부분을 차지하는데, 흑백 밝기만으로는 노란 부품과
    배경이 구분되지 않는다(실측: 마스크가 현수막 글자에 붙고 물체는 통째로 누락).
    빈 컨베이어를 한 번 찍어두고 그것과의 차이를 보면 배경이 무엇이든 상관없어진다.

    roi: 정규화 [x0, y0, x1, y1]. 물체가 멈추는 자리만 본다. 배경 캡처 이후 뒤에서
    사람·로봇팔이 움직이면 차분이 그것까지 물체로 잡는데(실측 2026-10-12 cam2·cam4),
    ROI 밖은 아예 보지 않으면 그 문제가 사라진다.
    """
    box = _roi_box(image.shape, roi)
    original = image
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
        mask = _in_roi(lambda m: m, mask, box)

        # ROI 가 있으면 차분은 GrabCut 의 씨앗으로만 쓴다. 배경 캡처 뒤에 ROI 테두리까지
        # 바뀌었어도(아래 '배경 무효') GrabCut 은 테두리를 배경으로 못박고 색으로 경계를 다시
        # 잡아서 채도 폴백보다 낫다(실측 2026-10-28: 그런 세트에서 뷰 일관성 0.1 → 0.6대).
        refined = _grabcut_refine(original, diff, box) if box is not None else None
        if refined is not None:
            mask = refined
        # 배경 차분이 화면 절반 이상을 물체로 잡으면 그 결과는 못 쓴다.
        # 배경 캡처 이후 조명이 바뀌었거나 배경에 다른 물체가 있었다는 뜻이고,
        # 이 상태로 두면 실루엣이 화면 전체가 되어 정합도 비교도 무의미해진다.
        # 조명 변화에 둔감한 채도 기반으로 갈아탄다(실측: 배경 차분 54~80% → 채도 기반).
        elif _diff_is_stale(mask, box):
            logger.warning(
                "배경 차분이 %s의 %.0f%% 를 물체로 잡았습니다. 배경 캡처가 현재 조명과 "
                "맞지 않는 것으로 보여 채도 기반으로 전환합니다.",
                "화면" if box is None else "ROI", 100 * (mask > 0).mean(),
            )
            mask = _in_roi(_saturation_mask, image, box)
    else:
        def _otsu(img: np.ndarray) -> np.ndarray:
            gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            # Otsu 자동 임계. 물체가 배경보다 어둡거나 밝은 양쪽 모두 처리하기 위해
            # 전경 비율이 절반을 넘으면 반전한다.
            _, m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            return cv2.bitwise_not(m) if m.mean() > 127 else m

        mask = _in_roi(_otsu, image, box)

    return _pick_object_component(mask, box)


def _grabcut_refine(image: np.ndarray, diff: np.ndarray, box) -> np.ndarray | None:
    """ROI 안에서 배경 차분을 씨앗으로 GrabCut 을 돌려 물체 경계를 다시 정한다(GRABCUT_* 참고).

    씨앗이 한쪽뿐이면(차분이 전부 낮으면) GrabCut 이 색 분포를 못 세운다 — None 을 돌려
    호출자가 차분·채도 경로로 가게 한다.
    """
    x0, y0, x1, y1 = box
    d, crop = diff[y0:y1, x0:x1], image[y0:y1, x0:x1]
    seeds = np.full(d.shape, cv2.GC_PR_BGD, np.uint8)
    seeds[d > GRABCUT_LO] = cv2.GC_PR_FGD
    sure = cv2.erode((d > GRABCUT_HI).astype(np.uint8), np.ones((5, 5), np.uint8))
    seeds[sure > 0] = cv2.GC_FGD
    h, w = d.shape
    by, bx = max(2, int(h * GRABCUT_RING)), max(2, int(w * GRABCUT_RING))
    ring = np.ones(d.shape, bool)
    ring[by:h - by, bx:w - bx] = False
    seeds[ring] = cv2.GC_BGD
    if not (seeds == cv2.GC_PR_FGD).any() and not (seeds == cv2.GC_FGD).any():
        return None
    # GrabCut 의 색 분포 초기화(k-means)는 OpenCV 전역 난수를 쓴다. 고정하지 않으면 같은 사진도
    # 돌릴 때마다 실루엣이 달라진다(실측 2026-10-28: 같은 세트에서 C1 이 빠졌다 남았다 함).
    cv2.setRNGSeed(0)
    try:
        cv2.grabCut(crop, seeds, None, np.zeros((1, 65)), np.zeros((1, 65)),
                    GRABCUT_ITERS, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None
    out = np.zeros(diff.shape, np.uint8)
    out[y0:y1, x0:x1] = np.where((seeds == cv2.GC_FGD) | (seeds == cv2.GC_PR_FGD), 255, 0)
    return out


def _fill_small_holes(mask: np.ndarray) -> np.ndarray:
    """실루엣 안의 작은 구멍을 메운다(MAX_HOLE_RATIO 참고)."""
    from scipy import ndimage

    solid = mask > 0
    filled = ndimage.binary_fill_holes(solid)
    holes = filled & ~solid
    if not holes.any():
        return mask
    labels, n = ndimage.label(holes)
    sizes = np.asarray(ndimage.sum(holes, labels, range(1, n + 1)))
    small = np.isin(labels, 1 + np.flatnonzero(sizes < MAX_HOLE_RATIO * filled.sum()))
    return np.where(solid | small, 255, 0).astype(np.uint8)


def _pick_object_component(mask: np.ndarray, box=None) -> np.ndarray:
    """마스크를 다듬고 '물체'에 해당하는 덩어리 하나만 남긴다.

    가장 큰 덩어리를 고르지 않는다. 스캔 리그가 실험실 한가운데 있으면
    배경(선반·창문·케이블)이 물체보다 훨씬 커서 배경을 물체로 착각하고,
    그러면 뷰마다 엉뚱한 영역이 잡혀 5뷰 교집합이 비어 형상이 통째로 사라진다
    (실측: 뷰별 실루엣 11~41% 인데 교집합 0복셀).

    물체는 리그 중앙에 놓인다는 전제로, 중앙 ROI 와 겹치는 면적이 가장 큰
    덩어리를 고른다. 중앙 픽셀 하나만 보면 물체에 구멍이 있거나 살짝 치우쳤을 때
    놓치므로 영역으로 판단한다. 검사 ROI(box)가 있으면 화면이 아니라 그 ROI 의
    가운데를 기준으로 삼는다.
    """
    # 잔 구멍 메우기 + 노이즈 제거
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return mask

    h, w = mask.shape
    bx0, by0, bx1, by1 = box if box is not None else (0, 0, w, h)
    bw, bh = bx1 - bx0, by1 - by0
    roi = np.zeros_like(mask, dtype=bool)
    y0, y1 = by0 + int(bh * (1 - CENTER_ROI) / 2), by0 + int(bh * (1 + CENTER_ROI) / 2)
    x0, x1 = bx0 + int(bw * (1 - CENTER_ROI) / 2), bx0 + int(bw * (1 + CENTER_ROI) / 2)
    roi[y0:y1, x0:x1] = True

    overlaps = [int(np.count_nonzero((labels == i) & roi)) for i in range(1, n)]
    best = 1 + int(np.argmax(overlaps))
    if overlaps[best - 1] == 0:
        # 중앙 ROI 에 걸치는 덩어리가 하나도 없다 = 물체가 화면 밖이거나
        # 배경과 구분이 안 된다. 넓이 기준으로 되돌아가되, 그대로 두면
        # 교집합이 비어 상위에서 EmptyHullError 로 잡힌다.
        best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return _fill_small_holes(np.where(labels == best, 255, 0).astype(np.uint8))


def view_basis(direction) -> tuple[np.ndarray, np.ndarray]:
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


def normalize_silhouette(mask: np.ndarray, size: int) -> np.ndarray:
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
    # 뷰마다 물체 크기·위치가 다르므로 바운딩박스 기준으로 맞춘 뒤 투영한다.
    masks = {n: normalize_silhouette(s, resolution) for n, s in silhouettes.items()}
    projections = _ortho_projections(masks, resolution, directions)
    return _carve(masks, projections, (resolution,) * 3).astype(np.float32)


def _view_direction(directions, name) -> tuple[float, float, float]:
    """뷰 하나의 시선 방향: config 값(directions) → 기본 배치(VIEW_DIRECTIONS) → +Z.

    깎기(_ortho_projections)와 색·표면 이상 샘플링(_ortho_view_samples)이 같은 규칙을 써야 한다.
    갈리면 형상은 한 방향 기준으로 깎아 놓고 색은 다른 방향에서 뽑는다.
    """
    return (directions or {}).get(name, VIEW_DIRECTIONS.get(name, (0.0, 0.0, 1.0)))


def _ortho_projections(names, resolution: int, directions) -> dict:
    """정사영 경로의 뷰별 (px, py, valid). 정규화 마스크 밖으로 나가는 복셀은 없어 valid=None."""
    # 시선 방향에 수직인 평면의 정규직교 기저(right, up)를 만들어 그 위에 정사영한다.
    # 예전 구현은 X·Y·Z 세 축 중 하나로만 투영해서, 대각선에 달린 카메라를
    # 가장 가까운 축으로 스냅해 버렸다. 실제 리그는 대각선 4대 + 하단 1대라
    # 어느 것도 축에 정렬돼 있지 않아 형상이 물체와 전혀 다르게 나왔다.
    out = {}
    for name in names:
        px, py = _project_pixels(resolution, _view_direction(directions, name), resolution)
        out[name] = (px.ravel(), py.ravel(), None)
    return out


def _carve(masks: dict[str, np.ndarray], projections: dict, shape) -> np.ndarray:
    """미리 투영해 둔 좌표로 깎는다. 정사영·원근 경로가 같은 AND 를 쓴다.

    valid 가 False 인 복셀(그 카메라 화면 밖·뒤)은 그 카메라가 판단할 근거가 없으므로
    깎지 않는다.

    'N-1 개 뷰에서 안쪽이면 남기기' 같은 관용 깎기는 시험 후 버렸다 — 한 뷰만 깎을 수 있는
    영역(하단 뷰의 X·Z 바깥 등)이 통째로 남아 점유율이 1.0 근처까지 부풀었다(실측 2026-10-28).
    틀린 뷰는 깎기 전에 빼는 쪽(_select_views)이 낫다.
    """
    keep = np.ones(int(np.prod(shape)), bool)
    for name, mask in masks.items():
        px, py, valid = projections[name]
        inside = mask[py, px] > 0
        keep &= inside if valid is None else (inside | ~valid)
    return keep.reshape(shape)


def _loo_scores(masks: dict, projections: dict, shape, close: int = 0) -> dict[str, float]:
    """뷰마다 leave-one-out IoU: 나머지 뷰로 깎은 형상이 이 뷰의 실루엣을 얼마나 맞히나.

    close: 원근 경로는 복셀 간격이 화면에서 몇 px 씩 벌어져 투영 점 사이에 틈이 난다.
    그 간격(px)보다 큰 커널로 한 번 닫아야 실루엣과 비교가 된다. 0 이면 닫지 않는다.
    """
    scores = {}
    for held in masks:
        vol = _carve({n: m for n, m in masks.items() if n != held}, projections, shape).ravel()
        px, py, valid = projections[held]
        sel = vol if valid is None else vol & valid
        mask = masks[held] > 0
        shadow = np.zeros(mask.shape, np.uint8)
        shadow[py[sel], px[sel]] = 1
        if close > 1:
            shadow = cv2.morphologyEx(shadow, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
        shadow = shadow > 0
        union = (shadow | mask).sum()
        scores[held] = float((shadow & mask).sum() / union) if union else 0.0
    return scores


def _select_views(masks: dict, projections: dict, shape, close: int = 0):
    """leave-one-out 평균이 가장 높은 뷰 조합을 고른다. (남길 뷰, 뺀 뷰{이름: 점수}, 뷰별 점수).

    최대 VIEW_MAX_DROP 대까지 빼 본 모든 조합(5대면 16가지)을 잰다. 나쁜 뷰는 깎기에 참여해
    다른 뷰들의 점수까지 끌어내리므로, 빼면 남은 뷰들의 평균이 오른다. 차이가
    VIEW_SUBSET_MARGIN 보다 작으면 뷰가 많은 쪽을 남긴다.

    예전에는 '절대 점수 0.35 미만이면서 나머지 중앙값의 0.6배 미만'인 뷰를 하나씩 뺐다.
    나쁜 뷰가 둘이면 서로 중앙값을 끌어내려 아무것도 못 뺐다 — 실측(2026-10-28) 형상이
    무너지던 2세트가 그랬다(볼록도 0.76·0.77 → 이 방식 0.94·0.94, CAD 정합 9세트 모두 상승).
    한계: 정사영 경로의 바운딩박스 정규화는 격자와 나란하지 않은 물체에서 뷰마다 축척이
    어긋나, 멀쩡한 뷰를 빼기도 한다(합성 구: C5 제외). 캘리브레이션 경로에는 이 문제가 없다.
    """
    import itertools

    names = list(masks)
    if len(names) < 3:
        return names, {}, {}
    full = _loo_scores(masks, projections, shape, close)
    best = (float(np.mean(list(full.values()))), names, full)
    for k in range(len(names) - 1, max(MIN_VIEWS, len(names) - VIEW_MAX_DROP) - 1, -1):
        for sub in itertools.combinations(names, k):
            sc = _loo_scores({n: masks[n] for n in sub}, projections, shape, close)
            mean = float(np.mean(list(sc.values())))
            if mean > best[0] + VIEW_SUBSET_MARGIN:
                best = (mean, list(sub), sc)
    keep = best[1]
    return keep, {n: round(full[n], 3) for n in names if n not in keep}, best[2]


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

    right, up = view_basis(direction)
    pu = cx * right[0] + cy * right[1] + cz * right[2]
    pv = cx * up[0] + cy * up[1] + cz * up[2]
    s = _ortho_scale(right, up)

    px = np.clip(((pu / s * 0.5 + 0.5) * (size - 1)).astype(np.int32), 0, size - 1)
    # 이미지 y 는 아래로 증가하므로 up 을 뒤집는다.
    py = np.clip(((0.5 - pv / s * 0.5) * (size - 1)).astype(np.int32), 0, size - 1)
    return px, py


def _ortho_scale(right, up) -> float:
    """정사영 좌표 → 정규화 마스크 픽셀의 배율. 가로·세로에 '같은' 값을 쓴다.

    스케일은 '이 방향에서 본 복셀 정육면체의 투영 폭'이다(꼭짓점의 최대 투영값 =
    0.5·(|ax|+|ay|+|az|)). 외접 반경(sqrt(3)/2)으로 나누면 투영이 마스크 안쪽으로
    쪼그라들어 아무것도 안 깎인다(실측: 점유율 100%).

    예전 구현은 가로(su)·세로(sv)를 따로 나눠 정육면체 투영을 정사각형에 '늘려' 채웠다.
    그런데 실루엣은 종횡비를 유지한 채 정사각으로 패딩된다(normalize_silhouette).
    둘이 안 맞아 대각선 카메라마다 형상이 한 축으로 눌렸다(합성 상자 IoU 비교는
    tests/test_scan3d.py). 긴 쪽 하나로 맞추면 패딩 규칙과 정확히 같아진다.
    """
    return 0.5 * max(float(np.abs(right).sum()), float(np.abs(up).sum()))


def _volume_to_mesh(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """복셀 볼륨 → 표면 메시(marching cubes).

    scikit-image 가 있으면 그걸 쓰고, 없으면 복셀 표면 큐브를 직접 생성하는
    폴백을 쓴다(의존성 하나라도 줄이는 게 배포에 유리).
    """
    try:
        from skimage import measure

        # 한 칸씩 0 으로 둘러싼다. 형상이 격자 경계에 닿으면(벨트에 놓인 물체의 밑면,
        # 정사영 경로는 정규화 때문에 늘 닿는다) marching cubes 가 그 면을 만들지 않아
        # 메시에 구멍이 뚫린다.
        verts, faces, normals, _ = measure.marching_cubes(
            np.pad(_clean_volume(volume), 1), level=0.5
        )
        verts -= 1.0
        # 투영을 위해 정규화 전 좌표(원점이 중심인 정규 좌표계) 저장
        raw_verts = verts / (volume.shape[0] - 1) - 0.5
        # 중심 정렬 + 단위 정규화
        verts = verts - verts.mean(axis=0)
        scale = np.abs(verts).max() or 1.0
        verts = verts / scale
        return verts.astype(np.float32), faces.astype(np.int32), normals.astype(np.float32), raw_verts.astype(np.float32)
    except ImportError:
        logger.info("scikit-image 없음 → 복셀 큐브 폴백 사용")
        return _voxel_cube_mesh(volume)


def _voxel_cube_mesh(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
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
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int32), np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32)

    # 정규화
    center = coords.mean(axis=0)
    scale = np.abs(coords - center).max() or 1.0

    cube_v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                       [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=np.float32)
    cube_f = np.array([[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6],
                       [0, 4, 5], [0, 5, 1], [1, 5, 6], [1, 6, 2],
                       [2, 6, 7], [2, 7, 3], [3, 7, 4], [3, 4, 0]], dtype=np.int32)

    all_v, all_f, all_raw = [], [], []
    vsize = 1.0 / res
    for i, c in enumerate(coords):
        raw = (c + cube_v * 0.9) / (res - 1) - 0.5
        v = (c + cube_v * 0.9 - center) / scale * vsize * res * 0.5
        all_v.append(v)
        all_raw.append(raw)
        all_f.append(cube_f + i * 8)

    verts = np.vstack(all_v).astype(np.float32)
    faces = np.vstack(all_f).astype(np.int32)
    raw_verts = np.vstack(all_raw).astype(np.float32)
    normals = verts / (np.linalg.norm(verts, axis=1, keepdims=True) + 1e-9)
    return verts, faces, normals, raw_verts


def _map_texture(values: np.ndarray, mask: np.ndarray, resolution: int) -> np.ndarray:
    """0~1 단일 채널 맵을 _view_texture 와 '똑같이' 잘라 펴 (res, res, 1) 로 만든다.

    좌표 변환을 따로 짜면 색과 이상 맵이 서로 다른 곳을 가리킨다 — 같은 함수를 그대로 탄다.
    """
    gray = cv2.cvtColor(np.clip(values * 255.0, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    tex, _ = _view_texture(gray, mask, resolution)
    return tex[:, :, :1]


def _ortho_view_samples(raw_verts, normals, view_images, silhouettes, directions, resolution) -> dict:
    """정사영 경로: 뷰별 (정점 색, 유효, 법선·시선 내적, 이상 맵). 텍스처·표면 이상이 같이 쓴다."""
    out = {}
    # 정점 깊이 비교 허용오차. 복셀 한 칸보다 넉넉해야 표면이 얼룩덜룩 빠지지
    # 않고, 너무 크면 뒷면이 새어 들어온다.
    depth_tol = OCCLUSION_DEPTH_TOL / max(resolution, 1)
    tex_res = max(resolution, TEXTURE_RES)

    for name, img in view_images.items():
        mask = silhouettes.get(name)
        if mask is None:
            continue
        tex, tex_mask = _view_texture(img, mask, tex_res)
        if tex is None:
            continue

        d = np.asarray(_view_direction(directions, name), dtype=np.float64)
        norm = np.linalg.norm(d)
        if norm < 1e-9:
            continue
        d = d / norm

        # 카메라를 향한 면일수록 크다. 등지고 있으면(<=0) 애초에 후보가 아니다.
        dot = (normals @ d.astype(np.float32)).astype(np.float32)

        # carve/_project_pixels 와 완전히 같은 정사영 식을 쓴다. 여기서 어긋나면
        # 형상은 맞는데 색만 밀려 칠해진다. 가림 판정은 복셀 격자 해상도로,
        # 색은 더 고운 텍스처에서 뽑는다.
        right, up = view_basis(d)
        fx, fy = _project_uv(raw_verts, right, up, resolution)

        # 시선 방향 깊이(클수록 카메라에 가깝다) → 픽셀당 최댓값이 보이는 표면.
        depth = (raw_verts @ d.astype(np.float32)).astype(np.float32)
        visible = _visible_by_zbuffer(fx, fy, depth, resolution, depth_tol)

        sx, sy = _project_uv(raw_verts, right, up, tex_res)
        # 색은 실루엣이 아니라 '부품 표면'(_part_surface)에서만 뽑는다. 실루엣에 덧붙은 배경
        # (매트·뒤쪽 물체)을 큐브 옆면에 칠하던 원인이었다 — 그런 정점은 다른 뷰의 색을 받거나
        # 중립 회색으로 남는다.
        surface = _part_surface(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), mask > 0)
        surf_tex = _map_texture(surface.astype(np.float32), mask, tex_res)[:, :, 0] > 0.5
        sample, inside = _sample_bilinear(tex, surf_tex & tex_mask, sx, sy)
        anom, _ = _sample_bilinear(_map_texture(anomaly_map(img, mask, surface), mask, tex_res), tex_mask, sx, sy)
        out[name] = (sample, (dot > 0) & visible & inside, dot, anom[:, 0])
    return out


def _metric_view_samples(verts_mm, normals, view_images, silhouettes, cameras, voxel_mm) -> dict:
    """캘리브레이션 경로: 정점(mm)을 실제 카메라 모델로 원본 이미지에 투영해 색을 뽑는다."""
    out = {}
    for name, img in view_images.items():
        cam, mask = cameras.get(name), silhouettes.get(name)
        if cam is None or mask is None or not mask.any():
            continue
        uv, valid = cam.project(verts_mm)
        depth = verts_mm @ cam.R[2] + cam.tvec[2]           # 카메라 광축 방향 거리(mm)
        to_cam = cam.center - verts_mm
        facing = ((normals * to_cam).sum(axis=1)
                  / (np.linalg.norm(to_cam, axis=1) + 1e-9)).astype(np.float32)

        # 가림 판정 격자 = 복셀 하나가 화면에서 차지하는 크기. 정점이 복셀마다 하나꼴이라
        # 이보다 잘게 나누면 앞면 정점 사이 빈칸으로 뒷면 정점이 '보이는' 것으로 새어 나온다.
        near = np.median(depth[valid]) if valid.any() else 1.0
        cell = max(1.0, 1.5 * float(cam.K[0, 0]) * voxel_mm / max(float(near), 1e-6))
        w, h = cam.size
        visible = _visible_by_zbuffer(
            uv[:, 0] / cell, uv[:, 1] / cell, -depth.astype(np.float32),
            (int(np.ceil(w / cell)), int(np.ceil(h / cell))), OCCLUSION_DEPTH_TOL * voxel_mm,
        )
        tex = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        u, v = uv[:, 0].astype(np.float32), uv[:, 1].astype(np.float32)
        # 색은 부품 표면에서만(정사영 경로와 같은 이유 — _ortho_view_samples).
        surface = _part_surface(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), mask > 0)
        sample, inside = _sample_bilinear(tex, surface, u, v)
        anom, _ = _sample_bilinear(anomaly_map(img, mask, surface)[:, :, None], mask > 0, u, v)
        out[name] = (sample, valid & visible & inside & (facing > 0), facing, anom[:, 0])
    return out


def _blend_texture(samples: dict, num_verts: int) -> np.ndarray:
    """뷰별 샘플을 정점마다 상위 K 개 카메라의 dot^2 가중 평균으로 섞는다."""
    colors = np.full((num_verts, 3), NEUTRAL_GRAY, dtype=np.float32)
    if num_verts == 0:
        return colors
    # 상위 K 개만 반영하기 위해 각 정점이 현재까지 받은 가중치를 들고 있는다.
    kept_weights = np.zeros((num_verts, TEXTURE_BLEND_VIEWS), dtype=np.float32)
    kept_colors = np.zeros((num_verts, TEXTURE_BLEND_VIEWS, 3), dtype=np.float32)
    for sample, ok, dot, *_ in samples.values():
        if not ok.any():
            continue
        # dot^2: 정면 카메라에 힘을 실어 흐릿해지지 않게 하면서 경계에서만 섞이게 한다.
        w = np.where(ok, dot * dot, 0.0).astype(np.float32)
        kept_weights, kept_colors = _keep_top_k(kept_weights, kept_colors, w, sample)

    weight_sum = kept_weights.sum(axis=1)
    color_sum = (kept_colors * kept_weights[:, :, None]).sum(axis=1)
    has_color = weight_sum > 1e-6
    colors[has_color] = color_sum[has_color] / weight_sum[has_color, None]
    return colors


def anomaly_map(image: np.ndarray, mask: np.ndarray, surface: np.ndarray | None = None) -> np.ndarray:
    """원본 이미지에서 '부품 표면 위의 얇고 어두운 자국'(균열·긁힘·펜 자국) 강도 0~1.

    실루엣 비교로는 원리적으로 못 잡는 결함이다 — 금이 가거나 자국이 나도 윤곽은 그대로다
    (실측 라벨: crack, discoloration). 3D 로 옮기기 전에 이미지에서 찾는 이유: 이미지에는
    투영 어긋남이 없다. 3D 정점은 이 맵을 '어디에 칠할지'에만 쓴다.

    한계: 커널(실루엣의 ANOMALY_KERNEL_FRAC)보다 넓은 얼룩, 밝아지거나 색만 바뀐 변색은
    잡지 못한다. 어두운 부품 위의 어두운 자국, 부품 윤곽에 걸쳐 한쪽이 트인 자국도
    마찬가지다(_part_surface 가 표면에 둘러싸인 곳만 본다).

    surface: 호출자가 이미 구한 _part_surface(gray, mask > 0). 주면 다시 계산하지 않는다 —
    색 샘플링도 같은 표면을 쓰므로 뷰마다 두 번 구할 이유가 없다.
    """
    out = np.zeros(mask.shape, np.float32)
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return out
    size = max(int(ys.max() - ys.min()), int(xs.max() - xs.min())) + 1
    k = max(7, int(size * ANOMALY_KERNEL_FRAC) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel).astype(np.float32) - gray
    if surface is None:
        surface = _part_surface(gray, mask > 0)
    out[surface] =np.clip((blackhat[surface] - ANOMALY_BLACKHAT) / ANOMALY_BLACKHAT, 0.0, 1.0)
    # 정점 투영은 몇 px 어긋난다. 1~3px 굵기 선을 정점이 비켜 가지 않게 살짝 넓힌다.
    return cv2.dilate(out, np.ones((3, 3), np.uint8))


def _part_surface(gray: np.ndarray, sil: np.ndarray) -> np.ndarray:
    """실루엣 안에서 '부품 표면'인 픽셀(ANOMALY_SURFACE_* 참고).

    밝은 픽셀 중 실루엣 중심부(거리 변환 상위 절반)와 겹치는 덩어리 하나만 남긴다 — 실루엣에
    덧붙은 배경은 가장자리에 붙은 띠라 중심부와 떨어져 있다. 그 안의 구멍을 메워 '표면에
    둘러싸인 자국'은 넣고, 테두리 바깥 틈처럼 한쪽이 트인 어두운 곳은 뺀다.
    """
    from scipy import ndimage

    bright = sil & (gray >= ANOMALY_SURFACE_FRAC * float(np.percentile(gray[sil], 75)))
    dist = cv2.distanceTransform(sil.astype(np.uint8), cv2.DIST_L2, 5)
    labels, n = ndimage.label(bright)
    if n == 0:
        return bright
    overlap = ndimage.sum(dist >= 0.5 * dist.max(), labels, range(1, n + 1))
    if overlap.max() == 0:
        return np.zeros_like(bright)
    region = ndimage.binary_fill_holes(labels == 1 + int(np.argmax(overlap)))
    e = 2 * ANOMALY_SURFACE_ERODE_PX + 1
    return cv2.erode(region.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (e, e))) > 0


def surface_anomaly(normals: np.ndarray, samples: dict, faces: np.ndarray | None = None):
    """뷰별 표면 이상 맵(anomaly_map)을 정점에 옮긴다. (정점별 0~1 점수, 요약 stats).

    samples[뷰] = (색, 유효, 시선 내적, 이상 맵 샘플). 그 뷰에서 실제로 보이고(가림 판정·
    실루엣 안) 정면에 가까운 정점만 그 뷰의 판정을 받는다. 최종 점수는 뷰별 최댓값 —
    자국은 그걸 정면으로 본 카메라 하나에만 또렷이 찍힌다.
    """
    n = len(normals)
    score = np.zeros(n, np.float32)
    per_view: dict[str, float] = {}
    if n == 0:
        return score, {"flagged_ratio": 0.0, "max_severity": 0.0, "num_regions": 0, "per_view": {}}

    for name, (_, ok, facing, anom) in samples.items():
        view_score = np.where(ok & (facing > ANOMALY_MIN_FACING), anom, 0.0).astype(np.float32)
        per_view[name] = round(float((view_score > 0).mean()), 4)
        np.maximum(score, view_score, out=score)

    num_regions = 0
    if faces is not None and len(faces) and score.any():
        score, num_regions = _drop_small_regions(score, faces)

    flagged = score > 0
    return score, {
        "flagged_ratio": round(float(flagged.mean()), 4),
        "max_severity": round(float(score.max()), 3),
        "num_regions": num_regions,
        "per_view": per_view,
    }


def _drop_small_regions(score: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, int]:
    """메시 위에서 이어진 표시 덩어리를 세고, ANOMALY_MIN_REGION 정점보다 작은 것은 지운다.

    정점 한두 개짜리 점은 JPEG 노이즈·가장자리 샘플링 오차다. 결함 자국은 선이든 얼룩이든
    여러 정점에 걸친다.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    flagged = score > 0
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges = edges[flagged[edges].all(axis=1)]       # 양 끝이 다 표시된 변만 잇는다(가는 선도 이어지게)
    n = len(score)
    graph = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    sizes = np.bincount(labels[flagged], minlength=labels.max() + 1)
    big = sizes >= ANOMALY_MIN_REGION
    keep = flagged & big[labels]
    return np.where(keep, score, 0.0).astype(np.float32), int(np.count_nonzero(big))


def _view_texture(
    image: np.ndarray, mask: np.ndarray, resolution: int
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """이미지와 실루엣을 같은 바운딩박스로 잘라 res×res 정사각 텍스처로 만든다.

    normalize_silhouette 과 같은 규칙(바운딩박스 → 정사각 패딩 → 리사이즈)이어야
    복셀 투영 좌표와 맞는다. 마스크도 똑같이 변환해 함께 돌려준다 — 배경 픽셀을
    걸러내려면 '이 텍셀이 물체인지'를 알아야 하기 때문이다.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None, None

    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)[y0:y1, x0:x1]
    sub = (mask[y0:y1, x0:x1] > 0).astype(np.uint8) * 255

    h, w = rgb.shape[:2]
    side = max(h, w)
    oy, ox = (side - h) // 2, (side - w) // 2
    square = np.zeros((side, side, 3), np.uint8)
    square_mask = np.zeros((side, side), np.uint8)
    square[oy:oy + h, ox:ox + w] = rgb
    square_mask[oy:oy + h, ox:ox + w] = sub

    tex = cv2.resize(square, (resolution, resolution), interpolation=cv2.INTER_AREA)
    # 마스크는 INTER_NEAREST 로 줄인다. 보간하면 경계에 중간값이 생겨 배경이
    # 절반쯤 섞인 텍셀이 '물체'로 통과한다.
    tex_mask = cv2.resize(square_mask, (resolution, resolution), interpolation=cv2.INTER_NEAREST)
    return tex.astype(np.float32) / 255.0, tex_mask > 127


def _project_uv(verts: np.ndarray, right, up, size: int) -> tuple[np.ndarray, np.ndarray]:
    """정점을 이미지 평면에 정사영한 '실수' 픽셀 좌표. _project_pixels 의 연속 버전."""
    pu = verts @ np.asarray(right, dtype=np.float32)
    pv = verts @ np.asarray(up, dtype=np.float32)
    s = _ortho_scale(right, up)
    fx = (pu / s * 0.5 + 0.5) * (size - 1)
    fy = (0.5 - pv / s * 0.5) * (size - 1)
    return fx.astype(np.float32), fy.astype(np.float32)


def _visible_by_zbuffer(
    fx: np.ndarray, fy: np.ndarray, depth: np.ndarray, size, tol: float
) -> np.ndarray:
    """같은 픽셀에 겹친 정점 중 카메라에 가장 가까운 것만 '보인다'고 판정한다.

    depth 는 클수록 카메라에 가깝다. size 는 정사각 한 변 또는 (가로, 세로).
    """
    w, h = (size, size) if np.isscalar(size) else size
    px = np.clip(np.rint(fx).astype(np.int32), 0, w - 1)
    py = np.clip(np.rint(fy).astype(np.int32), 0, h - 1)
    flat = py * w + px

    # np.maximum.at 은 중복 인덱스를 모두 반영한다(단순 대입은 마지막 것만 남는다).
    nearest = np.full(w * h, -np.inf, dtype=np.float32)
    np.maximum.at(nearest, flat, depth)
    return depth >= nearest[flat] - tol


def _sample_bilinear(
    tex: np.ndarray, tex_mask: np.ndarray, fx: np.ndarray, fy: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """이중선형 보간으로 색을 뽑는다. 네 이웃이 모두 물체일 때만 유효로 친다.

    하나라도 배경이면 버리는 이유: 경계 텍셀을 섞으면 배경색이 옅게 배어들어
    결국 A(배경 번짐)를 다시 만든다.
    """
    h, w = tex.shape[:2]
    x0 = np.clip(np.floor(fx).astype(np.int32), 0, w - 1)
    y0 = np.clip(np.floor(fy).astype(np.int32), 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    wx = np.clip(fx - x0, 0.0, 1.0).astype(np.float32)[:, None]
    wy = np.clip(fy - y0, 0.0, 1.0).astype(np.float32)[:, None]

    top = tex[y0, x0] * (1 - wx) + tex[y0, x1] * wx
    bottom = tex[y1, x0] * (1 - wx) + tex[y1, x1] * wx
    sample = top * (1 - wy) + bottom * wy

    inside = tex_mask[y0, x0] & tex_mask[y0, x1] & tex_mask[y1, x0] & tex_mask[y1, x1]
    return sample.astype(np.float32), inside


def _keep_top_k(
    weights: np.ndarray, colors: np.ndarray, new_w: np.ndarray, new_c: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """정점별 상위 K개 후보만 유지한다(가장 약한 것을 새 후보와 교체)."""
    weakest = weights.argmin(axis=1)
    rows = np.arange(len(weights))
    replace = new_w > weights[rows, weakest]
    idx = weakest[replace]
    target = rows[replace]
    weights[target, idx] = new_w[replace]
    colors[target, idx] = new_c[replace]
    return weights, colors


def _apply_defect_overlay(
    verts: np.ndarray, defect_regions: list[dict] | None, base_colors: np.ndarray | None = None
) -> np.ndarray:
    """정점 색상 생성. 불량 영역 근처 정점을 선셋 오렌지로 물들인다.

    defect_regions: [{"center": [x,y,z], "radius": r, "severity": 0~1}, ...]
      CNN/검출 결과에서 넘어오는 불량 위치. 정규화 좌표 기준.
    """
    if base_colors is None:
        colors = np.tile(np.array([0.72, 0.73, 0.75]), (len(verts), 1)).astype(np.float32)
    else:
        colors = base_colors.copy()

    sunset = np.array([1.0, 0.478, 0.09])    # #ff7a17

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


def _solidity(volume: np.ndarray) -> float:
    """깎은 형상의 볼록도 = 복셀 부피 / 볼록 껍질 부피(1 = 볼록).

    껍질은 복셀 '꼭짓점'으로 잡는다. 복셀 중심으로 잡으면 껍질이 반 칸씩 작아져 작은
    형상일수록 비율이 1 을 넘게 부풀고(12³ 정육면체 1.30) 파인 곳이 가려진다. 꼭짓점은
    겉면 복셀 것만 쓴다 — 볼록 껍질은 겉면만으로 정해진다.
    """
    from scipy import ndimage
    from scipy.spatial import ConvexHull

    occ = volume > 0.5
    n = int(occ.sum())
    if n < 8:
        return 0.0
    shell = np.argwhere(occ & ~ndimage.binary_erosion(occ))
    corners = np.array([(i, j, k) for i in (0, 1) for j in (0, 1) for k in (0, 1)])
    try:
        return float(min(1.0, n / ConvexHull((shell[:, None, :] + corners).reshape(-1, 3)).volume))
    except (RuntimeError, ValueError):      # 한 평면에 몰린 점(qhull 실패)
        return 0.0


def _quality(consistency: float, solidity: float, views_used: int) -> dict:
    """결과를 믿어도 되는지. 아니면 이유와 사용자가 할 일을 함께 돌려준다(QUALITY_* 참고)."""
    reasons, actions = [], []
    if views_used < MIN_VIEWS:
        reasons.append(f"쓸 수 있는 카메라가 {views_used}대뿐")
        actions.append("카메라 연결과 config 의 cameras.*.roi(물체가 ROI 안에 있는지)를 확인")
    elif consistency < QUALITY_MIN_CONSISTENCY:
        reasons.append(f"카메라 간 형상 일치도 {consistency:.2f} < {QUALITY_MIN_CONSISTENCY}")
        actions.append("물체를 치우고 '배경 캡처'를 새로 한 뒤 다시 촬영 · 물체 뒤(C2·C4 시야)를 비우거나 단색 배경판")
    if solidity < QUALITY_MIN_SOLIDITY:
        reasons.append(f"형상이 파여 있음(볼록도 {solidity:.2f} < {QUALITY_MIN_SOLIDITY}, 오목한 부품이면 정상일 수 있음)")
        actions.append("조명·노출을 확인하고 다시 촬영 — 한 카메라에서 물체 일부가 배경에 묻혔을 수 있음")
    return {"level": "low" if reasons else "ok", "consistency": round(consistency, 3),
            "solidity": round(solidity, 3), "views_used": views_used,
            "reasons": reasons, "actions": actions}


def _paint_anomaly(colors: np.ndarray, score: np.ndarray) -> np.ndarray:
    """표면 이상 점수를 주황으로 얹는다. 표시된 정점은 최소 절반은 섞는다 — 심각도가 낮다고
    거의 안 보이게 칠하면, 검사자는 '표시 없음'으로 읽는다. 진하기로 심각도를 구분한다."""
    w = np.where(score > 0, 0.5 + 0.5 * score, 0.0).astype(np.float32)[:, None]
    return colors * (1 - w) + SUNSET * w


def consistent_views(silhouettes: dict, directions=None):
    """정사영 기준으로 나머지와 어긋나는 뷰를 뺀다. (남길 뷰, 뺀 뷰{이름: 점수}, 뷰별 점수).

    거친 격자(CONSISTENCY_RES)에서 판정한다 — 부분집합마다 다시 깎기를 반복하므로.
    CAD 비교도 같은 판정을 쓴다(오염된 뷰 하나가 자세 정합을 통째로 망치지 않게).
    """
    masks = {n: normalize_silhouette(s, CONSISTENCY_RES) for n, s in silhouettes.items()}
    return _select_views(
        masks, _ortho_projections(masks, CONSISTENCY_RES, directions), (CONSISTENCY_RES,) * 3
    )


def _carve_ortho(silhouettes: dict, directions, resolution: int):
    """정사영 경로: 뷰 선택은 거친 격자에서, 최종 깎기는 resolution 에서."""
    keep, dropped, scores = consistent_views(silhouettes, directions)
    volume = _carve_visual_hull({n: silhouettes[n] for n in keep}, resolution, directions)
    return volume, keep, dropped, scores


def _metric_projections(names, cameras: dict, pts: np.ndarray) -> dict:
    """원근 경로의 뷰별 (px, py, valid). 화면 밖·카메라 뒤 복셀은 valid=False."""
    out = {}
    for n in names:
        uv, ok = cameras[n].project(pts)
        w, h = cameras[n].size
        px = np.clip(np.rint(uv[:, 0]).astype(np.int32), 0, w - 1)
        py = np.clip(np.rint(uv[:, 1]).astype(np.int32), 0, h - 1)
        out[n] = (px, py, ok)
    return out


def _carve_metric(silhouettes: dict, cameras: dict, rig, resolution: int):
    """캘리브레이션 경로: 작업 공간(mm) 복셀을 실제 카메라 모델로 원본 이미지에 투영해 깎는다."""
    pts, _, step = rig.grid(CONSISTENCY_RES)
    # 거친 격자는 복셀 사이가 화면에서 몇 px 씩 벌어진다. 그 간격보다 크게 닫아야
    # leave-one-out 그림자가 실루엣과 비교된다(벌어진 채면 점수가 고르게 낮아져 멀쩡한 뷰도 빠진다).
    pitch = 1.0
    for cam in cameras.values():
        depth = cam.depth(pts)
        front = depth > 1e-3
        if front.any():
            pitch = max(pitch, float(cam.K[0, 0]) * float(step[0]) / float(np.median(depth[front])))
    keep, dropped, scores = _select_views(
        silhouettes, _metric_projections(silhouettes, cameras, pts),
        (CONSISTENCY_RES,) * 3, close=int(np.ceil(pitch)) + 1,
    )
    pts, lo, step = rig.grid(resolution)
    used = {n: silhouettes[n] for n in keep}
    volume = _carve(used, _metric_projections(used, cameras, pts), (resolution,) * 3)
    return volume.astype(np.float32), keep, dropped, scores, lo, step


def reconstruct_from_views(
    view_images: dict[str, np.ndarray],
    *,
    resolution: int = 64,
    defect_regions: list[dict] | None = None,
    classification: dict | None = None,
    backgrounds: dict[str, np.ndarray] | None = None,
    directions: dict[str, tuple[float, float, float]] | None = None,
    rois: dict | None = None,
    rig=None,
) -> ReconstructionResult:
    """5뷰 이미지 → 3D 메시 + 표면 이상(주황) 오버레이.

    view_images: {"cam1": BGR np.ndarray, ...}. 최소 3뷰 이상 권장.
    backgrounds: 같은 키의 빈 스캔대 이미지. 있으면 배경 차분으로 실루엣을 뽑는다.
    rois: {뷰: 정규화 [x0, y0, x1, y1]}. 실루엣은 이 안에서만 뽑는다. 없는 뷰는 rig 의
      작업 공간 투영, 그것도 없으면 화면 전체.
    rig: rig_calibration.Rig. 보정된 뷰가 MIN_VIEWS 대 이상이면 그 뷰들만으로 실제 카메라 모델(원근)로
      mm 단위 형상을 깎는다. 아니면 모든 뷰로 공칭 시선 방향(directions) + 정사영 근사 — rig 가 있는데
      이쪽으로 돌았으면 stats["calibration_note"] 에 이유를 남긴다.
    classification: {"label": "good"|"defect", ...}. good 이면 외부 defect_regions 를 무시한다.

    주황은 외부 영역(defect_regions)과 별개로, 실제 촬영 색에서 찾은 표면 이상(균열·변색
    자국)이다 — 라벨과 무관하게 증거가 있는 곳에만 칠한다(surface_anomaly).
    나머지와 어긋나는 뷰(배경 오염·노출 과다)는 깎기·색칠 전에 뺀다(_select_views).
    """
    if len(view_images) < 2:
        raise ValueError("재구성에는 최소 2개 이상의 뷰가 필요합니다.")

    backgrounds = backgrounds or {}
    cameras = {n: c for n, c in (rig.cameras.items() if rig is not None else ()) if n in view_images}
    # 원근 경로는 보정 안 된 뷰를 버린다. 보정된 뷰가 MIN_VIEWS 대 미만이면 쓸 뷰가 모자라 품질이 늘 low 가
    # 되므로(2대만 보정된 리그에서 실측), 그때는 모든 뷰를 쓰는 정사영 근사로 돌린다.
    metric = len(cameras) >= MIN_VIEWS
    if metric:
        # 보정 안 된 뷰는 원근 경로에서 어디로 투영할지 모른다. 섞지 않고 뺀다.
        view_images = {n: view_images[n] for n in cameras}
    rois = dict(rois or {})
    for n in cameras:
        if rois.get(n) is None:
            rois[n] = rig.auto_roi(n)
    silhouettes = {
        name: extract_silhouette(img, backgrounds.get(name), rois.get(name))
        for name, img in view_images.items()
    }
    coverage = {name: float((m > 0).mean()) for name, m in silhouettes.items()}

    if metric:
        volume, keep, dropped, scores, lo, step = _carve_metric(silhouettes, cameras, rig, resolution)
    else:
        volume, keep, dropped, scores = _carve_ortho(silhouettes, directions, resolution)
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
            detail={"silhouette_coverage": {k: round(v, 4) for k, v in coverage.items()},
                    "dropped_views": dropped},
        )

    verts, faces, normals, raw_verts = _volume_to_mesh(volume)

    # 실제 이미지 픽셀을 3D 표면에 텍스처로 매핑한다. 뺀 뷰는 색에도 쓰지 않는다 —
    # 실루엣이 틀린 뷰는 바운딩박스 정규화(정사영)나 가림 판정이 어긋나 엉뚱한 색을 칠한다.
    used = {n: view_images[n] for n in keep}
    size_mm = None
    if metric:
        verts_mm = lo + (raw_verts.astype(np.float64) + 0.5) * (resolution - 1) * step
        size_mm = [round(float(v), 1) for v in verts_mm.max(axis=0) - verts_mm.min(axis=0)]
        samples = _metric_view_samples(verts_mm, normals, used, silhouettes, cameras, float(step[0]))
    else:
        samples = _ortho_view_samples(
            raw_verts, normals, used, silhouettes, directions or VIEW_DIRECTIONS, resolution
        )
    score, anomaly = surface_anomaly(normals, samples, faces)
    consistency = float(np.mean(list(scores.values()))) if scores else 0.0
    quality = _quality(consistency, _solidity(volume), len(keep))
    if quality["level"] == "low":
        # 깨진 형상 위의 색·주황은 엉뚱한 곳을 가리킨다. 형상만 중립 회색으로 보이고 이유를 단다.
        score = np.zeros_like(score)
        anomaly = {**anomaly, "flagged_ratio": 0.0, "max_severity": 0.0, "num_regions": 0,
                   "skipped": "형상 신뢰도가 낮아 표면 이상(주황)을 칠하지 않았습니다 — "
                              + " · ".join(quality["reasons"])}
        colors = np.full((len(verts), 3), NEUTRAL_GRAY, np.float32)
    else:
        colors = _paint_anomaly(_blend_texture(samples, len(verts)), score)

    # 외부 검출기(CNN 등)가 준 영역은 양품 라벨이면 끈다. 표면 이상은 라벨과 무관하다.
    label = (classification or {}).get("label", "unknown")
    regions = defect_regions if label != "good" else None
    colors = _apply_defect_overlay(verts, regions, base_colors=colors)

    stats = {
        "num_views": len(view_images),
        "views_used": keep,
        # 나머지와 어긋나 깎기·색칠에서 뺀 뷰와 그 leave-one-out 점수. 배경 오염·노출 과다가 흔한 원인.
        "dropped_views": dropped,
        "view_scores": {k: round(v, 3) for k, v in scores.items()},
        "resolution": resolution,
        "num_vertices": int(len(verts)),
        "num_faces": int(len(faces)),
        "voxel_occupancy": round(occupancy, 4),
        "silhouette_coverage": {k: round(v, 4) for k, v in coverage.items()},
        "classification": classification or {"label": "unknown"},
        "num_defect_regions": (len(regions) if regions else 0) + anomaly["num_regions"],
        "anomaly": anomaly,
        "mesh_method": "visual_hull_metric" if metric else "visual_hull",
        # 남은 뷰의 leave-one-out 평균 — 카메라 기하(view_dir/캘리브레이션)와 실루엣이
        # 서로 얼마나 맞는지. 절대값의 의미는 약하다. 같은 물체에서 설정끼리 비교할 때만 쓸 것.
        "view_dir_consistency": round(consistency, 4),
        # 이 결과를 믿어도 되는지 + 아니면 왜·무엇을 할지. 화면이 한 줄로 보여준다.
        "quality": quality,
    }
    if rig is not None and not metric:
        # quality.reasons 에 넣지 않는다 — reasons 가 하나라도 있으면 level 이 low 가 되어 색·주황이 꺼진다.
        stats["calibration_note"] = (
            f"캘리브레이션된 카메라가 {len(cameras)}대뿐이라 근사 형상(정사영)으로 계산했습니다 "
            f"— mm 치수는 {MIN_VIEWS}대 이상 보정해야 나옵니다."
        )
    if size_mm is not None:
        stats["size_mm"] = size_mm

    logger.info(
        "재구성 완료: %d뷰(제외 %s) → %d verts / %d faces (occupancy %.3f, 이상 %.1f%%)",
        len(keep), list(dropped) or "-", len(verts), len(faces), occupancy,
        100 * anomaly["flagged_ratio"],
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
