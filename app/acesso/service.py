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


def esperas_ativas(unidade_id, quando=None):
    """Esperas válidas no instante `quando` (hora da identificação; logs coletados depois de
    uma queda usam a hora real do evento, não a de agora)."""
    return db.query(
        "SELECT e.*, p.nome AS responsavel_nome FROM responsavel_em_espera e "
        "JOIN responsaveis r ON r.id = e.responsavel_id JOIN pessoas p ON p.id = r.pessoa_id "
        # responsável bloqueado ou desligado depois de chegar não retira ninguém (revisão 27/10)
        "WHERE e.unidade_id = %s AND e.consumido_em IS NULL AND e.expira_em > %s AND e.identificado_em <= %s AND p.status = 'ativo' "
        "ORDER BY e.identificado_em",
        (unidade_id, quando or agora_utc(), quando or agora_utc()),
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
    if p["tipo"] == "aluno" and not (ctx["aluno"] and ctx["turma"]):
        # cadastro pela metade não pode virar "sai livre" (antes caía no Decisao(True) do fim)
        return regras.Decisao(False, tipos.SAIDA_NEGADA, "cadastro do aluno incompleto (sem turma)")
    if p["tipo"] == "aluno":
        vinculos = db.query("SELECT * FROM aluno_responsavel WHERE aluno_id = %s", (ctx["aluno"]["id"],))
        esperas = esperas_ativas(unidade["id"], quando or agora_utc())
        # Espera anterior à entrada do aluno não vale: era a mãe DEIXANDO a criança de manhã.
        # Sem isso a Maria saía sozinha às 7:05 "com a mãe", que já tinha ido embora.
        pr = db.query("SELECT dentro, desde FROM presenca WHERE pessoa_id = %s", (pessoa_id,), one=True)
        if pr and pr["dentro"]:
            esperas = [e for e in esperas if e["identificado_em"] >= pr["desde"]]
        avisos = avisos_do_dia(ctx["aluno"]["id"], agora_local.date())
        return regras.decidir_saida_aluno(ctx["aluno"], ctx["turma"], vinculos, esperas, avisos, agora_local)
    return regras.Decisao(True, tipos.SAIDA)
