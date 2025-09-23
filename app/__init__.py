# app/__init__.py
from flask import Flask
from dotenv import load_dotenv
import os


def create_app():
    load_dotenv()
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )

    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")
    app.config["TIMEZONE"] = os.getenv("TIMEZONE", "Europe/Bucharest")

    from .routes import bp as main_bp
    app.register_blueprint(main_bp)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
