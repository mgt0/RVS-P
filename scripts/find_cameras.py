#!/usr/bin/env python3
"""카메라 확인 — 어떤 장치가 config.yaml 의 어느 슬롯이 되는지 눈으로 본다.

`find_ports.py` 가 arm 에 하는 일을 카메라에 한다. 인덱스는 USB 를 다시 꽂으면
밀리므로 config 는 장치 '이름'으로 지목하는데, 같은 이름이 4대라 어느 것이
cam2~cam5 중 어느 슬롯인지는 사람이 봐야 정해진다. 그래서 썸네일을 남긴다.

사용
----
    python scripts/find_cameras.py
    #   → scratch/camera_shots/nth0.jpg ... 를 열어보고
    #     물리 배치와 다르면 config.yaml 의 nth 값만 바꾼다

주의: 한 대씩 순서대로 연다. 4대를 동시에 열면 USB 대역폭이 모자라
      뒤쪽 카메라가 실패하는데, 그건 배선 문제지 설정 문제가 아니다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.camera_manager import list_device_names  # noqa: E402
from backend.core.storage import imwrite  # noqa: E402


def banding_score(image) -> float:
    """가로 줄무늬(조명 플리커 밴딩)의 세기를 0~1 로 수치화한다.

    밴딩은 '행마다 밝기가 주기적으로 오르내리는' 현상이다. 그래서 각 행의 평균
    밝기를 뽑아 1차원 신호로 만들고, 그 신호에서 저주파(장면의 밝기 기울기)를
    뺀 나머지의 진폭을 본다. 남는 것은 줄무늬뿐이다.

    장면 내용에 거의 영향받지 않는 것이 요점 — 물체가 무엇이든 '행 방향 주기성'만
    본다. 값이 작을수록 좋다.
    """
    import numpy as np

    rows = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean(axis=1)
    # 이동평균(15행)으로 저주파 성분을 만들고 뺀다 = 고역통과
    kernel = np.ones(15) / 15
    smooth = np.convolve(rows, kernel, mode="same")
    ripple = rows[8:-8] - smooth[8:-8]
    return float(ripple.std() / (rows.mean() + 1e-6))


def tune_exposure(index: int, nth: int) -> None:
    """노출값을 바꿔가며 밴딩이 가장 작은 값을 찾는다.

    한국은 60Hz 라 조명이 초당 120번 깜빡인다(주기 8.33ms). 노출 시간이 이
    주기의 정수배일 때 한 프레임이 밝기 변동을 온전히 적분해 줄무늬가 사라진다.
    OpenCV/DirectShow 의 노출값은 log2(초)라 -6 = 1/64초 식으로 2의 거듭제곱만
    쓸 수 있고, 8.33ms 의 정수배와 정확히 맞지 않는다. 그래서 계산 대신 실측한다.
    """
    print(f"\n  nth={nth} (dshow {index}) 노출 튜닝:")
    results = []
    for exposure in (None, -4, -5, -6, -7, -8):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            print("    open 실패")
            cap.release()
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if exposure is None:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)   # 자동
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # 수동
            cap.set(cv2.CAP_PROP_EXPOSURE, exposure)

        frame = None
        for _ in range(12):        # 노출 변경이 반영될 때까지 몇 장 버린다
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
        cap.release()

        if frame is None:
            print(f"    exposure={str(exposure):>5}: 프레임 없음")
            continue
        score = banding_score(frame)
        results.append((score, exposure, frame.mean()))
        label = "자동" if exposure is None else f"{exposure} (1/{2 ** -exposure}초)"
        print(f"    exposure={label:<16} 밴딩 {score:.4f}  밝기 {frame.mean():5.1f}")

    if results:
        best = min(results)
        name = "자동(설정 없음)" if best[1] is None else f"exposure: {best[1]}"
        print(f"    → 가장 줄무늬가 적은 설정: {name}  (밝기 {best[2]:.0f})")


def main() -> None:
    parser = argparse.ArgumentParser(description="카메라 식별")
    parser.add_argument("--match", default="Arducam", help="이 문자열을 포함하는 장치만 (기본: Arducam)")
    parser.add_argument("--out", default="scratch/camera_shots", help="썸네일 저장 위치")
    parser.add_argument("--tune-exposure", action="store_true",
                        help="검은 가로줄(조명 플리커)이 가장 적은 노출값을 실측으로 찾는다")
    args = parser.parse_args()

    names = list_device_names()
    if not names:
        sys.exit("장치를 열거하지 못했습니다. `pip install pygrabber` 후 다시 시도하세요.")

    print("연결된 카메라 (DirectShow 열거 순서):")
    for i, name in enumerate(names):
        used = args.match.lower() in name.lower()
        print(f"  [{i}] {name}{'' if used else '   ← 제외됨(이름 불일치)'}")

    matches = [i for i, n in enumerate(names) if args.match.lower() in n.lower()]
    if not matches:
        sys.exit(f"\n'{args.match}' 를 포함하는 장치가 없습니다.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"\n'{args.match}' 매칭 {len(matches)}대 — 한 대씩 프레임을 받아봅니다:")

    slots = ("cam2", "cam3", "cam4", "cam5")
    for nth, index in enumerate(matches):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            print(f"  nth={nth} (dshow {index}): open 실패")
            cap.release()
            continue

        # FOURCC 는 맨 마지막. 앞에 두면 DSHOW 가 YUY2 로 되돌린다(camera_manager 참고).
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        frame = None
        for _ in range(10):          # 첫 프레임은 버려지는 경우가 흔하다
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
                break
        cap.release()

        slot = slots[nth] if nth < len(slots) else f"slot{nth}"
        if frame is None:
            print(f"  nth={nth} (dshow {index}) → {slot}: 프레임 없음 (대역폭 부족 의심)")
            continue

        path = out / f"nth{nth}_{slot}.jpg"
        imwrite(path, frame, 90)
        print(f"  nth={nth} (dshow {index}) → {slot}: {frame.shape[1]}x{frame.shape[0]}"
              f"  밝기 {frame.mean():.0f}  밴딩 {banding_score(frame):.4f}  →  {path}")

        if args.tune_exposure:
            tune_exposure(index, nth)

    print(f"\n{out} 의 이미지를 열어 물리 배치와 맞는지 확인하세요.")
    print("다르면 config.yaml 의 nth 값만 서로 바꾸면 됩니다.")
    if not args.tune_exposure:
        print("검은 가로줄이 보이면 --tune-exposure 로 최적 노출값을 찾으세요.")


if __name__ == "__main__":
    main()
