"""Fila do Telegram → sendMessage. Mesmo padrão SKIP LOCKED da fila de sync."""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app import db
from app.auditoria import auditar
from app.notificacoes import telegram

log = logging.getLogger(__name__)

# Revisão (A8): 5 tentativas em ~21 min perdiam as entradas do dia num Telegram fora por meia
# hora às 7h. Agora tenta por umas 6 horas; depois disso o aviso perdeu o sentido.
BACKOFF_SEG = (15, 30, 60, 120, 300, 600, 900, 1800, 1800, 3600, 3600, 3600, 3600)
MAX_TENTATIVAS = len(BACKOFF_SEG)
PARALELO = 4  # ~25 mensagens/s: abaixo do limite do Telegram (30/s), dá conta do pico das 7h
RESERVA = timedelta(minutes=5)


def reservar(limite=100):
    """Reserva com "aluguel" (proximo_em + 5 min). Revisão (A10): só somar tentativa não
    segurava a linha — dois workers ao mesmo tempo mandavam o mesmo aviso duas vezes.
    Ordem por hora (A9): antes era por id, e os avisos de uma escola esperavam os da outra."""
    with db.transacao() as cur:
        cur.execute(
            "SELECT id FROM fila_notificacao WHERE status = 'pendente' AND proximo_em <= now() ORDER BY criado_em, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limite,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return []
        cur.execute("UPDATE fila_notificacao SET tentativas = tentativas + 1, proximo_em = now() + %s WHERE id = ANY(%s)", (RESERVA, ids))
        # O chat é decidido AGORA, na nuvem (A1), e só de responsável ativo
        cur.execute(
            "SELECT n.*, r.telegram_chat_id, p.status AS responsavel_status FROM fila_notificacao n "
            "JOIN responsaveis r ON r.id = n.responsavel_id JOIN pessoas p ON p.id = r.pessoa_id WHERE n.id = ANY(%s) ORDER BY n.criado_em, n.id",
            (ids,),
        )
        return cur.fetchall()


def _falhou(n, e):
    if isinstance(e, telegram.TelegramErro) and e.bloqueado:
        # bloqueou o bot: repetir não adianta; o painel passa a mostrar
        db.execute("UPDATE fila_notificacao SET status = 'erro', erro = %s WHERE id = %s", (str(e)[:300], n["id"]))
        db.execute("UPDATE responsaveis SET telegram_bloqueado_em = now(), telegram_chat_id = NULL WHERE id = %s AND telegram_chat_id = %s",
                   (n["responsavel_id"], n["telegram_chat_id"]))
        auditar("telegram_bloqueado", "responsavel", n["responsavel_id"], depois={"erro": str(e)[:200]}, unidade_id=n["unidade_id"])
        return
    if n["tentativas"] >= MAX_TENTATIVAS:
        db.execute("UPDATE fila_notificacao SET status = 'erro', erro = %s WHERE id = %s", (str(e)[:300], n["id"]))
        return
    espera = BACKOFF_SEG[min(n["tentativas"], len(BACKOFF_SEG)) - 1]
    if isinstance(e, telegram.TelegramErro) and e.retry_after:
        espera = max(espera, int(e.retry_after))  # 429: o Telegram diz quanto esperar
    db.execute("UPDATE fila_notificacao SET status = 'pendente', erro = %s, proximo_em = now() + %s WHERE id = %s",
               (str(e)[:300], timedelta(seconds=espera), n["id"]))


def enviar_pendentes(limite=100, enviar=None, paralelo=PARALELO):
    """`enviar(chat_id, texto)` injetável nos testes. Devolve (enviados, falhas)."""
    enviar = enviar or telegram.enviar
    lote = reservar(limite)
    mandar, vistos = [], {}
    for n in lote:
        if not n["telegram_chat_id"] or n["responsavel_status"] != "ativo":
            db.execute("UPDATE fila_notificacao SET status = 'sem_chat' WHERE id = %s", (n["id"],))
            continue
        # A11: pai e mãe no mesmo chat recebiam cada aviso em dobro — um por chat basta
        chave = (n["telegram_chat_id"], n["evento_id"], n["texto"])
        if n["evento_id"] and chave in vistos:
            vistos[chave].append(n)
            continue
        if n["evento_id"] and db.query("SELECT 1 FROM fila_notificacao f JOIN responsaveis r ON r.id = f.responsavel_id "
                                       "WHERE f.evento_id = %s AND f.texto = %s AND f.status = 'enviado' AND r.telegram_chat_id = %s AND f.id <> %s LIMIT 1",
                                       (n["evento_id"], n["texto"], n["telegram_chat_id"], n["id"]), one=True):
            db.execute("UPDATE fila_notificacao SET status = 'enviado', enviado_em = now(), erro = 'mesmo chat de outro responsável' WHERE id = %s", (n["id"],))
            continue
        vistos[chave] = [n]
        mandar.append(n)

    def _um(n):
        try:
            enviar(n["telegram_chat_id"], n["texto"])
            return n, None
        except Exception as e:  # noqa: BLE001 — rede/Telegram fora: agenda retry
            return n, e

    enviados = falhas = 0
    with ThreadPoolExecutor(max_workers=max(1, paralelo)) as pool:
        resultados = list(pool.map(_um, mandar))
    for n, erro in resultados:
        grupo = vistos[(n["telegram_chat_id"], n["evento_id"], n["texto"])]
        if erro is None:
            db.execute("UPDATE fila_notificacao SET status = 'enviado', enviado_em = now(), erro = NULL WHERE id = ANY(%s)", ([g["id"] for g in grupo],))
            enviados += 1
        else:
            falhas += 1
            for g in grupo:
                _falhou(g, erro)
    return enviados, falhas


def limpar_antigos():
    """A18: filas crescem pra sempre. Aviso enviado/sem chat some em 90 dias; item de catraca
    concluído, em 30 (o evento continua no histórico)."""
    db.execute("DELETE FROM fila_notificacao WHERE status IN ('enviado','sem_chat','erro') AND criado_em < now() - interval '90 days'")
    db.execute("DELETE FROM fila_sync WHERE status = 'concluido' AND concluido_em < now() - interval '30 days'")
