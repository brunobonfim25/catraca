"""Revisão de 27/10/2026, onda 4: cadastro (importação, consentimento) e avisos aos pais."""
import time

from app import db
from app.notificacoes import telegram
from app.pessoas import importacao
from app.pessoas import service as pessoas
from tests.conftest import JPEG_OK
from tests.test_consentimento import _abrir, _clique
from worker import notify


def _contato(chat, tel):
    return telegram.processar_update({"message": {"chat": {"id": chat}, "from": {"id": chat}, "contact": {"user_id": chat, "phone_number": "+55" + tel}}})


def _mapa():
    return {"aluno_nome": 0, "turma": 1, "matricula": None, "turno": None,
            "resp": [{"nome": 2, "telefone": 3, "email": None, "parentesco": None, "padrao": "mãe"},
                     {"nome": 4, "telefone": 5, "email": None, "parentesco": None, "padrao": "pai"}]}


# ---------------------------------------------------------------- importação

def test_A3_homonimos_na_mesma_turma_viram_erro():
    linhas = [["Maria Souza", "Pré 2", "Ana Souza", "21911110001", "", ""],
              ["Maria Souza", "Pré 2", "Carla Lima", "21922220002", "Rui Lima", "21933330003"]]
    regs = importacao.processar(linhas, _mapa())
    assert len(regs) == 1 and "mesmo nome" in regs[0]["erros"][0]
    # "uma linha por responsável": cada linha com um responsável só → junta, com aviso
    linhas = [["Maria Souza", "Pré 2", "Ana Souza", "21911110001", "", ""],
              ["Maria Souza", "Pré 2", "Rui Souza", "21933330003", "", ""]]
    regs = importacao.processar(linhas, _mapa())
    assert not regs[0]["erros"] and len(regs[0]["responsaveis"]) == 2 and "juntadas" in regs[0]["avisos"][0]


def test_A3_homonimos_ja_no_sistema(seed):
    un = seed["unidade"]["id"]
    pessoas.criar_aluno(un, "Maria Silva", seed["turma"]["id"])  # já existe outra Maria Silva no Pré 1
    regs = importacao.processar([["Maria Silva", "Pré 1", "Ana Silva", "21999990000", "", ""]], _mapa())
    plano = importacao.planejar(un, regs)
    assert plano["erros"] and "dois alunos" in plano["erros"][0]["erros"][0]


def test_A4_nunca_junta_nomes_diferentes_nem_homonimos_de_familias_diferentes(seed):
    un = seed["unidade"]["id"]
    linhas = [["Bia Rocha", "Pré 2", "Ana Paula Rocha", "21911110001", "", ""],
              ["Caio Mendes", "Pré 2", "Ana Paula Rocha", "21944440004", "", ""],          # outra família, outro celular
              ["Davi Lopes", "Pré 2", "Marta Lopes", "21955550005", "Jorge Lopes", "21955550005"]]  # pai e mãe, mesmo celular
    plano = importacao.planejar(un, importacao.processar(linhas, _mapa()))
    nomes = sorted(r["nome"] for r in plano["responsaveis_novos"])
    assert nomes == ["Ana Paula Rocha", "Ana Paula Rocha", "Jorge Lopes", "Marta Lopes"]
    # a mãe já cadastrada com o mesmo celular do pai: o pai NÃO vira a Ana
    linhas = [["Maria Silva", "Pré 1", "Ana Silva", "21999990000", "Pedro Silva", "21999990000"]]
    plano = importacao.planejar(un, importacao.processar(linhas, _mapa()))
    refs = {r["nome"]: r["ref"][0] for r in plano["itens"][0]["responsaveis"]}
    assert refs == {"Ana Silva": "id", "Pedro Silva": "novo"}


def test_A12_importacao_grande_e_rapida(seed):
    linhas = [[f"Aluno {i:05d}", f"Turma {i % 40}", f"Mãe {i:05d}", f"219{i:08d}", f"Pai {i:05d}", f"218{i:08d}"] for i in range(5000)]
    t = time.time()
    plano = importacao.planejar(seed["unidade"]["id"], importacao.processar(linhas, _mapa()))
    assert time.time() - t < 5 and plano["resumo"]["alunos_novos"] == 5000 and plano["resumo"]["responsaveis_novos"] == 10000


# ---------------------------------------------------------------- consentimento e foto

