#!/usr/bin/env python3
"""ACT 학습 CLI (헤드리스).

웹 대시보드 없이 터미널에서 학습을 돌리고 에폭마다 지표를 출력한다.
대시보드와 '똑같은' act_trainer 코드를 쓰므로 결과가 일치한다.

사용 예
------
    # 데모 (GPU·데이터셋 불필요 — 파이프라인 확인용)
    python scripts/train.py --epochs 50 --demo

    # 실기: scripts/train_act.py 로 돌린 가장 최근 학습의 로그를 따라 읽어 출력한다
    # (학습 자체는 하지 않는다. --epochs 는 DEMO 전용이라 무시된다)
    python scripts/train.py --lerobot
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_config  # noqa: E402
from backend.core.act_trainer import TrainingManager  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="ACT 학습 CLI")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--threshold-deg", type=float, default=5.0,
                        help="success@τ 판정 임계값(도)")
    parser.add_argument("--num-joints", type=int, default=5)
    parser.add_argument("--epoch-delay", type=float, default=0.0,
                        help="에폭 간 인위적 지연(초). 데모 시각화용")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="합성 학습 곡선(기본)")
    mode.add_argument("--lerobot", action="store_true", help="실제 lerobot 학습")
    args = parser.parse_args()

    cfg = load_config()
    manager = TrainingManager(cfg.recording.root)

    params = {
        "epochs": args.epochs,
        "lr": args.lr,
        "threshold_deg": args.threshold_deg,
        "num_joints": args.num_joints,
        "epoch_delay_sec": args.epoch_delay,
    }

    demo = not args.lerobot
    print(f"학습 시작 — DEMO 모드, {args.epochs} epochs" if demo else "학습 시작 — LEROBOT 모드")
    print(f"{'EPOCH':>6} {'TRAIN_LOSS':>12} {'VAL_LOSS':>10} {'VAL_MAE°':>10} {'SUCCESS@τ':>11}")
    print("-" * 54)

    manager.start(params, demo=demo)

    seen = 0
    while True:
        status = manager.status()
        records = status["records"]
        # 새로 추가된 에폭만 출력
        for rec in records[seen:]:
            print(
                f"{rec['epoch']:>6} {rec['train_loss']:>12.4f} "
                f"{(rec['val_loss'] or 0):>10.4f} {(rec['val_mae_deg'] or 0):>10.3f} "
                f"{(rec['val_success_rate'] or 0) * 100:>10.1f}%"
            )
        seen = len(records)

        if status["state"] in ("DONE", "STOPPED", "FAILED"):
            print("-" * 54)
            print(f"종료: {status['state']}")
            if status.get("error"):
                print(f"에러: {status['error']}")
            if status.get("best_epoch"):
                print(f"최적 에폭: {status['best_epoch']} "
                      f"(val_loss {status['best_val_loss']:.4f})")
            break

        time.sleep(0.1)


if __name__ == "__main__":
    main()
