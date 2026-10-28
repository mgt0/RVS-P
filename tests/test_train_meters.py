"""학습 지표 meters 전달 — lerobot 지표 줄의 나머지 미터가 EpochRecord → status/SSE 로 가는 경로.

lerobot 은 설치하지 않는다. 로그 대신 래퍼가 남기는 metrics.jsonl 의 train 줄과 같은 모양의 합성 기록을 쓴다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("OMX_SIMULATE", "1")

from backend.core.act_metrics import EpochRecord  # noqa: E402
from backend.core.act_trainer import (  # noqa: E402
    DemoTrainer,
    LeRobotTrainer,
    TrainingController,
    _demo_meters,
)

# metrics.jsonl 의 train 줄 하나(lerobot 0.6.1 콘솔의 미터 + 정책 출력 l1_loss·kld_loss)
TRAIN_REC = {"kind": "train", "step": 200, "loss": 5.123, "lr": 1e-5, "smpl": 1600, "ep": 2.7, "epch": 0.19,
             "grdn": 99.1, "data_s": 0.031, "updt_s": 0.215, "smp/s": 32.5, "mem_gb": 5.4,
             "l1_loss": 4.9, "kld_loss": 0.022}
METER_KEYS = {"smpl", "ep", "epch", "grdn", "data_s", "updt_s", "smp/s", "mem_gb", "l1_loss", "kld_loss"}
# meters 가 생기기 전부터 EpochRecord 가 내보내던 필드 — 하나도 바뀌거나 빠지면 안 된다
OLD_FIELDS = {"epoch", "train_loss", "val_loss", "val_mae_deg", "val_success_rate", "val_mae_per_joint",
              "lr", "seconds"}


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def make_run(root: Path, name: str, records: list[dict], *, status: str = "DONE", **run_extra) -> Path:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps(
        {"status": status, "returncode": 0, "log_freq": 50, "steps": 20000, **run_extra}), encoding="utf-8")
    return run_dir


# ------------------------------------------------------------------ 기록 하나 → meters
class TestApply:
    def test_이름_있는_필드_말고_숫자_키는_전부_meters(self):
        ctrl = TrainingController({})
        rec = LeRobotTrainer._apply(ctrl, TRAIN_REC, None)

        assert rec.meters == {k: TRAIN_REC[k] for k in METER_KEYS}
        # 기존 필드는 그대로
        assert (rec.epoch, rec.train_loss, rec.lr) == (200, 5.123, 1e-5)
        assert rec.val_loss is None and rec.val_mae_deg is None and rec.val_success_rate is None
        assert ctrl.history.snapshot()["num_epochs"] == 1

    def test_kind_step_loss_lr_는_meters_에_들어가지_않는다(self):
        rec = LeRobotTrainer._apply(TrainingController({}), TRAIN_REC, None)
        assert not {"kind", "step", "loss", "lr"} & set(rec.meters)

    def test_숫자가_아닌_값은_빠진다(self):
        rec = LeRobotTrainer._apply(TrainingController({}), {**TRAIN_REC, "note": "x", "tag": None, "obj": {"a": 1}}, None)
        assert set(rec.meters) == METER_KEYS

    def test_미터가_없는_버전의_로그는_빈_meters(self):
        # lerobot 버전마다 있는 키가 다르다 — 키를 가정하지 않고 있는 것만 담는다
        rec = LeRobotTrainer._apply(TrainingController({}), {"kind": "train", "step": 50, "loss": 1.5}, None)
        assert rec.meters == {}
        rec = LeRobotTrainer._apply(TrainingController({}), {"kind": "train", "step": 50, "loss": 1.5, "grdn": 3.0}, None)
        assert rec.meters == {"grdn": 3.0}

    def test_평가_줄은_val_loss_만_붙이고_meters_는_건드리지_않는다(self):
        ctrl = TrainingController({})
        last = LeRobotTrainer._apply(ctrl, TRAIN_REC, None)
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 200, "eval_loss": 0.3}, last)
        assert last.val_loss == 0.3 and set(last.meters) == METER_KEYS
        # 스텝이 안 맞는 평가 줄은 직전 train loss 를 이은 별도 점 — 미터는 지어내지 않는다
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 250, "eval_loss": 0.2}, last)
        assert last.epoch == 250 and last.meters == {}

    @staticmethod
    def points(ctrl):
        return [(r["epoch"], r["train_loss"], r["val_loss"]) for r in ctrl.history.snapshot()["records"]]

    def test_같은_스텝의_평가_줄이_또_와도_한_기록이다(self):
        ctrl = TrainingController({})
        last = LeRobotTrainer._apply(ctrl, TRAIN_REC, None)
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 200, "eval_loss": 0.3}, last)
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 200, "eval_loss": 0.25}, last)
        assert self.points(ctrl) == [(200, 5.123, 0.25)]       # 같은 스텝이 둘로 나뉘어 x축이 겹치지 않는다
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 250, "eval_loss": 0.2}, last)
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 250, "eval_loss": 0.2}, last)   # 별도 점도 마찬가지
        assert [p[0] for p in self.points(ctrl)] == [200, 250]

    def test_첫_지표_줄보다_먼저_온_평가_줄도_버리지_않는다(self):
        ctrl = TrainingController({})
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 100, "eval_loss": 0.4}, None)
        assert (last.epoch, last.train_loss, last.val_loss) == (100, None, 0.4)   # 이을 train loss 가 없다
        LeRobotTrainer._apply(ctrl, TRAIN_REC, last)
        assert self.points(ctrl) == [(100, None, 0.4), (200, 5.123, None)]
        snap = ctrl.status()
        assert (snap["best_epoch"], snap["best_val_loss"]) == (100, 0.4)
        json.dumps(snap)                                        # train_loss 가 null 이어도 SSE 로 나간다

    def test_평가_줄이_같은_스텝의_지표_줄보다_먼저_와도_한_기록이다(self):
        ctrl = TrainingController({})
        last = LeRobotTrainer._apply(ctrl, {"kind": "eval", "step": 200, "eval_loss": 0.3}, None)
        last = LeRobotTrainer._apply(ctrl, TRAIN_REC, last)
        assert self.points(ctrl) == [(200, 5.123, 0.3)]
        assert last.lr == 1e-5 and set(last.meters) == METER_KEYS

    def test_to_dict_와_json_직렬화(self):
        d = LeRobotTrainer._apply(TrainingController({}), TRAIN_REC, None).to_dict()
        assert set(d) == OLD_FIELDS | {"meters"}
        assert json.loads(json.dumps(d))["meters"]["smp/s"] == 32.5
        assert EpochRecord(epoch=1, train_loss=1.0, val_loss=None, val_mae_deg=None,
                           val_success_rate=None).to_dict()["meters"] == {}      # 기본값은 빈 dict


# ------------------------------------------------------------------ 따라 읽기 · params.batch_size
class TestFollower:
    def test_status_의_records_에_meters_가_실린다(self, tmp_path):
        make_run(tmp_path, "20261028_100000", [TRAIN_REC, {**TRAIN_REC, "step": 250, "grdn": 80.0}],
                 args={"batch_size": 16})
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)

        snap = ctrl.status()
        assert snap["state"] == "DONE"
        recs = snap["records"]
        assert [r["epoch"] for r in recs] == [200, 250]
        assert recs[0]["meters"]["grdn"] == 99.1 and recs[1]["meters"]["grdn"] == 80.0
        assert all(set(r) == OLD_FIELDS | {"meters"} for r in recs)
        json.dumps(snap)                                        # SSE 가 그대로 직렬화한다

    def test_평가_줄이_어긋난_순서로_와도_스텝은_한_번씩_오름차순(self, tmp_path):
        """차트 x축이 되돌아가거나 같은 스텝이 두 점으로 나오면 안 된다."""
        make_run(tmp_path, "20261028_100000", [
            {"kind": "eval", "step": 100, "eval_loss": 0.5},        # 첫 지표 줄보다 먼저
            {**TRAIN_REC, "step": 200},
            {"kind": "eval", "step": 200, "eval_loss": 0.4},
            {"kind": "eval", "step": 200, "eval_loss": 0.4},        # 같은 평가 줄이 또
            {"kind": "eval", "step": 250, "eval_loss": 0.3},        # 지표 줄이 없는 스텝
            {**TRAIN_REC, "step": 300},
            {"kind": "eval", "step": 300, "eval_loss": 0.2},
        ])
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)

        snap = ctrl.status()
        assert [r["epoch"] for r in snap["records"]] == [100, 200, 250, 300]
        assert [r["val_loss"] for r in snap["records"]] == [0.5, 0.4, 0.3, 0.2]
        assert (snap["best_epoch"], snap["best_val_loss"]) == (300, 0.2)
        json.dumps(snap)

    @pytest.mark.parametrize("extra, expected", [
        ({"args": {"batch_size": 16}}, 16),
        ({"args": {"batch_size": None}}, None),     # 래퍼에 --batch-size 를 안 줬다(lerobot 기본값은 서버가 모른다)
        ({"args": {}}, None),
        ({}, None),                                 # args 가 아예 없는 옛 run.json
    ])
    def test_params_batch_size(self, tmp_path, extra, expected):
        make_run(tmp_path, "20261028_100000", [TRAIN_REC], **extra)
        ctrl = TrainingController({})
        LeRobotTrainer(tmp_path).run(ctrl)
        assert ctrl.status()["params"]["batch_size"] == expected
        # 기존 params 는 그대로
        assert ctrl.params["total_steps"] == 20000 and ctrl.params["log_freq"] == 50
        assert ctrl.params["run_id"] == "20261028_100000"


# ------------------------------------------------------------------ DEMO 합성 미터
class TestDemoMeters:
    def run_demo(self, epochs: int = 6) -> TrainingController:
        ctrl = TrainingController({"epochs": epochs, "epoch_delay_sec": 0})
        DemoTrainer().run(ctrl)
        assert ctrl.state == "DONE"
        return ctrl

    def test_모든_에폭이_같은_키의_미터를_가진다(self):
        recs = self.run_demo().status()["records"]
        assert len(recs) == 6
        assert all(set(r["meters"]) == METER_KEYS for r in recs)
        assert all(set(r) == OLD_FIELDS | {"meters"} for r in recs)       # 기존 필드는 그대로
        assert all(r["val_mae_deg"] is not None for r in recs)             # DEMO 전용 지표도 그대로

    def test_params_batch_size_는_합성_미터의_배치(self):
        ctrl = self.run_demo(epochs=2)
        assert ctrl.status()["params"]["batch_size"] == 8
        assert ctrl.status()["params"]["epochs"] == 2                       # 사용자 params 는 보존
        assert [r["meters"]["smpl"] for r in ctrl.status()["records"]] == [100 * 8, 200 * 8]   # 1 에폭 = 100 스텝

    def test_loss_는_l1_더하기_10_kld(self):
        # ACT: loss = l1_loss + 10 × kld_loss — 화면이 같은 식으로 읽는다
        for r in self.run_demo(epochs=10).status()["records"]:
            m = r["meters"]
            assert m["l1_loss"] + 10 * m["kld_loss"] == pytest.approx(r["train_loss"], abs=1e-3)

    def test_초안_생성식의_성질(self):
        recs = self.run_demo(epochs=50).status()["records"]
        last = recs[-1]["meters"]
        assert (last["updt_s"], last["data_s"]) == (0.215, 0.031)            # 마지막 점이 정확히 이 값이 되게 잡음을 맞춘다
        assert last["smp/s"] == pytest.approx(8 / (0.215 + 0.031 + 0.004), abs=0.1)
        assert recs[0]["meters"]["kld_loss"] > last["kld_loss"]               # 정규화 항은 줄어든다
        assert recs[0]["meters"]["grdn"] > last["grdn"] > 0
        assert all(4.8 <= r["meters"]["mem_gb"] <= 5.4 for r in recs)
        assert last["ep"] == pytest.approx(last["smpl"] / 600, abs=0.1)
        assert last["epch"] == pytest.approx(last["smpl"] / (14 * 600), abs=0.01)

    def test_같은_입력이면_같은_값(self):
        assert _demo_meters(7, 50, 1.234) == _demo_meters(7, 50, 1.234)
        assert set(_demo_meters(1, 1, 0.0)) == METER_KEYS                    # 에폭 1개·loss 0 에서도 터지지 않는다
        assert _demo_meters(1, 1, 0.0)["l1_loss"] == 0.0                     # 음수 loss 는 만들지 않는다


# ------------------------------------------------------------------ API · SSE
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from backend.app import create_app
    from backend.config import load_config

    root = tmp_path_factory.mktemp("meters")
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True), recording=replace(cfg.recording, root=root))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    make_run(root / "logs", "20261028_100000", [TRAIN_REC, {**TRAIN_REC, "step": 250}], args={"batch_size": 8})
    hub.training._log_root = root / "logs"       # 실제 logs/train 을 읽지 않게 한다
    with app.test_client() as c:
        yield c
    hub.shutdown()


class TestApi:
    def test_status_와_SSE_가_meters_를_내보낸다(self, client):
        assert client.post("/api/train/start", json={"demo": False}).status_code == 200
        assert wait_until(lambda: client.get("/api/train/status").get_json()["training"]["state"] == "DONE")

        t = client.get("/api/train/status").get_json()["training"]
        assert t["params"]["batch_size"] == 8 and t["params"]["mode"] == "lerobot"
        assert t["records"][0]["meters"]["updt_s"] == 0.215
        assert [r["epoch"] for r in t["records"]] == [200, 250]

        body = client.get("/api/train/stream").get_data(as_text=True)
        frames = [json.loads(ln[len("data: "):]) for ln in body.splitlines() if ln.startswith("data: ")]
        assert frames and frames[-1]["state"] == "DONE"
        assert frames[-1]["records"][0]["meters"] == {k: TRAIN_REC[k] for k in METER_KEYS}

    def test_DEMO_모드도_같은_경로로_나간다(self, client):
        assert wait_until(lambda: client.get("/api/train/status").get_json()["training"]["state"] != "RUNNING")
        r = client.post("/api/train/start", json={"epochs": 3, "epoch_delay_sec": 0, "demo": True})
        assert r.status_code == 200
        assert wait_until(lambda: client.get("/api/train/status").get_json()["training"]["state"] == "DONE")
        t = client.get("/api/train/status").get_json()["training"]
        assert t["params"]["mode"] == "demo" and t["params"]["batch_size"] == 8
        assert all(set(r["meters"]) == METER_KEYS for r in t["records"])
