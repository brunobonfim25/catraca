"""create_app: Flask + pool psycopg2 + blueprints."""
from flask import Flask, g, redirect, session, url_for

from . import db
from .config import Config
from .util import formatar_data_hora, formatar_hora, formatar_telefone


def _unidade_da_requisicao():
    """nexosix.unidade na sessão SQL: a nuvem marca de qual escola é cada linha do outbox
    (o agente só puxa as linhas da unidade dele)."""
    from flask import has_request_context

    if has_request_context():
        if getattr(g, "unidade", None):
            return {"nexosix.unidade": g.unidade["id"]}
        # chamadas da catraca (sem login): a unidade vem do dispositivo — importa no failover,
        # quando a nuvem grava eventos que o agente vai puxar depois
        if getattr(g, "unidade_api_id", None):
            return {"nexosix.unidade": g.unidade_api_id}
    uid = getattr(db.contexto, "unidade_id", None)
    return {"nexosix.unidade": uid} if uid else {}


def create_app(config_object=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config_object or Config)
    modo = app.config.get("MODO", "unico")

    db.init(app.config["DATABASE_URL"], app.config.get("DB_POOL_MIN", 1), app.config.get("DB_POOL_MAX", 10))
    # Outbox do espelho só faz sentido quando há dois lados (nuvem ↔ agente)
    db.gucs = {"nexosix.outbox": "on"} if modo in ("nuvem", "local") else {}
    db.gucs_fn = [_unidade_da_requisicao]
    if modo == "local":
        db.contexto.unidade_id = app.config.get("UNIDADE_ID") or None

    from .auth.routes import bp as auth_bp
    from .pessoas.routes import bp as pessoas_bp
    from .dispositivos.routes import bp as dispositivos_bp
    from .painel.routes import bp as painel_bp
    from .api.routes import bp as api_bp
    from .notificacoes.routes import bp as notificacoes_bp
    from .espelho.routes import bp as espelho_bp, instalar_agente_local

    app.register_blueprint(espelho_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(pessoas_bp)
    app.register_blueprint(dispositivos_bp)
    app.register_blueprint(painel_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(notificacoes_bp)
    if modo == "local":
        instalar_agente_local(app)

    app.jinja_env.filters["hora"] = formatar_hora
    app.jinja_env.filters["data_hora"] = formatar_data_hora
    app.jinja_env.filters["telefone"] = formatar_telefone

    @app.route("/")
    def index():
        if modo == "local":
            return redirect(url_for("espelho.agente_status"))
        from .auth.rbac import carregar_usuario, home_de

        u = carregar_usuario()
        if u:
            return redirect(home_de(u))
        return redirect(url_for("auth.login"))

    @app.route("/health")
    def health():
        db.query("SELECT 1")
        return {"ok": True, "modo": modo}

    @app.context_processor
    def _modo():
        return {"modo": modo, "nuvem_url": app.config.get("NUVEM_URL", "")}

    from .cli import registrar_comandos

    registrar_comandos(app)
    return app
