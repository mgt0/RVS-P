#!/usr/bin/env python3
"""양품/불량 분류 CNN 학습 — ImageNet 사전학습 ResNet18 + LoRA.

왜 LoRA 인가
-----------
이 리그의 분류 데이터는 에피소드당 4장(뷰 1세트)만 쌓인다. 물체 50개를 찍어도
200장이라, 11M 파라미터를 전부 미세조정하면 곧바로 과적합한다.
LoRA 는 사전학습 가중치를 얼려두고 각 합성곱에 저랭크 보정항만 얹어
학습 파라미터를 ~3% 로 줄인다. 적은 데이터에서 사전학습 특징을 보존하면서
도메인(우리 조명·배경·부품)에만 맞춰 밀어주는 것이 목적이다.

    W' = W + (B @ A),  rank(BA) = r ≪ min(in, out)
    conv 에서는 A = conv(in→r, k×k), B = conv(r→out, 1×1) 두 개로 분해한다.

사용
----
    python scripts/train_classifier.py --epochs 20 --rank 8
    python scripts/train_classifier.py --epochs 20 --no-lora   # 헤드만 학습(비교용)
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_config  # noqa: E402
from backend.core.storage import imread  # noqa: E402

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)
INPUT_SIZE = 224


# ════════════════════════════════════════════════════════════ LoRA
class LoRAConv2d(nn.Module):
    """사전학습 conv 를 얼리고 저랭크 보정항만 학습한다.

    출력 = frozen_conv(x) + scale * B(A(x))
    A 는 원본과 같은 커널/스트라이드/패딩을 쓰고 채널만 r 로 줄인다.
    B 는 1×1 로 다시 out_channels 로 올린다. 이 조합이 커널 공간에서의
    저랭크 갱신 (B @ A) 와 같다.

    B 를 0 으로 초기화하는 것이 핵심이다. 학습 시작 시점의 출력이 사전학습
    모델과 정확히 같아야 초반에 특징이 망가지지 않는다.
    """

    def __init__(self, base: nn.Conv2d, rank: int, alpha: float = 1.0) -> None:
        super().__init__()
        self.base = base
        for p in self.base.parameters():
            p.requires_grad = False

        r = min(rank, base.in_channels, base.out_channels)
        self.scale = alpha / r
        self.down = nn.Conv2d(
            base.in_channels, r, base.kernel_size,
            stride=base.stride, padding=base.padding, dilation=base.dilation,
            groups=base.groups, bias=False,
        )
        self.up = nn.Conv2d(r, base.out_channels, 1, bias=False)

        nn.init.kaiming_uniform_(self.down.weight, a=5 ** 0.5)
        nn.init.zeros_(self.up.weight)   # 시작 시 보정항 = 0

    def forward(self, x):
        return self.base(x) + self.scale * self.up(self.down(x))


def apply_lora(module: nn.Module, rank: int, min_channels: int = 64) -> int:
    """모델 안의 Conv2d 를 LoRAConv2d 로 교체한다. 교체한 개수를 반환.

    채널이 작은 초반 층은 건드리지 않는다. 저수준 특징(에지·색)은 도메인이
    달라도 그대로 쓸 수 있고, 여기까지 열면 적은 데이터로 금방 망가진다.
    """
    replaced = 0
    for name, child in module.named_children():
        if isinstance(child, nn.Conv2d) and child.out_channels >= min_channels:
            setattr(module, name, LoRAConv2d(child, rank))
            replaced += 1
        else:
            replaced += apply_lora(child, rank, min_channels)
    return replaced


# ════════════════════════════════════════════════════════════ 데이터
class ScanDataset(Dataset):
    def __init__(self, samples: list[tuple[Path, int]], train: bool) -> None:
        self.samples = samples
        self.train = train

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, i: int):
        path, label = self.samples[i]
        img = imread(path)
        if img is None:
            raise FileNotFoundError(path)
        img = img[:, :, ::-1]                                  # BGR → RGB

        if self.train:
            # 데이터가 적으므로 가벼운 증강만. 과한 변형은 불량 흔적을 지운다.
            if random.random() < 0.5:
                img = img[:, ::-1]
            h, w = img.shape[:2]
            s = random.uniform(0.75, 1.0)
            ch, cw = int(h * s), int(w * s)
            y = random.randint(0, h - ch)
            x = random.randint(0, w - cw)
            img = img[y:y + ch, x:x + cw]

        img = _resize(img, INPUT_SIZE)
        img = (img.astype(np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
        return torch.from_numpy(img.transpose(2, 0, 1).copy()), label


def _resize(img: np.ndarray, size: int) -> np.ndarray:
    import cv2
    return cv2.resize(np.ascontiguousarray(img), (size, size), interpolation=cv2.INTER_AREA)


def collect(root: Path) -> dict[str, tuple[str, list[Path]]]:
    """object_id → (label, 이미지 목록). 물체 단위로 모으는 것이 핵심이다."""
    objects: dict[str, tuple[str, list[Path]]] = {}
    for label in ("good", "defect"):
        label_dir = root / "classification" / label
        if not label_dir.exists():
            continue
        for obj in sorted(p for p in label_dir.iterdir() if p.is_dir()):
            images = sorted(obj.glob("*.jpg"))
            if images:
                objects[f"{label}/{obj.name}"] = (label, images)
    return objects


def split_by_object(objects: dict, val_ratio: float, seed: int):
    """물체 단위로 나눈다.

    이미지 단위로 나누면 같은 물체의 cam2 가 train, cam3 가 val 로 들어간다.
    거의 같은 장면이라 검증 점수가 실제 일반화 성능보다 크게 부풀려진다
    (train/val 누수). 물체 단위로 갈라야 "처음 보는 부품"을 재는 것이 된다.
    """
    by_label = defaultdict(list)
    for key, (label, _) in objects.items():
        by_label[label].append(key)

    rng = random.Random(seed)
    train_keys, val_keys = [], []
    for label, keys in by_label.items():
        keys = sorted(keys)
        rng.shuffle(keys)
        n_val = max(1, round(len(keys) * val_ratio)) if len(keys) > 1 else 0
        val_keys += keys[:n_val]
        train_keys += keys[n_val:]
    return train_keys, val_keys


def to_samples(objects: dict, keys: list[str]) -> list[tuple[Path, int]]:
    out = []
    for key in keys:
        label, images = objects[key]
        y = 1 if label == "defect" else 0
        out += [(p, y) for p in images]
    return out


# ════════════════════════════════════════════════════════════ 학습
def build_model(rank: int, use_lora: bool) -> tuple[nn.Module, int, int, int]:
    from torchvision.models import ResNet18_Weights, resnet18

    model = resnet18(weights=ResNet18_Weights.IMAGENET1K_V1)
    for p in model.parameters():
        p.requires_grad = False

    n_lora = apply_lora(model, rank) if use_lora else 0
    model.fc = nn.Linear(model.fc.in_features, 2)   # 새 헤드는 항상 학습한다

    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return model, total, trainable, n_lora


def evaluate(model, loader, device) -> dict:
    model.eval()
    correct = total = 0
    confusion = np.zeros((2, 2), int)
    with torch.no_grad():
        for x, y in loader:
            pred = model(x.to(device)).argmax(1).cpu()
            correct += int((pred == y).sum())
            total += len(y)
            for t, p in zip(y.tolist(), pred.tolist()):
                confusion[t, p] += 1
    # 불량을 놓치는 것(재현율)이 양품 오판보다 훨씬 비싸므로 따로 본다.
    tp, fn = confusion[1, 1], confusion[1, 0]
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    return {"accuracy": correct / total if total else 0.0,
            "defect_recall": recall, "confusion": confusion.tolist()}


def main() -> None:
    parser = argparse.ArgumentParser(description="양품/불량 분류 CNN (ResNet18 + LoRA)")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--rank", type=int, default=8, help="LoRA 랭크")
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--val-ratio", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-lora", action="store_true", help="헤드만 학습(비교용)")
    parser.add_argument("--out", default="checkpoints/classifier.pt")
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)

    root = load_config().recording.root
    objects = collect(root)
    if not objects:
        sys.exit(f"분류 이미지가 없습니다: {root / 'classification'}")

    counts = defaultdict(int)
    for label, images in objects.values():
        counts[label] += 1
    print(f"물체 {len(objects)}개 (양품 {counts['good']} / 불량 {counts['defect']}), "
          f"이미지 {sum(len(i) for _, i in objects.values())}장")

    # 물체 20개 미만이면 val 이 몇 개짜리라 지표가 우연에 좌우된다. 숫자를 믿지 말라고
    # 명시한다 — 조용히 돌아가면 0.2 같은 값을 성능으로 오해한다.
    if min(counts["good"], counts["defect"]) < 20:
        print(f"\n경고: 클래스당 물체가 부족합니다 (양품 {counts['good']} / 불량 {counts['defect']}).")
        print("      학습은 돌지만 val 지표는 우연에 좌우되어 의미가 없습니다.")
        print("      서로 다른 object_id 로 클래스당 50개 이상을 목표로 수집하세요.")
        print("      (같은 물체를 여러 번 찍는 것은 도움이 되지 않습니다 — 물체 종류를 늘려야 합니다)\n")

    train_keys, val_keys = split_by_object(objects, args.val_ratio, args.seed)
    train_samples = to_samples(objects, train_keys)
    val_samples = to_samples(objects, val_keys)
    print(f"train {len(train_samples)}장 / {len(train_keys)}물체, "
          f"val {len(val_samples)}장 / {len(val_keys)}물체 (물체 단위 분할)")
    if not val_samples:
        print("검증 물체가 없어 val 지표는 생략합니다.")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, total, trainable, n_lora = build_model(args.rank, not args.no_lora)
    model.to(device)
    print(f"ResNet18 + {'LoRA rank ' + str(args.rank) if n_lora else 'head only'}"
          f" — conv 교체 {n_lora}개, 학습 파라미터 {trainable:,} / {total:,} "
          f"({100 * trainable / total:.1f}%)  device={device}")

    train_loader = DataLoader(ScanDataset(train_samples, True),
                              batch_size=args.batch_size, shuffle=True)
    val_loader = (DataLoader(ScanDataset(val_samples, False), batch_size=args.batch_size)
                  if val_samples else None)

    # 클래스 불균형 보정. 불량이 적은 상태로 두면 전부 '양품'이라 답해도 정확도가 높다.
    n_good = sum(1 for _, y in train_samples if y == 0)
    n_defect = len(train_samples) - n_good
    weight = torch.tensor(
        [1.0, n_good / n_defect if n_defect else 1.0], dtype=torch.float32, device=device
    )

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr, weight_decay=1e-4)

    print(f"\n{'EPOCH':>6} {'TRAIN_LOSS':>11} {'TRAIN_ACC':>10} {'VAL_ACC':>9} {'불량재현율':>10}")
    print("-" * 52)
    best = 0.0
    for epoch in range(1, args.epochs + 1):
        model.train()
        loss_sum = correct = seen = 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            logits = model(x)
            loss = F.cross_entropy(logits, y, weight=weight)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            loss_sum += loss.item() * len(y)
            correct += int((logits.argmax(1) == y).sum())
            seen += len(y)

        val = evaluate(model, val_loader, device) if val_loader else {}
        print(f"{epoch:>6} {loss_sum / seen:>11.4f} {correct / seen:>10.3f} "
              f"{val.get('accuracy', float('nan')):>9.3f} "
              f"{val.get('defect_recall', float('nan')):>10.3f}")

        score = val.get("accuracy", correct / seen)
        if score >= best:
            best = score
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"state_dict": model.state_dict(), "rank": args.rank,
                        "lora": not args.no_lora, "epoch": epoch,
                        "classes": ["good", "defect"]}, out)

    print("-" * 52)
    if val_loader:
        v = evaluate(model, val_loader, device)
        print(f"최종 val 정확도 {v['accuracy']:.3f} · 불량 재현율 {v['defect_recall']:.3f}")
        print(f"혼동행렬 [실제][예측] (0=양품,1=불량): {json.dumps(v['confusion'])}")
    print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