def test_A7_S10_recusa_dos_pais_vence_a_escola_e_tira_a_foto(cliente, sincronizado):
    maria, ana_rid = sincronizado["maria"]["pessoa_id"], sincronizado["ana"]["responsavel_id"]
    assert pessoas.tem_consentimento(maria)  # termo declarado pela escola (seed)
    db.execute("UPDATE responsaveis SET telegram_chat_id = NULL WHERE id = %s", (ana_rid,))
    _abrir(950, telegram.gerar_token(ana_rid), "21999990000")
    telegram.processar_update(_clique(950, "avisos"))  # a mãe: só avisos, sem foto
    assert not pessoas.tem_consentimento(maria)
    assert pessoas.obter_foto(maria) is None
    assert db.query("SELECT 1 FROM fila_sync WHERE tipo = 'remove_user' AND pessoa_id = %s AND status = 'pendente'", (maria,), one=True)
    # sem autorização a foto nem é aceita
    import base64

    r = cliente.post(f"/pessoas/{maria}/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(JPEG_OK).decode()})
    assert r.status_code == 409 and "autorização" in r.get_json()["erro"]


def test_A6_nao_autorizo_e_parar_desligam_os_avisos(seed):
    ana_rid = seed["ana"]["responsavel_id"]
    db.execute("UPDATE responsaveis SET telegram_chat_id = 951 WHERE id = %s", (ana_rid,))
    _contato(951, "21999990000")  # já ativa, pede a autorização de novo (link da escola)
    assert "desligados" in telegram.processar_update(_clique(951, "nao"))["texto"]
    assert db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (ana_rid,), one=True)["telegram_chat_id"] is None
    db.execute("UPDATE responsaveis SET telegram_chat_id = 952 WHERE id = %s", (ana_rid,))
    r = telegram.processar_update({"message": {"chat": {"id": 952}, "text": "/parar"}})
    assert "desligados" in r["texto"]
    assert db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (ana_rid,), one=True)["telegram_chat_id"] is None
    assert db.query("SELECT 1 FROM consentimentos_lgpd WHERE responsavel_id = %s AND escopo = 'avisos' AND NOT aceito AND texto = '/parar'", (ana_rid,), one=True)


# ---------------------------------------------------------------- ativação

def test_A5_link_vence_pede_o_telefone_e_avisa_o_chat_antigo(seed, monkeypatch):
    ana_rid = seed["ana"]["responsavel_id"]  # chat 111 ativo
    token = telegram.gerar_token(ana_rid)
    # quem recebeu o link encaminhado não tem o celular da mãe
    _ = telegram.processar_update({"message": {"chat": {"id": 960}, "text": f"/start {token}"}})
    assert "Não encontrei" in _contato(960, "21900000000")["texto"]
    # link vencido: não reconhece o token
    db.execute("UPDATE responsaveis SET telegram_token_em = now() - interval '40 days' WHERE id = %s", (ana_rid,))
    assert telegram.achar_por_token(token) is None
    # a mãe troca de celular/Telegram: o chat antigo é avisado
    enviados = []
    monkeypatch.setattr(telegram, "enviar", lambda chat, texto, **k: enviados.append((chat, texto)))
    _abrir(961, telegram.gerar_token(ana_rid), "21999990000")
    telegram.processar_update(_clique(961, "tudo"))
    assert enviados and enviados[0][0] == 111 and "transferidos" in enviados[0][1]


