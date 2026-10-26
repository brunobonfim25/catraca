"""Catraca sem servidor (contingência/standalone) nega a saída de quem precisa de responsável."""
from app import db
from app.eventos import tipos
from worker import sync


def test_regra_so_entrada_para_aluno_com_responsavel(sincronizado, simulador):
    est = simulador["estado"]
    ids = sincronizado["user_ids"]
    regras = {r["id"]: r["name"] for r in est.tabelas["access_rules"]}
    por_user = {r["user_id"]: regras[r["access_rule_id"]] for r in est.tabelas["user_access_rules"]}
    assert por_user[ids["maria"]] == sync.NOME_REGRA_SO_ENTRADA   # turma exige responsável
    assert por_user[ids["pedro"]] == sync.NOME_REGRA_LIBERADO     # turma autônoma
    assert por_user[ids["ana"]] == sync.NOME_REGRA_LIBERADO and por_user[ids["joao"]] == sync.NOME_REGRA_LIBERADO
    so_entrada = next(r["id"] for r in est.tabelas["access_rules"] if r["name"] == sync.NOME_REGRA_SO_ENTRADA)
    assert [p["portal_id"] for p in est.tabelas["portal_access_rules"] if p["access_rule_id"] == so_entrada] == [1]

    # sem servidor (standalone): Maria entra, mas não sai; Pedro entra e sai
    assert simulador["app"].identificar(ids["maria"], "0MS1")["event"] == 7
    assert simulador["app"].identificar(ids["maria"], "0MS2")["event"] == 6
    assert simulador["app"].identificar(ids["pedro"], "0MS2")["event"] == 7


def test_autorizacao_individual_muda_a_regra(sincronizado, simulador):
    db.execute("UPDATE alunos SET saida_autonoma = true WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],))
    from app.dispositivos import service as dispositivos

    dispositivos.enfileirar(sincronizado["dispositivo"]["id"], "set_rules", sincronizado["maria"]["pessoa_id"])
    sync.processar_fila()
    est = simulador["estado"]
    regras = {r["id"]: r["name"] for r in est.tabelas["access_rules"]}
    assert regras[next(r["access_rule_id"] for r in est.tabelas["user_access_rules"] if r["user_id"] == sincronizado["user_ids"]["maria"])] == sync.NOME_REGRA_LIBERADO


def test_status_contingencia_fica_ate_voltar(cliente, sincronizado):
    cliente.post("/api/notifications/tok-teste/operation_mode", json={"operation_mode": {"mode_name": "contingency"}, "device_id": 935107})
    assert db.query("SELECT status FROM dispositivos", one=True)["status"] == "contingencia"
    cliente.post("/api/notifications/tok-teste/device_is_alive", json={"access_logs": 0, "device_id": 935107, "time": 1})
    assert db.query("SELECT status FROM dispositivos", one=True)["status"] == "contingencia"
    assert cliente.get("/api/painel/estado").get_json()["dispositivos"][0]["status"] == "contingencia"
    cliente.post("/api/notifications/tok-teste/operation_mode", json={"operation_mode": {"mode_name": "online"}, "device_id": 935107})
    assert db.query("SELECT status FROM dispositivos", one=True)["status"] == "online"
    assert [e["tipo"] for e in db.query("SELECT tipo FROM eventos ORDER BY id")] == [tipos.DISPOSITIVO_CONTINGENCIA, tipos.DISPOSITIVO_ONLINE]
