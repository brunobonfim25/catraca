"""Buscar um objeto SÓ da escola de quem está logado.

Revisão de 27/10/2026: quase todas as falhas críticas de acesso entre escolas eram a mesma —
a rota recebia um id do formulário (aluno, responsável, catraca) e usava sem conferir a escola.
Com ids sequenciais, a recepção da escola A liberava a catraca da B, vinculava um "responsável"
a uma criança da B, pegava o QR de ativação de uma mãe da B. Toda rota que recebe id usa isto.
"""
from flask import abort

from . import db
from .auth.rbac import unidade_atual_ou_403

_SQL = {
    "pessoa": "SELECT p.* FROM pessoas p WHERE p.id = %s AND p.unidade_id = %s",
    "aluno": "SELECT a.*, p.nome, p.status, p.unidade_id FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id "
             "WHERE a.id = %s AND p.unidade_id = %s",
    "responsavel": "SELECT r.*, p.nome, p.status, p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                   "WHERE r.id = %s AND p.unidade_id = %s",
    "dispositivo": "SELECT * FROM dispositivos WHERE id = %s AND unidade_id = %s",
    "turma": "SELECT * FROM turmas WHERE id = %s AND unidade_id = %s",
    "espera": "SELECT * FROM responsavel_em_espera WHERE id = %s AND unidade_id = %s",
}


def da_unidade(tipo, id_, unidade_id=None):
    """Linha do `tipo` com esse id se for da escola atual (ou da `unidade_id` dada); senão 404."""
    if unidade_id is None:
        unidade_id = unidade_atual_ou_403()["id"]
    try:
        id_ = int(id_)
    except (TypeError, ValueError):
        abort(404)
    linha = db.query(_SQL[tipo], (id_, unidade_id), one=True)
    if linha is None:
        abort(404)
    return linha


def pertence(tipo, id_, unidade_id):
    """Mesma checagem sem abortar (para services, fora de requisição)."""
    try:
        return db.query(_SQL[tipo], (int(id_), unidade_id), one=True) is not None
    except (TypeError, ValueError):
        return False
