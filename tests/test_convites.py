"""Convites em massa (WhatsApp e e-mail) e a coluna "Pronto para a catraca?"."""
from urllib.parse import parse_qs, urlparse

from app import db
from app.notificacoes import convites
from app.pessoas import service as pessoas
from tests.conftest import JPEG_OK


def _carla(seed, email="carla@exemplo.com", telefone="21988880001"):
    pid, rid = pessoas.criar_responsavel(seed["unidade"]["id"], "Carla Mendes", telefone, None, email)
    pessoas.vincular(seed["pedro"]["aluno_id"], rid, "mãe")
    return pid, rid


def test_texto_do_convite_usa_o_da_escola_e_nao_quebra_com_chave_errada(seed):
    un = dict(seed["unidade"])
    un["config"] = {**(un["config"] or {}), "nome_convite": "Creche Pica Pau – Recreio", "convite_texto": "Oi {responsavel}, {alunos} {nome_errado}: {link}"}
    _, rid = _carla(seed)
    c = convites.montar(un, rid)
    assert c["texto"].startswith("Oi Carla, Pedro {nome_errado}: https://t.me/")
    assert "Creche Pica Pau – Recreio" in c["assunto"]
    assert 'href="https://t.me/' in c["html"] and "Ativar avisos no Telegram" in c["html"]


def test_token_e_reaproveitado_entre_canais(seed):
    _, rid = _carla(seed)
    a = convites.montar(seed["unidade"], rid)["link"]
    b = convites.montar(seed["unidade"], rid)["link"]
    assert a == b  # convite do WhatsApp não invalida o do e-mail


def test_whatsapp_registra_e_redireciona_com_mensagem_pronta(cliente, seed):
    _, rid = _carla(seed)
    r = cliente.post(f"/convites/{rid}/whatsapp")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://wa.me/5521988880001?text=")
    texto = parse_qs(urlparse(r.headers["Location"]).query)["text"][0]
    assert "Olá, Carla!" in texto and "Pedro" in texto and "https://t.me/" in texto
    c = db.query("SELECT canal, status FROM convites WHERE responsavel_id = %s", (rid,), one=True)
    assert c == {"canal": "whatsapp", "status": "enviado"}
    html = cliente.get("/convites").get_data(as_text=True)
    assert "Carla Mendes" in html and "Ana Silva" not in html  # Ana já recebe avisos (seed tem chat_id)


def test_email_sem_configuracao_nao_enfileira(cliente, seed):
    _, rid = _carla(seed)
    r = cliente.post("/convites/email", data={"todos": "1"}, follow_redirects=True)
    assert "não está configurado" in r.get_data(as_text=True)
    assert db.query("SELECT count(*) AS n FROM convites", one=True)["n"] == 0


def test_email_em_lote_e_worker(cliente, seed, app, monkeypatch):
    monkeypatch.setitem(app.config, "BREVO_API_KEY", "k")
    monkeypatch.setitem(app.config, "EMAIL_REMETENTE", "avisos@nexosix.com.br")
    _, rid = _carla(seed)
    _, sem_email = pessoas.criar_responsavel(seed["unidade"]["id"], "Sem Email", "21900000000")
    r = cliente.post("/convites/email", data={"todos": "1"}, follow_redirects=True)
    assert "1 e-mail(s) na fila" in r.get_data(as_text=True)
    cliente.post("/convites/email", data={"ids": [str(rid)]})  # já está na fila: não duplica
    assert db.query("SELECT count(*) AS n FROM convites WHERE canal = 'email'", one=True)["n"] == 1

    enviados = []
    n, falhas = convites.enviar_pendentes(enviar=lambda para, assunto, html, texto, **k: enviados.append((para, assunto, k)))
    assert (n, falhas) == (1, 0)
    assert enviados[0][0] == "carla@exemplo.com" and "Pedro" in enviados[0][1] and enviados[0][2]["nome_remetente"] == "Recreio"
    assert db.query("SELECT status FROM convites WHERE responsavel_id = %s", (rid,), one=True)["status"] == "enviado"


def test_email_com_erro_do_provedor_tenta_de_novo_depois(seed):
    _, rid = _carla(seed)
    db.execute("INSERT INTO convites (unidade_id, responsavel_id, canal, destino, assunto, corpo_html, corpo_texto) VALUES (%s,%s,'email','x@y.com','a','b','c')",
               (seed["unidade"]["id"], rid))

    def falha(*a, **k):
        raise RuntimeError("brevo 429: too many requests")

    assert convites.enviar_pendentes(enviar=falha) == (0, 1)
    c = db.query("SELECT status, erro, proximo_em > now() AS adiado FROM convites", one=True)
    assert c["status"] == "pendente" and c["adiado"] and "429" in c["erro"]


def test_configuracao_do_convite_exige_link_e_aceita_logo(cliente, seed):
    un = seed["unidade"]["id"]
    r = cliente.post(f"/unidades/{un}/convite", data={"convite_texto": "sem link"}, follow_redirects=True)
    assert "precisa ter {link}" in r.get_data(as_text=True)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    import io

    cliente.post(f"/unidades/{un}/convite", data={"nome_convite": "Creche Pica Pau", "logo": (io.BytesIO(png), "logo.png")}, content_type="multipart/form-data")
    assert db.query("SELECT config->>'nome_convite' AS n FROM unidades WHERE id = %s", (un,), one=True)["n"] == "Creche Pica Pau"
    r = cliente.application.test_client().get(f"/escola/{un}/logo")  # público: vai no e-mail
    assert r.status_code == 200 and r.content_type == "image/png"


def test_pronto_para_a_catraca(seed, sincronizado, cliente):
    un = seed["unidade"]["id"]
    s = pessoas.situacao_alunos(un)
    assert s[seed["maria"]["pessoa_id"]]["chave"] == "pronto"  # foto ok na catraca, mãe com foto
    assert s[seed["pedro"]["pessoa_id"]]["chave"] == "pronto"  # turma com saída livre: não precisa de responsável
    nova_pid, _ = pessoas.criar_aluno(un, "Bia Nova", seed["turma"]["id"])
    assert pessoas.situacao_alunos(un)[nova_pid]["chave"] == "falta_foto"
    pessoas.salvar_foto(nova_pid, JPEG_OK + b"\x09")
    assert pessoas.situacao_alunos(un)[nova_pid]["chave"] == "falta_responsavel"  # Pré 1 exige responsável
    _, rid = pessoas.criar_responsavel(un, "Pai Sem Foto", "21911112222")
    pessoas.vincular(db.query("SELECT id FROM alunos WHERE pessoa_id = %s", (nova_pid,), one=True)["id"], rid, "pai")
    assert pessoas.situacao_alunos(un)[nova_pid]["chave"] == "responsavel_sem_foto"
    db.execute("UPDATE dispositivo_pessoa SET foto_sync_status = 'erro', erro = 'face não detectada' WHERE pessoa_id = %s", (nova_pid,))
    st = pessoas.situacao_alunos(un)[nova_pid]
    assert st["chave"] == "erro_catraca" and st["erro"] == "face não detectada"
    html = cliente.get("/pessoas/?tipo=aluno").get_data(as_text=True)
    assert "Pronto para a catraca?" in html and "Erro na catraca — refazer foto" in html and "Telegram 1/1" in html
    html = cliente.get("/pessoas/?tipo=aluno&situacao=erro_catraca").get_data(as_text=True)
    assert "Bia Nova" in html and "Maria Silva" not in html
