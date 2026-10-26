"""Comandos `flask --app wsgi <comando>`."""
import click
from werkzeug.security import generate_password_hash

from . import db


def registrar_comandos(app):
    @app.cli.command("migrar")
    def migrar():
        """Aplica as migrations pendentes."""
        n = db.migrar()
        click.echo(f"{n} migration(s) aplicada(s)")

    @app.cli.command("criar-admin")
    @click.option("--email", required=True)
    @click.option("--senha", required=True)
    @click.option("--nome", default="Superadmin")
    def criar_admin(email, senha, nome):
        """Cria um superadmin (ou troca a senha se já existir)."""
        db.execute(
            "INSERT INTO usuarios_admin (nome, email, senha_hash, perfil) VALUES (%s,%s,%s,'superadmin') "
            "ON CONFLICT (email) DO UPDATE SET senha_hash = EXCLUDED.senha_hash, ativo = true",
            (nome, email.lower(), generate_password_hash(senha)),
        )
        click.echo(f"superadmin pronto: {email}")

    @app.cli.command("bootstrap")
    def bootstrap():
        """Primeiro deploy: migra e cria o superadmin de ADMIN_EMAIL/ADMIN_SENHA se não houver usuários."""
        n = db.migrar()
        click.echo(f"{n} migration(s) aplicada(s)")
        total = db.query("SELECT count(*) AS n FROM usuarios_admin", one=True)["n"]
        email, senha = app.config.get("ADMIN_EMAIL"), app.config.get("ADMIN_SENHA")
        if total == 0 and email and senha:
            db.execute(
                "INSERT INTO usuarios_admin (nome, email, senha_hash, perfil) VALUES ('Superadmin',%s,%s,'superadmin')",
                (email.lower(), generate_password_hash(senha)),
            )
            click.echo(f"superadmin criado: {email}")

    @app.cli.command("configurar-local")
    @click.option("--unidade-id", type=int, default=None, help="id da unidade na nuvem (padrão: UNIDADE_ID do .env)")
    def configurar_local(unidade_id):
        """Agente da escola: migra e reserva a faixa de ids (unidade_id × 10 milhões) pra que
        o que o agente cria (eventos, presença…) nunca colida com ids da nuvem. O cadastro em
        si vem da nuvem na primeira rodada do worker (snapshot)."""
        unidade_id = unidade_id or app.config.get("UNIDADE_ID")
        if not unidade_id:
            raise click.ClickException("informe --unidade-id ou UNIDADE_ID no .env")
        n = db.migrar()
        click.echo(f"{n} migration(s) aplicada(s)")
        base = unidade_id * 10_000_000
        for s in db.query("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'"):
            db.execute("SELECT setval(%s, GREATEST(COALESCE(%s, 0), %s), true)", (s["sequencename"], s["last_value"], base))
        db.execute("UPDATE espelho_estado SET cursor_nuvem = 0 WHERE id = 1")
        click.echo(f"agente da unidade {unidade_id} pronto; ids locais a partir de {base + 1}. Inicie o worker pra baixar o cadastro.")

    @app.cli.command("token-espelho")
    @click.option("--unidade-id", type=int, required=True)
    def token_espelho(unidade_id):
        """Nuvem: gera o token do agente da unidade (ESPELHO_TOKEN no .env da escola)."""
        import json
        import secrets

        token = secrets.token_urlsafe(24)
        n = db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (json.dumps({"espelho_token": token}), unidade_id))
        if not n:
            raise click.ClickException(f"unidade {unidade_id} não existe na nuvem")
        click.echo(f"ESPELHO_TOKEN={token}")

    @app.cli.command("telegram-webhook")
    def telegram_webhook():
        """Registra BASE_URL/api/telegram/webhook no Telegram."""
        from .notificacoes.telegram import registrar_webhook

        click.echo(registrar_webhook())
