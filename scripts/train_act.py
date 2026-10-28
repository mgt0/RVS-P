#!/usr/bin/env python3
"""ACT 학습 래퍼 — lerobot-train 을 하위 프로세스로 띄우고 진행 기록을 남긴다.

왜 래퍼인가
-----------
실제 ACT 학습은 GPU 를 몇 시간 점유하므로 카메라·시리얼 포트를 쥔 대시보드 서버
안에서 돌리지 않는다(backend/core/act_trainer.py). 대신 이 래퍼가 서버 밖에서
lerobot-train 을 돌리며 logs/train/<YYYYmmdd_HHMMSS>/ 에 기록을 남기고, 대시보드
'ACT 학습' 탭의 LEROBOT 모드가 그 기록을 따라 읽어 실제 loss 곡선을 그린다.

    run.json       인자·log_freq·precision·num_workers·시작 시각·상태(RUNNING/DONE/FAILED)·returncode·output_dir
    train.log      lerobot-train 의 stdout+stderr 원본
    metrics.jsonl  train.log 에서 뽑은 지표 한 줄씩 (스키마: backend/core/train_log.py)
    gpu.jsonl      학습 중 5초마다 nvidia-smi 로 잰 GPU 상태 (nvidia-smi 가 없으면 만들지 않는다)
    heartbeat      10초마다 수정 시각만 갱신하는 빈 파일. 래퍼가 강제 종료되면 서버가 RUNNING 을 FAILED 로 본다

사용
----
    python scripts/to_lerobot.py --repo-id omx/scan
    python scripts/train_act.py --repo-id omx/scan --steps 20000
    # 혼합정밀도(기본 bf16)와 DataLoader 워커 수(기본은 lerobot 기본값)
    python scripts/train_act.py --repo-id omx/scan --precision no --num-workers 2
    # 그 밖의 lerobot-train 인자는 -- 뒤에 그대로 넘긴다
    python scripts/train_act.py --repo-id omx/scan -- --policy.device=cuda --save_freq=5000

학습을 멈추려면 이 래퍼를 띄운 터미널에서 Ctrl+C. 대시보드 STOP 은 따라 읽기만 끊는다.
"""

from __future__ import annotations

# Windows 한정: cv2/pandas가 torch보다 먼저 로드되면 서로 다른 버전의 동명 런타임
# DLL(OpenMP 계열)을 먼저 점유해버려서, 뒤에 import되는 torch의 DLL 초기화가
# WinError 1114로 깨진다. 이 래퍼는 cv2/pandas를 import하지 않지만(torch를 쓰는 건 자식
# lerobot-train), 새 진입점은 같은 슬롯 선점을 둔다는 저장소 규칙(CLAUDE.md)을 따른다.
# torch가 없거나(ImportError) 보안 정책이 DLL 로드를 막아도(OSError, Smart App Control
# 등) 래퍼는 torch가 필요 없으므로 조용히 넘어간다.
try:
    import torch  # noqa: F401
except (ImportError, OSError):
    pass

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.train_log import (  # noqa: E402
    GPU_FILE,
    GPU_INTERVAL_SEC,
    GPU_QUERY,
    HEARTBEAT_FILE,
    HEARTBEAT_INTERVAL_SEC,
    LOG_FILE,
    METRICS_FILE,
    RUN_FILE,
    TRAIN_LOG_ROOT,
    TrainLogReader,
    parse_gpu_line,
)

# 래퍼 인자와 겹치면 run.json 에 적힌 값과 실제로 lerobot 이 쓴 값이 어긋난다. 특히
# --log_freq 는 스텝 복원(TrainLogReader)의 기준이라 어긋나면 차트 x축이 틀어진다.
_MANAGED = ("--dataset.repo_id", "--steps", "--batch_size", "--log_freq", "--output_dir",
            "--accelerator.mixed_precision", "--num_workers")

# GPU 계측용 호출. GPU 가 여럿이면 GPU 마다 한 줄이 나오는데 첫 줄(GPU 0)만 쓴다.
# ponytail: GPU 0 만 본다. 다른 GPU 로 학습하면(--policy.device=cuda:1) 인덱스를 고르는 옵션이 필요하다.
_SMI_CMD = ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"]
# 일시적인 실패(시간 초과·깨진 출력) 한 번에 몇 시간짜리 학습의 계측이 끊기지 않게, 연속 이만큼 실패해야 멈춘다.
_GPU_MAX_FAILS = 5

