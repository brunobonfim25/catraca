"""Autorização dos pais (LGPD) pedida pelo bot do Telegram antes de ativar os avisos."""
from app import db
from app.notificacoes import telegram
from app.pessoas import service as pessoas


def _clique(chat, opcao, de=None):
    return {"callback_query": {"id": "cb", "from": {"id": de or chat}, "data": f"lgpd:{opcao}",
                               "message": {"message_id": 1, "chat": {"id": chat}}}}


def _novo_aluno_com_mae(seed, nome="Bia Nova", mae="Carla Nova", tel="21988880001"):
    un = seed["unidade"]["id"]
    aluno_pid, aluno_id = pessoas.criar_aluno(un, nome, seed["turma"]["id"])
    mae_pid, mae_rid = pessoas.criar_responsavel(un, mae, tel)
    pessoas.vincular(aluno_id, mae_rid, "mãe")
    token = telegram.gerar_token(mae_rid)
    return aluno_pid, mae_pid, mae_rid, token


def test_autoriza_tudo_registra_prova_e_ativa(seed):
    aluno_pid, mae_pid, rid, token = _novo_aluno_com_mae(seed)
    assert pessoas.situacao_alunos(seed["unidade"]["id"])[aluno_pid]["chave"] == "falta_autorizacao"
    pergunta = telegram.processar_update({"message": {"chat": {"id": 900}, "text": f"/start {token}"}})
    botoes = [b[0]["callback_data"] for b in pergunta["reply_markup"]["inline_keyboard"]]
    assert botoes == ["lgpd:tudo", "lgpd:avisos", "lgpd:nao"] and "Bia" in pergunta["texto"]
    resposta = telegram.processar_update(_clique(900, "tudo"))
    assert "avisos ativados" in resposta["texto"] and resposta["callback"] == "cb"
    r = db.query("SELECT telegram_chat_id, telegram_chat_pendente, telegram_token FROM responsaveis WHERE id = %s", (rid,), one=True)
    assert r == {"telegram_chat_id": 900, "telegram_chat_pendente": None, "telegram_token": None}
    assert pessoas.tem_consentimento(aluno_pid) and pessoas.tem_consentimento(mae_pid)
    prova = db.query("SELECT canal, responsavel_id, texto FROM consentimentos_lgpd WHERE pessoa_id = %s AND escopo = 'biometria'", (aluno_pid,), one=True)
    assert prova["canal"] == "telegram" and prova["responsavel_id"] == rid
    assert "reconhecimento facial" in prova["texto"] and "Resposta: ✅ Autorizo a foto e os avisos" in prova["texto"]
    assert pessoas.situacao_alunos(seed["unidade"]["id"])[aluno_pid]["chave"] == "falta_foto"  # autorizado: agora a foto
    # botão apertado de novo (ou mensagem antiga): já respondido, nada muda
    assert "já foi respondida" in telegram.processar_update(_clique(900, "nao"))["texto"]
    assert pessoas.tem_consentimento(aluno_pid)


def test_so_avisos_ativa_sem_foto_e_nao_autoriza_nada(seed):
    aluno_pid, mae_pid, rid, token = _novo_aluno_com_mae(seed)
    telegram.processar_update({"message": {"chat": {"id": 901}, "text": f"/start {token}"}})
    assert "entrar e sair pela recepção" in telegram.processar_update(_clique(901, "avisos"))["texto"]
    assert db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (rid,), one=True)["telegram_chat_id"] == 901
    assert not pessoas.tem_consentimento(aluno_pid)
    assert pessoas.situacao_alunos(seed["unidade"]["id"])[aluno_pid]["chave"] == "foto_recusada"

    aluno2, _, rid2, token2 = _novo_aluno_com_mae(seed, "Leo Outro", "Paula Outra", "21977770002")
    telegram.processar_update({"message": {"chat": {"id": 902}, "text": f"/start {token2}"}})
    assert "Nada foi ativado" in telegram.processar_update(_clique(902, "nao"))["texto"]
    r = db.query("SELECT telegram_chat_id, telegram_token FROM responsaveis WHERE id = %s", (rid2,), one=True)
    assert r["telegram_chat_id"] is None and r["telegram_token"] == token2  # pode mudar de ideia pelo mesmo link
    assert db.query("SELECT count(*) AS n FROM consentimentos_lgpd WHERE responsavel_id = %s AND NOT aceito", (rid2,), one=True)["n"] == 3


def test_basta_um_dos_pais_e_recusa_nova_do_mesmo_vence(seed):
    aluno_pid, _, mae_rid, token = _novo_aluno_com_mae(seed)
    un = seed["unidade"]["id"]
    _, pai_rid = pessoas.criar_responsavel(un, "Pai Novo", "21966660003")
    pessoas.vincular(db.query("SELECT id FROM alunos WHERE pessoa_id = %s", (aluno_pid,), one=True)["id"], pai_rid, "pai")
    telegram.processar_update({"message": {"chat": {"id": 903}, "text": f"/start {token}"}})
    telegram.processar_update(_clique(903, "avisos"))           # mãe: sem foto
    telegram.processar_update({"message": {"chat": {"id": 904}, "text": f"/start {telegram.gerar_token(pai_rid)}"}})
    telegram.processar_update(_clique(904, "tudo"))             # pai: autoriza
    assert pessoas.tem_consentimento(aluno_pid)                 # um dos pais basta
    # o pai muda de ideia (secretaria registra pelo bot de novo): a recusa mais nova dele vence
    db.execute("UPDATE responsaveis SET telegram_chat_pendente = 904 WHERE id = %s", (pai_rid,))
    telegram.processar_update(_clique(904, "avisos"))
    assert not pessoas.tem_consentimento(aluno_pid)


def test_clique_de_outra_pessoa_ou_grupo_nao_vale(seed):
    aluno_pid, _, rid, token = _novo_aluno_com_mae(seed)
    telegram.processar_update({"message": {"chat": {"id": 905}, "text": f"/start {token}"}})
    r = telegram.processar_update(_clique(905, "tudo", de=999))  # chat ≠ quem clicou (grupo)
    assert r["texto"] is None
    assert not pessoas.tem_consentimento(aluno_pid)
    assert "expirou" in telegram.processar_update(_clique(906, "tudo"))["texto"]  # chat sem pedido pendente


def test_webhook_registra_botoes(monkeypatch, app):
    enviado = {}

    class R:
        ok, content = True, b"x"

        def json(self):
            return {"ok": True}

    monkeypatch.setitem(app.config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(telegram.requests, "post", lambda url, json, timeout: enviado.update(json) or R())
    with app.app_context():
        telegram.registrar_webhook()
    assert enviado["allowed_updates"] == ["message", "callback_query"]
