#!/usr/bin/env python3
"""자체 포맷 에피소드 → 공식 LeRobotDataset 변환 CLI (신규).

`LeRobotTrainer`가 학습 시작 시 자동으로 호출하는 것과 동일한 변환을,
학습과 분리해서 미리 돌려보고 결과를 확인하고 싶을 때 씁니다. 특히
대용량 변환은 시간이 걸리므로, 학습 버튼을 누르기 전에 변환만 먼저
검증하는 용도로 유용합니다.

사용법
------
    python scripts/convert_dataset.py --episodes-root lerobot_data/episodes \\
        --output lerobot_data/lerobot_datasets/local__omx_pick_and_place \\
        --repo-id local/omx_pick_and_place

lerobot/torch가 설치되어 있어야 합니다 (requirements-lerobot.txt).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="에피소드 → LeRobotDataset 변환")
    parser.add_argument(
        "--episodes-root", type=Path, default=Path("lerobot_data/episodes"),
        help="자체 포맷 에피소드 폴더 (기본: lerobot_data/episodes)",
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="변환된 LeRobotDataset을 저장할 로컬 경로",
    )
    parser.add_argument("--repo-id", default="local/omx_pick_and_place", help="데이터셋 식별자")
    parser.add_argument("--robot-type", default="omx_follower")
    parser.add_argument(
        "--task", default="pick the 3D print and place it on the conveyor",
        help="자연어 태스크 설명 (ACT는 필수 아니지만 다른 정책 실험 시 필요)",
    )
    parser.add_argument(
        "--labels", nargs="+", default=["good"],
        help="변환에 포함할 라벨 (기본: good만 — 정상 파지 시연만 학습에 사용)",
    )
    parser.add_argument("--no-videos", action="store_true", help="mp4 대신 개별 이미지로 저장")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    try:
        from backend.core.lerobot_bridge import LeRobotUnavailableError, convert_episodes_to_dataset
    except ImportError as exc:
        print(f"lerobot/torch가 설치되어 있지 않습니다: {exc}")
        print("→ pip install -r requirements-lerobot.txt")
        sys.exit(1)

    try:
        result = convert_episodes_to_dataset(
            args.episodes_root,
            args.output,
            repo_id=args.repo_id,
            robot_type=args.robot_type,
            task=args.task,
            only_labels=tuple(args.labels),
            use_videos=not args.no_videos,
        )
    except LeRobotUnavailableError as exc:
        print(f"변환 실패: {exc}")
        sys.exit(1)

    print(
        f"\n완료: {result.num_episodes}개 에피소드, {result.num_frames}개 프레임, "
        f"{result.seconds:.1f}초 소요\n저장 위치: {result.root}"
    )


if __name__ == "__main__":
    main()
