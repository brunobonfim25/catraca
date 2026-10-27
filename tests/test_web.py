"""Telas: login, permissões, cadastro com foto por webcam, Telegram."""
import base64

from app import db
from tests.conftest import JPEG_OK
from worker import notify


def test_login_e_logout(app, seed):
    c = app.test_client()
    assert c.get("/painel").status_code == 302
    r = c.post("/login", data={"email": "admin@teste.com", "senha": "errada"})
    assert "inválidos" in r.get_data(as_text=True)
    c.post("/login", data={"email": "ADMIN@teste.com", "senha": "senha123"})
    assert c.get("/painel").status_code == 200
    c.get("/logout")
    assert c.get("/painel").status_code == 302


def test_permissoes_por_perfil(app, seed):
    c = app.test_client()
    c.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"})
    assert c.get("/painel/recepcao").status_code == 200
    assert c.get("/usuarios").status_code == 403
    assert c.get("/painel/relatorios").status_code == 403
    assert c.post(f"/pessoas/{seed['maria']['pessoa_id']}/desligar").status_code == 403
    assert c.get("/dispositivos/novo").status_code == 403


def test_cadastro_de_aluno_com_foto_pela_webcam(cliente, seed, simulador):
    r = cliente.post("/pessoas/alunos/novo", data={"nome": "Lucas Lima", "turma_id": seed["turma"]["id"], "matricula": "9", "saida_autonoma": "turma", "consentimento_por": "Pai do Lucas"})
    assert r.status_code == 302 and "/foto" in r.headers["Location"]
    pid = db.query("SELECT id FROM pessoas WHERE nome = 'Lucas Lima'", one=True)["id"]
    assert db.query("SELECT 1 FROM consentimentos_lgpd WHERE pessoa_id = %s", (pid,), one=True)
    assert cliente.get(f"/pessoas/{pid}/foto").status_code == 200
    r = cliente.post(f"/pessoas/{pid}/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(JPEG_OK).decode()})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert cliente.get(f"/pessoas/{pid}/foto.jpg").data == JPEG_OK
    fila = {(f["tipo"]) for f in db.query("SELECT tipo FROM fila_sync WHERE pessoa_id = %s AND status = 'pendente'", (pid,))}
    assert fila == {"sync_user", "set_photo", "set_rules"}
    r = cliente.post(f"/pessoas/{pid}/foto", json={"imagem": "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"0" * 200).decode()})
    assert r.status_code == 400 and "JPEG" in r.get_json()["erro"]
    assert cliente.get(f"/pessoas/{pid}").status_code == 200
    assert "Lucas Lima" in cliente.get("/pessoas/?q=lucas").get_data(as_text=True)


def test_vinculo_maximo_cinco(cliente, seed):
    from app.pessoas import service as pessoas

    for i in range(5):
        _, rid = pessoas.criar_responsavel(seed["unidade"]["id"], f"R{i}")
        pessoas.vincular(seed["pedro"]["aluno_id"], rid)
    _, rid = pessoas.criar_responsavel(seed["unidade"]["id"], "R6")
    r = cliente.post(f"/pessoas/{seed['pedro']['pessoa_id']}/vincular", data={"responsavel_id": rid, "regra": "sempre"}, follow_redirects=True)
    assert "já tem 5" in r.get_data(as_text=True)


def test_telas_principais_abrem(cliente, seed):
    for url in ["/painel", "/painel/recepcao", "/painel/presenca", "/painel/presenca?evacuacao=1", "/painel/relatorios", "/painel/relatorios?tipo=atrasos",
                f"/painel/historico/{seed['maria']['pessoa_id']}", "/painel/auditoria", "/turmas", "/unidades", "/dispositivos/", f"/dispositivos/{seed['dispositivo']['id']}",
                "/pessoas/", f"/pessoas/{seed['maria']['pessoa_id']}", f"/pessoas/{seed['ana']['pessoa_id']}", f"/pessoas/alunos/{seed['maria']['pessoa_id']}/editar",
                "/pessoas/responsaveis/novo?aluno_id=1", "/pessoas/funcionarios/novo", "/pessoas/visitantes/novo", "/usuarios", "/senha",
                f"/unidades/{seed['unidade']['id']}/config", "/api/painel/estado"]:
        assert cliente.get(url).status_code == 200, url


def test_turma_nova_e_dispositivo_testar(cliente, seed, simulador):
    r = cliente.post("/turmas/nova", data={"nome": "Pré 2", "turno": "tarde", "exige_responsavel_saida": "1", "tolerancia_entrada_min": "10"})
    assert r.status_code == 302 and db.query("SELECT 1 FROM turmas WHERE nome = 'Pré 2'", one=True)
    import os

    from worker import sync

    os.environ["BASE_URL"], os.environ["MONITOR_TOKEN"] = "http://10.0.0.5:8000", "tok-teste"
    cliente.post(f"/dispositivos/{seed['dispositivo']['id']}/testar")
    cliente.post(f"/dispositivos/{seed['dispositivo']['id']}/monitor")
    cliente.post(f"/dispositivos/{seed['dispositivo']['id']}/modo", data={"modo": "pro"})
    assert db.query("SELECT count(*) AS n FROM fila_sync WHERE tipo IN ('testar','configurar_monitor','modo') AND status = 'pendente'", one=True)["n"] == 3
    ok, falhas = sync.processar_fila(limite=100)
    assert ok >= 3 and falhas == 0
    assert db.query("SELECT firmware, modo FROM dispositivos", one=True) == {"firmware": "6.50.0-sim", "modo": "pro"}
    assert simulador["estado"].config["monitor"] == {**simulador["estado"].config["monitor"], "hostname": "10.0.0.5", "port": "8000", "path": "api/notifications/tok-teste"}
    assert simulador["estado"].servidor_online_url() == "http://10.0.0.5:8000/api/catraca/tok-teste"
    assert "SIM-0001" in cliente.get(f"/dispositivos/{seed['dispositivo']['id']}").get_data(as_text=True)


def test_ativacao_telegram_por_token(cliente, seed, app):
    from app.notificacoes import telegram

    rid = seed["ana"]["responsavel_id"]
    db.execute("UPDATE responsaveis SET telegram_chat_id = NULL WHERE id = %s", (rid,))
    html = cliente.get(f"/telegram/ativar/{rid}").get_data(as_text=True)
    token = db.query("SELECT telegram_token FROM responsaveis WHERE id = %s", (rid,), one=True)["telegram_token"]
    assert token and f"?start={token}" in html
    assert cliente.get(f"/telegram/ativar/{rid}/qr.png").content_type == "image/png"
    telegram.enfileirar(seed["unidade"]["id"], rid, "msg antes da ativação")
    assert db.query("SELECT status FROM fila_notificacao", one=True)["status"] == "sem_chat"
    resposta = telegram.processar_update({"message": {"chat": {"id": 555}, "text": f"/start {token}"}})["texto"]
    assert "Avisos ativados" in resposta and "Maria" in resposta
    r = db.query("SELECT * FROM responsaveis WHERE id = %s", (rid,), one=True)
    assert r["telegram_chat_id"] == 555 and r["telegram_token"] is None
    assert db.query("SELECT status FROM fila_notificacao", one=True)["status"] == "pendente"  # destravou
    assert telegram.processar_update({"message": {"chat": {"id": 556}, "text": f"/start {token}"}})["reply_markup"]["keyboard"][0][0]["request_contact"]  # token usado: cai no telefone
    # webhook exige o segredo
    assert cliente.post("/api/telegram/webhook", json={}).status_code == 403
    assert cliente.post("/api/telegram/webhook", json={"message": {"chat": {"id": 1}, "text": "oi"}}, headers={"X-Telegram-Bot-Api-Secret-Token": "seg-teste"}).status_code == 200


def test_worker_envia_telegram_com_retry(seed):
    from app.notificacoes import telegram

    rid = seed["ana"]["responsavel_id"]
    telegram.enfileirar(seed["unidade"]["id"], rid, "Maria entrou")
    enviados = []
    assert notify.enviar_pendentes(enviar=lambda chat, texto: enviados.append((chat, texto))) == (1, 0)
    assert enviados == [(111, "Maria entrou")]
    telegram.enfileirar(seed["unidade"]["id"], rid, "falha")

    def quebrado(chat, texto):
        raise RuntimeError("Telegram 502")

    assert notify.enviar_pendentes(enviar=quebrado) == (0, 1)
    n = db.query("SELECT * FROM fila_notificacao WHERE texto = 'falha'", one=True)
    assert n["status"] == "pendente" and n["proximo_em"] > n["criado_em"] and "502" in n["erro"]


def test_fuso_invalido_cai_no_padrao(cliente, seed):
    from app import fusos

    assert fusos.validar("Itaguaí Rio de Janeiro ") == "America/Sao_Paulo"
    assert fusos.validar("America/Manaus") == "America/Manaus"
    cliente.post("/unidades/nova", data={"nome": "Itaguaí", "fuso": "Itaguaí Rio de Janeiro"})
    assert db.query("SELECT fuso FROM unidades WHERE nome = 'Itaguaí'", one=True)["fuso"] == "America/Sao_Paulo"
    db.execute("UPDATE unidades SET fuso = 'invalido' WHERE id = %s", (seed["unidade"]["id"],))
    assert cliente.get("/painel/presenca").status_code == 200  # formatação de hora não quebra com fuso ruim


def test_ativacao_pelo_link_da_escola_com_telefone(cliente, seed):
    from app.notificacoes import telegram

    rid = seed["ana"]["responsavel_id"]
    db.execute("UPDATE responsaveis SET telegram_chat_id = NULL, telefone = '(21) 99999-0000' WHERE id = %s", (rid,))
    # /start pelo link da escola (ou seco) pede o telefone
    r = telegram.processar_update({"message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/start u1"}})
    assert r["reply_markup"]["keyboard"][0][0]["request_contact"] is True
    # contato de outra pessoa não vale
    r = telegram.processar_update({"message": {"chat": {"id": 777}, "from": {"id": 777}, "contact": {"phone_number": "+5521999990000", "user_id": 999}}})
    assert "próprio contato" in r["texto"]
    # telefone desconhecido
    r = telegram.processar_update({"message": {"chat": {"id": 777}, "from": {"id": 777}, "contact": {"phone_number": "+5521988887777", "user_id": 777}}})
    assert "Não encontrei" in r["texto"]
    # telefone do cadastro, com DDI e nono dígito
    r = telegram.processar_update({"message": {"chat": {"id": 777}, "from": {"id": 777}, "contact": {"phone_number": "+5521999990000", "user_id": 777}}})
    assert "Avisos ativados" in r["texto"] and "Maria" in r["texto"]
    assert db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (rid,), one=True)["telegram_chat_id"] == 777
    # sem o nono dígito também casa
    db.execute("UPDATE responsaveis SET telegram_chat_id = NULL, telefone = '21 9999-0000' WHERE id = %s", (rid,))
    r = telegram.processar_update({"message": {"chat": {"id": 778}, "from": {"id": 778}, "contact": {"phone_number": "5521999990000", "user_id": 778}}})
    assert "Avisos ativados" in r["texto"]
    # telas: link da escola e WhatsApp
    html = cliente.get(f"/telegram/ativar/{rid}").get_data(as_text=True)
    assert "wa.me/5521" in html and "?start=u1" in html
    assert cliente.get("/telegram/escola/1/qr.png").content_type == "image/png"
    assert "?start=u1" in cliente.get("/unidades/1/config").get_data(as_text=True)
    assert "Ana Silva" not in cliente.get("/pessoas/?tipo=responsavel&pendencia=telegram").get_data(as_text=True)


def test_perfil_secretaria_cai_no_cadastro_e_nao_configura_a_escola(app, seed):
    """Fase 1 da reorganização: cada papel abre na sua tela e a secretaria mexe em cadastro
    e turmas, nunca em escola/catraca/usuários (isso é da direção)."""
    from werkzeug.security import generate_password_hash

    with app.app_context():
        db.execute(
            "INSERT INTO usuarios_admin (empresa_id, unidade_id, nome, email, senha_hash, perfil) VALUES (%s,%s,'Sec','sec@teste.com',%s,'secretaria')",
            (seed["unidade"]["empresa_id"], seed["unidade"]["id"], generate_password_hash("senha123")),
        )
    c = app.test_client()
    r = c.post("/login", data={"email": "sec@teste.com", "senha": "senha123"})
    assert r.headers["Location"].endswith("/pessoas/?tipo=aluno")
    assert c.get("/").headers["Location"].endswith("/pessoas/?tipo=aluno")
    html = c.get("/pessoas/?tipo=aluno").get_data(as_text=True)
    assert "<h3 class=\"mb-0\">Alunos</h3>" in html and "+ Novo aluno" in html
    assert "Configuração" not in html.split("<main")[0]  # menu lateral não oferece o que ela não pode
    assert c.get("/turmas").status_code == 200
    assert c.get(f"/unidades/{seed['unidade']['id']}/config").status_code == 403
    assert c.get("/dispositivos/").status_code == 403
    assert c.get("/usuarios").status_code == 403
    # recepção abre direto na Recepção; direção, no Agora
    c2 = app.test_client()
    assert c2.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"}).headers["Location"].endswith("/painel/recepcao")
    c3 = app.test_client()
    assert c3.post("/login", data={"email": "admin@teste.com", "senha": "senha123"}).headers["Location"].endswith("/painel")
