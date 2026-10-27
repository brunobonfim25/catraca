"""Revisão de 27/10/2026, onda 1: acesso entre escolas e criança saindo sem responsável.
Cada teste reproduz o cenário que a revisão confirmou (código do item no nome)."""
from datetime import timedelta

from werkzeug.security import generate_password_hash

from app import db
from app.eventos import service as eventos
from app.eventos import tipos
from app.pessoas import service as pessoas
from tests.conftest import access_log, dao, hoje_as, unix
from tests.test_modo_pro import identificar


def _outra_escola(seed, empresa=None):
    """Escola B (outra unidade; outra rede se empresa=None for passado como 'nova')."""
    emp = seed["empresa"] if empresa is None else db.execute("INSERT INTO empresas (nome) VALUES ('Rede B') RETURNING id", returning=True)["id"]
    un = db.execute("INSERT INTO unidades (empresa_id, nome) VALUES (%s,'Escola B') RETURNING *", (emp,), returning=True)
    turma = db.execute("INSERT INTO turmas (unidade_id, nome, turno) VALUES (%s,'B1','manha') RETURNING *", (un["id"],), returning=True)
    bia_pid, bia_aid = pessoas.criar_aluno(un["id"], "Bia Secreta", turma["id"])
    mae_pid, mae_rid = pessoas.criar_responsavel(un["id"], "Bete Secreta", "21977776666")
    pessoas.vincular(bia_aid, mae_rid, "mãe")
    disp = db.execute("INSERT INTO dispositivos (unidade_id, nome, ip, device_id_fabricante) VALUES (%s,'Catraca B','10.0.0.9',555) RETURNING *",
                      (un["id"],), returning=True)
    db.execute("INSERT INTO usuarios_admin (empresa_id, unidade_id, nome, email, senha_hash, perfil) VALUES (%s,%s,'Dir B','dirb@teste.com',%s,'admin_unidade')",
               (emp, un["id"], generate_password_hash("senha123")))
    return {"unidade": un, "turma": turma, "bia": {"pessoa_id": bia_pid, "aluno_id": bia_aid}, "bete": {"pessoa_id": mae_pid, "responsavel_id": mae_rid},
            "dispositivo": disp}


# ---------------------------------------------------------------- acesso entre escolas

def test_S2_liberacao_manual_nao_aceita_aluno_nem_catraca_de_outra_escola(cliente, seed):
    b = _outra_escola(seed)
    r = cliente.post("/painel/liberar", data={"pessoa_id": b["bia"]["pessoa_id"], "justificativa": "ataque teste", "sentido": "saida"})
    assert r.status_code == 404
    r = cliente.post("/painel/liberar", data={"dispositivo_id": b["dispositivo"]["id"], "visitante": "x", "justificativa": "ataque teste", "girar": "1"})
    assert r.status_code == 404
    assert not db.query("SELECT 1 FROM fila_sync WHERE tipo = 'liberar'", one=True)
    assert not db.query("SELECT 1 FROM fila_notificacao", one=True)


def test_S3_vinculo_com_aluno_ou_responsavel_de_outra_escola(cliente, seed):
    b = _outra_escola(seed)
    r = cliente.post("/pessoas/responsaveis/novo", data={"nome": "Atacante", "aluno_id": b["bia"]["aluno_id"]})
    assert r.status_code == 404
    r = cliente.post(f"/pessoas/{seed['maria']['pessoa_id']}/vincular", data={"responsavel_id": b["bete"]["responsavel_id"]})
    assert r.status_code == 404
    try:
        pessoas.vincular(b["bia"]["aluno_id"], seed["ana"]["responsavel_id"])
        assert False, "vinculou entre escolas"
    except ValueError:
        pass
    r = cliente.post(f"/pessoas/{seed['maria']['pessoa_id']}/aviso-retirada", data={"responsavel_id": b["bete"]["responsavel_id"]})
    assert r.status_code == 404


def test_S4_ativacao_do_telegram_de_responsavel_de_outra_escola(cliente, seed):
    b = _outra_escola(seed, empresa="nova")
    rid = b["bete"]["responsavel_id"]
    assert cliente.post(f"/telegram/ativar/{rid}/novo-token").status_code == 404
    assert cliente.get(f"/telegram/ativar/{rid}/qr.png").status_code == 404
    assert cliente.get(f"/telegram/ativar/{rid}").status_code == 404
    assert db.query("SELECT telegram_token FROM responsaveis WHERE id = %s", (rid,), one=True)["telegram_token"] is None


