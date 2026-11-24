"""召回停发登记服务。

规则要点（与业务口径一一对应）：
1. 每份记录各查各的：批次记录、召回事件分别按幂等键登记。
2. 召回沿血缘上下游双向闭包传播；同一连通分量共用上游。
3. 分量完整（所有祖先记录已到齐）且不含问题来料 → 证实未混入，照常出货。
4. 分量完整但含问题来料 → 分量内全部批次立即停发（CONTAMINATED）。
5. 分量缺父批次、无法排除混入 → 全部先停发待核（ANCESTOR_MISSING）；
   晚到记录补齐后按实际来料重算，待核可自动转为停发或恢复出货。
6. 同一批次可同时受多起召回约束；解除一起仅释放该召回对应的停发行，
   其余召回仍生效时批次继续停发，不能因一起解除就恢复出货。
7. 同一起召回的重试登记命中幂等键，不新增事件、不新增停发记录。
"""

import hashlib

from app.errors import DomainIssue
from app.recall.graph import Genealogy, parse_parents
from app.recall.store import RecallStore

CONTAMINATED = "CONTAMINATED"
ANCESTOR_MISSING = "ANCESTOR_MISSING"
PROBLEM_LOT_UNREGISTERED = "PROBLEM_LOT_UNREGISTERED"

HOLD = "HOLD"
PENDING = "PENDING"


