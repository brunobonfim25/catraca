"""Telas da secretaria: alunos, responsáveis, funcionários, visitantes, foto por webcam."""
import base64

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import db
from ..auditoria import auditar
from ..auth.rbac import exige, unidade_atual_ou_403
from ..dispositivos import service as dispositivos
from ..escopo import da_unidade
from ..eventos import tipos
from ..util import hoje_local
from . import service

bp = Blueprint("pessoas", __name__, url_prefix="/pessoas")

DIAS = [(1, "Seg"), (2, "Ter"), (3, "Qua"), (4, "Qui"), (5, "Sex"), (6, "Sáb"), (0, "Dom")]


def _pessoa_da_unidade(pessoa_id):
    un = unidade_atual_ou_403()
    p = service.detalhe(pessoa_id)
    if not p or p["unidade_id"] != un["id"]:
        abort(404)
    return un, p


def _turmas(unidade_id):
    return db.query("SELECT * FROM turmas WHERE unidade_id = %s AND ativo ORDER BY nome", (unidade_id,))


def _tri(valor):
    """select 'turma' | 'sim' | 'nao' → None/True/False"""
    return {"sim": True, "nao": False}.get(valor)


@bp.get("/")
@exige("ver_presenca")
def lista():
    un = unidade_atual_ou_403()
    tipo = request.args.get("tipo") or None
    termo = request.args.get("q") or None
    turma_id = request.args.get("turma_id", type=int)
    pessoas = service.buscar(un["id"], termo, tipo, turma_id, incluir_desligados=request.args.get("desligados") == "1", limite=3000)
    pendencia = request.args.get("pendencia")
    if pendencia == "foto":
        pessoas = [p for p in pessoas if not p["tem_foto"] and p["tipo"] != "visitante"]
    elif pendencia == "telegram":
        pessoas = [p for p in pessoas if p["responsavel_id"] and not p["telegram_chat_id"]]
    situacoes, contagem, filtro = {}, {}, request.args.get("situacao")
    if tipo == "aluno" and not pendencia:
        situacoes = service.situacao_alunos(un["id"])
        alunos = [p for p in pessoas if p["id"] in situacoes]
        for p in alunos:
            chave = situacoes[p["id"]]["chave"]
            contagem[chave] = contagem.get(chave, 0) + 1
        if filtro:
            pessoas = [p for p in alunos if situacoes[p["id"]]["chave"] == filtro]
    return render_template("pessoas/lista.html", pessoas=pessoas, tipo=tipo, termo=termo or "", turma_id=turma_id, turmas=_turmas(un["id"]),
                           pendencia=pendencia, situacoes=situacoes, contagem=contagem, filtro_situacao=filtro, rotulos_situacao=service.SITUACOES)


# ---------------------------------------------------------------- alunos

@bp.route("/alunos/novo", methods=["GET", "POST"])
@exige("cadastrar_pessoas")
def aluno_novo():
    un = unidade_atual_ou_403()
    if request.method == "POST":
        f = request.form
        if not f.get("nome") or not f.get("turma_id"):
            flash("Nome e turma são obrigatórios", "danger")
        else:
            turma = da_unidade("turma", f["turma_id"])
            pessoa_id, _ = service.criar_aluno(un["id"], f["nome"], turma["id"], f.get("matricula"), _tri(f.get("saida_autonoma")), f.get("documento"))
            if f.get("consentimento_por"):
                service.registrar_consentimento(pessoa_id, f["consentimento_por"], hoje_local(un["fuso"]))
            flash("Aluno cadastrado. Agora tire a foto.", "success")
            return redirect(url_for("pessoas.foto", pessoa_id=pessoa_id))
    return render_template("pessoas/aluno_form.html", p=None, turmas=_turmas(un["id"]))


@bp.route("/alunos/<int:pessoa_id>/editar", methods=["GET", "POST"])
@exige("cadastrar_pessoas")
def aluno_editar(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    if p["tipo"] != "aluno":
        abort(404)
    if request.method == "POST":
        f = request.form
        service.atualizar_aluno(pessoa_id, f["nome"], da_unidade("turma", f["turma_id"])["id"], f.get("matricula"), _tri(f.get("saida_autonoma")), f.get("documento"))
        flash("Aluno atualizado", "success")
        return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))
    return render_template("pessoas/aluno_form.html", p=p, turmas=_turmas(un["id"]))


