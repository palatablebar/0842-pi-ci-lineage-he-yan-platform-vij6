import pytest

from app.errors import DomainIssue
from app.recall.service import (
    ANCESTOR_MISSING,
    CONTAMINATED,
    HOLD,
    PENDING,
    PROBLEM_LOT_UNREGISTERED,
    RecallService,
)
from app.recall.store import RecallStore


@pytest.fixture
def service(tmp_path):
    return RecallService(RecallStore(str(tmp_path / "recall.sqlite3")))


def lot(lot_id, parent_lot="", qty_in=100, qty_out=100, loss_qty=0):
    return {"lot_id": lot_id, "parent_lot": parent_lot,
            "qty_in": qty_in, "qty_out": qty_out, "loss_qty": loss_qty}


def recall(recall_id, problem_lot, **extra):
    payload = {"recall_id": recall_id, "problem_lot": problem_lot}
    payload.update(extra)
    return payload


def shipping(snapshot, lot_id):
    return next(item for item in snapshot["lots"] if item["lot_id"] == lot_id)["shipping"]


def hold_row(snapshot, lot_id, recall_id):
    return next(
        row for row in snapshot["holds"]
        if row["lot_id"] == lot_id and row["recall_id"] == recall_id
    )


# 规则 1/2：召回沿血缘双向闭包传播，分量内立即停发；
# 共用上游的旁支同属一个分量一并停发，独立无共用上游的分支照常出货。

def test_recall_closes_over_genealogy_and_independent_branch_keeps_shipping(service):
    # 问题链：L1 → L2 → L4，L1 另有旁支 L3（与 L2 共用上游 L1）。
    # 独立链：R0 → R1，与问题链无任何共用上游。
    service.register_lot(lot("L1"))
    service.register_lot(lot("L2", "L1"))
    service.register_lot(lot("L3", "L1"))
    service.register_lot(lot("L4", "L2"))
    service.register_lot(lot("R0"))
    service.register_lot(lot("R1", "R0"))
    snap = service.register_recall(recall("R-1", "L4"))["recompute"]

    assert {row["lot_id"] for row in snap["holds"]} == {"L1", "L2", "L3", "L4"}
    assert all(row["state"] == HOLD and row["reason_code"] == CONTAMINATED
               for row in snap["holds"])
    # 独立分支证实未混入，照常出货。
    assert shipping(snap, "R0") == "SHIPPABLE"
    assert shipping(snap, "R1") == "SHIPPABLE"
    assert shipping(snap, "L1") == "HOLD"


# 规则 2：晚到的合批记录把原本独立的旁支并入问题分量，立即停发；其余独立分支不动。

def test_late_merge_record_extends_hold(service):
    service.register_lot(lot("L1"))
    service.register_lot(lot("L2", "L1"))
    service.register_lot(lot("L3"))
    service.register_recall(recall("R-1", "L2"))
    snap = service.snapshot()
    assert shipping(snap, "L3") == "SHIPPABLE"

    # 晚到合批：L6 同时消耗 L2 与 L3，L3 经 L6 进入问题分量。
    service.register_lot(lot("L6", "L2:40;L3:60", qty_in=100))
    snap = service.snapshot()
    assert shipping(snap, "L3") == "HOLD"
    assert shipping(snap, "L6") == "HOLD"
    # L1 本就在分量中，仍是同一停发行而非新增。
    assert hold_row(snap, "L1", "R-1")["state"] == HOLD
    assert len([r for r in snap["holds"] if r["lot_id"] == "L1"]) == 1


# 规则 3：缺父批次无法排除混入 → 先停发待核；补齐后按实际来料重算。

