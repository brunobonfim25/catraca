"""Código de instalação do servidor da escola.

O instalador (.exe) é o mesmo para todas as escolas — assim ele pode ser assinado e publicado
uma vez só. Quem diz de qual escola ele é o código curto que o painel gera: o instalador troca
o código pelo token do agente, e o código morre.

Antes o token ia dentro de um .cmd gerado por escola: o arquivo ficava esquecido na pasta
Downloads do PC da recepção e qualquer um que o copiasse virava o "agente" da escola.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json

from .. import db

# sem 0/O, 1/I/L: o código é ditado por telefone e digitado no PC da escola
ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
VALIDADE_HORAS = 24


def _hash(codigo):
    return hashlib.sha256(normalizar(codigo).encode()).hexdigest()


def normalizar(codigo):
    return "".join(c for c in (codigo or "").upper() if c.isalnum())


def gerar(unidade_id, usuario_id=None):
    """Novo código (o anterior deixa de valer). Só o hash fica no banco."""
    bruto = "".join(secrets.choice(ALFABETO) for _ in range(8))
    expira = datetime.now(timezone.utc) + timedelta(hours=VALIDADE_HORAS)
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s",
               (Json({"instalacao": {"hash": _hash(bruto), "expira": expira.isoformat(), "por": usuario_id}}), unidade_id), outbox=False)
    return f"{bruto[:4]}-{bruto[4:]}", expira


def _unidade(codigo):
    if len(normalizar(codigo)) != 8:
        return None
    un = db.query("SELECT * FROM unidades WHERE config->'instalacao'->>'hash' = %s", (_hash(codigo),), one=True)
    if not un:
        return None
    if datetime.fromisoformat(un["config"]["instalacao"]["expira"]) < datetime.now(timezone.utc):
        return None
    return un


def verificar(codigo):
    """Só confere (o instalador mostra "Escola: X" antes de instalar). Não consome o código."""
    return _unidade(codigo)


def ativar(codigo):
    """Consome o código e devolve (unidade, token). Token novo a cada ativação: o PC antigo
    (troca de máquina) para de sincronizar na hora, em vez de disputar a catraca com o novo."""
    un = _unidade(codigo)
    if not un:
        return None, None
    token = secrets.token_urlsafe(24)
    cfg = dict(un["config"] or {})
    cfg.pop("instalacao", None)
    # o agente novo tem outro id; sem limpar, a nuvem o recusaria por 5 min (um agente por escola)
    cfg.pop("agente_id", None)
    cfg["espelho_token"] = token
    n = db.execute("UPDATE unidades SET config = %s WHERE id = %s AND config->'instalacao'->>'hash' = %s",
                   (Json(cfg), un["id"], _hash(codigo)), outbox=False)
    if not n:  # duas ativações ao mesmo tempo com o mesmo código: só uma leva
        return None, None
    return un, token