# ---------------------------------------------------------------- responsáveis

@bp.route("/responsaveis/novo", methods=["GET", "POST"])
@exige("cadastrar_pessoas")
def responsavel_novo():
    un = unidade_atual_ou_403()
    aluno_id = request.args.get("aluno_id", type=int)
    if request.method == "POST":
        f = request.form
        if not f.get("nome"):
            flash("Nome é obrigatório", "danger")
        else:
            pessoa_id, resp_id = service.criar_responsavel(un["id"], f["nome"], f.get("telefone"), f.get("documento"), f.get("email"))
            if f.get("consentimento_por"):
                service.registrar_consentimento(pessoa_id, f["consentimento_por"], hoje_local(un["fuso"]))
            if f.get("aluno_id"):
                service.vincular(da_unidade("aluno", f["aluno_id"])["id"], resp_id, f.get("parentesco"), f.get("regra") or "sempre", [int(d) for d in f.getlist("dias")])
            flash("Responsável cadastrado. Agora tire a foto.", "success")
            return redirect(url_for("pessoas.foto", pessoa_id=pessoa_id))
    return render_template("pessoas/responsavel_form.html", p=None, aluno_id=aluno_id, dias=DIAS)


@bp.route("/responsaveis/<int:pessoa_id>/editar", methods=["GET", "POST"])
@exige("cadastrar_pessoas")
def responsavel_editar(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    if p["tipo"] != "responsavel":
        abort(404)
    if request.method == "POST":
        f = request.form
        service.atualizar_responsavel(pessoa_id, f["nome"], f.get("telefone"), f.get("documento"), f.get("email"))
        flash("Responsável atualizado", "success")
        return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))
    return render_template("pessoas/responsavel_form.html", p=p, aluno_id=None, dias=DIAS)


# ---------------------------------------------------------------- funcionários / visitantes

def _escala_do_form(f):
    dias = [int(d) for d in f.getlist("dias")]
    esc = {}
    if dias:
        esc["dias"] = dias
    if f.get("inicio") and f.get("fim"):
        esc["inicio"], esc["fim"] = f["inicio"], f["fim"]
    return esc


@bp.route("/funcionarios/novo", methods=["GET", "POST"])
@exige("cadastrar_pessoas")
def funcionario_novo():
    un = unidade_atual_ou_403()
    if request.method == "POST":
        f = request.form
        pessoa_id, _ = service.criar_funcionario(un["id"], f["nome"], f.get("cargo"), _escala_do_form(f), f.get("documento"))
        if f.get("consentimento_por"):
            service.registrar_consentimento(pessoa_id, f["consentimento_por"], hoje_local(un["fuso"]))
        flash("Funcionário cadastrado. Agora tire a foto.", "success")
        return redirect(url_for("pessoas.foto", pessoa_id=pessoa_id))
    return render_template("pessoas/funcionario_form.html", p=None, dias=DIAS)


@bp.route("/funcionarios/<int:pessoa_id>/editar", methods=["GET", "POST"])
@exige("cadastrar_pessoas")
def funcionario_editar(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    if p["tipo"] != "funcionario":
        abort(404)
    if request.method == "POST":
        f = request.form
        service.atualizar_funcionario(pessoa_id, f["nome"], f.get("cargo"), _escala_do_form(f), f.get("documento"))
        flash("Funcionário atualizado", "success")
        return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))
    return render_template("pessoas/funcionario_form.html", p=p, dias=DIAS)


@bp.route("/visitantes/novo", methods=["GET", "POST"])
@exige("liberar_manual")
def visitante_novo():
    un = unidade_atual_ou_403()
    if request.method == "POST":
        f = request.form
        pessoa_id = service.criar_visitante(un["id"], f["nome"], f.get("documento"))
        flash("Visitante registrado. Libere a entrada na Recepção.", "success")
        return redirect(url_for("painel.recepcao", pessoa_id=pessoa_id))
    return render_template("pessoas/visitante_form.html")


