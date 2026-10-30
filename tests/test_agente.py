"""Ponta a ponta nuvem ↔ agente: a nuvem roda em outro processo com outro banco; o agente é
o app em processo (banco de teste) + catraca simulada."""
import os
import subprocess
import sys
import time

import pytest
import requests
from werkzeug.security import generate_password_hash

from app import db
from app.config import TestConfig
from app.espelho import service
from app.eventos import tipos
from integrations.controlid.adapter import ControlIdAdapter
from tests.conftest import JPEG_OK
from worker import sync

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NUVEM_DB = TestConfig.DATABASE_URL.rsplit("/", 1)[0] + "/nexosix_test_nuvem"
TOKEN = "tok-nuvem"


def _esperar(cond, seg=10):
    fim = time.time() + seg
    while time.time() < fim:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def nuvem(app):
    """Banco + processo da nuvem (MODO=nuvem) com empresa 1 / unidade 1 / admin."""
    import psycopg2

    conn = psycopg2.connect(TestConfig.DATABASE_URL.rsplit("/", 1)[0] + "/postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'nexosix_test_nuvem'")
    if not cur.fetchone():
        cur.execute("CREATE DATABASE nexosix_test_nuvem")
    conn.close()
    local_url = TestConfig.DATABASE_URL
    db.init(NUVEM_DB)
    db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.migrar(log=lambda m: None)
    db.execute("INSERT INTO empresas (id, nome) VALUES (1, 'GEPPP')")
    db.execute("INSERT INTO unidades (id, empresa_id, nome, config) VALUES (1, 1, 'Recreio', %s)", ('{"espelho_token": "%s"}' % TOKEN,))
    db.execute("INSERT INTO usuarios_admin (empresa_id, unidade_id, nome, email, senha_hash, perfil) VALUES (1, 1, 'Admin', 'admin@nuvem.com', %s, 'admin_unidade')", (generate_password_hash("senha123"),))
    db.init(local_url)

    import socket

    s = socket.socket(); s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]; s.close()
    env = {**os.environ, "MODO": "nuvem", "DATABASE_URL": NUVEM_DB, "PORT": str(porta), "SECRET_KEY": "nuvem-chave-de-teste-com-32-caracteres!", "CSRF_ATIVO": "0", "NEXOSIX_INSEGURO_SO_PARA_TESTES": "1", "MONITOR_TOKEN": "tok-nuvem-monitor",
           "BASE_URL": f"http://127.0.0.1:{porta}", "TELEGRAM_WEBHOOK_SECRET": "x"}
    proc = subprocess.Popen([sys.executable, "serve.py"], cwd=RAIZ, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{porta}"
    assert _esperar(lambda: requests.get(url + "/health", timeout=2).json()["modo"] == "nuvem", 30), "nuvem não subiu"
    sess = requests.Session()
    sess.post(url + "/login", data={"email": "admin@nuvem.com", "senha": "senha123"})
    yield {"url": url, "sess": sess, "porta": porta}
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def agente(ctx, nuvem, simulador, servidor_app):
    """Lado da escola: banco de teste com faixa de ids da unidade 1, outbox ligado."""
    db.gucs = {"nexosix.outbox": "on"}
    db.contexto.unidade_id = 1
    app = ctx
    app.test_cli_runner().invoke(args=["configurar-local", "--unidade-id", "1"])
    os.environ["BASE_URL"] = f"http://127.0.0.1:{servidor_app.porta}"
    os.environ["MONITOR_TOKEN"] = "tok-teste"
    yield {"nuvem": nuvem["url"], "token": TOKEN, "unidade_id": 1}
    db.gucs, db.contexto.unidade_id = {}, None


def test_fluxo_nuvem_agente_catraca(agente, nuvem, simulador, servidor_app):
    N, S = nuvem["url"], nuvem["sess"]
    # ---- 1. cadastro na nuvem (turma, aluno com foto, mãe, catraca)
    assert S.post(N + "/turmas/nova", data={"nome": "Pré 1", "turno": "manha", "exige_responsavel_saida": "1", "tolerancia_entrada_min": "15", "entrada_fim": "23:59", "saida_inicio": "00:00", "saida_fim": "23:59"}).ok
    S.post(N + "/dispositivos/novo", data={"nome": "Catraca", "ip": "127.0.0.1", "porta": simulador["porta"], "senha_api": "admin", "usuario_api": "admin", "ativo": "1"})
    r = S.post(N + "/pessoas/alunos/novo", data={"nome": "Maria Nuvem", "turma_id": "1", "saida_autonoma": "turma", "consentimento_por": "Ana Nuvem"}, allow_redirects=False)
    assert r.status_code == 302 and "/foto" in r.headers["Location"]
    maria = int(r.headers["Location"].split("/pessoas/")[1].split("/")[0])
    import base64

    assert S.post(f"{N}/pessoas/{maria}/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(JPEG_OK).decode()}).json()["ok"]
    r = S.post(N + "/pessoas/responsaveis/novo", data={"nome": "Ana Nuvem", "aluno_id": "1", "parentesco": "mãe", "regra": "sempre", "consentimento_por": "Ana Nuvem"}, allow_redirects=False)
    ana = int(r.headers["Location"].split("/pessoas/")[1].split("/")[0])
    S.post(f"{N}/pessoas/{ana}/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(JPEG_OK + b"\x09").decode()})
    S.post(N + "/dispositivos/1/testar")

    # ---- 2. agente baixa o cadastro (snapshot) e executa a fila na catraca
    n = service.carga_inicial(agente["nuvem"], agente["token"], 1)
    assert n > 10
    assert db.query("SELECT nome FROM pessoas WHERE id = %s", (maria,), one=True)["nome"] == "Maria Nuvem"
    assert db.query("SELECT count(*) AS n FROM fila_sync WHERE status = 'pendente'", one=True)["n"] >= 7  # 3 por pessoa + testar
    ok, falhas = sync.processar_fila(limite=100)
    assert falhas == 0, db.query("SELECT tipo, erro FROM fila_sync WHERE status = 'erro'")
    est = simulador["estado"]
    assert {u["name"] for u in est.tabelas["users"]} == {"Maria Nuvem", "Ana Nuvem"} and len(est.imagens) == 2
    assert db.query("SELECT device_id_fabricante FROM dispositivos WHERE id = 1", one=True)["device_id_fabricante"] == est.device_id

    # ---- 3. agente sobe status; a nuvem vê a catraca e a foto sincronizada
    assert service.empurrar(agente["nuvem"], agente["token"]) > 0
    pagina = S.get(f"{N}/dispositivos/1").text
    assert "sincronizado" in pagina and "6.50.0-sim" in pagina
    assert S.get(N + "/api/painel/estado").json()["fila"].get("pendente", 0) == 0

    # ---- 4. catraca avisa o agente (Monitor) → evento sobe pra nuvem
    ControlIdAdapter("127.0.0.1", simulador["porta"]).configurar_monitor("127.0.0.1", servidor_app.porta, "api/notifications/tok-teste")
    uid = db.query("SELECT user_id_no_dispositivo FROM dispositivo_pessoa WHERE pessoa_id = %s", (maria,), one=True)["user_id_no_dispositivo"]
    log = simulador["app"].identificar(uid, "0MS1")
    assert log["event"] == 7
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True))
    ev = db.query("SELECT * FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True)
    assert ev["tipo"] == tipos.ENTRADA and ev["id"] > 10_000_000  # id da faixa do agente
    service.empurrar(agente["nuvem"], agente["token"])
    estado = S.get(N + "/api/painel/estado").json()
    assert estado["total_dentro"] == 1 and estado["ultimos"][0]["pessoa"] == "Maria Nuvem"
    assert S.get(N + "/api/espelho/status").json()[0]["visto_em"]

    # ---- 5. nuvem edita cadastro e libera manualmente; agente puxa e executa na catraca
    S.post(N + "/turmas/1/editar", data={"nome": "Pré 1 B", "turno": "manha", "exige_responsavel_saida": "1", "tolerancia_entrada_min": "15", "ativo": "1"})
    S.post(f"{N}/pessoas/{maria}/desvincular/1")
    r = S.post(N + "/painel/liberar", data={"pessoa_id": maria, "sentido": "saida", "justificativa": "consulta médica", "dispositivo_id": "1", "girar": "1"})
    assert r.ok
    n = service.puxar(agente["nuvem"], agente["token"], 1)
    assert n > 0
    assert db.query("SELECT nome FROM turmas WHERE id = 1", one=True)["nome"] == "Pré 1 B"
    assert db.query("SELECT 1 FROM aluno_responsavel", one=True) is None
    assert db.query("SELECT tipo FROM eventos WHERE origem = 'manual'", one=True)["tipo"] == tipos.SAIDA_ANTECIPADA
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (maria,), one=True)["dentro"] is False
    item = db.query("SELECT * FROM fila_sync WHERE tipo = 'liberar'", one=True)
    assert item["status"] == "pendente" and item["id"] < 10_000_000  # criado na nuvem
    sync.processar_fila(limite=100)
    assert est.liberacoes[-1]["parameters"] == "allow=anticlockwise"
    service.empurrar(agente["nuvem"], agente["token"])
    assert S.get(N + "/api/painel/estado").json()["fila"].get("pendente", 0) == 0

    # ---- 6. puxar de novo não reaplica nada; ping registra o agente
    assert service.puxar(agente["nuvem"], agente["token"], 1) == 0
    assert service.ping(agente["nuvem"], agente["token"], {"versao": "1", "base_url": os.environ["BASE_URL"]})
    assert S.get(N + "/api/espelho/status").json()[0]["agente"]["base_url"] == os.environ["BASE_URL"]
