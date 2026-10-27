"""Fixtures: banco de teste limpo, app Flask, simulador de catraca em HTTP real.

Requer PostgreSQL acessível em DATABASE_URL_TEST (padrão postgresql://postgres@127.0.0.1:5433/nexosix_test).
"""
import logging
import threading
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from app import create_app, db
from app.config import TestConfig
from integrations.controlid.simulator import EstadoCatraca, criar_simulador

logging.getLogger("werkzeug").setLevel(logging.ERROR)

FUSO = ZoneInfo("America/Sao_Paulo")
DEVICE_ID = 935107

# JPEG mínimo "válido" pro simulador (só checa magic bytes); SEMROSTO força o erro 2
JPEG_OK = b"\xff\xd8\xff\xe0" + b"\x00" * 300 + b"\xff\xd9"
JPEG_OUTRO = b"\xff\xd8\xff\xe0" + b"\x01" * 300 + b"\xff\xd9"
JPEG_SEM_ROSTO = b"\xff\xd8\xff\xe0" + b"SEMROSTO" + b"\x00" * 300 + b"\xff\xd9"


class ServidorThread:
    def __init__(self, app):
        self.srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.porta = self.srv.server_port
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.t.start()
        return self

    def stop(self):
        self.srv.shutdown()


@pytest.fixture(scope="session")
def app():
    a = create_app(TestConfig)
    with a.app_context():
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.migrar(log=lambda m: None)
    yield a
    db.fechar()


@pytest.fixture(scope="session")
def servidor_app(app):
    """O app servido em HTTP real, pra catraca simulada chamar Monitor e modo Pro."""
    s = ServidorThread(app).start()
    app.config["BASE_URL"] = f"http://127.0.0.1:{s.porta}"
    yield s
    s.stop()


@pytest.fixture(scope="session")
def simulador():
    estado = EstadoCatraca(device_id=DEVICE_ID)
    sim_app = criar_simulador(estado)
    s = ServidorThread(sim_app).start()
    yield {"app": sim_app, "estado": estado, "porta": s.porta}
    s.stop()


