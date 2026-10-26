"""Ingestão pelo Monitor (dao) em Standalone: presença, atraso, espera, dedupe, Telegram."""
from app import db
from app.eventos import tipos
from tests.conftest import access_log, dao, hoje_as


def _eventos(tipo=None):
    if tipo:
        return db.query("SELECT * FROM eventos WHERE tipo = %s ORDER BY id", (tipo,))
    return db.query("SELECT * FROM eventos ORDER BY id")


def _dentro(pid):
    r = db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (pid,), one=True)
    return r and r["dentro"]


def test_token_errado_e_device_desconhecido(cliente, sincronizado):
    assert dao(cliente, access_log(1, 1, 1), token="errado").status_code == 403
    r = cliente.post("/api/notifications/tok-teste/dao", json={"object_changes": [], "device_id": 1})
    assert r.status_code == 404


def test_entrada_marca_presenca_e_avisa_telegram(cliente, sincronizado):
    uid = sincronizado["user_ids"]["maria"]
    r = dao(cliente, access_log(10, uid, 1, quando=hoje_as(7, 12)))
    assert r.status_code == 200 and r.get_json()["eventos"] == 1
    ev = _eventos()[-1]
    assert ev["tipo"] == tipos.ENTRADA and ev["sentido"] == "entrada" and ev["log_id_dispositivo"] == 10
    assert _dentro(sincronizado["maria"]["pessoa_id"])
    n = db.query("SELECT * FROM fila_notificacao", one=True)
    assert n["responsavel_id"] == sincronizado["ana"]["responsavel_id"] and n["texto"] == "Maria (Pré 1) entrou às 07:12." and n["status"] == "pendente"
    assert db.query("SELECT ultimo_log_id FROM dispositivos", one=True)["ultimo_log_id"] == 10


def test_entrada_atrasada(cliente, sincronizado):
    dao(cliente, access_log(1, sincronizado["user_ids"]["maria"], 1, quando=hoje_as(8, 40)))
    assert _eventos()[-1]["tipo"] == tipos.ENTRADA_ATRASADA
    assert "tolerância" in db.query("SELECT texto FROM fila_notificacao", one=True)["texto"]


def test_dedupe_monitor_e_coleta(cliente, sincronizado):
    log = access_log(5, sincronizado["user_ids"]["maria"], 1)
    dao(cliente, log)
    assert dao(cliente, log).get_json()["eventos"] == 0
    assert len(_eventos()) == 1
    assert db.query("SELECT count(*) AS n FROM fila_notificacao", one=True)["n"] == 1


def test_desistencia_desfaz_presenca(cliente, sincronizado):
    uid = sincronizado["user_ids"]["maria"]
    dao(cliente, access_log(1, uid, 1))
    assert _dentro(sincronizado["maria"]["pessoa_id"])
    dao(cliente, access_log(2, uid, 1, event=13))
    assert not _dentro(sincronizado["maria"]["pessoa_id"])
    assert _eventos()[-1]["tipo"] == tipos.DESISTENCIA


def test_responsavel_no_leitor_de_fora_vira_espera(cliente, sincronizado):
    dao(cliente, access_log(1, sincronizado["user_ids"]["ana"], 1))
    ev = _eventos()[-1]
    assert ev["tipo"] == tipos.RESPONSAVEL_CHEGOU and ev["detalhes"]["alunos"] == ["Maria Silva"]
    esp = db.query("SELECT * FROM responsavel_em_espera", one=True)
    assert esp["responsavel_id"] == sincronizado["ana"]["responsavel_id"] and esp["consumido_em"] is None
    assert (esp["expira_em"] - esp["identificado_em"]).total_seconds() == 15 * 60
    estado = cliente.get("/api/painel/estado").get_json()
    assert estado["esperas"][0]["responsavel"] == "Ana Silva" and "Maria" in estado["esperas"][0]["alunos"]