def test_S5_S11_direcao_de_escola_nao_cria_usuario_da_rede_nem_mexe_em_outra_escola(cliente, seed):
    b = _outra_escola(seed)
    cliente.post("/usuarios/novo", data={"nome": "Curto", "email": "curto@x.com", "senha": "1", "perfil": "recepcao"})
    assert not db.query("SELECT 1 FROM usuarios_admin WHERE email = 'curto@x.com'", one=True)  # senha curta
    cliente.post("/usuarios/novo", data={"nome": "Rede", "email": "rede@x.com", "senha": "senha-boa-123", "perfil": "admin_unidade", "unidade_id": ""})
    u = db.query("SELECT unidade_id FROM usuarios_admin WHERE email = 'rede@x.com'", one=True)
    assert u["unidade_id"] == seed["unidade"]["id"]  # forçado para a escola de quem criou
    dir_b = db.query("SELECT id FROM usuarios_admin WHERE email = 'dirb@teste.com'", one=True)["id"]
    cliente.post(f"/usuarios/{dir_b}/ativo")
    assert db.query("SELECT ativo FROM usuarios_admin WHERE id = %s", (dir_b,), one=True)["ativo"]
    assert "Dir B" not in cliente.get("/usuarios").get_data(as_text=True)


def test_S8_auditoria_sem_escola_so_para_a_nexosix(cliente, seed):
    db.execute("INSERT INTO auditoria (unidade_id, acao, entidade, depois) VALUES (NULL, 'consentimento', 'pessoa', '{\"assinado_por\": \"Bete Secreta\"}')")
    assert "Bete Secreta" not in cliente.get("/painel/auditoria").get_data(as_text=True)


def test_S15_login_nao_redireciona_para_outro_site(app, seed):
    c = app.test_client()
    r = c.post("/login?proximo=https://evil.example/fake", data={"email": "admin@teste.com", "senha": "senha123"})
    assert r.headers["Location"] in ("/painel", "http://localhost/painel")
    r = c.post("/login?proximo=//evil.example", data={"email": "admin@teste.com", "senha": "senha123"})
    assert "evil" not in r.headers["Location"]


def test_S16_status_dos_servidores_so_das_escolas_visiveis(cliente, seed, app):
    _outra_escola(seed, empresa="nova")
    nomes = [u["nome"] for u in cliente.get("/api/espelho/status").get_json()]
    assert nomes == ["Recreio"]
    rec = app.test_client()
    rec.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"})
    assert rec.get("/api/espelho/status").status_code == 403


# ---------------------------------------------------------------- criança saindo sem responsável

def test_C1_responsavel_bloqueado_nao_retira(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    pessoas.bloquear(sincronizado["ana"]["pessoa_id"])  # recepção bloqueia depois que ela chegou
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 2))["event"] == 6
    # já bloqueada antes de chegar: nem cria espera
    r = identificar(cliente, ids["ana"], "0MS1", hoje_as(17, 3))
    assert r["event"] == 6
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 4))["event"] == 6


def test_C2_mae_que_deixou_de_manha_nao_libera_a_saida(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    dao(cliente, access_log(1, ids["ana"], 1, quando=hoje_as(7, 0)))      # mãe entra com a Maria
    dao(cliente, access_log(2, ids["maria"], 1, quando=hoje_as(7, 0) + timedelta(seconds=30)))
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(7, 5))["event"] == 6
    # e quando ela vai buscar à tarde, libera
    identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 1))["event"] == 7


def test_C2_saida_do_responsavel_encerra_a_espera_em_2_minutos(cliente, sincronizado):
    ids = sincronizado["user_ids"]
    dao(cliente, access_log(1, ids["ana"], 1, quando=hoje_as(17)))
    dao(cliente, access_log(2, ids["ana"], 2, quando=hoje_as(17, 1)))     # ela sai
    e = db.query("SELECT expira_em - now() AS falta FROM responsavel_em_espera", one=True)
    assert e["falta"] <= timedelta(minutes=2, seconds=5)


def test_C3_liberar_vencido_nao_gira_e_nao_repete(seed, sincronizado, simulador):
    from worker import sync

    un = sincronizado["unidade"]
    eventos.liberar_manual(un, sincronizado["admin"]["id"], "saida", "teste vencido", dispositivo_id=sincronizado["dispositivo"]["id"])
    db.execute("UPDATE fila_sync SET payload = payload || '{\"expira_em\": \"2020-01-01T00:00:00+00:00\"}' WHERE tipo = 'liberar'")
    sync.processar_fila()
    item = db.query("SELECT status, erro FROM fila_sync WHERE tipo = 'liberar'", one=True)
    assert item["status"] == "erro" and "vencida" in item["erro"]
    assert simulador["estado"].liberacoes == []
    # válido: gira uma vez
    eventos.liberar_manual(un, sincronizado["admin"]["id"], "saida", "teste valido", dispositivo_id=sincronizado["dispositivo"]["id"])
    sync.processar_fila()
    assert len(simulador["estado"].liberacoes) == 1


