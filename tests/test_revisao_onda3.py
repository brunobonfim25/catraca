"""Revisão de 27/10/2026, onda 3: catraca que reseta, perde notificação ou repete pedido."""
from datetime import timedelta

from psycopg2.extras import Json

from app import db
from app.dispositivos import service as dispositivos
from app.eventos import tipos
from tests.conftest import access_log, dao, hoje_as
from tests.test_modo_pro import identificar
from worker import collect, sync
from worker import main as wmain


def _disp(seed):
    return db.query("SELECT * FROM dispositivos WHERE id = %s", (seed["dispositivo"]["id"],), one=True)


def test_C4_reset_nao_troca_as_pessoas_e_reconciliar_refaz(sincronizado, simulador):
    est = simulador["estado"]
    est.reset()
    # depois do reset alguém cadastrou um usuário direto na catraca: ele ganha o número 1,
    # que no nosso banco ainda é de outra pessoa
    with est.lock:
        est.tabelas["users"].append({"id": 1, "name": "Teste instalador", "registration": "999"})
        est.proximo_id["users"] = 2
    maria = sincronizado["maria"]["pessoa_id"]
    db.execute("UPDATE dispositivo_pessoa SET user_id_no_dispositivo = 1 WHERE pessoa_id = %s", (maria,))
    dispositivos.enfileirar(sincronizado["dispositivo"]["id"], "sync_user", maria)
    sync.processar_fila(limite=50)
    assert next(u for u in est.tabelas["users"] if u["id"] == 1)["name"] == "Teste instalador"  # não sobrescreveu
    # reconciliar: remove o estranho, refaz os números e reenvia cadastro, foto e regra
    dispositivos.enfileirar(sincronizado["dispositivo"]["id"], "reconciliar")
    for _ in range(3):
        sync.processar_fila(limite=100)
    regs = {u["registration"]: u["id"] for u in est.tabelas["users"]}
    assert "999" not in regs
    for dp in db.query("SELECT * FROM dispositivo_pessoa WHERE dispositivo_id = %s", (sincronizado["dispositivo"]["id"],)):
        assert regs[str(dp["pessoa_id"])] == dp["user_id_no_dispositivo"]
    assert len(est.imagens) == 3  # Maria, Ana e Pedro de novo com foto
    resumo = db.query("SELECT payload FROM fila_sync WHERE tipo = 'reconciliar' ORDER BY id DESC LIMIT 1", one=True)["payload"]["resumo"]
    assert resumo["removidos"] >= 1 and resumo["reenviados"] >= 3


def test_C5_numeracao_recomecou_abre_epoca_nova(sincronizado, simulador):
    sim, est = simulador["app"], simulador["estado"]
    uid = sincronizado["user_ids"]["pedro"]
    for _ in range(3):
        sim.identificar(uid, "0MS1")
    collect.coletar_todos()
    d = _disp(sincronizado)
    assert d["coleta_log_id"] == 3 and d["log_epoca"] == 0
    # reset da catraca: registros recomeçam do 1
    with est.lock:
        est.tabelas["access_logs"] = []
        est.proximo_id["access_logs"] = 1
    sim.identificar(uid, "0MS1")
    collect.coletar_todos()
    d = _disp(sincronizado)
    assert d["log_epoca"] == 1 and d["coleta_log_id"] == 1
    assert db.query("SELECT 1 FROM eventos WHERE log_epoca = 1 AND log_id_dispositivo = 1 AND pessoa_id = %s", (sincronizado["pedro"]["pessoa_id"],), one=True)
    assert db.query("SELECT 1 FROM eventos WHERE tipo = %s", (tipos.DISPOSITIVO_RESET,), one=True)
    assert db.query("SELECT 1 FROM fila_sync WHERE tipo = 'reconciliar' AND status = 'pendente'", one=True)


def test_C5_mesmo_numero_com_outra_hora_pelo_monitor(cliente, sincronizado):
    uid = sincronizado["user_ids"]["pedro"]
    dao(cliente, access_log(1, uid, 1, quando=hoje_as(7)))
    dao(cliente, access_log(1, uid, 1, quando=hoje_as(7)))  # repetido de verdade: descarta
    assert db.query("SELECT count(*) AS n FROM eventos WHERE log_id_dispositivo = 1", one=True)["n"] == 1
    dao(cliente, access_log(1, uid, 1, quando=hoje_as(9)))  # mesmo número, 2 h depois: catraca resetou
    assert db.query("SELECT count(*) AS n FROM eventos WHERE log_id_dispositivo = 1", one=True)["n"] == 2
    assert _disp(sincronizado)["log_epoca"] == 1


def test_C8_coleta_recupera_o_que_o_monitor_perdeu(cliente, sincronizado, simulador):
    sim = simulador["app"]
    uid = sincronizado["user_ids"]["pedro"]
    logs = [sim.identificar(uid, "0MS1") for _ in range(3)]  # Monitor desligado: só ficam na catraca
    # o Monitor entrega um registro mais novo (o 4) e o cursor do Monitor passa do 1–3
    dao(cliente, access_log(4, uid, 1))
    assert _disp(sincronizado)["ultimo_log_id"] == 4
    collect.coletar_todos()
    for l in logs:
        assert db.query("SELECT 1 FROM eventos WHERE log_id_dispositivo = %s", (l["id"],), one=True), l["id"]


