"""Telegram: fila de envio, ativação por /start <token>, webhook, QR."""
import io
import logging
import secrets
from datetime import date

import requests
from flask import current_app

from .. import db
from ..auditoria import auditar
from ..util import mesmo_telefone, normalizar_telefone, primeiro_nome

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{metodo}"


def _cfg(chave):
    try:
        return current_app.config.get(chave) or ""
    except RuntimeError:  # fora do app (worker)
        import os

        return os.environ.get(chave, "")


def enfileirar(unidade_id, responsavel_id, texto, evento_id=None):
    r = db.query("SELECT telegram_chat_id FROM responsaveis WHERE id = %s", (responsavel_id,), one=True)
    status = "pendente" if r and r["telegram_chat_id"] else "sem_chat"
    return db.execute(
        "INSERT INTO fila_notificacao (unidade_id, responsavel_id, evento_id, texto, status) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (unidade_id, responsavel_id, evento_id, texto, status), returning=True,
    )["id"]


def enviar(chat_id, texto, token=None, reply_markup=None):
    token = token or _cfg("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")
    corpo = {"chat_id": chat_id, "text": texto}
    if reply_markup:
        corpo["reply_markup"] = reply_markup
    r = requests.post(API.format(token=token, metodo="sendMessage"), json=corpo, timeout=10)
    dados = r.json() if r.content else {}
    if not r.ok or not dados.get("ok"):
        raise RuntimeError(f"Telegram {r.status_code}: {dados.get('description') or r.text[:120]}")
    return dados


def registrar_webhook():
    token = _cfg("TELEGRAM_BOT_TOKEN")
    url = f"{_cfg('BASE_URL')}/api/telegram/webhook"
    r = requests.post(API.format(token=token, metodo="setWebhook"),
                      json={"url": url, "secret_token": _cfg("TELEGRAM_WEBHOOK_SECRET"), "allowed_updates": ["message", "callback_query"]}, timeout=10)
    return r.json()


# ---------------------------------------------------------------- ativação

def gerar_token(responsavel_id):
    token = secrets.token_urlsafe(12)
    db.execute("UPDATE responsaveis SET telegram_token = %s WHERE id = %s", (token, responsavel_id))
    return token


def link_ativacao(token):
    return f"https://t.me/{_cfg('TELEGRAM_BOT_USERNAME')}?start={token}"


def link_escola(unidade_id):
    """Link único da escola (cartaz, grupo da turma): o responsável ativa compartilhando o
    próprio telefone no bot, sem passar pela secretaria."""
    return f"https://t.me/{_cfg('TELEGRAM_BOT_USERNAME')}?start=u{int(unidade_id)}"


def link_whatsapp(telefone, texto):
    """Abre o WhatsApp (web ou app) da secretaria com a mensagem pronta. Sem custo."""
    from urllib.parse import quote

    digitos = normalizar_telefone(telefone)
    if len(digitos) < 10:
        return None
    return f"https://wa.me/55{digitos}?text={quote(texto)}"


