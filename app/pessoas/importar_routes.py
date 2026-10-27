"""Telas da importação de planilha: enviar → conferir colunas e prévia → importar."""
import json

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

from .. import db
from ..auditoria import auditar
from ..auth.rbac import exige, unidade_atual_ou_403
from . import importacao
from .routes import bp

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _importacao(importacao_id):
    un = unidade_atual_ou_403()
    imp = db.query("SELECT * FROM importacoes WHERE id = %s AND unidade_id = %s", (importacao_id, un["id"]), one=True)
    if not imp:
        abort(404)
    return un, imp


@bp.get("/importar")
@exige("gerir_turmas")
def importar():
    un = unidade_atual_ou_403()
    anteriores = db.query(
        "SELECT i.id, i.arquivo_nome, i.status, i.criado_em, i.importado_em, i.resultado, u.nome AS usuario_nome FROM importacoes i "
        "LEFT JOIN usuarios_admin u ON u.id = i.usuario_id WHERE i.unidade_id = %s ORDER BY i.criado_em DESC LIMIT 10", (un["id"],),
    )
    return render_template("pessoas/importar.html", anteriores=anteriores, fuso=un["fuso"])


@bp.get("/importar/modelo.xlsx")
@exige("gerir_turmas")
def importar_modelo():
    return Response(importacao.modelo_xlsx(), mimetype=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="modelo-cadastro-alunos.xlsx"'})


@bp.post("/importar")
@exige("gerir_turmas")
def importar_enviar():
    un = unidade_atual_ou_403()
    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        flash("Escolha o arquivo da planilha", "danger")
        return redirect(url_for("pessoas.importar"))
    try:
        cab, linhas, inicio = importacao.ler_arquivo(arq.filename, arq.read())
    except importacao.PlanilhaInvalida as e:
        flash(f"Não deu para ler a planilha: {e}", "danger")
        return redirect(url_for("pessoas.importar"))
    mapa = importacao.adivinhar_mapeamento(cab)
    novo = db.execute(
        "INSERT INTO importacoes (unidade_id, usuario_id, arquivo_nome, cabecalho, linhas, linha_inicial, mapeamento) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (un["id"], g.usuario["id"], arq.filename[:200], json.dumps(cab), json.dumps(linhas), inicio, json.dumps(mapa)), returning=True,
    )
    return redirect(url_for("pessoas.importar_conferir", importacao_id=novo["id"]))


@bp.get("/importar/<int:importacao_id>")
@exige("gerir_turmas")
def importar_conferir(importacao_id):
    un, imp = _importacao(importacao_id)
    if imp["status"] == "importada":
        sem_foto = db.query(
            "SELECT count(*) AS n FROM pessoas p LEFT JOIN pessoa_fotos f ON f.pessoa_id = p.id "
            "WHERE p.unidade_id = %s AND p.status = 'ativo' AND p.tipo IN ('aluno','responsavel') AND f.pessoa_id IS NULL", (un["id"],), one=True,
        )["n"]
        return render_template("pessoas/importar_resultado.html", imp=imp, r=imp["resultado"] or {}, sem_foto=sem_foto, fuso=un["fuso"])
    mapa = imp["mapeamento"]
    registros = importacao.processar(imp["linhas"], mapa, imp["linha_inicial"])
    plano = importacao.planejar(un["id"], registros)
    # amostra de cada coluna ajuda a secretaria a reconhecer o que é cada uma
    amostras = []
    for k in range(len(imp["cabecalho"])):
        amostras.append(next((ln[k] for ln in imp["linhas"] if k < len(ln) and ln[k]), ""))
    return render_template(
        "pessoas/importar_conferir.html", imp=imp, mapa=mapa, plano=plano, r=plano["resumo"], amostras=amostras,
        faltando=importacao.faltando(mapa), campos_aluno=importacao.CAMPOS_ALUNO, campos_resp=importacao.CAMPOS_RESP,
    )


@bp.post("/importar/<int:importacao_id>/mapeamento")
@exige("gerir_turmas")
def importar_mapear(importacao_id):
    un, imp = _importacao(importacao_id)
    if imp["status"] != "rascunho":
        return redirect(url_for("pessoas.importar_conferir", importacao_id=importacao_id))
    mapa = importacao.mapeamento_do_form(request.form, len(imp["cabecalho"]))
    db.execute("UPDATE importacoes SET mapeamento = %s WHERE id = %s", (json.dumps(mapa), importacao_id))
    return redirect(url_for("pessoas.importar_conferir", importacao_id=importacao_id))


@bp.post("/importar/<int:importacao_id>/confirmar")
@exige("gerir_turmas")
def importar_confirmar(importacao_id):
    un, imp = _importacao(importacao_id)
    if importacao.faltando(imp["mapeamento"]):
        flash("Escolha as colunas de Nome do aluno e Turma antes de importar", "danger")
        return redirect(url_for("pessoas.importar_conferir", importacao_id=importacao_id))
    consentimento = None
    if request.form.get("consentimento") == "1":
        consentimento = {"assinado_por": "Responsável (termo assinado na matrícula)",
                         "observacao": f"Declarado por {g.usuario['nome']} ao importar a planilha {imp['arquivo_nome']} (importação {importacao_id})"}
    with db.lote():
        # trava a linha: dois cliques em "Importar" não importam duas vezes
        atual = db.query("SELECT status FROM importacoes WHERE id = %s FOR UPDATE", (importacao_id,), one=True)
        if atual["status"] != "rascunho":
            return redirect(url_for("pessoas.importar_conferir", importacao_id=importacao_id))
        registros = importacao.processar(imp["linhas"], imp["mapeamento"], imp["linha_inicial"])
        resumo = importacao.executar(un["id"], registros, consentimento)
        resumo["consentimento"] = bool(consentimento)
        # a planilha crua (nomes, celulares) não fica guardada depois de importada — LGPD: o dado vive no cadastro
        db.execute("UPDATE importacoes SET status = 'importada', resultado = %s, importado_em = now(), linhas = '[]' WHERE id = %s",
                   (json.dumps(resumo, ensure_ascii=False), importacao_id))
        auditar("importar_planilha", "importacao", importacao_id, depois=resumo, unidade_id=un["id"])
    flash(f"Importação concluída: {resumo['alunos_novos']} alunos e {resumo['responsaveis_novos']} responsáveis novos.", "success")
    return redirect(url_for("pessoas.importar_conferir", importacao_id=importacao_id))


@bp.post("/importar/<int:importacao_id>/descartar")
@exige("gerir_turmas")
def importar_descartar(importacao_id):
    un, imp = _importacao(importacao_id)
    if imp["status"] == "rascunho":
        db.execute("DELETE FROM importacoes WHERE id = %s", (importacao_id,))
    return redirect(url_for("pessoas.importar"))
