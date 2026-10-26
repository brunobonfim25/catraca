"""Motor de regras — funções puras, sem banco."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.acesso import regras
from app.eventos import tipos

FUSO = ZoneInfo("America/Sao_Paulo")
TURMA = {"exige_responsavel_saida": True, "tolerancia_entrada_min": 15, "entrada_inicio": time(6, 30), "entrada_fim": time(8, 0), "saida_inicio": time(16, 30), "saida_fim": time(18, 30)}


def seg(h, m=0):  # segunda-feira às h:m
    return datetime(2026, 10, 26, h, m, tzinfo=FUSO)


def test_entrada_dentro_da_tolerancia_nao_e_atraso():
    assert regras.classificar_entrada(TURMA, seg(7, 12)) == tipos.ENTRADA
    assert regras.classificar_entrada(TURMA, seg(8, 15)) == tipos.ENTRADA
    assert regras.classificar_entrada(TURMA, seg(5, 0)) == tipos.ENTRADA  # cedo não é atraso


def test_entrada_depois_da_tolerancia_e_atraso():
    assert regras.classificar_entrada(TURMA, seg(8, 16)) == tipos.ENTRADA_ATRASADA
    assert regras.classificar_entrada(TURMA, seg(10, 0)) == tipos.ENTRADA_ATRASADA


def test_vinculo_dias_usa_padrao_postgres_domingo_zero():
    v = {"regra": "dias", "dias_semana": [1, 3]}  # seg e qua
    assert regras.vinculo_vale_hoje(v, seg(10), False)
    assert not regras.vinculo_vale_hoje(v, seg(10) + timedelta(days=1), False)
    assert regras.vinculo_vale_hoje({"regra": "aviso"}, seg(10), True)
    assert not regras.vinculo_vale_hoje({"regra": "aviso"}, seg(10), False)
    assert regras.vinculo_vale_hoje({"regra": "sempre"}, seg(10), False)


def test_saida_com_responsavel_em_espera_libera():
    vinculos = [{"responsavel_id": 5, "regra": "sempre", "dias_semana": []}]
    esperas = [{"id": 9, "responsavel_id": 5, "identificado_em": seg(17)}]
    d = regras.decidir_saida_aluno({"saida_autonoma": None}, TURMA, vinculos, esperas, set(), seg(17, 5))
    assert d.liberar and d.tipo == tipos.SAIDA_ALUNO and d.responsavel_id == 5 and d.espera_id == 9


def test_saida_sem_responsavel_nega():
    vinculos = [{"responsavel_id": 5, "regra": "sempre", "dias_semana": []}]
    d = regras.decidir_saida_aluno({"saida_autonoma": None}, TURMA, vinculos, [], set(), seg(17, 5))
    assert not d.liberar and d.tipo == tipos.SAIDA_NEGADA
    # responsável de OUTRO aluno esperando não serve
    d = regras.decidir_saida_aluno({"saida_autonoma": None}, TURMA, vinculos, [{"id": 1, "responsavel_id": 99, "identificado_em": seg(17)}], set(), seg(17, 5))
    assert not d.liberar


def test_responsavel_so_com_aviso_precisa_do_aviso_do_dia():
    vinculos = [{"responsavel_id": 7, "regra": "aviso", "dias_semana": []}]
    esperas = [{"id": 1, "responsavel_id": 7, "identificado_em": seg(17)}]
    assert not regras.decidir_saida_aluno({"saida_autonoma": None}, TURMA, vinculos, esperas, set(), seg(17)).liberar
    assert regras.decidir_saida_aluno({"saida_autonoma": None}, TURMA, vinculos, esperas, {7}, seg(17)).liberar


def test_saida_autonoma_so_na_janela():
    livre = {**TURMA, "exige_responsavel_saida": False}
    assert regras.decidir_saida_aluno({"saida_autonoma": None}, livre, [], [], set(), seg(17)).tipo == tipos.SAIDA_AUTONOMA
    assert regras.decidir_saida_aluno({"saida_autonoma": None}, livre, [], [], set(), seg(14)).tipo == tipos.SAIDA_NEGADA
    # autorização individual vence a turma
    assert regras.decidir_saida_aluno({"saida_autonoma": True}, TURMA, [], [], set(), seg(17)).liberar
    assert not regras.decidir_saida_aluno({"saida_autonoma": False}, livre, [], [], set(), seg(17)).liberar


def test_responsavel_em_espera_vence_saida_autonoma():
    livre = {**TURMA, "exige_responsavel_saida": False}
    vinculos = [{"responsavel_id": 5, "regra": "sempre", "dias_semana": []}]
    d = regras.decidir_saida_aluno({"saida_autonoma": None}, livre, vinculos, [{"id": 2, "responsavel_id": 5, "identificado_em": seg(17)}], set(), seg(17))
    assert d.tipo == tipos.SAIDA_ALUNO


def test_escala_funcionario():
    assert regras.funcionario_na_escala({}, seg(3))
    esc = {"dias": [1, 2, 3, 4, 5], "inicio": "07:00", "fim": "18:00"}
    assert regras.funcionario_na_escala(esc, seg(7, 30))
    assert not regras.funcionario_na_escala(esc, seg(19))
    assert not regras.funcionario_na_escala(esc, seg(10) + timedelta(days=5))  # sábado


def test_entrada_pessoa_bloqueada_ou_visitante_nega():
    assert not regras.decidir_entrada_pessoa({"status": "bloqueado", "tipo": "aluno"}, TURMA, seg(7)).liberar
    assert not regras.decidir_entrada_pessoa({"status": "ativo", "tipo": "visitante"}, None, seg(7)).liberar
    assert regras.decidir_entrada_pessoa({"status": "ativo", "tipo": "funcionario"}, None, seg(7)).tipo == tipos.ENTRADA