_INSTALL_HELP = (
    "lerobot 이 설치되어 있지 않습니다 (lerobot-train 도 없음).\n"
    "  pip install -r requirements.txt -r requirements-lerobot.txt\n"
    "자세한 설치 함정은 README 의 'Windows에서 실제로 겪은 문제' 절을 보세요."
)


def find_lerobot_train() -> list[str] | None:
    """실행할 lerobot-train 명령. 이 python 과 같은 환경의 것을 써야 해서 PATH 는 보지 않는다."""
    exe_dir = Path(sys.executable).parent
    for d in (exe_dir, exe_dir / "Scripts"):
        for name in ("lerobot-train.exe", "lerobot-train"):
            if (d / name).is_file():
                return [str(d / name)]
    # find_spec("lerobot") 은 최상위만 찾을 뿐 임포트하지 않는다(무거운 torch 로딩 없음).
    if importlib.util.find_spec("lerobot") is not None:
        return [sys.executable, "-m", "lerobot.scripts.lerobot_train"]
    return None


def lerobot_has_accelerator() -> bool:
    """설치된 lerobot 이 --accelerator.* 인자(혼합정밀도)를 아는가.

    이 인자는 lerobot main 에만 있고 릴리스(0.6.1 까지 확인)에는 없다. 모르는 인자를 넘기면
    lerobot-train 이 학습을 시작하기도 전에 죽으므로 래퍼가 먼저 확인한다. 임포트하지 않고(torch
    로딩 없음) 패키지 폴더에 main 의 configs/accelerator.py 가 있는지만 본다.
    ponytail: 파일 존재로 가리는 휴리스틱이다. 릴리스에 정식 포함되면 이 검사는 지워도 된다.
    """
    spec = importlib.util.find_spec("lerobot")
    return spec is not None and any(
        (Path(p) / "configs" / "accelerator.py").is_file() for p in spec.submodule_search_locations or ())


def split_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """래퍼 인자와 `--` 뒤 lerobot 인자(extra)를 나눈다."""
    cut = argv.index("--") if "--" in argv else len(argv)
    own, extra = argv[:cut], argv[cut + 1:]

    p = argparse.ArgumentParser(
        description="lerobot-train 래퍼: 실행 기록을 logs/train/ 에 남긴다. "
        "그 밖의 lerobot 인자는 -- 뒤에 그대로 쓴다.")
    p.add_argument("--repo-id", required=True, help="to_lerobot.py 로 만든 데이터셋 repo id (예: omx/scan)")
    p.add_argument("--steps", type=int, help="총 학습 스텝 (생략하면 lerobot 기본값)")
    p.add_argument("--batch-size", type=int, help="배치 크기 (생략하면 lerobot 기본값)")
    p.add_argument("--log-freq", type=int, default=50,
                   help="지표 로그 간격(스텝). 대시보드 곡선의 점 간격이며 lerobot 에 명시적으로 넘긴다")
    p.add_argument("--output-dir", help="체크포인트 폴더 (생략하면 outputs/train/<실행 시각>)")
    p.add_argument("--precision", choices=("bf16", "fp16", "no"), default="bf16",
                   help="혼합정밀도 (기본 bf16). bf16 은 RTX 30 시리즈 이상만 지원하니 그 아래 GPU 면 no")
    p.add_argument("--num-workers", type=int,
                   help="DataLoader 워커 수 (생략하면 lerobot 기본값). GPU 사용률이 들쭉날쭉하면 조정")
    args = p.parse_args(own)
    if args.log_freq < 1:
        p.error("--log-freq 는 1 이상이어야 합니다")
    clash = [a for a in extra if a.split("=", 1)[0] in _MANAGED]
    if clash:
        p.error(f"{clash} 는 래퍼 인자로 지정하세요 "
                "(--repo-id/--steps/--batch-size/--log-freq/--output-dir/--precision/--num-workers)")
    return args, extra


