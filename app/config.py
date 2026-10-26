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
    TZ_PADRAO = os.environ.get("TZ_PADRAO", "America/Sao_Paulo")
    CONTROLID_SIMULADOR = os.environ.get("CONTROLID_SIMULADOR", "0") == "1"
    # Tempo que um responsável fica "em espera" depois de se identificar no leitor de fora
    ESPERA_RESPONSAVEL_MIN = int(os.environ.get("ESPERA_RESPONSAVEL_MIN", "15"))
    # Fotos: limite de upload (a catraca aceita até 2 MB; 1 MB já é mais do que o suficiente)
    MAX_CONTENT_LENGTH = 3 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 10


class TestConfig(Config):
    TESTING = True
    DATABASE_URL = os.environ.get("DATABASE_URL_TEST", "postgresql://postgres@127.0.0.1:5433/nexosix_test")
    SECRET_KEY = "teste"
    MONITOR_TOKEN = "tok-teste"
    TELEGRAM_WEBHOOK_SECRET = "seg-teste"
    WTF_CSRF_ENABLED = False
