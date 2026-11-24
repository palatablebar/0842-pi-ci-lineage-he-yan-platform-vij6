"""召回停发登记的 SQLite 持久化。

四张表：
- ``recall_lots``    批次/合批记录（每份记录各查各的，按内容幂等 upsert）。
- ``recall_events``  召回登记与解除事件（同一召回重试登记不新增记录）。
- ``recall_holds``   停发名单，(批次, 召回) 唯一；重算时同步状态，解除保留审计行。
- ``recall_pending`` 待核事项：问题来料记录尚未登记等无法落到具体批次的阻断原因。
"""

import json
import sqlite3
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class RecallStore:
    def __init__(self, path):
        self.path = path
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript(
                """
            CREATE TABLE IF NOT EXISTS recall_lots(
              lot_id      TEXT PRIMARY KEY,
              parents     TEXT NOT NULL,
              qty_in      INTEGER NOT NULL,
              qty_out     INTEGER NOT NULL,
              loss_qty    INTEGER NOT NULL,
              content_key TEXT NOT NULL,
              version     INTEGER NOT NULL DEFAULT 1,
              updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recall_events(
              event_id    TEXT PRIMARY KEY,
              recall_id   TEXT NOT NULL UNIQUE,
              problem_lot TEXT NOT NULL,
              kind        TEXT NOT NULL,
              reason      TEXT NOT NULL DEFAULT '',
              created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recall_holds(
              lot_id      TEXT NOT NULL,
              recall_id   TEXT NOT NULL,
              state       TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              detail      TEXT NOT NULL DEFAULT '',
              updated_at  TEXT NOT NULL,
              PRIMARY KEY (lot_id, recall_id)
            );
            CREATE TABLE IF NOT EXISTS recall_pending(
              recall_id   TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              detail      TEXT NOT NULL DEFAULT '',
              updated_at  TEXT NOT NULL,
              PRIMARY KEY (recall_id, reason_code)
            );
            """
            )

    # -- 批次登记 -------------------------------------------------------

    def get_lot(self, lot_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recall_lots WHERE lot_id=?", (lot_id,)).fetchone()
        return self._lot(row) if row else None

    def list_lots(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recall_lots ORDER BY lot_id").fetchall()
        return [self._lot(row) for row in rows]

    def upsert_lot(self, lot):
        """按内容幂等写入。返回 (记录, 是否发生变化)。

        同一编号内容一致视为重复投递，不计新版本；内容变化（晚到的更正/合批记录）
        才累加版本并触发重算。读写在同一连接事务内完成，避免跨连接读不到未提交行。
        """
        ts = now_iso()
        with self.connect() as db:
            row = db.execute("SELECT * FROM recall_lots WHERE lot_id=?", (lot["lot_id"],)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO recall_lots(lot_id,parents,qty_in,qty_out,loss_qty,content_key,version,updated_at)"
                    " VALUES(?,?,?,?,?,?,1,?)",
                    (lot["lot_id"], json.dumps(lot["parents"], ensure_ascii=False), lot["qty_in"],
                     lot["qty_out"], lot["loss_qty"], lot["content_key"], ts),
                )
                row = db.execute("SELECT * FROM recall_lots WHERE lot_id=?", (lot["lot_id"],)).fetchone()
                return self._lot(row), True
            existing = self._lot(row)
            if existing["content_key"] == lot["content_key"]:
                return existing, False
            db.execute(
                "UPDATE recall_lots SET parents=?,qty_in=?,qty_out=?,loss_qty=?,content_key=?,"
                "version=version+1,updated_at=? WHERE lot_id=?",
                (json.dumps(lot["parents"], ensure_ascii=False), lot["qty_in"], lot["qty_out"],
                 lot["loss_qty"], lot["content_key"], ts, lot["lot_id"]),
            )
            row = db.execute("SELECT * FROM recall_lots WHERE lot_id=?", (lot["lot_id"],)).fetchone()
            return self._lot(row), True

    # -- 召回事件 -------------------------------------------------------

    def get_recall(self, recall_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recall_events WHERE recall_id=?", (recall_id,)).fetchone()
        return dict(row) if row else None

    def list_recalls(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recall_events ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def active_recalls(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recall_events WHERE kind='OPEN'").fetchall()
        return [dict(row) for row in rows]

    def add_event(self, event_id, recall_id, problem_lot, kind, reason=""):
        """登记召回事件。返回 (事件, 是否新写入)。

        ``recall_id`` 唯一：同一起召回的重试登记命中既有记录，
        不新增事件、不新增停发。
        """
        existing = self.get_recall(recall_id)
        if existing:
            return existing, False
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO recall_events(event_id,recall_id,problem_lot,kind,reason,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (event_id, recall_id, problem_lot, kind, reason, now_iso()),
                )
            except sqlite3.IntegrityError:
                from app.errors import DomainIssue
                raise DomainIssue("EVENT_CONFLICT", 409)
        return self.get_recall(recall_id), True

    def close_recall(self, recall_id):
        with self.connect() as db:
            db.execute("UPDATE recall_events SET kind='CLOSED' WHERE recall_id=?", (recall_id,))
        return self.get_recall(recall_id)

    # -- 停发名单 -------------------------------------------------------

    def list_holds(self, active_only=True):
        sql = "SELECT * FROM recall_holds"
        if active_only:
            sql += " WHERE state IN ('HOLD','PENDING')"
        sql += " ORDER BY lot_id, recall_id"
        with self.connect() as db:
            rows = db.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def sync_holds(self, desired):
        """把期望停发集合同步到表中（单事务）。

        desired: [{"lot_id","recall_id","state","reason_code","detail"}]
        - 已存在且未变：保持；状态/原因变化：原地更新；
        - 不再需要的 (批次,召回)：标记 RELEASED 保留审计轨迹；新的：插入。
        """
        ts = now_iso()
        wanted = {(item["lot_id"], item["recall_id"]): item for item in desired}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = {
                (row["lot_id"], row["recall_id"]): dict(row)
                for row in db.execute("SELECT * FROM recall_holds").fetchall()
            }
            for key, item in wanted.items():
                if key in current:
                    row = current[key]
                    if row["state"] != item["state"] or row["reason_code"] != item["reason_code"] \
                            or row["detail"] != item["detail"]:
                        db.execute(
                            "UPDATE recall_holds SET state=?,reason_code=?,detail=?,updated_at=? "
                            "WHERE lot_id=? AND recall_id=?",
                            (item["state"], item["reason_code"], item["detail"], ts, key[0], key[1]),
                        )
                else:
                    db.execute(
                        "INSERT INTO recall_holds(lot_id,recall_id,state,reason_code,detail,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (item["lot_id"], item["recall_id"], item["state"], item["reason_code"],
                         item["detail"], ts),
                    )
            for key in current.keys() - wanted.keys():
                db.execute(
                    "UPDATE recall_holds SET state='RELEASED',updated_at=? WHERE lot_id=? AND recall_id=?",
                    (ts, key[0], key[1]),
                )

    # -- 待核事项 -------------------------------------------------------

    def list_pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recall_pending ORDER BY recall_id").fetchall()
        return [dict(row) for row in rows]

    def sync_pending(self, desired):
        """同步不针对具体批次的待核事项；解除召回或问题批次补齐后自动移除。"""
        ts = now_iso()
        wanted = {(item["recall_id"], item["reason_code"]): item for item in desired}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = {
                (row["recall_id"], row["reason_code"]): dict(row)
                for row in db.execute("SELECT * FROM recall_pending").fetchall()
            }
            for key, item in wanted.items():
                if key not in current or current[key]["detail"] != item["detail"]:
                    db.execute(
                        "INSERT INTO recall_pending(recall_id,reason_code,detail,updated_at) VALUES(?,?,?,?)"
                        " ON CONFLICT(recall_id,reason_code) DO UPDATE SET detail=excluded.detail, updated_at=excluded.updated_at",
                        (key[0], key[1], item["detail"], ts),
                    )
            for key in current.keys() - wanted.keys():
                db.execute(
                    "DELETE FROM recall_pending WHERE recall_id=? AND reason_code=?", (key[0], key[1])
                )

    # -- 内部 -----------------------------------------------------------

    @staticmethod
    def _lot(row):
        item = dict(row)
        item["parents"] = json.loads(item["parents"])
        return item