def test_C6_mudar_mapeamento_recria_a_regra_de_contingencia(cliente, sincronizado, simulador):
    est = simulador["estado"]
    d = _disp(sincronizado)
    velha = int(d["config"]["regra_so_entrada_id"])
    assert d["config"]["regra_so_entrada_portais"] == [1]
    # descobriu-se na instalação: portal 2 é a entrada
    cliente.post(f"/dispositivos/{d['id']}/leitores", data={"n": 2, "component_0": "0MS1", "portal_0": "2", "sentido_0": "entrada",
                                                             "component_1": "0MS2", "portal_1": "1", "sentido_1": "saida"})
    assert db.query("SELECT count(*) AS n FROM fila_sync WHERE tipo = 'set_rules' AND status = 'pendente'", one=True)["n"] == 4
    sync.processar_fila(limite=100)
    d = _disp(sincronizado)
    nova = int(d["config"]["regra_so_entrada_id"])
    assert nova != velha and d["config"]["regra_so_entrada_portais"] == [2]
    assert not any(r["id"] == velha for r in est.tabelas["access_rules"])
    assert {p["portal_id"] for p in est.tabelas["portal_access_rules"] if p["access_rule_id"] == nova} == {2}
    maria_uid = sincronizado["user_ids"]["maria"]
    assert {r["access_rule_id"] for r in est.tabelas["user_access_rules"] if r["user_id"] == maria_uid} == {nova}


def test_C6_mudar_a_turma_reenvia_as_regras(cliente, sincronizado):
    t = sincronizado["turma"]
    cliente.post(f"/turmas/{t['id']}/editar", data={"nome": t["nome"], "turno": "manha", "tolerancia_entrada_min": "15", "ativo": "1"})  # tira "só com responsável"
    assert db.query("SELECT 1 FROM fila_sync WHERE tipo = 'set_rules' AND status = 'pendente' AND pessoa_id = %s",
                    (sincronizado["maria"]["pessoa_id"],), one=True)


def test_C9_pedido_repetido_tem_a_mesma_resposta(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    r1 = identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 2), uuid="pedido-1")
    r2 = identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 2), uuid="pedido-1")
    assert r1 == r2 and r1["event"] == 7
    assert db.query("SELECT count(*) AS n FROM eventos WHERE tipo = %s", (tipos.SAIDA_ALUNO,), one=True)["n"] == 1
    textos = [r["texto"] for r in db.query("SELECT texto FROM fila_notificacao")]
    assert not any("tentou sair" in t for t in textos)


def test_C9_desistencia_reabre_a_espera_e_corrige_o_aviso(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    maria = sincronizado["maria"]["pessoa_id"]
    identificar(cliente, ids["maria"], "0MS1", hoje_as(7))
    identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 2))["event"] == 7
    assert db.query("SELECT consumido_em FROM responsavel_em_espera", one=True)["consumido_em"]
    # ela hesitou e não girou: a catraca registra a desistência (13)
    dao(cliente, access_log(50, ids["maria"], 2, event=13, quando=hoje_as(17, 2) + timedelta(seconds=8)))
    assert db.query("SELECT consumido_em FROM responsavel_em_espera", one=True)["consumido_em"] is None
    assert db.query("SELECT dentro FROM presenca WHERE pessoa_id = %s", (maria,), one=True)["dentro"] is True
    assert any(r["texto"].startswith("Correção: Maria não saiu") for r in db.query("SELECT texto FROM fila_notificacao"))
    # segunda tentativa: sai com a mesma espera
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 3))["event"] == 7


def test_C10_item_parado_em_processando_volta(sincronizado):
    did = sincronizado["dispositivo"]["id"]
    db.execute("INSERT INTO fila_sync (dispositivo_id, pessoa_id, tipo, status, proximo_em) VALUES (%s,%s,'set_rules','processando', now() - interval '1 minute')",
               (did, sincronizado["maria"]["pessoa_id"]))
    assert sync.processar_fila(limite=10) == (1, 0)
    # e o status da pessoa mostra o que ainda está na fila
    db.execute("INSERT INTO fila_sync (dispositivo_id, pessoa_id, tipo, status, erro) VALUES (%s,%s,'remove_user','erro','HTTP 500')",
               (did, sincronizado["maria"]["pessoa_id"]))
    st = dispositivos.status_sync_pessoa(sincronizado["maria"]["pessoa_id"])[0]
    assert "remove_user: erro — HTTP 500" in st["fila_aberta"]


def test_C10_catraca_fora_nao_tenta_os_outros_itens(seed):
    db.execute("UPDATE dispositivos SET porta = 1 WHERE id = %s", (seed["dispositivo"]["id"],))
    pendentes = db.query("SELECT count(*) AS n FROM fila_sync WHERE status = 'pendente'", one=True)["n"]
    assert pendentes > 3
    sync.processar_fila(limite=100)
    linhas = db.query("SELECT tentativas, status FROM fila_sync")
    assert all(l["tentativas"] == 0 and l["status"] == "pendente" for l in linhas)


def test_C12_acerto_diario_da_hora(seed):
    db.contexto.unidade_id = seed["unidade"]["id"]
    wmain.acertar_relogios()
    db.contexto.unidade_id = None
    item = db.query("SELECT payload FROM fila_sync WHERE tipo = 'set_clock'", one=True)
    assert item["payload"]["fuso"] == "America/Sao_Paulo"


def test_C14_funcionario_fora_da_escala_gera_alerta(cliente, sincronizado):
    joao = sincronizado["joao"]["pessoa_id"]
    db.execute("UPDATE funcionarios SET escala = %s WHERE pessoa_id = %s", (Json({"inicio": "00:00", "fim": "00:01"}), joao))
    uid = db.query("SELECT user_id_no_dispositivo FROM dispositivo_pessoa WHERE pessoa_id = %s", (joao,), one=True)["user_id_no_dispositivo"]
    dao(cliente, access_log(70, uid, 1, quando=hoje_as(10)))
    ev = db.query("SELECT detalhes FROM eventos WHERE log_id_dispositivo = 70", one=True)
    assert "fora da escala" in ev["detalhes"]["alerta"]