# ---------------------------------------------------------------- detalhe / ações

@bp.get("/<int:pessoa_id>")
@exige("ver_presenca")
def detalhe(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    responsaveis = service.responsaveis_do_aluno(p["aluno_id"]) if p["aluno_id"] else []
    alunos = service.alunos_do_responsavel(p["responsavel_id"]) if p["responsavel_id"] else []
    eventos_rows = db.query(
        "SELECT e.*, pc.nome AS correlacao_nome, u.nome AS autorizado_nome FROM eventos e LEFT JOIN pessoas pc ON pc.id = e.correlacao_id "
        "LEFT JOIN usuarios_admin u ON u.id = e.autorizado_por WHERE e.pessoa_id = %s ORDER BY e.ocorrido_em DESC LIMIT 50", (pessoa_id,),
    )
    candidatos = []
    if p["aluno_id"]:
        candidatos = db.query(
            "SELECT r.id, pp.nome FROM responsaveis r JOIN pessoas pp ON pp.id = r.pessoa_id WHERE pp.unidade_id = %s AND pp.status <> 'desligado' "
            "AND r.id NOT IN (SELECT responsavel_id FROM aluno_responsavel WHERE aluno_id = %s) ORDER BY pp.nome", (un["id"], p["aluno_id"]),
        )
    presenca = db.query("SELECT * FROM presenca WHERE pessoa_id = %s", (pessoa_id,), one=True)
    avisos = db.query(
        "SELECT av.*, pp.nome AS responsavel_nome FROM avisos_retirada av JOIN responsaveis r ON r.id = av.responsavel_id JOIN pessoas pp ON pp.id = r.pessoa_id "
        "WHERE av.aluno_id = %s AND av.data >= current_date ORDER BY av.data", (p["aluno_id"],),
    ) if p["aluno_id"] else []
    return render_template(
        "pessoas/detalhe.html", p=p, responsaveis=responsaveis, alunos=alunos, eventos=eventos_rows, rotulos=tipos.ROTULOS,
        sync=dispositivos.status_sync_pessoa(pessoa_id), candidatos=candidatos, dias=DIAS, presenca=presenca,
        consentimento=service.tem_consentimento(pessoa_id), autorizacoes=service.consentimentos(pessoa_id), avisos=avisos, fuso=un["fuso"],
    )


@bp.post("/<int:pessoa_id>/vincular")
@exige("cadastrar_pessoas")
def vincular(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    f = request.form
    try:
        service.vincular(p["aluno_id"], da_unidade("responsavel", f["responsavel_id"])["id"], f.get("parentesco"), f.get("regra") or "sempre", [int(d) for d in f.getlist("dias")])
        flash("Responsável vinculado", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


@bp.post("/<int:pessoa_id>/desvincular/<int:responsavel_id>")
@exige("cadastrar_pessoas")
def desvincular(pessoa_id, responsavel_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    service.desvincular(p["aluno_id"], responsavel_id)
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


@bp.post("/<int:pessoa_id>/aviso-retirada")
@exige("liberar_manual")
def aviso_retirada(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    f = request.form
    db.execute(
        "INSERT INTO avisos_retirada (unidade_id, aluno_id, responsavel_id, data, observacao, criado_por) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (aluno_id, responsavel_id, data) DO UPDATE SET observacao = EXCLUDED.observacao",
        (un["id"], p["aluno_id"], da_unidade("responsavel", f["responsavel_id"])["id"], f.get("data") or hoje_local(un["fuso"]), f.get("observacao"), g.usuario["id"]),
    )
    auditar("aviso_retirada", "aluno", p["aluno_id"], depois={"responsavel_id": f["responsavel_id"], "data": f.get("data")})
    flash("Aviso de retirada registrado", "success")
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


@bp.post("/<int:pessoa_id>/bloquear")
@exige("cadastrar_pessoas")
def bloquear(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    service.bloquear(pessoa_id, p["status"] == "ativo")
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


@bp.post("/<int:pessoa_id>/desligar")
@exige("excluir_lgpd")
def desligar(pessoa_id):
    _pessoa_da_unidade(pessoa_id)
    service.desligar(pessoa_id)
    flash("Pessoa desligada: removida das catracas; a foto é apagada em 30 dias.", "warning")
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


@bp.post("/<int:pessoa_id>/apagar-foto")
@exige("excluir_lgpd")
def apagar_foto(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    service.apagar_dados_biometricos(pessoa_id)
    if p["status"] != "desligado":
        dispositivos.enfileirar_remocao(pessoa_id, un["id"])
    flash("Foto apagada do banco e remoção enfileirada nas catracas.", "warning")
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


@bp.post("/<int:pessoa_id>/consentimento")
@exige("cadastrar_pessoas")
def consentimento(pessoa_id):
    un, _ = _pessoa_da_unidade(pessoa_id)
    f = request.form
    arq = request.files.get("arquivo")
    service.registrar_consentimento(pessoa_id, f["assinado_por"], f.get("assinado_em") or hoje_local(un["fuso"]),
                                    arq.filename if arq and arq.filename else None, arq.read() if arq and arq.filename else None, f.get("observacao"))
    flash("Consentimento registrado", "success")
    return redirect(url_for("pessoas.detalhe", pessoa_id=pessoa_id))


# ---------------------------------------------------------------- foto (webcam)

@bp.get("/<int:pessoa_id>/foto")
@exige("cadastrar_pessoas")
def foto(pessoa_id):
    un, p = _pessoa_da_unidade(pessoa_id)
    return render_template("pessoas/foto.html", p=p, consentimento=service.tem_consentimento(pessoa_id) or not service.precisa_consentimento(p))


@bp.post("/<int:pessoa_id>/foto")
@exige("cadastrar_pessoas")
def foto_salvar(pessoa_id):
    """Recebe {"imagem": "data:image/jpeg;base64,..."} da webcam."""
    _, p = _pessoa_da_unidade(pessoa_id)
    if service.precisa_consentimento(p) and not service.tem_consentimento(pessoa_id):
        # S10: sem autorização dos pais a foto (biométrica) não é aceita
        return jsonify({"ok": False, "erro": "Falta autorização dos pais para a foto. Registre o termo assinado ou peça para o responsável autorizar pelo Telegram."}), 409
    dados = request.get_json(silent=True) or {}
    imagem = dados.get("imagem") or ""
    if "," in imagem:
        imagem = imagem.split(",", 1)[1]
    try:
        jpeg = base64.b64decode(imagem, validate=True)
        service.salvar_foto(pessoa_id, jpeg)
    except (ValueError, service.FotoInvalida) as e:
        return jsonify({"ok": False, "erro": str(e)}), 400
    return jsonify({"ok": True, "redirect": url_for("pessoas.detalhe", pessoa_id=pessoa_id)})


@bp.get("/<int:pessoa_id>/foto.jpg")
@exige("ver_presenca")
def foto_jpg(pessoa_id):
    import time

    from flask import session

    _pessoa_da_unidade(pessoa_id)
    jpeg = service.obter_foto(pessoa_id)
    if not jpeg:
        abort(404)
    # Revisão (S19): ver a foto (dado biométrico) não ficava registrado. Uma linha por pessoa
    # por hora e por sessão — a miniatura da lista não conta (senão 300 linhas por página).
    if request.args.get("mini") != "1":
        vistas = session.get("fotos_vistas") or {}
        agora = int(time.time())
        if agora - int(vistas.get(str(pessoa_id), 0)) > 3600:
            auditar("ver_foto", "pessoa", pessoa_id)
            vistas = {k: v for k, v in vistas.items() if agora - int(v) <= 3600}
            vistas[str(pessoa_id)] = agora
            session["fotos_vistas"] = vistas
    return Response(jpeg, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


# rotas da importação de planilha ficam em arquivo próprio, no mesmo blueprint
from . import importar_routes  # noqa: E402,F401
