"""Ponta a ponta por HTTP real: catraca simulada ↔ servidor Flask servido em thread.
Usa a turma '5º ano' (janelas 00:00–23:59) pra não depender da hora do teste."""
import time

from app import db
from app.eventos import tipos
from app.pessoas import service as pessoas
from integrations.controlid.adapter import ControlIdAdapter
from tests.conftest import JPEG_OUTRO
from worker import collect, sync


def _esperar(cond, seg=3):
    fim = time.time() + seg
    while time.time() < fim:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _adapter(simulador):
    return ControlIdAdapter("127.0.0.1", simulador["porta"], "admin", "admin")


def _configurar(servidor_app, simulador):
    a = _adapter(simulador)
    a.configurar_monitor("127.0.0.1", servidor_app.porta, "api/notifications/tok-teste")
    return a


def test_standalone_monitor_entrega_evento(servidor_app, sincronizado, simulador):
    _configurar(servidor_app, simulador)
    uid = sincronizado["user_ids"]["pedro"]
    log = simulador["app"].identificar(uid, "0MS1")
    assert log["event"] == 7
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True))
    ev = db.query("SELECT * FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True)
    assert ev["tipo"] == tipos.ENTRADA and ev["detalhes"].get("giro") == "EVENT_TURN_RIGHT"
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (sincronizado["pedro"]["pessoa_id"],), one=True)["dentro"]
    assert simulador["estado"].monitor_falhas == []


def test_standalone_desistencia(servidor_app, sincronizado, simulador):
    _configurar(servidor_app, simulador)
    uid = sincronizado["user_ids"]["pedro"]
    simulador["app"].identificar(uid, "0MS1", girar=False)
    assert _esperar(lambda: db.query("SELECT count(*) AS n FROM eventos", one=True)["n"] == 2)
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (sincronizado["pedro"]["pessoa_id"],), one=True)["dentro"] is False


def test_modo_pro_ponta_a_ponta(servidor_app, sincronizado, simulador):
    a = _configurar(servidor_app, simulador)
    a.configurar_modo_online(f"http://127.0.0.1:{servidor_app.porta}/api/catraca/tok-teste")
    # Pedro é turma livre (sai sozinho); Maria precisa de responsável. Dá um responsável pro Pedro pra testar a saída com ele.
    from app.pessoas import service as ps

    carlos_pid, carlos_rid = ps.criar_responsavel(sincronizado["unidade"]["id"], "Carlos Souza")
    ps.vincular(sincronizado["pedro"]["aluno_id"], carlos_rid, "pai")
    ps.salvar_foto(carlos_pid, JPEG_OUTRO + b"\x02")
    sync.processar_fila(limite=100)
    carlos_uid = db.query("SELECT user_id_no_dispositivo FROM dispositivo_pessoa WHERE pessoa_id = %s", (carlos_pid,), one=True)["user_id_no_dispositivo"]
    ids = sincronizado["user_ids"]

    # entrada do Pedro: servidor libera com giro de entrada
    log = simulador["app"].identificar(ids["pedro"], "0MS1")
    assert log["event"] == 7
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True))
    assert db.query("SELECT count(*) AS n FROM eventos WHERE pessoa_id = %s", (sincronizado["pedro"]["pessoa_id"],), one=True)["n"] == 1

    # Maria tenta sair sem responsável → catraca recebe 6
    log = simulador["app"].identificar(ids["maria"], "0MS2")
    assert log["event"] == 6
    assert db.query("SELECT tipo FROM eventos ORDER BY id DESC LIMIT 1", one=True)["tipo"] == tipos.SAIDA_NEGADA

    # Carlos chega (0MS1): espera registrada e catraca não gira
    log = simulador["app"].identificar(carlos_uid, "0MS1")
    assert log["event"] == 7
    assert db.query("SELECT count(*) AS n FROM responsavel_em_espera WHERE consumido_em IS NULL", one=True)["n"] == 1
    assert not any(l["parameters"] for l in simulador["estado"].liberacoes)

    # Pedro sai com o pai
    log = simulador["app"].identificar(ids["pedro"], "0MS2")
    assert log["event"] == 7
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True))
    ev = db.query("SELECT * FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True)
    assert ev["tipo"] == tipos.SAIDA_ALUNO and ev["correlacao_id"] == carlos_pid and ev["detalhes"].get("giro") == "EVENT_TURN_LEFT"
    assert simulador["estado"].modo_contingencia is False


def test_contingencia_quando_servidor_some_e_volta(servidor_app, sincronizado, simulador):
    a = _configurar(servidor_app, simulador)
    a.configurar_modo_online("http://127.0.0.1:1/api/catraca/tok-teste")  # servidor inexistente
    uid = sincronizado["user_ids"]["pedro"]
    log = simulador["app"].identificar(uid, "0MS1")
    assert simulador["estado"].modo_contingencia and log["event"] == 7  # decidiu local pela regra "liberado"
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE tipo = %s", (tipos.DISPOSITIVO_CONTINGENCIA,), one=True))
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True))
    a.configurar_modo_online(f"http://127.0.0.1:{servidor_app.porta}/api/catraca/tok-teste")
    simulador["app"].alive()
    assert simulador["estado"].modo_contingencia is False


def test_botoeira_em_modo_online_vira_um_evento(servidor_app, sincronizado, simulador):
    import requests

    a = _configurar(servidor_app, simulador)
    a.configurar_modo_online(f"http://127.0.0.1:{servidor_app.porta}/api/catraca/tok-teste")
    log = requests.post(f"http://127.0.0.1:{simulador['porta']}/sim/botoeira", json={"portal_id": 2}, timeout=5).json()
    assert _esperar(lambda: db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (log["id"],), one=True))
    assert db.query("SELECT count(*) AS n FROM eventos WHERE tipo = %s", (tipos.BOTOEIRA,), one=True)["n"] == 1


def test_coleta_recupera_logs_que_o_monitor_nao_entregou(sincronizado, simulador):
    # sem Monitor configurado: a catraca guarda; o worker busca por load_objects
    uid = sincronizado["user_ids"]["pedro"]
    simulador["app"].identificar(uid, "0MS1")
    simulador["app"].identificar(uid, "0MS2")
    assert db.query("SELECT count(*) AS n FROM eventos", one=True)["n"] == 0
    assert collect.coletar_todos() == 2
    tipos_ = [e["tipo"] for e in db.query("SELECT tipo FROM eventos ORDER BY id")]
    assert tipos_ == [tipos.ENTRADA, tipos.SAIDA_AUTONOMA]
    assert collect.coletar_todos() == 0
    assert db.query("SELECT ultimo_log_id, status FROM dispositivos", one=True)["ultimo_log_id"] == 2


def test_dispositivo_fora_do_ar_fica_offline(sincronizado):
    db.execute("UPDATE dispositivos SET porta = 1, ultimo_contato = now() - interval '10 minutes'")
    collect.coletar_todos()
    assert db.query("SELECT status FROM dispositivos", one=True)["status"] == "offline"
    assert db.query("SELECT 1 FROM eventos WHERE tipo = %s", (tipos.DISPOSITIVO_OFFLINE,), one=True)
