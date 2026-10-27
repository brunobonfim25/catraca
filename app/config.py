"""Configuração lida do ambiente (.env local, variáveis no Railway)."""
import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insegura")
    DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nexosix")
    DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
    DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
    ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "")
    MONITOR_TOKEN = os.environ.get("MONITOR_TOKEN", "")
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
    # convite por e-mail (app/notificacoes/email.py): Brevo ou Resend + remetente verificado
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE", "")
    TZ_PADRAO = os.environ.get("TZ_PADRAO", "America/Sao_Paulo")
    CONTROLID_SIMULADOR = os.environ.get("CONTROLID_SIMULADOR", "0") == "1"
    # Tempo que um responsável fica "em espera" depois de se identificar no leitor de fora
    ESPERA_RESPONSAVEL_MIN = int(os.environ.get("ESPERA_RESPONSAVEL_MIN", "15"))
    # Fotos: limite de upload (a catraca aceita até 2 MB; 1 MB já é mais do que o suficiente)
    MAX_CONTENT_LENGTH = 3 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Na nuvem (HTTPS) o cookie de sessão não deve viajar em HTTP
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 10

    # Onde este processo roda:
    #   nuvem → o sistema: painel, cadastro, Telegram; manda comandos e cadastro pro agente
    #   local → agente na escola: só a catraca (Monitor, modo Pro, fotos, coleta) + cópia local
    #   unico → tudo junto numa máquina (desenvolvimento, ou escola sem nuvem)
    MODO = os.environ.get("MODO", "unico")
    # Agente: id da unidade (definido na nuvem), URL da nuvem e token gerado lá (token-espelho)
    UNIDADE_ID = int(os.environ.get("UNIDADE_ID", "0") or 0)
    NUVEM_URL = os.environ.get("NUVEM_URL", "").rstrip("/")
    ESPELHO_TOKEN = os.environ.get("ESPELHO_TOKEN", "")


class TestConfig(Config):
    TESTING = True
    DATABASE_URL = os.environ.get("DATABASE_URL_TEST", "postgresql://postgres@127.0.0.1:5433/nexosix_test")
    SECRET_KEY = "teste"
    MONITOR_TOKEN = "tok-teste"
    TELEGRAM_WEBHOOK_SECRET = "seg-teste"
    BREVO_API_KEY = RESEND_API_KEY = EMAIL_REMETENTE = ""
    MODO = "unico"
    NUVEM_URL = ""
    ESPELHO_TOKEN = ""
