"""Trilha de auditoria: quem fez o quê, antes/depois em jsonb."""
import json
from datetime import date, datetime
from decimal import Decimal

from flask import g, has_request_context

from . import db


def _serializavel(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        return "<binário>"
    return str(v)


def _json(d):
    if d is None:
        return None
    return json.dumps(d, default=_serializavel, ensure_ascii=False)


def auditar(acao, entidade, entidade_id=None, antes=None, depois=None, unidade_id=None, usuario_id=None):
    if has_request_context():
        usuario = getattr(g, "usuario", None)
        unidade = getattr(g, "unidade", None)
        usuario_id = usuario_id or (usuario["id"] if usuario else None)
        unidade_id = unidade_id or (unidade["id"] if unidade else None)
    db.execute(
        "INSERT INTO auditoria (unidade_id, usuario_id, acao, entidade, entidade_id, antes, depois) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (unidade_id, usuario_id, acao, entidade, str(entidade_id) if entidade_id is not None else None, _json(antes), _json(depois)),
    )