def test_C7_irmaos_saem_com_a_mesma_espera(cliente, sincronizado):
    ids, un = sincronizado["user_ids"], sincronizado["unidade"]["id"]
    jose_pid, jose_aid = pessoas.criar_aluno(un, "José Silva", sincronizado["turma"]["id"])
    pessoas.vincular(jose_aid, sincronizado["ana"]["responsavel_id"], "mãe")
    db.execute("UPDATE dispositivo_pessoa SET user_id_no_dispositivo = 99 WHERE pessoa_id = %s", (jose_pid,))
    eventos.atualizar_presenca(jose_pid, un, True, None, hoje_as(7))
    eventos.atualizar_presenca(sincronizado["maria"]["pessoa_id"], un, True, None, hoje_as(7))
    identificar(cliente, ids["ana"], "0MS1", hoje_as(17))
    assert identificar(cliente, ids["maria"], "0MS2", hoje_as(17, 1))["event"] == 7
    assert identificar(cliente, 99, "0MS2", hoje_as(17, 2))["event"] == 7  # antes: negado
    assert db.query("SELECT consumido_em IS NOT NULL AS fim FROM responsavel_em_espera", one=True)["fim"]  # os dois saíram


def test_A2_responsavel_desligado_nao_recebe_mais_avisos(cliente, sincronizado):
    pessoas.desligar(sincronizado["ana"]["pessoa_id"])
    assert db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (sincronizado["ana"]["responsavel_id"],), one=True)["telegram_chat_id"] is None
    dao(cliente, access_log(5, sincronizado["user_ids"]["maria"], 1, quando=hoje_as(7, 10)))
    assert not db.query("SELECT 1 FROM fila_notificacao", one=True)


def test_A13_saida_sem_responsavel_avisa_que_foi_irregular(cliente, sincronizado):
    dao(cliente, access_log(3, sincronizado["user_ids"]["maria"], 2, quando=hoje_as(17)))  # Standalone liberou sozinha
    texto = db.query("SELECT texto FROM fila_notificacao ORDER BY id DESC LIMIT 1", one=True)["texto"]
    assert "sem responsável registrado" in texto


def test_A16_aviso_atrasado_traz_data(cliente, sincronizado, monkeypatch):
    monkeypatch.setattr(eventos, "ATRASO_AVISO_MIN", 10)
    dao(cliente, access_log(4, sincronizado["user_ids"]["maria"], 1, quando=eventos.agora_utc() - timedelta(hours=3)))
    assert db.query("SELECT texto FROM fila_notificacao", one=True)["texto"].endswith("(aviso atrasado)")


def test_C12_relogio_da_catraca_fora_decide_pela_hora_do_servidor(cliente, sincronizado, monkeypatch):
    monkeypatch.setattr(eventos, "TOLERANCIA_RELOGIO", timedelta(minutes=10))
    errado = eventos.agora_utc() + timedelta(hours=3)
    identificar(cliente, sincronizado["user_ids"]["maria"], "0MS1", errado)
    ev = db.query("SELECT ocorrido_em, detalhes FROM eventos ORDER BY id DESC LIMIT 1", one=True)
    assert "relógio da catraca" in ev["detalhes"]["alerta"] and abs(ev["ocorrido_em"] - eventos.agora_utc()) < timedelta(minutes=1)


def test_C13_uma_negativa_avisada_a_cada_10_minutos(cliente, sincronizado):
    uid = sincronizado["user_ids"]["maria"]
    for minuto in (0, 1, 2, 3):
        identificar(cliente, uid, "0MS2", hoje_as(16, minuto))
    assert db.query("SELECT count(*) AS n FROM fila_notificacao", one=True)["n"] == 1


def test_C15_aluno_sem_turma_e_leitor_sem_sentido_negam(cliente, sincronizado):
    db.execute("DELETE FROM alunos WHERE pessoa_id = %s", (sincronizado["maria"]["pessoa_id"],))
    assert identificar(cliente, sincronizado["user_ids"]["maria"], "0MS2", hoje_as(17))["event"] == 6
    db.execute("DELETE FROM dispositivo_leitores")
    db.execute("UPDATE dispositivos SET sentido = 'bidirecional'")
    r = cliente.post("/api/catraca/tok-teste/new_user_identified.fcgi", data={
        "device_id": sincronizado["dispositivo"]["device_id_fabricante"], "time": unix(hoje_as(17)), "user_id": sincronizado["user_ids"]["joao"],
        "event": 7, "component_id": "X", "portal_id": 9}).get_json()["result"]
    assert r["event"] == 6