def build_command(base: list[str], args: argparse.Namespace, extra: list[str], output_dir: Path) -> list[str]:
    """lerobot-train 명령줄. README 의 학습 명령(--policy.type=act, --policy.repo_id 필수)과 같다."""
    opts = {
        "--dataset.repo_id": args.repo_id,
        "--log_freq": args.log_freq,
        "--output_dir": output_dir,
    }
    if args.steps:
        opts["--steps"] = args.steps
    if args.batch_size:
        opts["--batch_size"] = args.batch_size
    # 혼합정밀도는 텐서코어를 쓰고 메모리를 아낀다(bf16 은 RTX 30 시리즈 이상, 미지원 GPU 면
    # --precision no). 설치된 lerobot 이 이 인자를 모르면 main() 이 None 으로 비워 여기서 빠진다.
    if args.precision:
        opts["--accelerator.mixed_precision"] = args.precision
    # 0(메인 프로세스에서 로딩)도 유효한 값이라 `if args.num_workers:` 로 거르지 않는다.
    if args.num_workers is not None:
        opts["--num_workers"] = args.num_workers
    # 허브에 올릴 생각이 없어도 --policy.repo_id 는 필수다(없으면 lerobot-train 이 즉시 ValueError).
    # 사용자가 -- 뒤에서 직접 주면 그 값을 따른다.
    given = {a.split("=", 1)[0] for a in extra}
    for key, val in {
        "--policy.type": "act",
        "--policy.repo_id": f"{args.repo_id}_act",
        "--policy.push_to_hub": "false",
    }.items():
        if key not in given:
            opts[key] = val
    return [*base, *(f"{k}={v}" for k, v in opts.items()), *extra]


def pump(stream, log_fp, metrics_fp, reader: TrainLogReader, echo=None) -> None:
    """자식 출력(바이트 줄)을 흘린다: 원본은 train.log 에, 파싱되면 metrics.jsonl 에, 콘솔에도.

    줄마다 flush 하는 이유: 서버가 파일을 따라 읽으므로 버퍼에 머문 줄은 차트에 안 보인다.
    """
    for raw in stream:
        log_fp.write(raw)
        log_fp.flush()
        line = raw.decode("utf-8", errors="replace")
        if echo:
            echo(line)
        rec = reader.feed(line)
        if rec is not None:
            metrics_fp.write(json.dumps(rec) + "\n")
            metrics_fp.flush()


def sample_gpu(path: Path, stop: threading.Event, interval: float = GPU_INTERVAL_SEC) -> None:
    """stop 이 켜질 때까지 interval 마다 nvidia-smi 로 GPU 상태를 재어 path 에 한 줄씩 덧붙인다.

    계측은 부가 기능이다. nvidia-smi 가 없으면(OSError) 한 번 알리고 바로 멈춘다. 그 밖의 실패(시간
    초과·종료 코드·깨진 출력)는 일시적일 수 있어 다음 주기에 다시 시도하고, 연속 _GPU_MAX_FAILS 번
    실패해야 멈춘다. 안내는 첫 실패와 멈출 때만 낸다. 어느 쪽이든 학습은 건드리지 않는다. 줄마다
    파일을 새로 여는 이유: 첫 샘플이 성공하기 전엔 빈 파일도 만들지 않으려는 것이다(nvidia-smi 가
    없으면 gpu.jsonl 도 없다).
    """
    fails, warned = 0, False
    while True:
        try:
            r = subprocess.run(_SMI_CMD, capture_output=True, text=True, errors="replace", timeout=10)
            rec = parse_gpu_line(r.stdout) if r.returncode == 0 else None
            if rec is None:
                raise ValueError((r.stderr or r.stdout).strip()[:200] or f"종료 코드 {r.returncode}, 출력 없음")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            fails += 1
            give_up = isinstance(exc, OSError) or fails >= _GPU_MAX_FAILS
            # 학습이 끝나는 중이면(Ctrl+C 는 nvidia-smi 도 죽인다) 알릴 필요 없다
            if (give_up or not warned) and not stop.is_set():
                warned = True
                note = "멈춥니다" if give_up else "다시 시도합니다"
                print(f"[train_act] GPU 계측을 {note} (학습은 계속) — nvidia-smi: {exc}", file=sys.stderr, flush=True)
            if give_up:
                return
        else:
            fails = 0
            with open(path, "a", encoding="utf-8") as fp:
                fp.write(json.dumps({"ts": round(time.time(), 1), **rec}) + "\n")
        if stop.wait(interval):
            return


def beat(path: Path, stop: threading.Event, interval: float = HEARTBEAT_INTERVAL_SEC) -> None:
    """stop 이 켜질 때까지 interval 마다 path 의 수정 시각을 갱신한다(없으면 만든다).

    래퍼가 강제 종료되면 이 갱신이 끊기고, 서버(train_log.read_run)가 낡은 수정 시각으로 그걸 알아챈다.
    """
    while True:
        try:
            path.touch()
        except OSError:   # 한 번 못 써도 학습은 그대로 — 다음 주기에 다시 시도한다
            pass
        if stop.wait(interval):
            return


