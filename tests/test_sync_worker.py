"""Fila de sincronização → catraca simulada."""
from app import db
from app.pessoas import service as pessoas
from tests.conftest import JPEG_SEM_ROSTO
from worker import sync


def test_cadastro_enfileira_e_worker_envia(sincronizado, simulador):
    est = simulador["estado"]
    assert {u["name"] for u in est.tabelas["users"]} == {"Maria Silva", "Pedro Souza", "Ana Silva", "João Porteiro"}
    assert len(est.imagens) == 3  # Maria, Ana e Pedro têm foto; João não
    dp = db.query("SELECT * FROM dispositivo_pessoa WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],), one=True)
    assert dp["foto_sync_status"] == "sincronizado" and dp["regras_sync_status"] == "sincronizado"
    assert dp["user_id_no_dispositivo"] == sincronizado["user_ids"]["maria"]
    regras = est.tabelas["user_access_rules"]
    assert len(regras) == 4 and len({r["access_rule_id"] for r in regras}) == 2  # "liberado" e "só entrada" (Maria)
    assert db.query("SELECT count(*) AS n FROM fila_sync WHERE status <> 'concluido'", one=True)["n"] == 0


def test_foto_rejeitada_vira_erro_visivel_sem_retry(seed, simulador):
    pessoas.salvar_foto(seed["joao"]["pessoa_id"], JPEG_SEM_ROSTO)
    ok, falhas = sync.processar_fila(limite=100)
    assert falhas == 1
    dp = db.query("SELECT * FROM dispositivo_pessoa WHERE pessoa_id = %s", (seed["joao"]["pessoa_id"],), one=True)
    assert dp["foto_sync_status"] == "erro" and "Face not detected" in dp["erro"]
    item = db.query("SELECT * FROM fila_sync WHERE pessoa_id = %s AND tipo = 'set_photo'", (seed["joao"]["pessoa_id"],), one=True)
    assert item["status"] == "erro"  # não fica tentando pra sempre
    assert sync.processar_fila(limite=100) == (0, 0)


def test_catraca_fora_do_ar_agenda_retry(seed, simulador):
    db.execute("UPDATE dispositivos SET porta = 1 WHERE id = %s", (seed["dispositivo"]["id"],))  # nada escuta na porta 1
    ok, falhas = sync.processar_fila(limite=100)
    assert ok == 0 and falhas > 0
    item = db.query("SELECT * FROM fila_sync ORDER BY id LIMIT 1", one=True)
    # catraca fora não gasta tentativa (C10): o item espera e volta, quantas vezes precisar
    assert item["status"] == "pendente" and item["tentativas"] == 0 and item["proximo_em"] > item["criado_em"] and "sem resposta" in item["erro"]


def test_bloquear_revoga_na_catraca_e_desligar_remove(sincronizado, simulador):
    est = simulador["estado"]
    uid = sincronizado["user_ids"]["maria"]
    pessoas.bloquear(sincronizado["maria"]["pessoa_id"])
    sync.processar_fila(limite=100)
    assert not any(r["user_id"] == uid for r in est.tabelas["user_access_rules"])
    pessoas.desligar(sincronizado["maria"]["pessoa_id"])
    sync.processar_fila(limite=100)
    assert not any(u["id"] == uid for u in est.tabelas["users"]) and uid not in est.imagens
    assert db.query("SELECT 1 FROM dispositivo_pessoa WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],), one=True) is None


def test_catraca_nova_recebe_todo_mundo(sincronizado, simulador):
    from app.dispositivos import service as dispositivos

    d2 = db.execute("INSERT INTO dispositivos (unidade_id, nome, ip, porta, senha_api, device_id_fabricante) VALUES (%s,'Catraca 2','127.0.0.1',%s,'admin',2) RETURNING id",
                    (sincronizado["unidade"]["id"], simulador["porta"]), returning=True)["id"]
    assert dispositivos.enfileirar_todos_da_unidade(d2, sincronizado["unidade"]["id"]) == 4
    assert db.query("SELECT count(*) AS n FROM fila_sync WHERE dispositivo_id = %s AND status = 'pendente'", (d2,), one=True)["n"] == 12


def test_lgpd_purga_foto_apos_30_dias(sincronizado):
    from worker import lgpd

    pid = sincronizado["maria"]["pessoa_id"]
    pessoas.desligar(pid)
    assert lgpd.purgar_desligados() == 0
    db.execute("UPDATE pessoas SET desligado_em = now() - interval '31 days' WHERE id = %s", (pid,))
    assert lgpd.purgar_desligados() == 1
    assert pessoas.obter_foto(pid) is None
    assert db.query("SELECT 1 FROM auditoria WHERE acao = 'purga_lgpd' AND entidade_id = %s", (str(pid),), one=True)
