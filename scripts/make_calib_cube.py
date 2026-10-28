#!/usr/bin/env python3
"""리그 캘리브레이션용 ArUco 마커 큐브 전개도를 A4(300dpi) PNG 로 만든다.

    python scripts/make_calib_cube.py                 # → calib_cube.png
    python scripts/make_calib_cube.py --out a.png --cube-mm 50 --marker-mm 40

인쇄 → 바깥선을 잘라 → 50mm 큐브(검사용 큐브를 그대로 써도 된다)에 감싸 붙인다. 접으면
각 면 마커의 방향이 backend/core/rig_calibration.FACES 관례와 맞는다(tests/test_scan3d.py 가
접기를 계산해 확인한다). 반드시 '실제 크기(100%)'로 인쇄하고 눈금자를 자로 재 볼 것 —
크기가 다르면 대시보드 계산 요청에 실측 cube_mm·marker_mm 을 넘긴다.
"""

# run.py 와 같은 이유의 torch 선점 import(CLAUDE.md 참고). cv2 보다 먼저 와야 한다.
# 이 PC 처럼 앱 제어 정책이 torch DLL 을 막으면 ImportError 가 아니라 OSError 가 난다.
try:
    import torch  # noqa: F401
except (ImportError, OSError):
    pass

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.rig_calibration import ARUCO_DICT, CUBE_MM, MARKER_MM, detect_markers  # noqa: E402

DPI = 300
A4_MM = (210.0, 297.0)
TOP_MARGIN_MM = 12.0

# 마커 id → (전개도 열, 행, 종이 위에서 반시계로 돌릴 횟수(90°)). 앞(ID 2)을 가운데 두고
# 위(0)·아래(1)·뒤(3)를 세로로, 왼쪽(5)·오른쪽(4)을 옆에 붙인 십자형이다.
# 뒤만 180° 돌려 인쇄한다 — 아래 면을 지나 두 번 접히면서 종이의 위쪽이 3D 의 아래쪽이 된다.
NET: dict[int, tuple[int, int, int]] = {
    0: (1, 0, 0), 5: (0, 1, 0), 2: (1, 1, 0), 4: (2, 1, 0), 1: (1, 2, 0), 3: (1, 3, 2),
}
FACE_NAMES = {0: "위", 1: "아래(벨트 쪽)", 2: "앞", 3: "뒤", 4: "오른쪽", 5: "왼쪽"}
GUIDE = [
    "리그 캘리브레이션 큐브 — 실제 크기(100%)로 인쇄하고 아래 눈금이 50mm 인지 자로 확인",
    "1. 바깥선을 잘라 50mm 큐브에 감싸 붙인다(점선 = 접는 선). 마커가 구겨지면 검출이 흔들린다.",
    "2. 정지 위치에 '앞' 면이 C2·C4(수평 카메라) 사이를 향하게 놓고 대시보드에서 [캘리브레이션 촬영].",
    "   첫 샷이 좌표계 기준이다 — 모든 카메라에 마커가 하나 이상 보이는지 결과를 확인할 것.",
    "3. 큐브를 조금씩 돌리거나 옮겨 2~4장 더 찍고 [계산]. 렌즈 왜곡까지 맞추려면 샷이 여러 장 필요하다.",
]


def _px(mm: float) -> int:
    return int(round(mm * DPI / 25.4))


