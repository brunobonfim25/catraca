"""Motor de regras — funções puras, sem banco. Quem busca os dados é acesso/service.py.

Entradas são dicts simples (linhas do banco) e datetimes já no fuso da escola.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..eventos import tipos


@dataclass
class Decisao:
    liberar: bool
    tipo: str                 # tipo de evento a registrar
    motivo: str = ""
    responsavel_id: int = None  # quem retirou (saída com responsável)
    espera_id: int = None
    detalhes: dict = field(default_factory=dict)


def classificar_entrada(turma, agora_local: datetime) -> str:
    """Dentro da janela + tolerância = ENTRADA; depois disso = ENTRADA_ATRASADA.
    Antes da janela também é ENTRADA (chegou cedo não é atraso)."""
    if not turma:
        return tipos.ENTRADA
    limite = datetime.combine(agora_local.date(), turma["entrada_fim"], tzinfo=agora_local.tzinfo)
    limite += timedelta(minutes=int(turma.get("tolerancia_entrada_min") or 0))
    return tipos.ENTRADA_ATRASADA if agora_local > limite else tipos.ENTRADA


def saida_autonoma_permitida(aluno, turma) -> bool:
    """Autorização individual dos pais vence a regra da turma."""
    if aluno.get("saida_autonoma") is not None:
        return bool(aluno["saida_autonoma"])
    return not bool(turma["exige_responsavel_saida"])


def dentro_da_janela_saida(turma, agora_local: datetime) -> bool:
    h = agora_local.time()
    return turma["saida_inicio"] <= h <= turma["saida_fim"]


def vinculo_vale_hoje(vinculo, agora_local: datetime, tem_aviso: bool) -> bool:
    """regra: sempre | dias (dias_semana, 0=domingo) | aviso (precisa de aviso prévio do dia)."""
    regra = vinculo.get("regra") or "sempre"
    if regra == "sempre":
        return True
    if regra == "dias":
        dow = (agora_local.weekday() + 1) % 7  # Python: segunda=0 → Postgres: domingo=0
        return dow in (vinculo.get("dias_semana") or [])
    if regra == "aviso":
        return tem_aviso
    return False


def decidir_saida_aluno(aluno, turma, vinculos, esperas, avisos_hoje, agora_local: datetime) -> Decisao:
    """vinculos: linhas de aluno_responsavel do aluno (com responsavel_id)
    esperas: responsavel_em_espera ativas da unidade (responsavel_id, id, expira_em)
    avisos_hoje: set de responsavel_id com aviso de retirada pra hoje"""
    por_resp = {v["responsavel_id"]: v for v in vinculos}
    # 1) Responsável autorizado esperando lá fora → sai com ele
    for espera in sorted(esperas, key=lambda e: e["identificado_em"]):
        v = por_resp.get(espera["responsavel_id"])
        if not v:
            continue
        if vinculo_vale_hoje(v, agora_local, espera["responsavel_id"] in avisos_hoje):
            return Decisao(True, tipos.SAIDA_ALUNO, "responsável em espera", responsavel_id=v["responsavel_id"], espera_id=espera["id"])
    # 2) Saída autônoma na janela
    if saida_autonoma_permitida(aluno, turma):
        if dentro_da_janela_saida(turma, agora_local):
            return Decisao(True, tipos.SAIDA_AUTONOMA, "saída autônoma na janela do turno")
        return Decisao(False, tipos.SAIDA_NEGADA, "saída autônoma fora da janela do turno")
    # 3) Nega
    if esperas and any(e["responsavel_id"] in por_resp for e in esperas):
        return Decisao(False, tipos.SAIDA_NEGADA, "responsável em espera não autorizado hoje")
    return Decisao(False, tipos.SAIDA_NEGADA, "nenhum responsável autorizado em espera")


def decidir_entrada_pessoa(pessoa, turma, agora_local: datetime) -> Decisao:
    if pessoa["status"] != "ativo":
        return Decisao(False, tipos.ACESSO_NEGADO, f"pessoa {pessoa['status']}")
    if pessoa["tipo"] == "aluno":
        return Decisao(True, classificar_entrada(turma, agora_local))
    if pessoa["tipo"] == "visitante":
        return Decisao(False, tipos.ACESSO_NEGADO, "visitante só entra por liberação manual")
    return Decisao(True, tipos.ENTRADA)


def funcionario_na_escala(escala, agora_local: datetime) -> bool:
    """escala: {"dias":[1..5], "inicio":"07:00", "fim":"18:00"} (dias no padrão 0=domingo).
    Escala vazia = sem restrição."""
    if not escala:
        return True
    dias = escala.get("dias")
    if dias:
        dow = (agora_local.weekday() + 1) % 7
        if dow not in dias:
            return False
    ini, fim = escala.get("inicio"), escala.get("fim")
    if ini and fim:
        h = agora_local.strftime("%H:%M")
        if not (ini <= h <= fim):
            return False
    return True
