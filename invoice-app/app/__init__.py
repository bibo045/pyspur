import os

from flask import Flask, g

from . import db as dbmod


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,  # logo uploads and form posts
        SEND_FILE_MAX_AGE_DEFAULT=0,
    )
    if test_config:
        app.config.update(test_config)

    with app.app_context():
        dbmod.init_db().close()

    @app.before_request
    def open_db():
        g.db = dbmod.connect()

    @app.teardown_request
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def no_store(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp

    from .web import bp
    app.register_blueprint(bp)
    return app