def test_saida_com_responsavel_em_espera(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    dao(cliente, access_log(1, ids["maria"], 1, quando=hoje_as(7)))
    dao(cliente, access_log(2, ids["ana"], 1, quando=hoje_as(17)))
    dao(cliente, access_log(3, ids["maria"], 2, quando=hoje_as(17, 5)))
    ev = _eventos()[-1]
    assert ev["tipo"] == tipos.SAIDA_ALUNO and ev["correlacao_id"] == sincronizado["ana"]["pessoa_id"]
    assert not _dentro(sincronizado["maria"]["pessoa_id"])
    assert db.query("SELECT consumido_em, aluno_id FROM responsavel_em_espera", one=True)["aluno_id"] == sincronizado["maria"]["aluno_id"]
    textos = [n["texto"] for n in db.query("SELECT texto FROM fila_notificacao ORDER BY id")]
    assert textos[-1] == "Maria saiu às 17:05 com Ana (mãe)."


def test_saida_liberada_pela_catraca_sem_responsavel_gera_alerta(cliente, sincronizado):
    # Standalone na saída: a catraca abriu, o servidor registra e sinaliza
    dao(cliente, access_log(1, sincronizado["user_ids"]["maria"], 2, quando=hoje_as(17)))
    ev = _eventos()[-1]
    assert ev["tipo"] == tipos.SAIDA and "alerta" in ev["detalhes"]


def test_saida_negada_pela_catraca_avisa_pais(cliente, sincronizado):
    dao(cliente, access_log(1, sincronizado["user_ids"]["maria"], 2, event=6, quando=hoje_as(17)))
    assert _eventos()[-1]["tipo"] == tipos.SAIDA_NEGADA
    assert "não liberou" in db.query("SELECT texto FROM fila_notificacao", one=True)["texto"]


def test_aluno_acompanhado_pelo_responsavel_na_entrada(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    dao(cliente, access_log(1, ids["ana"], 1, quando=hoje_as(7, 0)))
    dao(cliente, access_log(2, ids["maria"], 1, quando=hoje_as(7, 0)))
    ev = _eventos(tipos.ENTRADA)[0]
    assert ev["detalhes"]["acompanhado_por"] == "Ana Silva" and ev["correlacao_id"] == sincronizado["ana"]["pessoa_id"]


def test_nao_identificado_e_botoeira(cliente, sincronizado):
    dao(cliente, access_log(1, 0, 1, event=3))
    dao(cliente, access_log(2, 0, 1, event=11))
    assert [e["tipo"] for e in _eventos()] == [tipos.NAO_IDENTIFICADO, tipos.BOTOEIRA]


def test_outras_notificacoes_do_monitor(cliente, sincronizado):
    r = cliente.post("/api/notifications/tok-teste/device_is_alive", json={"access_logs": 7, "device_id": 935107, "time": 1})
    assert r.status_code == 200
    assert db.query("SELECT config FROM dispositivos", one=True)["config"]["logs_no_dispositivo"] == 7
    cliente.post("/api/notifications/tok-teste/operation_mode", json={"operation_mode": {"mode_name": "contingency"}, "device_id": 935107})
    assert _eventos()[-1]["tipo"] == tipos.DISPOSITIVO_CONTINGENCIA
    dao(cliente, access_log(9, sincronizado["user_ids"]["maria"], 1))
    cliente.post("/api/notifications/tok-teste/catra_event", json={"event": {"type": 7, "name": "EVENT_TURN_RIGHT"}, "access_event_id": 9, "device_id": 935107})
    assert db.query("SELECT detalhes FROM eventos WHERE log_id_dispositivo = 9", one=True)["detalhes"]["giro"] == "EVENT_TURN_RIGHT"


def test_liberacao_manual_registra_quem_e_avisa(cliente, sincronizado, simulador):
    r = cliente.post("/painel/liberar", data={"pessoa_id": sincronizado["maria"]["pessoa_id"], "sentido": "saida", "justificativa": "consulta médica, mãe ligou", "dispositivo_id": sincronizado["dispositivo"]["id"], "girar": "1"})
    assert r.status_code == 302
    ev = _eventos()[-1]
    assert ev["tipo"] == tipos.SAIDA_ANTECIPADA and ev["origem"] == "manual" and ev["autorizado_por"] == sincronizado["admin"]["id"]
    assert simulador["estado"].liberacoes[-1]["parameters"] == "allow=anticlockwise"
    assert "saída antecipada" in db.query("SELECT texto FROM fila_notificacao ORDER BY id DESC", one=True)["texto"]
    assert cliente.post("/painel/liberar", data={"pessoa_id": sincronizado["maria"]["pessoa_id"], "sentido": "saida", "justificativa": "x"}).status_code == 302
    assert len(_eventos(tipos.SAIDA_ANTECIPADA)) == 1  # justificativa curta demais não registra
