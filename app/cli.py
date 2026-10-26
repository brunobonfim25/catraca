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

    @app.cli.command("telegram-webhook")
    def telegram_webhook():
        """Registra BASE_URL/api/telegram/webhook no Telegram."""
        from .notificacoes.telegram import registrar_webhook

        click.echo(registrar_webhook())
