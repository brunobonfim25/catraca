"""Telegram: fila de envio, ativação por /start <token>, webhook, QR."""
import io
import logging
import secrets

import requests
from flask import current_app

from .. import db
from ..auditoria import auditar
from ..util import primeiro_nome

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


def enviar(chat_id, texto, token=None):
    token = token or _cfg("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")
    r = requests.post(API.format(token=token, metodo="sendMessage"), json={"chat_id": chat_id, "text": texto}, timeout=10)
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


def processar_update(update):
    """Webhook/polling: só trata /start <token>. Resposta é texto a enviar (ou None)."""
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    texto = (msg.get("text") or "").strip()
    if not chat.get("id"):
        return None
    if texto.startswith("/start"):
        partes = texto.split(maxsplit=1)
        if len(partes) == 2:
            r = ativar_por_token(partes[1].strip(), chat["id"])
            if r:
                alunos = db.query(
                    "SELECT p.nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas p ON p.id = a.pessoa_id WHERE ar.responsavel_id = %s",
                    (r["id"],),
                )
                nomes = ", ".join(primeiro_nome(a["nome"]) for a in alunos) or "seus alunos"
                return f"Olá, {primeiro_nome(r['nome'])}! Avisos ativados. Você vai receber aqui as entradas e saídas de {nomes}."
            return "Este link de ativação não é válido ou já foi usado. Peça um novo na secretaria."
        return "Para ativar os avisos, use o link/QR Code que a secretaria da escola gerou para você."
    return None


def responder_update(update):
    resposta = processar_update(update)
    if resposta:
        try:
            enviar(update["message"]["chat"]["id"], resposta)
        except Exception as e:  # noqa: BLE001 — resposta ao /start é cortesia; não pode derrubar o webhook
            log.warning("falha ao responder Telegram: %s", e)
    return resposta
