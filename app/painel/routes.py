"""Dashboard, recepção, presença, relatórios, auditoria, unidades e turmas."""
import json
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import db
from ..auditoria import auditar
from ..auth.rbac import exige, login_obrigatorio, unidade_atual_ou_403, unidades_visiveis
from ..dispositivos import service as dispositivos
from ..eventos import service as eventos
from ..eventos import tipos

bp = Blueprint("painel", __name__)


def _periodo():
    hoje = date.today()
    ini = request.args.get("de") or (hoje - timedelta(days=6)).isoformat()
    fim = request.args.get("ate") or hoje.isoformat()
    return date.fromisoformat(ini), date.fromisoformat(fim)


@bp.get("/painel/rede")
@login_obrigatorio
def rede():
    """Consolidado das unidades (na nuvem é a tela principal da direção da rede)."""
    unidades = unidades_visiveis(g.usuario)
    ids = [u["id"] for u in unidades] or [0]
    presenca = {r["unidade_id"]: r["n"] for r in db.query("SELECT unidade_id, count(*) AS n FROM presenca WHERE dentro AND unidade_id = ANY(%s) GROUP BY unidade_id", (ids,))}
    hoje = {r["unidade_id"]: r for r in db.query(
        "SELECT e.unidade_id, count(*) FILTER (WHERE e.tipo = ANY(%s)) AS entradas, count(*) FILTER (WHERE e.tipo = %s) AS atrasos, "
        "count(*) FILTER (WHERE e.tipo = ANY(%s)) AS negados, count(*) FILTER (WHERE e.tipo = %s) AS antecipadas, max(e.ocorrido_em) AS ultimo "
        "FROM eventos e JOIN unidades u ON u.id = e.unidade_id WHERE e.unidade_id = ANY(%s) "
        "AND e.ocorrido_em >= date_trunc('day', now() AT TIME ZONE u.fuso) AT TIME ZONE u.fuso GROUP BY e.unidade_id",
        (list(tipos.ENTRADAS), tipos.ENTRADA_ATRASADA, list(tipos.NEGADOS), tipos.SAIDA_ANTECIPADA, ids),
    )}
    disps = {}
    for d in db.query("SELECT unidade_id, nome, status FROM dispositivos WHERE ativo AND unidade_id = ANY(%s) ORDER BY nome", (ids,)):
        disps.setdefault(d["unidade_id"], []).append(d)
    alunos = {r["unidade_id"]: r["n"] for r in db.query("SELECT p.unidade_id, count(*) AS n FROM pessoas p WHERE p.tipo = 'aluno' AND p.status = 'ativo' AND p.unidade_id = ANY(%s) GROUP BY p.unidade_id", (ids,))}
    linhas = []
    for u in unidades:
        cfg = u["config"] or {}
        linhas.append({"unidade": u, "dentro": presenca.get(u["id"], 0), "alunos": alunos.get(u["id"], 0), "hoje": hoje.get(u["id"]),
                       "dispositivos": disps.get(u["id"], []), "agente_em": cfg.get("agente_visto_em"), "agente_erro": cfg.get("agente_erro")})
    return render_template("painel/rede.html", linhas=linhas)


@bp.get("/painel")
@login_obrigatorio
def dashboard():
    if g.unidade is None:
        if unidades_visiveis(g.usuario):
            return redirect(url_for("painel.rede"))
        return redirect(url_for("painel.unidades"))
    un = g.unidade
    turmas = db.query(
        "SELECT t.id, t.nome, count(a.id) AS alunos, count(pr.pessoa_id) FILTER (WHERE pr.dentro) AS dentro "
        "FROM turmas t LEFT JOIN alunos a ON a.turma_id = t.id LEFT JOIN pessoas p ON p.id = a.pessoa_id AND p.status = 'ativo' "
        "LEFT JOIN presenca pr ON pr.pessoa_id = p.id WHERE t.unidade_id = %s AND t.ativo GROUP BY t.id ORDER BY t.nome", (un["id"],),
    )
    hoje = db.query(
        "SELECT count(*) FILTER (WHERE tipo = ANY(%s)) AS entradas, count(*) FILTER (WHERE tipo = %s) AS atrasos, "
        "count(*) FILTER (WHERE tipo = ANY(%s)) AS negados, count(*) FILTER (WHERE tipo = %s) AS antecipadas "
        "FROM eventos WHERE unidade_id = %s AND ocorrido_em >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s",
        (list(tipos.ENTRADAS), tipos.ENTRADA_ATRASADA, list(tipos.NEGADOS), tipos.SAIDA_ANTECIPADA, un["id"], un["fuso"], un["fuso"]), one=True,
    )
    return render_template("painel/dashboard.html", turmas=turmas, hoje=hoje, dispositivos=dispositivos.listar(un["id"]), rotulos=tipos.ROTULOS)


