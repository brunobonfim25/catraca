"""create_app: Flask + pool psycopg2 + blueprints."""
import os

from flask import Flask, g, redirect, session, url_for

from . import db
from .config import Config
from .util import formatar_hora, formatar_data_hora


def create_app(config_object=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config_object or Config)

    db.init(app.config["DATABASE_URL"], app.config.get("DB_POOL_MIN", 1), app.config.get("DB_POOL_MAX", 10))
    # Servidor local liga o outbox do espelho; a nuvem nunca (ela só recebe)
    db.gucs = {"nexosix.outbox": "on"} if app.config.get("MODO", "local") == "local" else {}

    from .auth.routes import bp as auth_bp
    from .pessoas.routes import bp as pessoas_bp
    from .dispositivos.routes import bp as dispositivos_bp
    from .painel.routes import bp as painel_bp
    from .api.routes import bp as api_bp
    from .notificacoes.routes import bp as notificacoes_bp
    from .espelho.routes import bp as espelho_bp, instalar_somente_leitura

    app.register_blueprint(espelho_bp)
    if app.config.get("MODO") == "nuvem":
        instalar_somente_leitura(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(pessoas_bp)
    app.register_blueprint(dispositivos_bp)
    app.register_blueprint(painel_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(notificacoes_bp)

    app.jinja_env.filters["hora"] = formatar_hora
    app.jinja_env.filters["data_hora"] = formatar_data_hora

    @app.route("/")
    def index():
        if session.get("usuario_id"):
            return redirect(url_for("painel.dashboard"))
        return redirect(url_for("auth.login"))

    @app.route("/health")
    def health():
        db.query("SELECT 1")
        return {"ok": True, "modo": app.config.get("MODO", "local")}

    @app.context_processor
    def _modo():
        return {"modo": app.config.get("MODO", "local")}

    from .cli import registrar_comandos

    registrar_comandos(app)
    return app
