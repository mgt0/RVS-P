#!/usr/bin/env python3
"""수집한 에피소드를 LeRobot 데이터셋으로 변환한다.

    lerobot_data/episodes/episode_XXXXXX/   →   LeRobotDataset (parquet + meta)

포맷 스펙(meta/*.jsonl, 청킹, stats 집계)을 직접 구현하지 않고 lerobot 의
LeRobotDataset.create/add_frame/save_episode 를 그대로 쓴다. 어차피 학습에는
lerobot 이 필요하고, 손으로 쓴 포맷은 lerobot 버전이 오르면 조용히 어긋난다.

모방학습의 핵심 대응:
    observation.state = follower_joint_*   (지금 로봇이 있는 자세)
    action            = leader_joint_*     (사람이 시킨 자세)
이 둘이 바뀌면 학습은 돌지만 정책은 아무것도 배우지 못한다.

사용
----
    python scripts/to_lerobot.py --repo-id omx/scan
    lerobot-train --dataset.repo_id=omx/scan --policy.type=act
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 한정: cv2/pandas가 torch보다 먼저 로드되면 서로 다른 버전의 동명 런타임
# DLL(OpenMP 계열)을 먼저 점유해버려서, 뒤에 import되는 torch의 DLL 초기화가
# WinError 1114로 깨진다. torch를 아무것도 안 하고 먼저 import만 해두면(있으면)
# 슬롯을 먼저 차지해서 이후 cv2/pandas/lerobot import가 전부 정상 동작한다.
# torch가 아예 없는 환경이면 조용히 넘어가고, 아래 lerobot import 실패 시
# 기존 안내 메시지(`lerobot 이 설치되어 있지 않습니다`)로 처리된다.
try:
    import torch  # noqa: F401
except (ImportError, OSError):   # OSError: OS 정책이 torch DLL 을 막은 경우(WinError 4551)
    pass

import cv2
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_config  # noqa: E402
from backend.core.storage import imread  # noqa: E402


def cameras_in(ep_path: Path) -> list[str]:
    """실제로 프레임이 저장된 카메라만. metadata 의 cameras 는 '설정된' 목록이라
    연결이 끊겼던 카메라까지 들어있다."""
    return sorted(
        d.name.removeprefix("camera_")
        for d in ep_path.glob("camera_*")
        if d.is_dir() and any(d.glob("*.jpg"))
    )


def iter_frames(ep_path: Path, cams: list[str], num_joints: int):
    """(frame_dict, frame_index) 를 순서대로 내놓는다. lerobot 없이도 검증 가능한
    순수 함수라 여기에 매핑 로직을 모아둔다."""
    df = pd.read_parquet(ep_path / "joint_states.parquet")
    for row in df.itertuples(index=False):
        idx = int(row.frame_index)
        frame = {
            "observation.state": np.array(
                [getattr(row, f"follower_joint_{i}") for i in range(num_joints)], dtype=np.float32
            ),
            "action": np.array(
                [getattr(row, f"leader_joint_{i}") for i in range(num_joints)], dtype=np.float32
            ),
        }
        missing = False
        for cam in cams:
            img = imread(ep_path / f"camera_{cam}" / f"frame_{idx:06d}.jpg")
            if img is None:
                missing = True
                break
            frame[f"observation.images.{cam}"] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if missing:
            continue  # 프레임 일부만 있는 타임스텝은 통째로 버린다
        yield frame, idx


def _add_frame(ds, frame: dict, task: str) -> None:
    # lerobot 버전에 따라 task 를 인자로 받기도, 프레임 키로 받기도 한다.
    try:
        ds.add_frame(frame, task=task)
    except TypeError:
        ds.add_frame({**frame, "task": task})


def _save_episode(ds, task: str) -> None:
    try:
        ds.save_episode()
    except TypeError:
        ds.save_episode(task=task)


def main() -> None:
    parser = argparse.ArgumentParser(description="에피소드 → LeRobot 데이터셋 변환")
    parser.add_argument("--repo-id", default="omx/scan", help="LeRobot repo id")
    parser.add_argument("--out", default=None, help="출력 루트(기본: lerobot 캐시)")
    parser.add_argument("--task", default=None, help="자연어 태스크 설명(기본: 에피소드 metadata 의 task, 없으면 라벨 기반 자동)")
    args = parser.parse_args()

    try:
        from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
    except ImportError:
        try:
            from lerobot.datasets.lerobot_dataset import LeRobotDataset  # lerobot >= 0.3
        except ImportError:
            sys.exit("lerobot 이 설치되어 있지 않습니다: pip install lerobot")

    root = load_config().recording.root
    eps = sorted(p for p in (root / "episodes").glob("episode_*") if (p / "metadata.json").exists())
    if not eps:
        sys.exit(f"변환할 에피소드가 없습니다: {root / 'episodes'}")

    first = json.loads((eps[0] / "metadata.json").read_text(encoding="utf-8"))
    cams = cameras_in(eps[0])
    if not cams:
        sys.exit(f"{eps[0].name} 에 카메라 프레임이 없습니다.")

    num_joints = len(first["joint_names"])
    probe = imread(next((eps[0] / f"camera_{cams[0]}").glob("*.jpg")))
    h, w = probe.shape[:2]

    features = {
        "observation.state": {"dtype": "float32", "shape": (num_joints,), "names": first["joint_names"]},
        "action": {"dtype": "float32", "shape": (num_joints,), "names": first["joint_names"]},
    }
    for cam in cams:
        features[f"observation.images.{cam}"] = {
            "dtype": "image",  # use_videos=False — ffmpeg 인코딩 없이 JPEG 그대로
            "shape": (h, w, 3),
            "names": ["height", "width", "channels"],
        }

    print(f"에피소드 {len(eps)}개 · 카메라 {', '.join(cams)} · {num_joints}축 · {first['fps']}Hz")
    ds = LeRobotDataset.create(
        repo_id=args.repo_id,
        fps=first["fps"],
        root=args.out,
        features=features,
        use_videos=False,
        image_writer_threads=4,
    )

    total = 0
    for ep_path in eps:
        meta = json.loads((ep_path / "metadata.json").read_text(encoding="utf-8"))
        ep_cams = cameras_in(ep_path)
        if ep_cams != cams:
            # 카메라 구성이 다른 에피소드를 섞으면 feature 스키마가 깨진다.
            print(f"  건너뜀 {ep_path.name}: 카메라 구성 불일치 ({', '.join(ep_cams)})")
            continue
        # 시연 세션(kind=demo)은 에피소드마다 사람이 적은 task 를 metadata.json 에 남긴다.
        task = args.task or meta.get("task") or f"scan a {meta['label']} object"

        n = 0
        for frame, _ in iter_frames(ep_path, cams, num_joints):
            _add_frame(ds, frame, task)
            n += 1
        if n == 0:
            print(f"  건너뜀 {ep_path.name}: 유효 프레임 0")
            continue
        _save_episode(ds, task)
        total += n
        print(f"  {ep_path.name}: {n} frames ({meta['label']}/{meta['object_id']})")

    print(f"\n완료 — {total} frames → {ds.root}")
    print(f"학습: lerobot-train --dataset.repo_id={args.repo_id} --policy.type=act")


if __name__ == "__main__":
    main()