def _text(img: np.ndarray, lines: list[str], xy: tuple[int, int], size_px: int) -> np.ndarray:
    """한글 안내문. cv2.putText 는 한글을 못 그려 Pillow + 맑은 고딕을 쓴다(없으면 영문 대체)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype("malgun.ttf", size_px)
    except (ImportError, OSError):
        cv2.putText(img, "ArUco calibration cube - print at 100% (scale bar = 50 mm)",
                    xy, cv2.FONT_HERSHEY_SIMPLEX, size_px / 30, 0, 2)
        return img
    pil = Image.fromarray(img)
    draw = ImageDraw.Draw(pil)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, fill=0, font=font)
        y += int(size_px * 1.6)
    return np.asarray(pil)


def render(cube_mm: float = CUBE_MM, marker_mm: float = MARKER_MM) -> np.ndarray:
    if not 0 < marker_mm < cube_mm:
        raise ValueError("marker_mm 는 cube_mm 보다 작아야 합니다.")
    w, h = _px(A4_MM[0]), _px(A4_MM[1])
    if 3 * cube_mm > A4_MM[0] - 10 or 4 * cube_mm + TOP_MARGIN_MM + 60 > A4_MM[1]:
        raise ValueError(f"한 변 {cube_mm}mm 큐브 전개도는 A4 한 장에 들어가지 않습니다.")
    img = np.full((h, w), 255, np.uint8)
    face, marker = _px(cube_mm), _px(marker_mm)
    ox, oy = (w - 3 * face) // 2, _px(TOP_MARGIN_MM)
    dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)

    for marker_id, (col, row, turns) in NET.items():
        x0, y0 = ox + col * face, oy + row * face
        cv2.rectangle(img, (x0, y0), (x0 + face, y0 + face), 170, 2)
        tag = np.rot90(cv2.aruco.generateImageMarker(dictionary, marker_id, marker, borderBits=1), turns)
        mx, my = x0 + (face - marker) // 2, y0 + (face - marker) // 2
        img[my:my + marker, mx:mx + marker] = tag
        cv2.putText(img, f"ID{marker_id}", (x0 + 8, y0 + face - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 150, 2)

    # 접는 선(안쪽 경계)은 점선으로 덧그린다 — 자를 선과 헷갈리지 않게.
    for (c0, r0), (c1, r1) in (((1, 1), (1, 0)), ((1, 1), (0, 1)), ((1, 1), (2, 1)),
                               ((1, 1), (1, 2)), ((1, 2), (1, 3))):
        if r0 == r1:
            x = ox + max(c0, c1) * face
            ys = np.arange(oy + r0 * face, oy + (r0 + 1) * face, 24)
            for y in ys:
                img[y:y + 12, x - 2:x + 3] = 255
        else:
            y = oy + max(r0, r1) * face
            xs = np.arange(ox + c0 * face, ox + (c0 + 1) * face, 24)
            for x in xs:
                img[y - 2:y + 3, x:x + 12] = 255

    # 50mm 눈금자(1mm·5mm·10mm 눈금). 인쇄 배율 확인용.
    rx, ry = ox, oy + 4 * face + _px(10)
    cv2.line(img, (rx, ry), (rx + _px(50), ry), 0, 3)
    for k in range(51):
        tick = _px(4) if k % 10 == 0 else _px(2.5) if k % 5 == 0 else _px(1.5)
        x = rx + _px(k)
        cv2.line(img, (x, ry), (x, ry - tick), 0, 2)
    cv2.putText(img, "50 mm", (rx + _px(52), ry), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 2)

    names = "  ".join(f"ID{i}={FACE_NAMES[i]}" for i in sorted(FACE_NAMES))
    return _text(img, GUIDE + [names], (rx, ry + _px(5)), _px(3.2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="calib_cube.png")
    ap.add_argument("--cube-mm", type=float, default=CUBE_MM)
    ap.add_argument("--marker-mm", type=float, default=MARKER_MM)
    args = ap.parse_args()

    img = render(args.cube_mm, args.marker_mm)
    found = sorted(detect_markers(img))
    if found != sorted(NET):
        # 인쇄물에서 못 찾는 마커는 카메라에서도 못 찾는다. 저장하기 전에 막는다.
        print(f"생성한 이미지에서 마커 {sorted(set(NET) - set(found))} 를 다시 찾지 못했습니다.")
        return 1
    # DPI 를 파일에 적어 둬야 뷰어·프린터가 '실제 크기'로 잡는다.
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return 1
    Path(args.out).write_bytes(_with_dpi(buf.tobytes(), DPI))
    # 콘솔이 cp949 라 줄표(—) 같은 문자는 못 찍는다 — 출력 문구는 기본 문자만.
    print(f"{args.out} 저장: A4 {DPI}dpi, 큐브 {args.cube_mm:g}mm, 마커 {args.marker_mm:g}mm, "
          f"마커 {len(found)}개 재검출 확인")
    return 0


def _with_dpi(png: bytes, dpi: int) -> bytes:
    """PNG 에 pHYs(픽셀/미터) 청크를 넣는다. 없으면 인쇄 대화상자가 72/96dpi 로 보고 키운다."""
    import struct
    import zlib

    ppm = int(round(dpi / 0.0254))
    body = b"pHYs" + struct.pack(">IIB", ppm, ppm, 1)
    chunk = struct.pack(">I", 9) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr_end = 8 + 8 + 13 + 4                       # 시그니처 + IHDR(길이·타입 + 13 + CRC)
    return png[:ihdr_end] + chunk + png[ihdr_end:]


if __name__ == "__main__":
    sys.exit(main())
