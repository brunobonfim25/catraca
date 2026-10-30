"""Importação de planilha: leitura, mapeamento adivinhado, prévia, gravação e reimportação."""
import io

import openpyxl
import pytest

from app import db
from app.pessoas import importacao as imp


def _xlsx(linhas, aba="Planilha1"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = aba
    for ln in linhas:
        ws.append(ln)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# formato típico de export de sistema escolar: maiúsculas, mãe/pai em colunas próprias, título antes do cabeçalho
ACTIVESOFT = [
    ["RELAÇÃO DE ALUNOS 2026"],
    ["Nome do Aluno", "Série/Turma", "Data de Nascimento", "Nome da Mãe", "Celular da Mãe", "E-mail da Mãe", "Nome do Pai", "Celular do Pai"],
    ["LAURA MENDES DA COSTA", "Pré 2", "03/04/2021", "CARLA MENDES", "(21) 98888-0001", "carla@exemplo.com", "RAFAEL COSTA", 21977770002],
    ["PEDRO MENDES DA COSTA", "2º ano", "10/09/2018", "CARLA MENDES", "21988880001", "", "RAFAEL COSTA", "21 97777-0002"],
    ["", "Pré 2", "", "FULANA", "", "", "", ""],
]


def test_le_xlsx_pula_titulo_e_adivinha_colunas_de_maes_e_pais():
    cab, linhas, inicio = imp.ler_arquivo("alunos.xlsx", _xlsx(ACTIVESOFT))
    assert cab[0] == "Nome do Aluno" and inicio == 3
    m = imp.adivinhar_mapeamento(cab)
    assert (m["aluno_nome"], m["turma"], m["matricula"]) == (0, 1, None)
    assert m["resp"][0] == {"nome": 3, "parentesco": None, "telefone": 4, "email": 5, "padrao": "mãe"}
    assert m["resp"][1] == {"nome": 6, "parentesco": None, "telefone": 7, "email": None, "padrao": "pai"}
    assert imp.faltando(m) == []


def test_modelo_da_nexosix_e_reconhecido_inteiro():
    cab, _, _ = imp.ler_arquivo("modelo.xlsx", _xlsx([imp.MODELO_CABECALHO, imp.EXEMPLO[0]]))
    m = imp.adivinhar_mapeamento(cab)
    assert [m[c] for c in ("aluno_nome", "turma", "turno", "matricula")] == [0, 1, 2, 3]
    for s in range(3):
        assert m["resp"][s] == {"nome": 4 + 4 * s, "parentesco": 5 + 4 * s, "telefone": 6 + 4 * s, "email": 7 + 4 * s, "padrao": ""}


def test_modelo_xlsx_vazio_e_lido_pela_aba_alunos():
    with pytest.raises(imp.PlanilhaInvalida, match="só tem o cabeçalho"):
        imp.ler_arquivo("modelo.xlsx", imp.modelo_xlsx())  # aba Exemplo não conta


def test_csv_do_excel_em_portugues_ponto_e_virgula_cp1252():
    conteudo = "Aluno;Turma;Responsável;Celular\nJoão Antônio;Pré 1;Márcia;(21) 99999-1234\n".encode("cp1252")
    cab, linhas, _ = imp.ler_arquivo("x.csv", conteudo)
    regs = imp.processar(linhas, imp.adivinhar_mapeamento(cab))
    assert regs[0]["aluno"]["nome"] == "João Antônio" and regs[0]["responsaveis"][0]["telefone"] == "21999991234"


def test_processar_junta_mae_dos_irmaos_e_aponta_linha_com_erro():
    cab, linhas, inicio = imp.ler_arquivo("alunos.xlsx", _xlsx(ACTIVESOFT))
    regs = imp.processar(linhas, imp.adivinhar_mapeamento(cab), inicio)
    validos = [r for r in regs if not r["erros"]]
    assert [r["aluno"]["nome"] for r in validos] == ["Laura Mendes da Costa", "Pedro Mendes da Costa"]  # MAIÚSCULAS → nome próprio
    assert validos[0]["responsaveis"][0] == {"nome": "Carla Mendes", "telefone": "21988880001", "email": "carla@exemplo.com", "parentesco": "mãe"}
    assert validos[1]["responsaveis"][1]["telefone"] == "21977770002"  # celular digitado como número no Excel
    erro = next(r for r in regs if r["erros"])
    assert erro["linhas"] == [5] and erro["erros"] == ["sem nome do aluno"]


def test_planejar_e_executar_cria_turmas_alunos_e_uma_mae_para_dois_irmaos(seed):
    cab, linhas, inicio = imp.ler_arquivo("alunos.xlsx", _xlsx(ACTIVESOFT))
    regs = imp.processar(linhas, imp.adivinhar_mapeamento(cab), inicio)
    un = seed["unidade"]["id"]
    plano = imp.planejar(un, regs)
    r = plano["resumo"]
    assert (r["alunos_novos"], r["responsaveis_novos"], r["vinculos_novos"], r["linhas_com_erro"]) == (2, 2, 4, 1)
    assert r["turmas_novas"] == ["Pré 2 (manhã)", "2º Ano (manhã)"]
    assert db.query("SELECT count(*) AS n FROM turmas WHERE unidade_id = %s", (un,), one=True)["n"] == 2  # prévia não grava

    resumo = imp.executar(un, regs, {"assinado_por": "Responsável (termo da matrícula)", "observacao": "teste"})
    assert resumo["alunos_novos"] == 2
    carla = db.query("SELECT r.id, r.email FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE p.nome = 'Carla Mendes'")
    assert len(carla) == 1 and carla[0]["email"] == "carla@exemplo.com"
    assert db.query("SELECT count(*) AS n FROM aluno_responsavel WHERE responsavel_id = %s", (carla[0]["id"],), one=True)["n"] == 2
    assert db.query("SELECT count(*) AS n FROM consentimentos_lgpd WHERE canal = 'importacao'", one=True)["n"] == 4  # 2 alunos + 2 responsáveis novos

    # reimportar a mesma planilha não duplica nada
    de_novo = imp.planejar(un, regs)["resumo"]
    assert (de_novo["alunos_novos"], de_novo["responsaveis_novos"], de_novo["vinculos_novos"], de_novo["turmas_novas"]) == (0, 0, 0, [])
    assert de_novo["alunos_existentes"] == 2 and de_novo["responsaveis_existentes"] == 2


def test_reconhece_aluno_e_responsavel_ja_cadastrados_e_mudanca_de_turma(seed):
    """Maria (matrícula 2026001, Pré 1) e Ana (mesmo celular com outra máscara) já existem."""
    linhas = [["Maria Silva", "5º ano", "2026001", "Ana Silva", "mãe", "+55 (21) 99999-0000", "ana@exemplo.com"],
              ["Bia Silva", "Pré 1", "", "Ana S.", "mãe", "21 99999-0000", ""]]
    mapa = imp.adivinhar_mapeamento(["Aluno", "Turma", "Matrícula", "Responsável", "Parentesco", "Celular", "E-mail"])
    regs = imp.processar(linhas, mapa)
    un = seed["unidade"]["id"]
    r = imp.planejar(un, regs)["resumo"]
    assert (r["alunos_novos"], r["alunos_existentes"], r["mudam_turma"], r["responsaveis_novos"], r["responsaveis_existentes"]) == (1, 1, 1, 0, 1)
    assert r["vinculos_novos"] == 1  # só Bia ↔ Ana; Maria ↔ Ana já existia
    imp.executar(un, regs)
    maria = db.query("SELECT turma_id FROM alunos WHERE id = %s", (seed["maria"]["aluno_id"],), one=True)
    assert maria["turma_id"] == seed["turma_livre"]["id"]
    ana = db.query("SELECT email, telefone FROM responsaveis WHERE id = %s", (seed["ana"]["responsavel_id"],), one=True)
    assert ana == {"email": "ana@exemplo.com", "telefone": "21999990000"}  # completa o e-mail, não troca o celular
    # mudança de turma muda a regra de saída na catraca → set_rules na fila
    assert db.query("SELECT 1 FROM fila_sync WHERE pessoa_id = %s AND tipo = 'set_rules' AND status = 'pendente'", (seed["maria"]["pessoa_id"],), one=True)


def test_falha_no_meio_nao_deixa_importacao_pela_metade(seed, monkeypatch):
    from app.pessoas import service

    regs = imp.processar([["A Um", "Turma X", "", "Resp Um", "", "21911110001", ""], ["B Dois", "Turma X", "", "Resp Dois", "", "21911110002", ""]],
                         imp.adivinhar_mapeamento(["Aluno", "Turma", "Matrícula", "Responsável", "Parentesco", "Celular", "E-mail"]))
    original, chamadas = service.criar_aluno, []

    def quebra_no_segundo(*a, **k):
        chamadas.append(1)
        if len(chamadas) == 2:
            raise RuntimeError("banco caiu")
        return original(*a, **k)

    monkeypatch.setattr(service, "criar_aluno", quebra_no_segundo)
    with pytest.raises(RuntimeError):
        imp.executar(seed["unidade"]["id"], regs)
    assert db.query("SELECT count(*) AS n FROM turmas WHERE nome = 'Turma X'", one=True)["n"] == 0
    assert db.query("SELECT count(*) AS n FROM pessoas WHERE nome IN ('A Um', 'Resp Um')", one=True)["n"] == 0


def test_fluxo_pelas_telas(cliente, seed, app):
    r = cliente.get("/pessoas/importar/modelo.xlsx")
    assert r.status_code == 200 and r.data[:2] == b"PK"
    r = cliente.post("/pessoas/importar", data={"arquivo": (io.BytesIO(_xlsx(ACTIVESOFT)), "alunos.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 302
    url = r.headers["Location"]
    html = cliente.get(url).get_data(as_text=True)
    assert "Conferir importação" in html and "Importar 2 alunos" in html and "sem nome do aluno" in html
    imp_id = int(url.rstrip("/").split("/")[-1])
    # secretaria corrige o mapeamento: tira o pai
    form = {"m_aluno_nome": "0", "m_turma": "1", "m_r0_nome": "3", "m_r0_telefone": "4", "m_r0_email": "5", "m_r0_padrao": "mãe"}
    cliente.post(f"/pessoas/importar/{imp_id}/mapeamento", data=form)
    assert db.query("SELECT mapeamento FROM importacoes WHERE id = %s", (imp_id,), one=True)["mapeamento"]["resp"][1]["nome"] is None
    r = cliente.post(f"/pessoas/importar/{imp_id}/confirmar", data={})
    assert r.status_code == 302
    linha = db.query("SELECT status, linhas, resultado FROM importacoes WHERE id = %s", (imp_id,), one=True)
    assert linha["status"] == "importada" and linha["linhas"] == [] and linha["resultado"]["responsaveis_novos"] == 1
    assert "Importação concluída" in cliente.get(url).get_data(as_text=True)
    cliente.post(f"/pessoas/importar/{imp_id}/confirmar", data={})  # clique duplo não importa de novo
    assert db.query("SELECT count(*) AS n FROM pessoas WHERE nome = 'Laura Mendes da Costa'", one=True)["n"] == 1


def test_recepcao_nao_importa(app, seed):
    c = app.test_client()
    c.post("/login", data={"email": "recepcao@teste.com", "senha": "senha123"})
    assert c.get("/pessoas/importar").status_code == 403


# export do sistema da escola (30/10): "filiação 1/2", turma como caminho, turno "Manhã/Tarde"
ESCOLA = [
    ["Nome do aluno", "Matrícula", "Nome da filiação 1", "Celular da filiação 1", "E-mail da filiação 1", "Nome da filiação 2", "E-mail da filiação 2", "Celular da filiação 2", "Turma", "Turno principal"],
    ["Ana Exemplo", "289'", "Carla Exemplo", "21 98888-0001", "carla@exemplo.com", "Rafael Exemplo", "", "", "Educação Infantil / Pré 1 ( 4anos ) / 2026 / PRÉ 1 M", "Manhã"],
    ["Bento Exemplo", "290", "Carla Exemplo", "21988880001", "", "", "", "", "Educação Infantil / Berçário 2 (13 a 23 meses) / 2026 / BERÇÁRIO 2", "Manhã/Tarde"],
]


def test_export_do_sistema_da_escola_e_reconhecido():
    cab, linhas, ini = imp.ler_arquivo("alunos.xlsx", _xlsx(ESCOLA))
    m = imp.adivinhar_mapeamento(cab)
    assert m["aluno_nome"] == 0 and m["turma"] == 8 and m["turno"] == 9 and m["matricula"] == 1
    r1, r2 = m["resp"][0], m["resp"][1]
    assert (r1["nome"], r1["telefone"], r1["email"]) == (2, 3, 4) and (r2["nome"], r2["telefone"], r2["email"]) == (5, 7, 6)
    regs = imp.processar(linhas, m, ini)
    assert [r["aluno"]["turma"] for r in regs] == ["Pré 1 M", "Berçário 2"]
    assert [imp._turma(x) for x in ("Ensino Fundamental / 1º Ano / 2026 / 1° ANO M", "Ensino Fundamental / 3° Ano / 2026 / 3º Ano M", "Ensino Fundamental / 5° ano / 2026 / 5º ano T")] == ["1º Ano M", "3º Ano M", "5º Ano T"]
    assert regs[0]["aluno"]["matricula"] == "289"
    assert regs[1]["aluno"]["turno"] == "integral" and not regs[1]["avisos"]
    assert [x["nome"] for x in regs[0]["responsaveis"]] == ["Carla Exemplo", "Rafael Exemplo"]
