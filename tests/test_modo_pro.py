"""Modo Pro: a catraca pergunta ao servidor em /new_user_identified.fcgi."""
import secrets

from app import db
from app.eventos import tipos
from tests.conftest import DEVICE_ID, access_log, dao, hoje_as, unix


def identificar(cliente, user_id, component_id, quando, portal_id=None, uuid=None):
    # [DOC] cada identificação tem um uuid próprio; o mesmo uuid = a catraca repetindo o pedido
    return cliente.post("/api/catraca/tok-teste/new_user_identified.fcgi", data={
        "device_id": DEVICE_ID, "identifier_id": 1, "time": unix(quando), "portal_id": portal_id or (1 if component_id == "0MS1" else 2),
        "uuid": uuid or secrets.token_hex(8), "user_id": user_id, "user_name": "x", "event": 7, "component_id": component_id,
    }).get_json()["result"]


def test_session_is_valid(cliente):
    assert cliente.post("/api/catraca/tok-teste/session_is_valid.fcgi").get_json() == {"session_is_valid": True}
    assert cliente.post("/api/catraca/errado/session_is_valid.fcgi").status_code == 403


def test_entrada_de_aluno_libera_com_giro_de_entrada(cliente, sincronizado):
    r = identificar(cliente, sincronizado["user_ids"]["maria"], "0MS1", hoje_as(7, 10))
    assert r["event"] == 7 and r["actions"] == [{"action": "catra", "parameters": "allow=clockwise"}]
    ev = db.query("SELECT * FROM eventos ORDER BY id DESC LIMIT 1", one=True)
    assert ev["tipo"] == tipos.ENTRADA and ev["detalhes"]["modo"] == "pro" and ev["log_id_dispositivo"] is None
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],), one=True)["dentro"]


def test_log_do_monitor_que_chega_depois_so_anexa_o_id(cliente, sincronizado):
    uid = sincronizado["user_ids"]["maria"]
    identificar(cliente, uid, "0MS1", hoje_as(7, 10))
    dao(cliente, access_log(77, uid, 1, quando=hoje_as(7, 10)))
    evs = db.query("SELECT * FROM eventos")
    assert len(evs) == 1 and evs[0]["log_id_dispositivo"] == 77
    assert db.query("SELECT count(*) AS n FROM fila_notificacao", one=True)["n"] == 1  # avisou uma vez só


def test_saida_sem_responsavel_nega(cliente, sincronizado):
    r = identificar(cliente, sincronizado["user_ids"]["maria"], "0MS2", hoje_as(17))
    assert r["event"] == 6 and "actions" not in r and r["message"] == "Aguardando responsável"
    ev = db.query("SELECT * FROM eventos ORDER BY id DESC LIMIT 1", one=True)
    assert ev["tipo"] == tipos.SAIDA_NEGADA and "nenhum responsável" in ev["detalhes"]["motivo"]


def test_fluxo_completo_responsavel_depois_aluno(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    r = identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    assert r["event"] == 7 and "actions" not in r and r["message"] == "Aguarde Maria"
    assert db.query("SELECT count(*) AS n FROM responsavel_em_espera WHERE consumido_em IS NULL", one=True)["n"] == 1
    r = identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 3))
    assert r["event"] == 7 and r["actions"] == [{"action": "catra", "parameters": "allow=anticlockwise"}]
    ev = db.query("SELECT * FROM eventos ORDER BY id DESC LIMIT 1", one=True)
    assert ev["tipo"] == tipos.SAIDA_ALUNO and ev["correlacao_id"] == sincronizado["ana"]["pessoa_id"]
    assert db.query("SELECT consumido_em FROM responsavel_em_espera", one=True)["consumido_em"] is not None
    # segunda tentativa: espera já consumida → nega
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 4))["event"] == 6


def test_espera_expirada_nao_vale(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    db.execute("UPDATE responsavel_em_espera SET expira_em = identificado_em + interval '1 minute'")  # venceu às 17:01
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 20))["event"] == 6


def test_saida_autonoma_na_janela(cliente, sincronizado):
    r = identificar(cliente, sincronizado["user_ids"]["pedro"], "0MS2", hoje_as(17))
    assert r["event"] == 7
    assert db.query("SELECT tipo FROM eventos ORDER BY id DESC LIMIT 1", one=True)["tipo"] == tipos.SAIDA_AUTONOMA


def test_bloqueado_e_desconhecido_negam(cliente, sincronizado):
    db.execute("UPDATE pessoas SET status = 'bloqueado' WHERE id = %s", (sincronizado["maria"]["pessoa_id"],))
    assert identificar(cliente, sincronizado["user_ids"]["maria"], "0MS1", hoje_as(7))["event"] == 6
    r = identificar(cliente, 4242, "0MS1", hoje_as(7))
    assert r["event"] == 6 and r["message"] == "Não cadastrado"


def test_responsavel_resposta_configuravel(cliente, sincronizado):
    db.execute("""UPDATE unidades SET config = config || '{"responsavel_resposta": "liberar"}'""")
    r = identificar(cliente, sincronizado["user_ids"]["ana"], "0MS1", hoje_as(17))
    assert r["event"] == 7 and r["actions"][0]["parameters"] == "allow=clockwise"


def test_mapeamento_de_leitores_do_painel_vence_o_padrao(cliente, sincronizado):
    # escola instalou o primário do lado de dentro: 0MS1 = saída
    db.execute("UPDATE dispositivo_leitores SET sentido = CASE component_id WHEN '0MS1' THEN 'saida' ELSE 'entrada' END")
    r = identificar(cliente, sincronizado["user_ids"]["maria"], "0MS1", hoje_as(17), portal_id=1)
    assert r["event"] == 6  # tratou como saída sem responsável
