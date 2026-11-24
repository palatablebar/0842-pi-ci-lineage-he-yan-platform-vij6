import os

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import RequestEntityTooLarge

from app.catalog import SPEC
from app.config import MAX_FILE, ROOT
from app.errors import DomainIssue, error_response
from app.recall.routes import recall_api
from app.recall.service import RecallService
from app.recall.store import RecallStore
from app.review.store import Store
from app.routes import api


def create_app(database=None, recall_database=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 64 * 1024
    app.extensions["runs"] = Store(database or os.getenv("WORKBENCH_DB", str(ROOT / "instance" / "workbench.sqlite3")))
    app.extensions["recall"] = RecallService(RecallStore(
        recall_database or os.getenv("RECALL_DB", str(ROOT / "instance" / "recall.sqlite3"))))
    app.register_error_handler(DomainIssue, error_response)
    app.register_error_handler(RequestEntityTooLarge, lambda _: error_response(DomainIssue("FILE_TOO_LARGE", 413)))
    app.register_blueprint(api)
    app.register_blueprint(recall_api)

    @app.get("/")
    def home():
        return render_template("index.html", p=SPEC)

    @app.get("/recall")
    def recall_board():
        return render_template("recall.html")

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app


app = create_app()
