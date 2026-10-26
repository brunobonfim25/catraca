"""Fila do Telegram → sendMessage. Mesmo padrão SKIP LOCKED da fila de sync."""
import logging
from datetime import timedelta

from app import db
from app.notificacoes import telegram

log = logging.getLogger(__name__)

MAX_TENTATIVAS = 5
BACKOFF_SEG = (15, 60, 300, 900, 3600)


def reservar(limite=50):
    with db.transacao() as cur:
        cur.execute(
            "SELECT id FROM fila_notificacao WHERE status = 'pendente' AND proximo_em <= now() ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limite,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return []
        cur.execute("UPDATE fila_notificacao SET tentativas = tentativas + 1 WHERE id = ANY(%s)", (ids,))
        cur.execute(
            "SELECT n.*, r.telegram_chat_id FROM fila_notificacao n JOIN responsaveis r ON r.id = n.responsavel_id WHERE n.id = ANY(%s) ORDER BY n.id",
            (ids,),
        )
        return cur.fetchall()


def enviar_pendentes(limite=50, enviar=None):
    """`enviar(chat_id, texto)` injetável nos testes. Devolve (enviados, falhas)."""
    enviar = enviar or telegram.enviar
    enviados = falhas = 0
    for n in reservar(limite):
        if not n["telegram_chat_id"]:
            db.execute("UPDATE fila_notificacao SET status = 'sem_chat' WHERE id = %s", (n["id"],))
            continue
        try:
            enviar(n["telegram_chat_id"], n["texto"])
            db.execute("UPDATE fila_notificacao SET status = 'enviado', enviado_em = now(), erro = NULL WHERE id = %s", (n["id"],))
            enviados += 1
        except Exception as e:  # noqa: BLE001 — rede/Telegram fora: agenda retry
            falhas += 1
            if n["tentativas"] >= MAX_TENTATIVAS:
                db.execute("UPDATE fila_notificacao SET status = 'erro', erro = %s WHERE id = %s", (str(e)[:300], n["id"]))
            else:
                espera = BACKOFF_SEG[min(n["tentativas"], len(BACKOFF_SEG)) - 1]
                db.execute("UPDATE fila_notificacao SET erro = %s, proximo_em = now() + %s WHERE id = %s", (str(e)[:300], timedelta(seconds=espera), n["id"]))
    return enviados, falhas