@bp.get("/painel/recepcao")
@exige("liberar_manual")
def recepcao():
    un = unidade_atual_ou_403()
    pessoa_id = request.args.get("pessoa_id", type=int)
    pessoa = db.query("SELECT p.*, t.nome AS turma_nome FROM pessoas p LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id WHERE p.id = %s AND p.unidade_id = %s", (pessoa_id, un["id"]), one=True) if pessoa_id else None
    termo = request.args.get("q") or ""
    resultados = db.query(
        "SELECT p.id, p.nome, p.tipo, t.nome AS turma_nome, pr.dentro FROM pessoas p LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id "
        "LEFT JOIN presenca pr ON pr.pessoa_id = p.id WHERE p.unidade_id = %s AND p.status <> 'desligado' AND lower(p.nome) LIKE %s ORDER BY p.nome LIMIT 20",
        (un["id"], f"%{termo.lower()}%"),
    ) if termo else []
    return render_template("painel/recepcao.html", pessoa=pessoa, termo=termo, resultados=resultados, dispositivos=dispositivos.listar(un["id"]))


@bp.post("/painel/liberar")
@exige("liberar_manual")
def liberar():
    un = unidade_atual_ou_403()
    f = request.form
    justificativa = (f.get("justificativa") or "").strip()
    if len(justificativa) < 5:
        flash("Justificativa obrigatória (mínimo 5 caracteres)", "danger")
        return redirect(request.referrer or url_for("painel.recepcao"))
    pessoa_id = int(f["pessoa_id"]) if f.get("pessoa_id") else None
    dispositivo_id = int(f["dispositivo_id"]) if f.get("dispositivo_id") else None
    evento, erro = eventos.liberar_manual(un, g.usuario["id"], f.get("sentido") or "saida", justificativa, pessoa_id, dispositivo_id,
                                         nome_visitante=f.get("visitante"), girar_catraca=f.get("girar") == "1")
    auditar("liberar_manual", "evento", evento["id"], depois={"pessoa_id": pessoa_id, "sentido": f.get("sentido"), "justificativa": justificativa})
    if erro:
        flash(f"Liberação registrada, mas não foi possível acionar a catraca: {erro}. Use o portão PCD e anote.", "warning")
    else:
        flash("Liberação registrada" + (" — comando enviado à catraca (alguns segundos)." if f.get("girar") == "1" else "."), "success")
    return redirect(url_for("painel.recepcao"))


@bp.post("/painel/espera/<int:espera_id>/encerrar")
@exige("liberar_manual")
def encerrar_espera(espera_id):
    un = unidade_atual_ou_403()
    db.execute("UPDATE responsavel_em_espera SET consumido_em = now() WHERE id = %s AND unidade_id = %s", (espera_id, un["id"]))
    return redirect(url_for("painel.recepcao"))


@bp.get("/painel/presenca")
@exige("ver_presenca")
def presenca():
    un = unidade_atual_ou_403()
    turma_id = request.args.get("turma_id", type=int)
    sql = (
        "SELECT p.id, p.nome, t.nome AS turma, pr.dentro, pr.desde FROM pessoas p JOIN alunos a ON a.pessoa_id = p.id JOIN turmas t ON t.id = a.turma_id "
        "LEFT JOIN presenca pr ON pr.pessoa_id = p.id WHERE p.unidade_id = %s AND p.status = 'ativo'"
    )
    params = [un["id"]]
    if turma_id:
        sql += " AND t.id = %s"
        params.append(turma_id)
    alunos = db.query(sql + " ORDER BY t.nome, p.nome", params)
    funcionarios = db.query(
        "SELECT p.id, p.nome, f.cargo, pr.dentro, pr.desde FROM pessoas p JOIN funcionarios f ON f.pessoa_id = p.id LEFT JOIN presenca pr ON pr.pessoa_id = p.id "
        "WHERE p.unidade_id = %s AND p.status = 'ativo' ORDER BY p.nome", (un["id"],),
    )
    turmas = db.query("SELECT * FROM turmas WHERE unidade_id = %s AND ativo ORDER BY nome", (un["id"],))
    return render_template("painel/presenca.html", alunos=alunos, funcionarios=funcionarios, turmas=turmas, turma_id=turma_id,
                           evacuacao=request.args.get("evacuacao") == "1", fuso=un["fuso"])


