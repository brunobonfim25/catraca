"""Decisão de acesso com dados do banco (usada pelo modo Pro e pela ingestão de eventos)."""
from datetime import date

from .. import db
from ..eventos import tipos
from ..util import agora_utc, para_local
from . import regras


def carregar_contexto_pessoa(pessoa_id):
    """Pessoa + (aluno, turma) ou (funcionario) ou (responsavel) numa consulta por tipo."""
    p = db.query("SELECT * FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    if not p:
        return None
    ctx = {"pessoa": p, "aluno": None, "turma": None, "funcionario": None, "responsavel": None}
    if p["tipo"] == "aluno":
        ctx["aluno"] = db.query("SELECT * FROM alunos WHERE pessoa_id = %s", (pessoa_id,), one=True)
        if ctx["aluno"]:
            ctx["turma"] = db.query("SELECT * FROM turmas WHERE id = %s", (ctx["aluno"]["turma_id"],), one=True)
    elif p["tipo"] == "funcionario":
        ctx["funcionario"] = db.query("SELECT * FROM funcionarios WHERE pessoa_id = %s", (pessoa_id,), one=True)
    elif p["tipo"] == "responsavel":
        ctx["responsavel"] = db.query("SELECT * FROM responsaveis WHERE pessoa_id = %s", (pessoa_id,), one=True)
    return ctx


def esperas_ativas(unidade_id):
    return db.query(
        "SELECT e.*, p.nome AS responsavel_nome FROM responsavel_em_espera e "
        "JOIN responsaveis r ON r.id = e.responsavel_id JOIN pessoas p ON p.id = r.pessoa_id "
        "WHERE e.unidade_id = %s AND e.consumido_em IS NULL AND e.expira_em > now() ORDER BY e.identificado_em",
        (unidade_id,),
    )


def avisos_do_dia(aluno_id, dia: date):
    rows = db.query("SELECT responsavel_id FROM avisos_retirada WHERE aluno_id = %s AND data = %s", (aluno_id, dia))
    return {r["responsavel_id"] for r in rows}


def decidir(pessoa_id, sentido, unidade, quando=None):
    """Decisão completa pra uma identificação. Devolve regras.Decisao (não grava nada)."""
    ctx = carregar_contexto_pessoa(pessoa_id)
    if ctx is None:
        return regras.Decisao(False, tipos.ACESSO_NEGADO, "pessoa desconhecida")
    agora_local = para_local(quando or agora_utc(), unidade["fuso"])
    p = ctx["pessoa"]
    if p["status"] != "ativo":
        return regras.Decisao(False, tipos.ACESSO_NEGADO, f"pessoa {p['status']}")

    if sentido == "entrada":
        if p["tipo"] == "funcionario" and ctx["funcionario"] and not regras.funcionario_na_escala(ctx["funcionario"]["escala"], agora_local):
            return regras.Decisao(False, tipos.ACESSO_NEGADO, "fora da escala")
        return regras.decidir_entrada_pessoa(p, ctx["turma"], agora_local)

    # saída
    if p["tipo"] == "aluno" and ctx["aluno"] and ctx["turma"]:
        vinculos = db.query("SELECT * FROM aluno_responsavel WHERE aluno_id = %s", (ctx["aluno"]["id"],))
        esperas = esperas_ativas(unidade["id"])
        avisos = avisos_do_dia(ctx["aluno"]["id"], agora_local.date())
        return regras.decidir_saida_aluno(ctx["aluno"], ctx["turma"], vinculos, esperas, avisos, agora_local)
    return regras.Decisao(True, tipos.SAIDA)