def test_A11_mesmo_celular_pergunta_quem_e_e_manda_um_aviso_so(seed):
    un = seed["unidade"]["id"]
    maria_aid = seed["maria"]["aluno_id"]
    _, pai_rid = pessoas.criar_responsavel(un, "Paulo Silva", "21999990000")  # mesmo celular da Ana
    pessoas.vincular(maria_aid, pai_rid, "pai")
    db.execute("UPDATE responsaveis SET telegram_chat_id = NULL WHERE id = %s", (seed["ana"]["responsavel_id"],))
    r = _contato(970, "21999990000")
    assert "Quem é você" in r["texto"]
    escolha = next(b[0]["callback_data"] for b in r["reply_markup"]["inline_keyboard"] if "Paulo" in b[0]["text"])
    r = telegram.processar_update({"callback_query": {"id": "c", "from": {"id": 970}, "data": escolha, "message": {"message_id": 1, "chat": {"id": 970}}}})
    assert r["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lgpd:tudo" and "Paulo" in r["texto"]
    telegram.processar_update(_clique(970, "tudo"))
    assert db.query("SELECT responsavel_id FROM consentimentos_lgpd WHERE canal = 'telegram' AND escopo = 'avisos'", one=True)["responsavel_id"] == pai_rid
    # os dois no mesmo chat: o aviso do mesmo evento sai uma vez só
    db.execute("UPDATE responsaveis SET telegram_chat_id = 970 WHERE id IN (%s, %s)", (seed["ana"]["responsavel_id"], pai_rid))
    ev = db.execute("INSERT INTO eventos (unidade_id, tipo, ocorrido_em) VALUES (%s, 'ENTRADA', now()) RETURNING id", (un,), returning=True)["id"]
    telegram.enfileirar(un, seed["ana"]["responsavel_id"], "Maria entrou", ev)
    telegram.enfileirar(un, pai_rid, "Maria entrou", ev)
    enviados = []
    assert notify.enviar_pendentes(enviar=lambda c, t: enviados.append(c)) == (1, 0)
    assert enviados == [970]
    assert db.query("SELECT count(*) AS n FROM fila_notificacao WHERE status = 'enviado'", one=True)["n"] == 2


def test_A15_texto_cita_todas_as_escolas(seed):
    outra = db.execute("INSERT INTO unidades (empresa_id, nome) VALUES (%s, 'Itaguaí') RETURNING id", (seed["empresa"],), returning=True)["id"]
    _, rid2 = pessoas.criar_responsavel(outra, "Ana Silva", "21999990000")
    rs = telegram.achar_por_telefone("21999990000")
    texto = telegram.texto_consentimento(rs)
    assert "Recreio" in texto and "Itaguaí" in texto and "as escolas" in texto
    assert rid2


# ---------------------------------------------------------------- envio

def test_A8_bloqueou_o_bot_e_429(seed):
    rid = seed["ana"]["responsavel_id"]
    telegram.enfileirar(seed["unidade"]["id"], rid, "a")

    def bloqueado(c, t):
        raise telegram.TelegramErro(403, "Forbidden: bot was blocked by the user")

    notify.enviar_pendentes(enviar=bloqueado)
    r = db.query("SELECT telegram_chat_id, telegram_bloqueado_em FROM responsaveis WHERE id = %s", (rid,), one=True)
    assert r["telegram_chat_id"] is None and r["telegram_bloqueado_em"]
    assert db.query("SELECT status FROM fila_notificacao", one=True)["status"] == "erro"
    db.execute("UPDATE responsaveis SET telegram_chat_id = 111 WHERE id = %s", (rid,))
    db.execute("DELETE FROM fila_notificacao")
    telegram.enfileirar(seed["unidade"]["id"], rid, "b")

    def devagar(c, t):
        raise telegram.TelegramErro(429, "Too Many Requests", retry_after=120)

    notify.enviar_pendentes(enviar=devagar)
    n = db.query("SELECT status, proximo_em - now() AS falta FROM fila_notificacao", one=True)
    assert n["status"] == "pendente" and n["falta"].total_seconds() > 100
    assert notify.MAX_TENTATIVAS >= 12 and sum(notify.BACKOFF_SEG) > 5 * 3600  # aguenta horas de Telegram fora


def test_A10_reserva_segura_a_linha(seed):
    telegram.enfileirar(seed["unidade"]["id"], seed["ana"]["responsavel_id"], "x")
    assert len(notify.reservar()) == 1
    assert notify.reservar() == []  # outro worker não pega a mesma linha


def test_A9_ordem_por_hora(seed):
    un = seed["unidade"]["id"]
    rid = seed["ana"]["responsavel_id"]
    novo = telegram.enfileirar(un, rid, "novo")
    velho = telegram.enfileirar(un, rid, "velho")
    db.execute("UPDATE fila_notificacao SET criado_em = now() - interval '1 hour' WHERE id = %s", (velho,))
    assert [n["id"] for n in notify.reservar()] == [velho, novo]


def test_A18_limpa_filas_antigas(seed):
    rid = seed["ana"]["responsavel_id"]
    nid = telegram.enfileirar(seed["unidade"]["id"], rid, "velho")
    db.execute("UPDATE fila_notificacao SET status = 'enviado', criado_em = now() - interval '100 days' WHERE id = %s", (nid,))
    telegram.enfileirar(seed["unidade"]["id"], rid, "novo")
    notify.limpar_antigos()
    assert [r["texto"] for r in db.query("SELECT texto FROM fila_notificacao")] == ["novo"]


def test_A19_cota_do_email_fica_para_amanha(seed):
    from app.notificacoes import convites
    from app.notificacoes.email import EmailErro

    rid = seed["ana"]["responsavel_id"]
    db.execute("UPDATE responsaveis SET email = 'ana@x.com' WHERE id = %s", (rid,))
    convites.enfileirar_email(seed["unidade"]["id"], [rid]) if hasattr(convites, "enfileirar_email") else None
    if not db.query("SELECT 1 FROM convites", one=True):
        db.execute("INSERT INTO convites (unidade_id, responsavel_id, canal, destino, status, assunto, corpo_html, corpo_texto) "
                   "VALUES (%s,%s,'email','ana@x.com','pendente','a','b','c')", (seed["unidade"]["id"], rid))

    def cota(*a, **k):
        raise EmailErro("brevo 402: daily sending limit reached", 402)

    convites.enviar_pendentes(enviar=cota)
    c = db.query("SELECT status, tentativas, proximo_em > now() + interval '1 hour' AS amanha, erro FROM convites", one=True)
    assert c["status"] == "pendente" and c["tentativas"] == 0 and c["amanha"] and "amanhã" in c["erro"]


def test_A14_faixa_de_sem_atualizacao(cliente):
    html = cliente.get("/painel").get_data(as_text=True)
    assert "sem-atualizacao" in html and "marcarAtualizado()" in html
