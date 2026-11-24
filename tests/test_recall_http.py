def post_lot(client, **payload):
    return client.post("/api/recall/lots", json=payload)


def post_recall(client, **payload):
    return client.post("/api/recall/recalls", json=payload)


def test_board_starts_empty(client):
    data = client.get("/api/recall/board").get_json()
    assert data["holds"] == []
    assert data["lots"] == []
    assert data["hold_count"] == 0


def test_lot_registration_returns_created_record(client):
    # 回归：写入与读取必须在同一事务视图内，接口不能回传 null 批次。
    response = post_lot(client, lot_id="A", qty_in=100, qty_out=100)
    assert response.status_code == 200
    result = response.get_json()
    assert result["lot"]["lot_id"] == "A"
    assert result["lot"]["version"] == 1
    recall_resp = post_recall(client, recall_id="R1", problem_lot="A")
    assert recall_resp.get_json()["recall"]["recall_id"] == "R1"


def test_full_recall_flow_over_http(client):
    post_lot(client, lot_id="L1", qty_in=100, qty_out=100)
    post_lot(client, lot_id="L2", parent_lot="L1", qty_in=60, qty_out=60)
    post_lot(client, lot_id="L3", qty_in=40, qty_out=40)
    response = post_recall(client, recall_id="R1", problem_lot="L2")
    assert response.status_code == 200
    snap = response.get_json()["recompute"]
    assert {row["lot_id"] for row in snap["holds"]} == {"L1", "L2"}
    l3 = next(item for item in snap["lots"] if item["lot_id"] == "L3")
    assert l3["shipping"] == "SHIPPABLE"

    # 晚到合批把 L3 并入问题分量。
    post_lot(client, lot_id="L6", parent_lot="L2:20;L3:40", qty_in=60, qty_out=60)
    snap = client.get("/api/recall/board").get_json()
    assert next(item for item in snap["lots"] if item["lot_id"] == "L3")["shipping"] == "HOLD"


def test_retry_recall_creates_no_extra_hold(client):
    post_lot(client, lot_id="L1", qty_in=100, qty_out=100)
    post_recall(client, recall_id="R1", problem_lot="L1", event_id="e1")
    again = post_recall(client, recall_id="R1", problem_lot="L1", event_id="e1")
    assert again.get_json()["created"] is False
    board = client.get("/api/recall/board").get_json()
    assert len(board["holds"]) == 1
    assert len(board["recalls"]) == 1


def test_pending_missing_parent_and_recompute_after_fill(client):
    post_lot(client, lot_id="A1", qty_in=100, qty_out=100)
    post_recall(client, recall_id="R1", problem_lot="A1")
    post_lot(client, lot_id="C1", parent_lot="A1:40;P9:60", qty_in=100, qty_out=100)
    board = client.get("/api/recall/board").get_json()
    row = next(r for r in board["holds"] if r["lot_id"] == "C1")
    assert row["state"] == "PENDING"
    assert row["reason_code"] == "ANCESTOR_MISSING"

    post_lot(client, lot_id="P9", qty_in=60, qty_out=60)
    board = client.get("/api/recall/board").get_json()
    row = next(r for r in board["holds"] if r["lot_id"] == "C1")
    assert row["state"] == "HOLD"
    assert row["reason_code"] == "CONTAMINATED"


def test_close_one_of_two_recalls_keeps_hold(client):
    post_lot(client, lot_id="L1", qty_in=100, qty_out=100)
    post_lot(client, lot_id="L2", parent_lot="L1", qty_in=50, qty_out=50)
    post_lot(client, lot_id="L3", parent_lot="L1", qty_in=50, qty_out=50)
    post_recall(client, recall_id="R1", problem_lot="L2")
    post_recall(client, recall_id="R2", problem_lot="L3")

    closed = client.post("/api/recall/recalls/R1/close")
    assert closed.status_code == 200
    board = client.get("/api/recall/board").get_json()
    l1 = next(item for item in board["lots"] if item["lot_id"] == "L1")
    assert l1["shipping"] == "HOLD"
    assert {h["recall_id"] for h in l1["holds"]} == {"R2"}

    client.post("/api/recall/recalls/R2/close")
    board = client.get("/api/recall/board").get_json()
    assert next(item for item in board["lots"] if item["lot_id"] == "L1")["shipping"] == "SHIPPABLE"


def test_unregistered_problem_lot_listed_as_pending_item(client):
    response = post_recall(client, recall_id="R9", problem_lot="GHOST")
    snap = response.get_json()["recompute"]
    assert snap["pending_items"][0]["reason_code"] == "PROBLEM_LOT_UNREGISTERED"

    post_lot(client, lot_id="GHOST", qty_in=10, qty_out=10)
    board = client.get("/api/recall/board").get_json()
    assert board["pending_items"] == []
    assert {r["lot_id"] for r in board["holds"]} == {"GHOST"}


def test_validation_errors(client):
    bad = post_lot(client, lot_id="L1", qty_in=10, qty_out=8, loss_qty=1)
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "MASS_MISMATCH"

    missing = client.post("/api/recall/recalls/NOPE/close")
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "RECALL_NOT_FOUND"

    no_body = client.post("/api/recall/recalls", json={"recall_id": "X"})
    assert no_body.status_code == 400


def test_recall_page_renders(client):
    response = client.get("/recall")
    assert response.status_code == 200
    assert "停发".encode() in response.data