def test_missing_parent_is_pending_then_recomputed_when_filled(service):
    service.register_lot(lot("L1"))
    service.register_lot(lot("M1", "L1"))
    # M2 的父批次 P9 记录未到，且与问题链不连通，暂不受召回影响。
    service.register_lot(lot("M2", "P9"))
    service.register_recall(recall("R-1", "M1"))
    snap = service.snapshot()
    assert shipping(snap, "M2") == "SHIPPABLE"

    # 合批记录 M3 同时引用问题侧 M1 与缺失父批次 P9。
    service.register_lot(lot("M3", "M1:50;P9:50"))
    snap = service.snapshot()
    row = hold_row(snap, "M3", "R-1")
    assert row["state"] == PENDING
    assert row["reason_code"] == ANCESTOR_MISSING
    assert "P9" in row["detail"]
    assert shipping(snap, "M3") == "PENDING"

    # 补齐 P9：M3 仍经 M1 与问题来料连通 → 待核转正式停发（CONTAMINATED）。
    service.register_lot(lot("P9"))
    snap = service.snapshot()
    assert hold_row(snap, "M3", "R-1")["state"] == HOLD
    assert hold_row(snap, "M3", "R-1")["reason_code"] == CONTAMINATED


def test_pending_releases_after_fill_when_not_connected(service):
    # C 合批同时挂问题侧 A1 与缺失父批次 PB，先待核；补齐后转为正式停发，
    # 解除召回后停发行释放、恢复出货。
    service.register_lot(lot("A1"))
    service.register_lot(lot("A2", "A1"))
    service.register_recall(recall("R-1", "A2"))
    service.register_lot(lot("C", "A1:40;PB:60"))
    assert hold_row(service.snapshot(), "C", "R-1")["state"] == PENDING

    service.register_lot(lot("PB"))
    snap = service.snapshot()
    assert hold_row(snap, "C", "R-1")["state"] == HOLD

    snap = service.close_recall("R-1")["recompute"]
    assert shipping(snap, "C") == "SHIPPABLE"
    assert all(row["recall_id"] != "R-1" for row in snap["holds"])


# 规则 4：问题来料批次本身未登记 → 待核事项；补齐后转为正式停发。

def test_unregistered_problem_lot_creates_pending_item(service):
    service.register_lot(lot("L1"))
    snap = service.register_recall(recall("R9", "GHOST"))["recompute"]
    assert len(snap["pending_items"]) == 1
    item = snap["pending_items"][0]
    assert item["reason_code"] == PROBLEM_LOT_UNREGISTERED
    assert "GHOST" in item["detail"]
    assert not snap["holds"]

    service.register_lot(lot("GHOST"))
    service.register_lot(lot("L1"))  # 重复投递不产生新版本
    snap = service.snapshot()
    assert not snap["pending_items"]
    assert {row["lot_id"] for row in snap["holds"]} == {"GHOST"}


# 规则 5：一批同时受两起召回，解除一起不能恢复出货。

def test_two_recalls_one_lift_keeps_hold(service):
    service.register_lot(lot("L1"))
    service.register_lot(lot("L2", "L1"))
    service.register_lot(lot("L3", "L1"))
    service.register_recall(recall("R1", "L2"))
    service.register_recall(recall("R2", "L3"))
    snap = service.snapshot()
    # L1 同时受两起召回约束，存在两条停发行。
    rows = [row for row in snap["holds"] if row["lot_id"] == "L1"]
    assert {row["recall_id"] for row in rows} == {"R1", "R2"}
    assert shipping(snap, "L1") == "HOLD"

    snap = service.close_recall("R1")["recompute"]
    assert shipping(snap, "L1") == "HOLD"
    assert hold_row(snap, "L1", "R2")["state"] == HOLD
    # R1 的停发行被释放（审计状态 RELEASED，不出现在当前名单）。
    assert all(not (row["lot_id"] == "L1" and row["recall_id"] == "R1") for row in snap["holds"])

    snap = service.close_recall("R2")["recompute"]
    assert shipping(snap, "L1") == "SHIPPABLE"


# 规则 6：同一起召回重试登记幂等，不新增事件、不新增停发记录。

