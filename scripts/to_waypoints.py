#!/usr/bin/env python3
"""녹화한 시연 에피소드를 사람이 읽고 고칠 수 있는 웨이포인트 파일로 뽑는다.

    lerobot_data/episodes/episode_XXXXXX/joint_states.parquet
        →  lerobot_data/waypoints/episode_XXXXXX.json

ACT 학습(신경망)과 달리 결과물이 그냥 각도 목록이라 텍스트 편집기로 고칠 수
있고, 재생은 완전히 결정적이다. 같은 동작을 반복하기만 하면 되는 작업이면
학습·GPU 없이 이쪽이 낫다.

기본은 follower(실제로 움직인 팔) 컬럼을 쓴다. leader 는 사람이 쥐고 흔든
쪽이라 떨림이 더 많다.

사용
----
    python scripts/to_waypoints.py --episode 000001
    python scripts/to_waypoints.py --episode 000001 --tol 3.0   # 더 성기게
    → 재생: POST /api/rollout/start {"waypoints": "<출력 경로>"}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# run.py 와 같은 이유의 선점 import (CLAUDE.md 참고). 지우지 말 것.
try:
    import torch  # noqa: F401
except (ImportError, OSError):   # OSError: OS 정책이 torch DLL 을 막은 경우(WinError 4551)
    pass

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_config  # noqa: E402


def simplify(times: list[float], poses: list[list[float]], tol: float) -> list[dict]:
    """마지막으로 남긴 자세에서 어느 관절이든 tol(도) 이상 벗어나면 새 웨이포인트.

    50Hz 로 녹화한 수천 프레임을 그대로 재생하면 편집이 불가능하다. tol 이
    유일한 조절 손잡이다 — 작게 잡으면 궤적에 충실하고 파일이 길어진다.
    """
    kept = [{"t": round(times[0], 3), "joints": [round(v, 2) for v in poses[0]]}]
    for t, pose in zip(times[1:], poses[1:]):
        last = kept[-1]["joints"]
        if max(abs(a - b) for a, b in zip(pose, last)) >= tol:
            kept.append({"t": round(t, 3), "joints": [round(v, 2) for v in pose]})
    tail = {"t": round(times[-1], 3), "joints": [round(v, 2) for v in poses[-1]]}
    if tail["t"] != kept[-1]["t"]:
        kept.append(tail)          # 끝점은 tol 과 무관하게 반드시 남긴다
    return kept


def convert(episode_dir: Path, out_path: Path, tol: float, source: str) -> dict:
    df = pd.read_parquet(episode_dir / "joint_states.parquet")
    cols = sorted(c for c in df.columns if c.startswith(f"{source}_joint_"))
    if not cols:
        raise SystemExit(f"{episode_dir.name} 에 {source}_joint_* 컬럼이 없습니다.")

    payload = {
        "source_episode": episode_dir.name,
        "source_arm": source,
        "tol_deg": tol,
        "joint_names": cols,
        "waypoints": simplify(
            df["timestamp"].tolist(), df[cols].values.tolist(), tol
        ),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--episode", required=True, help="에피소드 ID (예: 000001)")
    ap.add_argument("--tol", type=float, default=1.5, help="웨이포인트 간 최소 관절 변화(도)")
    ap.add_argument("--source", default="follower", choices=["follower", "leader"])
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    root = load_config().recording.root
    episode_dir = root / "episodes" / f"episode_{args.episode}"
    if not episode_dir.exists():
        raise SystemExit(f"에피소드를 찾을 수 없습니다: {episode_dir}")

    out = args.out or root / "waypoints" / f"episode_{args.episode}.json"
    payload = convert(episode_dir, out, args.tol, args.source)
    print(f"{len(payload['waypoints'])} 웨이포인트 → {out}")


if __name__ == "__main__":
    main()
