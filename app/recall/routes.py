"""召回停发登记台 HTTP 接口。"""

from flask import Blueprint, jsonify, request

from app.errors import DomainIssue

recall_api = Blueprint("recall_api", __name__, url_prefix="/api/recall")


def service():
    from flask import current_app
    return current_app.extensions["recall"]


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    return data


@recall_api.get("/board")
def board():
    """工作台数据：当前停发名单、待核事项与批次出货状态。"""
    return jsonify(service().snapshot())


@recall_api.post("/lots")
def register_lot():
    """登记一条批次/合批记录（每份记录各查各的，晚到记录触发重算）。"""
    return jsonify(service().register_lot(body()))


@recall_api.post("/recalls")
def register_recall():
    """登记一起召回；同一起召回重试登记幂等，不新增停发记录。"""
    return jsonify(service().register_recall(body()))


@recall_api.post("/recalls/<recall_id>/close")
def close_recall(recall_id):
    """解除一起召回；仅释放该召回对应的停发，其他召回约束保留。"""
    return jsonify(service().close_recall(recall_id))