@bp.get("/painel/relatorios")
@exige("ver_relatorios")
def relatorios():
    un = unidade_atual_ou_403()
    de, ate = _periodo()
    tipo = request.args.get("tipo") or "frequencia"
    turma_id = request.args.get("turma_id", type=int)
    turmas = db.query("SELECT * FROM turmas WHERE unidade_id = %s AND ativo ORDER BY nome", (un["id"],))
    filtro_turma = " AND a.turma_id = %s" if turma_id else ""
    params_turma = [turma_id] if turma_id else []
    linhas = []
    if tipo == "frequencia":
        linhas = db.query(
            "SELECT p.id, p.nome, t.nome AS turma, count(DISTINCT (e.ocorrido_em AT TIME ZONE %s)::date) FILTER (WHERE e.tipo = ANY(%s)) AS dias_presente, "
            "count(*) FILTER (WHERE e.tipo = %s) AS atrasos "
            "FROM pessoas p JOIN alunos a ON a.pessoa_id = p.id JOIN turmas t ON t.id = a.turma_id "
            "LEFT JOIN eventos e ON e.pessoa_id = p.id AND (e.ocorrido_em AT TIME ZONE %s)::date BETWEEN %s AND %s "
            f"WHERE p.unidade_id = %s AND p.status = 'ativo'{filtro_turma} GROUP BY p.id, t.nome ORDER BY t.nome, p.nome",
            [un["fuso"], list(tipos.ENTRADAS), tipos.ENTRADA_ATRASADA, un["fuso"], de, ate, un["id"]] + params_turma,
        )
    elif tipo in ("atrasos", "antecipadas", "negados"):
        alvo = {"atrasos": [tipos.ENTRADA_ATRASADA], "antecipadas": [tipos.SAIDA_ANTECIPADA], "negados": list(tipos.NEGADOS)}[tipo]
        linhas = db.query(
            "SELECT e.*, p.nome, t.nome AS turma, u.nome AS autorizado_nome FROM eventos e LEFT JOIN pessoas p ON p.id = e.pessoa_id "
            "LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id LEFT JOIN usuarios_admin u ON u.id = e.autorizado_por "
            f"WHERE e.unidade_id = %s AND e.tipo = ANY(%s) AND (e.ocorrido_em AT TIME ZONE %s)::date BETWEEN %s AND %s{filtro_turma} ORDER BY e.ocorrido_em DESC",
            [un["id"], alvo, un["fuso"], de, ate] + params_turma,
        )
    dias_uteis = sum(1 for i in range((ate - de).days + 1) if (de + timedelta(days=i)).weekday() < 5)
    return render_template("painel/relatorios.html", tipo=tipo, linhas=linhas, de=de, ate=ate, turmas=turmas, turma_id=turma_id,
                           dias_uteis=dias_uteis, rotulos=tipos.ROTULOS, fuso=un["fuso"])


@bp.get("/painel/historico/<int:pessoa_id>")
@exige("ver_relatorios")
def historico(pessoa_id):
    un = unidade_atual_ou_403()
    de, ate = _periodo()
    p = db.query("SELECT * FROM pessoas WHERE id = %s AND unidade_id = %s", (pessoa_id, un["id"]), one=True)
    if not p:
        abort(404)
    linhas = db.query(
        "SELECT e.*, pc.nome AS correlacao_nome, u.nome AS autorizado_nome, d.nome AS dispositivo_nome FROM eventos e LEFT JOIN pessoas pc ON pc.id = e.correlacao_id "
        "LEFT JOIN usuarios_admin u ON u.id = e.autorizado_por LEFT JOIN dispositivos d ON d.id = e.dispositivo_id "
        "WHERE e.pessoa_id = %s AND (e.ocorrido_em AT TIME ZONE %s)::date BETWEEN %s AND %s ORDER BY e.ocorrido_em DESC",
        (pessoa_id, un["fuso"], de, ate),
    )
    return render_template("painel/historico.html", p=p, linhas=linhas, de=de, ate=ate, rotulos=tipos.ROTULOS, fuso=un["fuso"])


@bp.get("/painel/auditoria")
@exige("ver_auditoria")
def auditoria():
    un = unidade_atual_ou_403()
    linhas = db.query(
        "SELECT a.*, u.nome AS usuario_nome FROM auditoria a LEFT JOIN usuarios_admin u ON u.id = a.usuario_id "
        "WHERE a.unidade_id = %s OR a.unidade_id IS NULL ORDER BY a.id DESC LIMIT 300", (un["id"],),
    )
    return render_template("painel/auditoria.html", linhas=linhas, fuso=un["fuso"])


# ---------------------------------------------------------------- unidades e turmas

@bp.get("/unidades")
@login_obrigatorio
def unidades():
    empresas = db.query("SELECT * FROM empresas ORDER BY nome") if g.usuario["perfil"] == "superadmin" else []
    return render_template("painel/unidades.html", unidades=unidades_visiveis(g.usuario), empresas=empresas)