@pytest.fixture(autouse=True)
def limpar(app, simulador):
    with app.app_context():
        tabelas = [r["tablename"] for r in db.query("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
        db.execute("TRUNCATE " + ", ".join(tabelas) + " RESTART IDENTITY CASCADE")
        db.execute("INSERT INTO espelho_estado DEFAULT VALUES")
    simulador["estado"].reset()
    db.gucs, db.contexto.unidade_id = {}, None
    yield


@pytest.fixture
def outbox_ligado(ctx):
    """Liga os triggers do espelho como no agente/nuvem (TestConfig roda em MODO=unico)."""
    db.gucs = {"nexosix.outbox": "on"}
    db.contexto.unidade_id = None
    yield
    db.gucs, db.contexto.unidade_id = {}, None


@pytest.fixture(autouse=True)
def horario_simulado(monkeypatch):
    """Os testes simulam a catraca às 7h, 17h etc. (hoje_as), não na hora real em que rodam:
    sem isso a tolerância do relógio e o "aviso atrasado" mudariam o resultado conforme o
    horário da execução. Testes desses dois casos ajustam os valores de volta."""
    from datetime import timedelta as _td

    from app.eventos import service as _ev

    monkeypatch.setattr(_ev, "TOLERANCIA_RELOGIO", _td(days=3650))
    monkeypatch.setattr(_ev, "ATRASO_AVISO_MIN", 60 * 24 * 3650)


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield app


def hoje_as(h, m=0):
    """datetime de hoje às h:m no fuso da escola, em UTC."""
    agora = datetime.now(FUSO)
    return agora.replace(hour=h, minute=m, second=0, microsecond=0).astimezone(timezone.utc)


def unix(dt):
    return int(dt.timestamp())


@pytest.fixture
def seed(ctx, simulador):
    """Empresa/unidade/turma/admin/catraca + Maria (aluna), Ana (mãe), João (funcionário)."""
    emp = db.execute("INSERT INTO empresas (nome) VALUES ('GEPPP') RETURNING id", returning=True)["id"]
    un = db.execute("INSERT INTO unidades (empresa_id, nome, config) VALUES (%s,'Recreio','{\"espera_responsavel_min\": 15}') RETURNING *", (emp,), returning=True)
    turma = db.execute(
        "INSERT INTO turmas (unidade_id, nome, turno, exige_responsavel_saida, tolerancia_entrada_min, entrada_inicio, entrada_fim, saida_inicio, saida_fim) "
        "VALUES (%s,'Pré 1','manha',true,15,'06:30','08:00','16:30','18:30') RETURNING *", (un["id"],), returning=True,
    )
    turma_livre = db.execute(
        "INSERT INTO turmas (unidade_id, nome, turno, exige_responsavel_saida, tolerancia_entrada_min, entrada_inicio, entrada_fim, saida_inicio, saida_fim) "
        "VALUES (%s,'5º ano','integral',false,15,'00:00','23:59','00:00','23:59') RETURNING *", (un["id"],), returning=True,
    )
    admin = db.execute(
        "INSERT INTO usuarios_admin (empresa_id, unidade_id, nome, email, senha_hash, perfil) VALUES (%s,%s,'Admin','admin@teste.com',%s,'admin_unidade') RETURNING *",
        (emp, un["id"], generate_password_hash("senha123")), returning=True,
    )
    recepcao = db.execute(
        "INSERT INTO usuarios_admin (empresa_id, unidade_id, nome, email, senha_hash, perfil) VALUES (%s,%s,'Recepção','recepcao@teste.com',%s,'recepcao') RETURNING *",
        (emp, un["id"], generate_password_hash("senha123")), returning=True,
    )
    disp = db.execute(
        "INSERT INTO dispositivos (unidade_id, nome, fabricante, ip, porta, senha_api, device_id_fabricante, status) VALUES (%s,'Catraca','controlid','127.0.0.1',%s,'admin',%s,'online') RETURNING *",
        (un["id"], simulador["porta"], DEVICE_ID), returning=True,
    )
    from app.dispositivos import service as dispositivos
    from app.pessoas import service as pessoas

    dispositivos.criar_leitores_padrao(disp["id"])
    maria_pid, maria_aid = pessoas.criar_aluno(un["id"], "Maria Silva", turma["id"], "2026001")
    pedro_pid, pedro_aid = pessoas.criar_aluno(un["id"], "Pedro Souza", turma_livre["id"], "2026002")
    ana_pid, ana_rid = pessoas.criar_responsavel(un["id"], "Ana Silva", "21999990000")
    pessoas.vincular(maria_aid, ana_rid, "mãe", "sempre")
    db.execute("UPDATE responsaveis SET telegram_chat_id = 111 WHERE id = %s", (ana_rid,))
    joao_pid, _ = pessoas.criar_funcionario(un["id"], "João Porteiro", "porteiro", {})
    # termo em papel da Maria e do Pedro: sem ele a situação do aluno é "falta autorização"
    pessoas.registrar_consentimento(maria_pid, "Ana Silva", date.today())
    pessoas.registrar_consentimento(pedro_pid, "Responsável do Pedro", date.today())
    pessoas.salvar_foto(maria_pid, JPEG_OK)
    pessoas.salvar_foto(ana_pid, JPEG_OUTRO)
    pessoas.salvar_foto(pedro_pid, JPEG_OK + b"\x03")  # João fica sem foto de propósito
    return {
        "empresa": emp, "unidade": db.query("SELECT * FROM unidades WHERE id = %s", (un["id"],), one=True), "turma": turma, "turma_livre": turma_livre,
        "admin": admin, "recepcao": recepcao, "dispositivo": disp,
        "maria": {"pessoa_id": maria_pid, "aluno_id": maria_aid}, "pedro": {"pessoa_id": pedro_pid, "aluno_id": pedro_aid},
        "ana": {"pessoa_id": ana_pid, "responsavel_id": ana_rid}, "joao": {"pessoa_id": joao_pid},
    }


@pytest.fixture
def sincronizado(seed, simulador):
    """Roda a fila de sync: Maria, Pedro, Ana e João existem na catraca simulada com foto/regras."""
    from worker import sync

    ok, falhas = sync.processar_fila(limite=100)
    assert falhas == 0, db.query("SELECT tipo, erro FROM fila_sync WHERE status <> 'concluido'")
    uid = lambda pid: db.query("SELECT user_id_no_dispositivo FROM dispositivo_pessoa WHERE pessoa_id = %s", (pid,), one=True)["user_id_no_dispositivo"]  # noqa: E731
    seed["user_ids"] = {k: uid(v["pessoa_id"]) for k, v in seed.items() if isinstance(v, dict) and "pessoa_id" in v}
    return seed


@pytest.fixture
def cliente(app, seed):
    c = app.test_client()
    c.post("/login", data={"email": "admin@teste.com", "senha": "senha123"})
    return c


def dao(cliente_ou_app, log, token="tok-teste"):
    """Simula a notificação `dao` do Monitor com um access_log."""
    c = cliente_ou_app if hasattr(cliente_ou_app, "post") else cliente_ou_app.test_client()
    return c.post(f"/api/notifications/{token}/dao", json={"object_changes": [{"object": "access_logs", "type": "inserted", "values": log}], "device_id": DEVICE_ID})


def access_log(log_id, user_id, portal_id, event=7, quando=None):
    return {"id": log_id, "time": unix(quando or datetime.now(timezone.utc)), "event": event, "device_id": DEVICE_ID, "identifier_id": 1,
            "user_id": user_id, "portal_id": portal_id, "identification_rule_id": 0, "card_value": 0, "log_type_id": 0}