TECLADO_TELEFONE = {"keyboard": [[{"text": "📱 Compartilhar meu telefone", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}
TECLADO_REMOVER = {"remove_keyboard": True}


def qr_png(texto):
    import qrcode

    img = qrcode.make(texto)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _chamar(metodo, corpo, token=None):
    token = token or _cfg("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")
    r = requests.post(API.format(token=token, metodo=metodo), json=corpo, timeout=10)
    dados = r.json() if r.content else {}
    if not r.ok or not dados.get("ok"):
        raise RuntimeError(f"Telegram {metodo} {r.status_code}: {dados.get('description') or r.text[:120]}")
    return dados


# ---------------------------------------------------------------- quem é o responsável

def _alunos(responsavel_id):
    return db.query(
        "SELECT pa.id AS pessoa_id, pa.nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id "
        "JOIN pessoas pa ON pa.id = a.pessoa_id WHERE ar.responsavel_id = %s AND pa.status <> 'desligado' ORDER BY pa.nome",
        (responsavel_id,),
    )


def _alunos_de(responsavel_id):
    return ", ".join(primeiro_nome(a["nome"]) for a in _alunos(responsavel_id)) or "seus alunos"


def _nomes_alunos(responsaveis):
    nomes = sorted({primeiro_nome(a["nome"]) for r in responsaveis for a in _alunos(r["id"])}) or ["seu filho(a)"]
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]


def achar_por_token(token):
    """Link individual (/start <token>). Não ativa nada: a ativação vem depois da autorização."""
    return db.query("SELECT r.*, p.nome, p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                    "WHERE r.telegram_token = %s AND p.status <> 'desligado'", (token,), one=True)


def achar_por_telefone(telefone):
    """Link da escola: casa o telefone compartilhado com os responsáveis cadastrados (pode ser
    mais de um — mesma pessoa em duas escolas)."""
    digitos = normalizar_telefone(telefone)
    if len(digitos) < 10:
        return []
    candidatos = db.query(
        "SELECT r.*, p.nome, p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
        "WHERE p.status <> 'desligado' AND right(regexp_replace(coalesce(r.telefone, ''), '\\D', '', 'g'), 8) = %s",
        (digitos[-8:],),
    )
    return [r for r in candidatos if mesmo_telefone(r["telefone"], digitos)]


# ---------------------------------------------------------------- autorização (LGPD) antes de ativar
#
# Decisão de 27/10/2026: o termo dos pais saiu do contrato e passou a ser pedido aqui. Foto
# de criança é dado biométrico (LGPD art. 11 e 14): consentimento específico de um dos pais,
# separado dos avisos — por isso três respostas, e a recusa também fica registrada.

OPCOES = {
    "tudo": "✅ Autorizo a foto e os avisos",
    "avisos": "🔔 Só os avisos, sem foto",
    "nao": "❌ Não autorizo",
}
TECLADO_CONSENTIMENTO = {"inline_keyboard": [[{"text": rot, "callback_data": f"lgpd:{k}"}] for k, rot in OPCOES.items()]}


def texto_consentimento(responsaveis):
    from .convites import nome_escola

    un = db.query("SELECT nome, config FROM unidades WHERE id = %s", (responsaveis[0]["unidade_id"],), one=True)
    return (
        f"Olá, {primeiro_nome(responsaveis[0]['nome'])}! Antes de ativar os avisos, a escola ({nome_escola(un)}) "
        "precisa da sua autorização, conforme a LGPD.\n\n"
        f"A catraca da escola usa reconhecimento facial. Para isso, a escola guarda a foto do rosto de {_nomes_alunos(responsaveis)} "
        "e a sua. As fotos servem só para liberar a entrada e a saída e avisar você, e são apagadas em até 30 dias "
        "depois que o aluno deixa a escola.\n\n"
        "Você pode pedir à secretaria, quando quiser, para ver, corrigir ou apagar esses dados, ou mudar esta resposta.\n\n"
        "Escolha uma opção:"
    )


def pedir_consentimento(responsaveis, chat_id):
    """Marca o chat como pendente desses responsáveis e devolve a pergunta com os botões."""
    chat_id = int(chat_id)
    db.execute("UPDATE responsaveis SET telegram_chat_pendente = NULL WHERE telegram_chat_pendente = %s", (chat_id,))
    db.execute("UPDATE responsaveis SET telegram_chat_pendente = %s WHERE id = ANY(%s)", (chat_id, [r["id"] for r in responsaveis]))
    return {"texto": texto_consentimento(responsaveis), "reply_markup": TECLADO_CONSENTIMENTO}


def _ativar(r, chat_id):
    db.execute("UPDATE responsaveis SET telegram_chat_id = %s, telegram_ativado_em = now(), telegram_token = NULL, "
               "telegram_chat_pendente = NULL WHERE id = %s", (chat_id, r["id"]))
    db.execute("UPDATE fila_notificacao SET status = 'pendente' WHERE responsavel_id = %s AND status = 'sem_chat' AND criado_em > now() - interval '1 day'", (r["id"],))


def responder_consentimento(chat_id, opcao):
    """Botão apertado. Grava a resposta (aceite ou recusa) por aluno e pelo próprio responsável,
    com o texto exato mostrado, e ativa os avisos se ele aceitou. Devolve o texto de resposta."""
    from ..pessoas import service as pessoas

    chat_id = int(chat_id)
    if opcao not in OPCOES:
        return "Opção inválida. Toque de novo no link enviado pela escola."
    rs = db.query("SELECT r.*, p.nome, p.unidade_id, p.id AS pessoa_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                  "WHERE r.telegram_chat_pendente = %s AND p.status <> 'desligado' ORDER BY r.id", (chat_id,))
    if not rs:
        return "Esta autorização já foi respondida ou expirou. Para ativar, toque de novo no link enviado pela escola."
    visto = texto_consentimento(rs) + f"\n\nResposta: {OPCOES[opcao]}"
    alunos = _nomes_alunos(rs)
    with db.lote():
        for r in rs:
            for pessoa_id in [a["pessoa_id"] for a in _alunos(r["id"])] + [r["pessoa_id"]]:
                pessoas.registrar_consentimento(pessoa_id, r["nome"], date.today(), escopo="biometria", aceito=opcao == "tudo",
                                                canal="telegram", responsavel_id=r["id"], texto=visto, observacao=f"Telegram chat {chat_id}")
            pessoas.registrar_consentimento(r["pessoa_id"], r["nome"], date.today(), escopo="avisos", aceito=opcao != "nao",
                                            canal="telegram", responsavel_id=r["id"], texto=visto, observacao=f"Telegram chat {chat_id}")
            if opcao == "nao":
                db.execute("UPDATE responsaveis SET telegram_chat_pendente = NULL WHERE id = %s", (r["id"],))
            else:
                _ativar(r, chat_id)
            auditar("consentimento_telegram", "responsavel", r["id"], depois={"opcao": opcao, "chat_id": chat_id}, unidade_id=r["unidade_id"])
    nome = primeiro_nome(rs[0]["nome"])
    if opcao == "tudo":
        return f"Obrigado, {nome}! Autorização registrada e avisos ativados. Você vai receber aqui as entradas e saídas de {alunos}."
    if opcao == "avisos":
        return (f"Combinado, {nome}: avisos ativados, sem uso de foto. Sem a foto, a catraca não reconhece {alunos}, "
                "que vai entrar e sair pela recepção. Se mudar de ideia, fale com a secretaria.")
    return f"Tudo bem, {nome}. Nada foi ativado e nenhuma foto será usada. Se mudar de ideia, toque de novo no link da escola."


# ---------------------------------------------------------------- webhook

def processar_update(update):
    """Webhook: /start <token individual>, /start u<unidade> (link da escola), contato
    compartilhado ou botão da autorização. Devolve {"texto", "reply_markup", "callback"?} ou None."""
    cq = update.get("callback_query")
    if cq:
        chat = ((cq.get("message") or {}).get("chat") or {})
        # só em conversa privada: o chat é a própria pessoa que apertou
        if not chat.get("id") or chat.get("id") != (cq.get("from") or {}).get("id"):
            return {"texto": None, "callback": cq.get("id"), "aviso": "Use a conversa privada com o bot."}
        dado = cq.get("data") or ""
        texto = responder_consentimento(chat["id"], dado.split(":", 1)[1] if dado.startswith("lgpd:") else "")
        return {"texto": texto, "reply_markup": TECLADO_REMOVER, "callback": cq.get("id"),
                "mensagem": (cq.get("message") or {}).get("message_id"), "chat": chat["id"]}

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    if not chat.get("id"):
        return None
    contato = msg.get("contact")
    if contato:
        remetente = (msg.get("from") or {}).get("id")
        if contato.get("user_id") != remetente:
            return {"texto": "Compartilhe o seu próprio contato (botão abaixo), não o de outra pessoa.", "reply_markup": TECLADO_TELEFONE}
        achados = achar_por_telefone(contato.get("phone_number"))
        if not achados:
            return {"texto": "Não encontrei um cadastro com este telefone. Confirme na secretaria da escola qual número está no seu cadastro.", "reply_markup": TECLADO_REMOVER}
        return pedir_consentimento(achados, chat["id"])

    texto = (msg.get("text") or "").strip()
    if texto.startswith("/start"):
        partes = texto.split(maxsplit=1)
        if len(partes) == 2:
            r = achar_por_token(partes[1].strip())
            if r and r["telegram_chat_id"] == chat["id"]:
                return {"texto": f"Os avisos já estão ativos aqui, {primeiro_nome(r['nome'])}.", "reply_markup": TECLADO_REMOVER}
            if r:
                return pedir_consentimento([r], chat["id"])
        # link da escola (u<id>), link vencido ou /start seco: acha pelo telefone
        return {"texto": "Para ativar os avisos de entrada e saída, toque no botão abaixo e compartilhe o telefone que está no cadastro da escola.", "reply_markup": TECLADO_TELEFONE}
    if texto.startswith("/"):
        return {"texto": "Este bot só envia os avisos da escola. Para ativar, mande /start.", "reply_markup": TECLADO_REMOVER}
    return None


def responder_update(update):
    resposta = processar_update(update)
    if not resposta:
        return resposta
    try:
        if resposta.get("callback"):
            # tira o reloginho do botão e os próprios botões (evita resposta dupla)
            _chamar("answerCallbackQuery", {"callback_query_id": resposta["callback"], "text": resposta.get("aviso") or ""})
            if resposta.get("mensagem"):
                _chamar("editMessageReplyMarkup", {"chat_id": resposta["chat"], "message_id": resposta["mensagem"], "reply_markup": {"inline_keyboard": []}})
        if resposta.get("texto"):
            chat = resposta.get("chat") or update["message"]["chat"]["id"]
            enviar(chat, resposta["texto"], reply_markup=resposta.get("reply_markup"))
    except Exception as e:  # noqa: BLE001 — resposta é cortesia; não pode derrubar o webhook
        log.warning("falha ao responder Telegram: %s", e)
    return resposta