def _content_key(lot_id, parents, qty_in, qty_out, loss_qty):
    raw = "|".join([
        lot_id,
        ";".join(f"{p}:{q if q is not None else ''}" for p, q in sorted(parents.items())),
        str(qty_in), str(qty_out), str(loss_qty),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _require_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainIssue("BAD_REQUEST", detail=f"{field} 必须是整数")
    return value


class RecallService:
    def __init__(self, store: RecallStore):
        self.store = store

    # -- 登记 -----------------------------------------------------------

    def register_lot(self, payload):
        lot_id = str(payload.get("lot_id") or "").strip()
        if not lot_id:
            raise DomainIssue("BAD_REQUEST", detail="lot_id 不能为空")
        qty_in = _require_int(payload.get("qty_in"), "qty_in")
        qty_out = _require_int(payload.get("qty_out"), "qty_out")
        loss_qty = _require_int(payload.get("loss_qty", 0), "loss_qty")
        if qty_in < 0 or qty_out < 0 or loss_qty < 0:
            raise DomainIssue("BAD_REQUEST", detail="数量不能为负")
        if qty_in != qty_out + loss_qty:
            raise DomainIssue("MASS_MISMATCH")
        parents = parse_parents(payload.get("parent_lot"))
        if lot_id in parents:
            raise DomainIssue("BAD_PARENT_EDGE", detail="批次不能以自身为父批次")
        if None in parents.values():
            if len(parents) != 1:
                raise DomainIssue("BAD_PARENT_EDGE", detail="合批投料必须逐项填写贡献量")
            only = next(iter(parents))
            parents[only] = qty_in
        if parents and sum(parents.values()) != qty_in:
            raise DomainIssue("PARENT_INPUT_MISMATCH")
        lot = {
            "lot_id": lot_id,
            "parents": parents,
            "qty_in": qty_in,
            "qty_out": qty_out,
            "loss_qty": loss_qty,
            "content_key": _content_key(lot_id, parents, qty_in, qty_out, loss_qty),
        }
        record, changed = self.store.upsert_lot(lot)
        return {"lot": record, "changed": changed, "recompute": self.recompute()}

    def register_recall(self, payload):
        recall_id = str(payload.get("recall_id") or "").strip()
        problem_lot = str(payload.get("problem_lot") or "").strip()
        event_id = str(payload.get("event_id") or "").strip() or f"recall:{recall_id}"
        reason = str(payload.get("reason") or "").strip()
        if not recall_id or not problem_lot:
            raise DomainIssue("BAD_REQUEST", detail="recall_id 与 problem_lot 不能为空")
        existing = self.store.get_recall(recall_id)
        if existing:
            # 同一起召回重试登记：幂等返回，不新增事件、不新增停发记录。
            return {"recall": existing, "created": False, "recompute": self.snapshot()}
        event, created = self.store.add_event(event_id, recall_id, problem_lot, "OPEN", reason)
        return {"recall": event, "created": created, "recompute": self.recompute()}

    def close_recall(self, recall_id):
        existing = self.store.get_recall(recall_id)
        if existing is None:
            raise DomainIssue("RECALL_NOT_FOUND", 404)
        if existing["kind"] == "CLOSED":
            return {"recall": existing, "changed": False, "recompute": self.snapshot()}
        event = self.store.close_recall(recall_id)
        return {"recall": event, "changed": True, "recompute": self.recompute()}

    # -- 重算 -----------------------------------------------------------

    def recompute(self):
        """按当前全部批次记录与未解除召回，重算停发名单与待核事项并落库。"""
        genealogy = Genealogy({item["lot_id"]: item for item in self.store.list_lots()})
        complete = genealogy.complete_lots()
        desired_holds = []
        desired_pending = []
        for event in self.store.active_recalls():
            recall_id = event["recall_id"]
            problem_lot = event["problem_lot"]
            if problem_lot not in genealogy.lots:
                # 问题来料批次本身未登记：无法定位闭包，先建待核事项。
                desired_pending.append({
                    "recall_id": recall_id,
                    "reason_code": PROBLEM_LOT_UNREGISTERED,
                    "detail": f"问题批次 {problem_lot} 的记录尚未登记，待来料补齐后按实际来料重算",
                })
                continue
            component = genealogy.component_of(problem_lot)
            for lot_id in sorted(component):
                if lot_id in complete:
                    desired_holds.append({
                        "lot_id": lot_id, "recall_id": recall_id, "state": HOLD,
                        "reason_code": CONTAMINATED,
                        "detail": f"与问题批次 {problem_lot} 同属一个血缘连通分量，已混入或不能排除混入",
                    })
                else:
                    missing = sorted({
                        parent
                        for node in component
                        for parent in genealogy.missing.get(node, ())
                    })
                    desired_holds.append({
                        "lot_id": lot_id, "recall_id": recall_id, "state": PENDING,
                        "reason_code": ANCESTOR_MISSING,
                        "detail": "缺父批次 " + ",".join(missing) + "，无法排除混入，停发待核",
                    })
        self.store.sync_holds(desired_holds)
        self.store.sync_pending(desired_pending)
        return self.snapshot()

    # -- 工作台视图 -----------------------------------------------------

    def snapshot(self):
        lots = self.store.list_lots()
        holds = self.store.list_holds(active_only=True)
        pending_items = self.store.list_pending()
        blocked = {}
        for row in holds:
            blocked.setdefault(row["lot_id"], []).append(row)
        lot_views = []
        for lot in lots:
            rows = blocked.get(lot["lot_id"], [])
            if not rows:
                shipping = "SHIPPABLE"
            elif all(r["state"] == PENDING for r in rows):
                shipping = "PENDING"
            else:
                shipping = "HOLD"
            lot_views.append({
                "lot_id": lot["lot_id"],
                "parents": lot["parents"],
                "qty_in": lot["qty_in"],
                "qty_out": lot["qty_out"],
                "version": lot["version"],
                "shipping": shipping,
                "holds": [
                    {"recall_id": r["recall_id"], "state": r["state"],
                     "reason_code": r["reason_code"], "detail": r["detail"]}
                    for r in rows
                ],
            })
        lot_views.sort(key=lambda item: item["lot_id"])
        return {
            "recalls": self.store.list_recalls(),
            "holds": holds,
            "pending_items": pending_items,
            "lots": lot_views,
            "hold_count": sum(1 for r in holds if r["state"] == HOLD),
            "pending_count": sum(1 for r in holds if r["state"] == PENDING) + len(pending_items),
        }
