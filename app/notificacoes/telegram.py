"""Telegram: fila de envio, ativação por /start <token>, webhook, QR."""
import io
import logging
import secrets

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
                      json={"url": url, "secret_token": _cfg("TELEGRAM_WEBHOOK_SECRET"), "allowed_updates": ["message"]}, timeout=10)
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


def ativar_por_token(token, chat_id):
    """/start <token> → grava chat_id. Devolve o responsável ou None. Token é de uso único."""
    r = db.query("SELECT r.*, p.nome FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE r.telegram_token = %s", (token,), one=True)
    if not r:
        return None
    db.execute("UPDATE responsaveis SET telegram_chat_id = %s, telegram_ativado_em = now(), telegram_token = NULL WHERE id = %s", (int(chat_id), r["id"]))
    db.execute("UPDATE fila_notificacao SET status = 'pendente' WHERE responsavel_id = %s AND status = 'sem_chat' AND criado_em > now() - interval '1 day'", (r["id"],))
    auditar("telegram_ativado", "responsavel", r["id"], depois={"chat_id": int(chat_id)}, unidade_id=None)
    return r


def _alunos_de(responsavel_id):
    rows = db.query(
        "SELECT p.nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas p ON p.id = a.pessoa_id WHERE ar.responsavel_id = %s",
        (responsavel_id,),
    )
    return ", ".join(primeiro_nome(a["nome"]) for a in rows) or "seus alunos"


def ativar_por_telefone(telefone, chat_id):
    """Link da escola: casa o telefone compartilhado com os responsáveis cadastrados (pode ser
    mais de um — mesma pessoa em duas escolas). Devolve a lista ativada."""
    digitos = normalizar_telefone(telefone)
    if len(digitos) < 10:
        return []
    candidatos = db.query(
        "SELECT r.*, p.nome, p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
        "WHERE p.status <> 'desligado' AND right(regexp_replace(coalesce(r.telefone, ''), '\\D', '', 'g'), 8) = %s",
        (digitos[-8:],),
    )
    ativados = [r for r in candidatos if mesmo_telefone(r["telefone"], digitos)]
    for r in ativados:
        db.execute("UPDATE responsaveis SET telegram_chat_id = %s, telegram_ativado_em = now(), telegram_token = NULL WHERE id = %s", (int(chat_id), r["id"]))
        db.execute("UPDATE fila_notificacao SET status = 'pendente' WHERE responsavel_id = %s AND status = 'sem_chat' AND criado_em > now() - interval '1 day'", (r["id"],))
        auditar("telegram_ativado", "responsavel", r["id"], depois={"chat_id": int(chat_id), "por": "telefone"}, unidade_id=r["unidade_id"])
    return ativados


def processar_update(update):
    """Webhook: /start <token individual>, /start u<unidade> (link da escola) ou contato
    compartilhado. Devolve {"texto", "reply_markup"} ou None."""
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    if not chat.get("id"):
        return None
    contato = msg.get("contact")
    if contato:
        remetente = (msg.get("from") or {}).get("id")
        if contato.get("user_id") != remetente:
            return {"texto": "Compartilhe o seu próprio contato (botão abaixo), não o de outra pessoa.", "reply_markup": TECLADO_TELEFONE}
        ativados = ativar_por_telefone(contato.get("phone_number"), chat["id"])
        if not ativados:
            return {"texto": "Não encontrei um cadastro com este telefone. Confirme na secretaria da escola qual número está no seu cadastro.", "reply_markup": TECLADO_REMOVER}
        alunos = ", ".join(sorted({_alunos_de(r["id"]) for r in ativados}))
        return {"texto": f"Olá, {primeiro_nome(ativados[0]['nome'])}! Avisos ativados. Você vai receber aqui as entradas e saídas de {alunos}.", "reply_markup": TECLADO_REMOVER}

    texto = (msg.get("text") or "").strip()
    if texto.startswith("/start"):
        partes = texto.split(maxsplit=1)
        if len(partes) == 2:
            r = ativar_por_token(partes[1].strip(), chat["id"])
            if r:
                return {"texto": f"Olá, {primeiro_nome(r['nome'])}! Avisos ativados. Você vai receber aqui as entradas e saídas de {_alunos_de(r['id'])}.", "reply_markup": TECLADO_REMOVER}
        # link da escola (u<id>), link vencido ou /start seco: ativa pelo telefone
        return {"texto": "Para ativar os avisos de entrada e saída, toque no botão abaixo e compartilhe o telefone que está no cadastro da escola.", "reply_markup": TECLADO_TELEFONE}
    if texto.startswith("/"):
        return {"texto": "Este bot só envia os avisos da escola. Para ativar, mande /start.", "reply_markup": TECLADO_REMOVER}
    return None


def responder_update(update):
    resposta = processar_update(update)
    if resposta:
        try:
            enviar(update["message"]["chat"]["id"], resposta["texto"], reply_markup=resposta.get("reply_markup"))
        except Exception as e:  # noqa: BLE001 — resposta ao /start é cortesia; não pode derrubar o webhook
            log.warning("falha ao responder Telegram: %s", e)
    return resposta