@bp.post("/empresas/nova")
@exige("gerir_turmas")
def empresa_nova():
    if g.usuario["perfil"] != "superadmin":
        abort(403)
    row = db.execute("INSERT INTO empresas (nome) VALUES (%s) RETURNING id", (request.form["nome"].strip(),), returning=True)
    auditar("criar", "empresa", row["id"], depois={"nome": request.form["nome"]})
    return redirect(url_for("painel.unidades"))


@bp.post("/unidades/nova")
@exige("gerir_turmas")
def unidade_nova():
    f = request.form
    empresa_id = int(f["empresa_id"]) if g.usuario["perfil"] == "superadmin" else g.usuario["empresa_id"]
    if not empresa_id:
        abort(400)
    row = db.execute(
        "INSERT INTO unidades (empresa_id, nome, endereco, fuso) VALUES (%s,%s,%s,%s) RETURNING id",
        (empresa_id, f["nome"].strip(), f.get("endereco"), f.get("fuso") or "America/Sao_Paulo"), returning=True,
    )
    auditar("criar", "unidade", row["id"], depois={"nome": f["nome"]}, unidade_id=row["id"])
    return redirect(url_for("auth.trocar_unidade", unidade_id=row["id"]))


@bp.route("/unidades/<int:unidade_id>/config", methods=["GET", "POST"])
@exige("gerir_turmas")
def unidade_config(unidade_id):
    un = unidade_atual_ou_403()
    if un["id"] != unidade_id:
        abort(404)
    if request.method == "POST":
        f = request.form
        cfg = {
            "espera_responsavel_min": int(f.get("espera_responsavel_min") or 15),
            "responsavel_resposta": f.get("responsavel_resposta") or "aguardar",
        }
        db.execute("UPDATE unidades SET nome = %s, endereco = %s, fuso = %s, config = config || %s WHERE id = %s",
                   (f["nome"].strip(), f.get("endereco"), f.get("fuso") or "America/Sao_Paulo", json.dumps(cfg), unidade_id))
        auditar("config", "unidade", unidade_id, antes=un["config"], depois=cfg)
        flash("Unidade atualizada", "success")
        return redirect(url_for("painel.unidade_config", unidade_id=unidade_id))
    return render_template("painel/unidade_config.html", un=un)


@bp.get("/turmas")
@exige("ver_presenca")
def turmas():
    un = unidade_atual_ou_403()
    rows = db.query(
        "SELECT t.*, count(a.id) AS alunos FROM turmas t LEFT JOIN alunos a ON a.turma_id = t.id LEFT JOIN pessoas p ON p.id = a.pessoa_id AND p.status = 'ativo' "
        "WHERE t.unidade_id = %s GROUP BY t.id ORDER BY t.ativo DESC, t.nome", (un["id"],),
    )
    return render_template("painel/turmas.html", turmas=rows, t=None)


@bp.route("/turmas/nova", methods=["POST"])
@bp.route("/turmas/<int:turma_id>/editar", methods=["POST"])
@exige("gerir_turmas")
def turma_salvar(turma_id=None):
    un = unidade_atual_ou_403()
    f = request.form
    campos = (
        f["nome"].strip(), f.get("turno") or "manha", f.get("exige_responsavel_saida") == "1", int(f.get("tolerancia_entrada_min") or 15),
        f.get("entrada_inicio") or "06:30", f.get("entrada_fim") or "08:00", f.get("saida_inicio") or "16:30", f.get("saida_fim") or "18:30", f.get("ativo", "1") == "1",
    )
    if turma_id:
        t = db.query("SELECT * FROM turmas WHERE id = %s AND unidade_id = %s", (turma_id, un["id"]), one=True)
        if not t:
            abort(404)
        db.execute("UPDATE turmas SET nome=%s, turno=%s, exige_responsavel_saida=%s, tolerancia_entrada_min=%s, entrada_inicio=%s, entrada_fim=%s, saida_inicio=%s, saida_fim=%s, ativo=%s WHERE id = %s", campos + (turma_id,))
        auditar("editar", "turma", turma_id, antes={k: str(v) for k, v in t.items() if k != "id"}, depois={"nome": campos[0], "exige_responsavel_saida": campos[2]})
    else:
        row = db.execute("INSERT INTO turmas (unidade_id, nome, turno, exige_responsavel_saida, tolerancia_entrada_min, entrada_inicio, entrada_fim, saida_inicio, saida_fim, ativo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", (un["id"],) + campos, returning=True)
        auditar("criar", "turma", row["id"], depois={"nome": campos[0]})
    flash("Turma salva", "success")
    return redirect(url_for("painel.turmas"))
