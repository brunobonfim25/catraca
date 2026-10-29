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
        if app.config.get("MODO") != "local":
            from .espelho.service import garantir_monitor_token

            for u in db.query("SELECT id FROM unidades WHERE NOT config ? 'monitor_token'"):
                garantir_monitor_token(u["id"])
        # Webhook do Telegram a cada deploy: o bot passou a usar botões (autorização dos pais),
        # que chegam como callback_query — sem re-registrar, o Telegram nem manda o clique.
        base = app.config.get("BASE_URL", "")
        if app.config.get("TELEGRAM_BOT_TOKEN") and base.startswith("https://") and app.config.get("MODO") == "nuvem":
            from .notificacoes.telegram import registrar_webhook

            try:
                click.echo(f"webhook do Telegram: {registrar_webhook()}")
            except Exception as e:  # noqa: BLE001 — Telegram fora não pode impedir o site de subir
                click.echo(f"webhook do Telegram falhou: {e}")

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
        # Revisão (E10): o instalador roda isto a cada atualização; zerar o cursor sempre
        # obrigava a baixar tudo de novo e, até lá, o agente operava com o cadastro velho.
        # Só zera se a carga inicial ainda não foi feita (ou se a escola mudou).
        est = db.query("SELECT * FROM espelho_estado WHERE id = 1", one=True)
        uni_local = db.query("SELECT id FROM unidades WHERE id = %s", (unidade_id,), one=True)
        if est["carga_em"] is None or uni_local is None:
            db.execute("UPDATE espelho_estado SET cursor_nuvem = 0, cursor_xid = '0', carga_em = NULL WHERE id = 1", outbox=False)
        # Identidade deste PC: a nuvem recusa um segundo agente ativo na mesma escola
        db.execute("UPDATE espelho_estado SET agente_id = COALESCE(agente_id, gen_random_uuid()::text) WHERE id = 1", outbox=False)
        click.echo(f"agente da unidade {unidade_id} pronto; ids locais a partir de {base + 1}. Inicie o worker pra baixar o cadastro.")

    @app.cli.command("espelho-enviar")
    def espelho_enviar():
        """Agente: sobe pra nuvem o que ainda não subiu (o instalador roda antes de parar os serviços)."""
        import os

        from .espelho import service

        nuvem, token = os.environ.get("NUVEM_URL", "").rstrip("/"), os.environ.get("ESPELHO_TOKEN", "")
        if not (nuvem and token):
            click.echo("sem NUVEM_URL/ESPELHO_TOKEN: nada a enviar")
            return
        try:
            n = service.empurrar(nuvem, token, max_lotes=200)
            click.echo(f"{n} linha(s) enviadas; pendentes agora: {service.pendentes()['n']}")
        except service.EspelhoErro as e:
            click.echo(f"não consegui enviar ({e}); fica pendente e sobe quando o agente voltar")

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

    @app.cli.command("codigo-instalacao")
    @click.option("--unidade-id", type=int, required=True)
    def codigo_instalacao(unidade_id):
        """Nuvem: código de uso único que o instalador do servidor da escola pede (o mesmo do painel)."""
        from .espelho import instalacao

        if not db.query("SELECT 1 FROM unidades WHERE id = %s", (unidade_id,), one=True):
            raise click.ClickException(f"unidade {unidade_id} não existe")
        codigo, expira = instalacao.gerar(unidade_id)
        click.echo(codigo)

    @app.cli.command("backup-agora")
    @click.option("--pasta", default=None, help="salvar as partes nesta pasta em vez de mandar pelo Telegram")
    def backup_agora(pasta):
        """Cópia criptografada do banco: manda pro Telegram (BACKUP_TELEGRAM_CHAT) ou salva em --pasta."""
        import os

        from . import backup

        if pasta:
            senha = os.environ.get("BACKUP_SENHA") or click.prompt("Senha do backup (12+ caracteres)", hide_input=True, confirmation_prompt=True)
            os.makedirs(pasta, exist_ok=True)
            resumo, partes = backup.gerar_partes(pasta, senha)
            click.echo(f"{len(partes)} parte(s) em {pasta} — versão {resumo['versao']}")
        else:
            n, total = backup.enviar_agora()
            click.echo(f"enviado: {n} parte(s), {total / 1e6:.1f} MB")

    @app.cli.command("restaurar-backup")
    @click.argument("partes", nargs=-1, required=True)
    @click.option("--forcar", is_flag=True, help="restaurar mesmo com cadastro no banco (apaga o que houver)")
    def restaurar_backup(partes, forcar):
        """Restaura as partes (em ordem: .parte01, .parte02…) no banco do DATABASE_URL."""
        from . import backup

        senha = click.prompt("Senha do backup", hide_input=True)
        try:
            backup.restaurar(sorted(partes), senha, log_fn=click.echo, forcar=forcar)
        except backup.BackupErro as e:
            raise click.ClickException(str(e))

    @app.cli.command("telegram-webhook")
    def telegram_webhook():
        """Registra BASE_URL/api/telegram/webhook no Telegram."""
        from .notificacoes.telegram import registrar_webhook

        click.echo(registrar_webhook())
