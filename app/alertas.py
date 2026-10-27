"""Sinal de vida dos processos e alertas para a Nexosix (revisão de 27/10/2026, O2).

Antes nada avisava: servidor da escola parado, catraca offline, Telegram falhando — só
descobria quem olhasse o painel. O worker da nuvem roda `verificar()` a cada minuto e manda
para ALERTA_TELEGRAM_CHAT (o chat do Bruno no bot) quando um problema começa e quando acaba.
"""
import logging
import os
from datetime import datetime, timezone

from psycopg2.extras import Json

from . import db

log = logging.getLogger(__name__)

AGENTE_MUDO_SEG = 300
CATRACA_FORA_SEG = 600
WORKER_MUDO_SEG = 180


def batimento(nome):
    db.execute("INSERT INTO sistema_status (chave, valor, atualizado_em) VALUES (%s, '{}', now()) "
               "ON CONFLICT (chave) DO UPDATE SET atualizado_em = now()", (f"worker:{nome}",), outbox=False)


def worker_visto(nome="nuvem"):
    r = db.query("SELECT atualizado_em FROM sistema_status WHERE chave = %s", (f"worker:{nome}",), one=True)
    return r["atualizado_em"] if r else None


def worker_ok(nome="nuvem"):
    visto = worker_visto(nome)
    return bool(visto and (datetime.now(timezone.utc) - visto).total_seconds() < WORKER_MUDO_SEG)


def problemas():
    """{chave: texto} de tudo que está errado agora."""
    out = {}
    for u in db.query("SELECT * FROM unidades WHERE ativo AND config ? 'espelho_token'"):
        visto = (u["config"] or {}).get("agente_visto_em")
        if visto and (datetime.now(timezone.utc) - datetime.fromisoformat(visto)).total_seconds() > AGENTE_MUDO_SEG:
            out[f"agente:{u['id']}"] = f"Servidor da escola {u['nome']} sem sinal desde {visto[11:16]} UTC"
        ag = (u["config"] or {}).get("agente") or {}
        if ag.get("web_ok") is False:
            out[f"agente_web:{u['id']}"] = f"Site do servidor da escola {u['nome']} fora do ar (a catraca não consegue falar com ele)"
    for d in db.query("SELECT d.id, d.nome, d.status, d.ultimo_contato, u.nome AS escola FROM dispositivos d JOIN unidades u ON u.id = d.unidade_id "
                      "WHERE d.ativo AND d.status IN ('offline','contingencia') AND (d.ultimo_contato IS NULL OR d.ultimo_contato < now() - make_interval(secs => %s))",
                      (CATRACA_FORA_SEG,)):
        out[f"catraca:{d['id']}"] = f"Catraca {d['nome']} ({d['escola']}) {d['status']}"
    n = db.query("SELECT count(*) AS n FROM fila_notificacao WHERE status = 'erro' AND criado_em > now() - interval '1 hour'", one=True)["n"]
    if n >= 10:
        out["telegram"] = f"{n} avisos aos pais falharam na última hora"
    for q in db.query("SELECT unidade_id, count(*) AS n FROM espelho_quarentena GROUP BY unidade_id"):
        out[f"quarentena:{q['unidade_id']}"] = f"{q['n']} alteração(ões) da escola {q['unidade_id']} em quarentena na sincronização"
    return out


def verificar(enviar=None):
    """Manda o que começou e o que se resolveu desde a última volta. Devolve as mensagens."""
    chat = os.environ.get("ALERTA_TELEGRAM_CHAT", "")
    agora = problemas()
    antes = {r["chave"][7:]: r["valor"] for r in db.query("SELECT chave, valor FROM sistema_status WHERE chave LIKE 'alerta:%%'")}
    msgs = []
    for k, texto in agora.items():
        if k not in antes:
            msgs.append(f"⚠️ {texto}")
            db.execute("INSERT INTO sistema_status (chave, valor) VALUES (%s, %s) ON CONFLICT (chave) DO NOTHING",
                       (f"alerta:{k}", Json({"texto": texto})), outbox=False)
    for k, valor in antes.items():
        if k not in agora:
            msgs.append(f"✅ Resolvido: {valor.get('texto', k)}")
            db.execute("DELETE FROM sistema_status WHERE chave = %s", (f"alerta:{k}",), outbox=False)
    if msgs and chat:
        from .notificacoes import telegram

        try:
            (enviar or telegram.enviar)(chat, "Nexosix Acesso\n" + "\n".join(msgs))
        except Exception as e:  # noqa: BLE001
            log.warning("alerta não enviado: %s", e)
    for m in msgs:
        log.warning("alerta: %s", m)
    return msgs