def test_recall_retry_is_idempotent(service):
    service.register_lot(lot("L1"))
    service.register_lot(lot("L2", "L1"))
    first = service.register_recall(recall("R1", "L2", event_id="evt-1"))
    assert first["created"] is True
    before = list(first["recompute"]["holds"])

    second = service.register_recall(recall("R1", "L2", event_id="evt-1"))
    assert second["created"] is False
    assert len(service.store.list_recalls()) == 1
    assert second["recompute"]["holds"] == before

    # 即使换 event_id，同一 recall_id 仍按同一起召回处理。
    third = service.register_recall(recall("R1", "L2", event_id="evt-other"))
    assert third["created"] is False


def test_distinct_event_id_conflict_is_rejected(service):
    service.register_recall(recall("R1", "L1", event_id="evt-x"))
    with pytest.raises(DomainIssue) as exc:
        service.register_recall(recall("R2", "L2", event_id="evt-x"))
    assert exc.value.code == "EVENT_CONFLICT"


# 登记幂等与晚到更正。

def test_duplicate_lot_does_not_bump_version(service):
    service.register_lot(lot("L1", qty_in=100))
    again = service.register_lot(lot("L1", qty_in=100))
    assert again["changed"] is False
    assert again["lot"]["version"] == 1

    corrected = service.register_lot(lot("L1", qty_in=120, qty_out=120))
    assert corrected["changed"] is True
    assert corrected["lot"]["version"] == 2


# 输入校验。

def test_mass_mismatch_rejected(service):
    with pytest.raises(DomainIssue) as exc:
        service.register_lot(lot("L1", qty_in=10, qty_out=8, loss_qty=1))
    assert exc.value.code == "MASS_MISMATCH"


def test_parent_contribution_mismatch_rejected(service):
    service.register_lot(lot("P1"))
    service.register_lot(lot("P2"))
    with pytest.raises(DomainIssue) as exc:
        service.register_lot(lot("C", "P1:30;P2:30", qty_in=100))
    assert exc.value.code == "PARENT_INPUT_MISMATCH"


def test_merge_requires_each_contribution(service):
    with pytest.raises(DomainIssue) as exc:
        service.register_lot(lot("C", "P1;P2:60"))
    assert exc.value.code == "BAD_PARENT_EDGE"


def test_self_parent_rejected(service):
    with pytest.raises(DomainIssue) as exc:
        service.register_lot(lot("L1", "L1"))
    assert exc.value.code == "BAD_PARENT_EDGE"


def test_independent_lot_with_only_missing_parent_keeps_shipping(service):
    # N 仅依赖尚未登记的 Q，与问题链 P→R 不连通：不能因别人缺父就误停 N。
    service.register_lot(lot("P"))
    service.register_lot(lot("R", "P"))
    service.register_recall(recall("R-1", "R"))
    service.register_lot(lot("N", "Q", qty_in=60, qty_out=60))
    assert shipping(service.snapshot(), "N") == "SHIPPABLE"

    # M 同时挂问题侧 P 与缺失父 Q → 待核；解除召回后待核行一并释放。
    service.register_lot(lot("M", "P:40;Q:60"))
    assert shipping(service.snapshot(), "M") == "PENDING"
    snap = service.close_recall("R-1")["recompute"]
    assert shipping(snap, "M") == "SHIPPABLE"
    assert not snap["holds"]


def test_close_unknown_recall_404(service):
    with pytest.raises(DomainIssue) as exc:
        service.close_recall("NOPE")
    assert exc.value.status == 404


# 解除后再登记同一起召回：重新生效。

def test_recall_can_be_reopened_after_close(service):
    service.register_lot(lot("L1"))
    service.register_recall(recall("R1", "L1"))
    assert service.snapshot()["hold_count"] == 1
    service.close_recall("R1")
    assert service.snapshot()["hold_count"] == 0
    # recall_id 已被关闭事件占用；重新登记同一起召回应返回既有（关闭）记录而不是新建。
    again = service.register_recall(recall("R1", "L1"))
    assert again["created"] is False
    assert again["recall"]["kind"] == "CLOSED"