def _write_run(run_dir: Path, meta: dict) -> None:
    # 제자리 쓰기. 서버가 반쯤 읽어도 JSON 오류로 다음 폴링에 다시 읽는다(교체 방식은
    # 서버가 파일을 연 순간 Windows 에서 PermissionError 가 난다).
    (run_dir / RUN_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _echo(line: str) -> None:
    sys.stdout.write(line)
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    # 콘솔 코드페이지(cp949)로 못 찍는 글자가 학습 중간에 래퍼를 죽이면 안 된다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args, extra = split_args(sys.argv[1:] if argv is None else argv)

    base = find_lerobot_train()
    if base is None:
        print(_INSTALL_HELP, file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 같은 초에 래퍼가 또 뜨면 _02, _03 … 을 붙여 다시 시도한다. 두 자리여야 폴더 이름의 사전순(latest_run_dir)이
    # 시간순을 지킨다(한 자리면 _10 이 _9 보다 앞에 정렬된다).
    run_id, n = stamp, 1
    while True:
        run_dir = TRAIN_LOG_ROOT / run_id
        try:
            run_dir.mkdir(parents=True)
            break
        except FileExistsError:
            n += 1
            run_id = f"{stamp}_{n:02d}"
    # 자식은 cwd=ROOT 에서 도니, 상대 경로를 사용자의 cwd 기준으로 먼저 확정해 기록과 실제를 맞춘다.
    output_dir = Path(args.output_dir).resolve() if args.output_dir else ROOT / "outputs" / "train" / run_id
    # 모르는 인자를 넘기면 lerobot-train 이 학습을 시작하기도 전에 죽는다. 인자를 빼고(precision=None 이
    # 되어 run.json 에도 실제대로 남는다) 알린다.
    if args.precision and not lerobot_has_accelerator():
        print("[train_act] 설치된 lerobot 에 --accelerator.mixed_precision 이 없어(lerobot main 에만 있음) "
              "혼합정밀도 인자를 생략합니다.", file=sys.stderr, flush=True)
        args.precision = None
    cmd = build_command(base, args, extra, output_dir)

    meta = {
        "status": "RUNNING",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "returncode": None,
        "log_freq": args.log_freq,
        "steps": args.steps,
        "precision": args.precision,
        "num_workers": args.num_workers,
        "output_dir": str(output_dir),
        "args": vars(args),
        "extra": extra,
        "cmd": cmd,
    }
    _write_run(run_dir, meta)
    print(f"[train_act] 실행 기록: {run_dir}\n[train_act] {' '.join(cmd)}", flush=True)

    # 자식의 stdout 을 파이프로 받으면 Windows 에서 로케일(cp949) 인코딩으로 나오고 블록
    # 버퍼링된다. UTF-8 로 고정해야 아래 디코딩이 맞고, unbuffered 여야 줄이 바로 흐른다.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = None
    rc = 1
    gpu_stop = threading.Event()
    gpu_thread = threading.Thread(target=sample_gpu, args=(run_dir / GPU_FILE, gpu_stop),
                                  name="gpu-sampler", daemon=True)
    # GPU 계측과 따로 둔다 — nvidia-smi 가 없는 PC 에서도 생존 신호는 있어야 한다.
    beat_stop = threading.Event()
    beat_thread = threading.Thread(target=beat, args=(run_dir / HEARTBEAT_FILE, beat_stop),
                                   name="heartbeat", daemon=True)
    try:
        beat_thread.start()
        with open(run_dir / LOG_FILE, "wb") as log_fp, \
                open(run_dir / METRICS_FILE, "a", encoding="utf-8") as metrics_fp:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            gpu_thread.start()
            pump(proc.stdout, log_fp, metrics_fp, TrainLogReader(args.log_freq), _echo)
            rc = proc.wait()
    except KeyboardInterrupt:
        print("\n[train_act] 중단 요청 — 학습 프로세스를 정리합니다.", file=sys.stderr)
    finally:
        gpu_stop.set()
        beat_stop.set()
        # 어떤 경로로 빠지든 run.json 이 RUNNING 으로 남으면 대시보드가 영원히 기다린다.
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            rc = proc.returncode
        meta.update(status="DONE" if rc == 0 else "FAILED", returncode=rc,
                    finished_at=datetime.now().isoformat(timespec="seconds"))
        _write_run(run_dir, meta)
        if gpu_thread.is_alive():
            gpu_thread.join(timeout=3)   # 샘플 쓰기·안내 출력이 종료 메시지보다 먼저 끝나게
    print(f"[train_act] 종료: {meta['status']} (returncode={rc})  체크포인트: {output_dir}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
