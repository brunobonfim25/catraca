"""Dashboard, recepção, presença, relatórios, auditoria, unidades e turmas."""
import json
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import db, fusos
from ..auditoria import auditar
from ..auth.rbac import exige, login_obrigatorio, unidade_atual_ou_403, unidades_visiveis
from ..dispositivos import service as dispositivos
from ..eventos import service as eventos
from ..eventos import tipos

bp = Blueprint("painel", __name__)


def _periodo():
    from ..util import hoje_local

    hoje = hoje_local(g.unidade["fuso"] if getattr(g, "unidade", None) else "America/Sao_Paulo")  # A17: data da escola, não de Londres
    try:
        ini = date.fromisoformat(request.args.get("de") or (hoje - timedelta(days=6)).isoformat())
        fim = date.fromisoformat(request.args.get("ate") or hoje.isoformat())
    except ValueError:
        abort(400)
    if fim < ini:
        ini, fim = fim, ini
    # A18: período sem limite lia a tabela de eventos inteira
    return max(ini, fim - timedelta(days=366)), fim


# Faixa do período em hora da escola, escrita de um jeito que usa o índice (unidade, ocorrido_em).
# Antes: (ocorrido_em AT TIME ZONE fuso)::date BETWEEN … — calculado linha a linha, tabela inteira.
_FAIXA = "e.ocorrido_em >= (%s::date::timestamp AT TIME ZONE %s) AND e.ocorrido_em < ((%s::date + 1)::timestamp AT TIME ZONE %s)"


def _so_nexosix():
    if not g.usuario or g.usuario["perfil"] != "superadmin":
        abort(403)


@bp.get("/painel/monitor")
@login_obrigatorio
def monitor():
    """Monitoramento em tempo real de todos os serviços (só a Nexosix)."""
    _so_nexosix()
    import os

    return render_template("painel/monitor.html", chat_alerta=bool(os.environ.get("ALERTA_TELEGRAM_CHAT")))


@bp.get("/painel/monitor/estado")
@login_obrigatorio
def monitor_estado():
    from flask import jsonify

    from .. import alertas

    from flask import current_app

    _so_nexosix()
    return jsonify(alertas.estado(current_app.config.get("MODO", "nuvem")))


@bp.post("/painel/monitor/testar")
@login_obrigatorio
def monitor_testar():
    """Manda uma mensagem de teste para o chat dos alertas (confere a configuração)."""
    import os

    from ..notificacoes import telegram

    _so_nexosix()
    chat = os.environ.get("ALERTA_TELEGRAM_CHAT", "")
    if not chat:
        flash("ALERTA_TELEGRAM_CHAT não está definido no serviço web.", "danger")
    else:
        try:
            telegram.enviar(chat, f"✅ Teste de alerta da Nexosix — enviado por {g.usuario['nome']}. Se você está lendo, os alertas chegam aqui.")
            flash("Mensagem de teste enviada. Confira no Telegram.", "success")
        except Exception as e:  # noqa: BLE001
            flash(f"O Telegram recusou: {e}", "danger")
    return redirect(url_for("painel.monitor"))


@bp.post("/painel/monitor/<int:inc_id>/<acao>")
@login_obrigatorio
def monitor_acao(inc_id, acao):
    from .. import alertas

    _so_nexosix()
    if acao == "assumir":
        alertas.reconhecer(inc_id, g.usuario["nome"])
    elif acao == "encerrar":
        db.execute("UPDATE incidentes SET fim = now() WHERE id = %s AND fim IS NULL", (inc_id,), outbox=False)
    else:
        abort(404)
    auditar(f"incidente_{acao}", "incidente", inc_id)
    return redirect(url_for("painel.monitor"))


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
        "FROM eventos e JOIN unidades u ON u.id = e.unidade_id WHERE e.unidade_id = ANY(%s) AND e.ocorrido_em > now() - interval '30 hours' "
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
    from flask import current_app

    from .. import alertas

    worker_parado = current_app.config.get("MODO") == "nuvem" and not alertas.worker_ok("nuvem")
    return render_template("painel/rede.html", linhas=linhas, worker_parado=worker_parado,
                           problemas=alertas.problemas() if g.usuario["perfil"] == "superadmin" else {})


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
    from ..escopo import da_unidade

    # aluno e catraca têm de ser desta escola (antes: ids de outra escola eram aceitos)
    pessoa_id = da_unidade("pessoa", f["pessoa_id"])["id"] if f.get("pessoa_id") else None
    dispositivo_id = da_unidade("dispositivo", f["dispositivo_id"])["id"] if f.get("dispositivo_id") else None
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
    auditar("encerrar_espera", "responsavel_em_espera", espera_id)  # mexe na guarda da criança: fica registrado
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
            f"LEFT JOIN eventos e ON e.pessoa_id = p.id AND {_FAIXA} "
            f"WHERE p.unidade_id = %s AND p.status = 'ativo'{filtro_turma} GROUP BY p.id, t.nome ORDER BY t.nome, p.nome",
            [un["fuso"], list(tipos.ENTRADAS), tipos.ENTRADA_ATRASADA, de, un["fuso"], ate, un["fuso"], un["id"]] + params_turma,
        )
    elif tipo in ("atrasos", "antecipadas", "negados"):
        alvo = {"atrasos": [tipos.ENTRADA_ATRASADA], "antecipadas": [tipos.SAIDA_ANTECIPADA], "negados": list(tipos.NEGADOS)}[tipo]
        linhas = db.query(
            "SELECT e.*, p.nome, t.nome AS turma, u.nome AS autorizado_nome FROM eventos e LEFT JOIN pessoas p ON p.id = e.pessoa_id "
            "LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id LEFT JOIN usuarios_admin u ON u.id = e.autorizado_por "
            f"WHERE e.unidade_id = %s AND e.tipo = ANY(%s) AND {_FAIXA}{filtro_turma} ORDER BY e.ocorrido_em DESC LIMIT 5000",
            [un["id"], alvo, de, un["fuso"], ate, un["fuso"]] + params_turma,
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
        f"WHERE e.pessoa_id = %s AND {_FAIXA} ORDER BY e.ocorrido_em DESC LIMIT 2000",
        (pessoa_id, de, un["fuso"], ate, un["fuso"]),
    )
    return render_template("painel/historico.html", p=p, linhas=linhas, de=de, ate=ate, rotulos=tipos.ROTULOS, fuso=un["fuso"])


@bp.get("/painel/auditoria")
@exige("ver_auditoria")
def auditoria():
    un = unidade_atual_ou_403()
    # Registro sem escola (ações da Nexosix, webhook antigo) só para o superadmin: antes o
    # auditor de uma escola via nomes de pais de outras redes por aqui.
    sem_escola = " OR a.unidade_id IS NULL" if g.usuario["perfil"] == "superadmin" else ""
    linhas = db.query(
        "SELECT a.*, u.nome AS usuario_nome FROM auditoria a LEFT JOIN usuarios_admin u ON u.id = a.usuario_id "
        f"WHERE a.unidade_id = %s{sem_escola} ORDER BY a.id DESC LIMIT 300", (un["id"],),
    )
    return render_template("painel/auditoria.html", linhas=linhas, fuso=un["fuso"])


# ---------------------------------------------------------------- unidades e turmas

@bp.get("/unidades")
@login_obrigatorio
def unidades():
    empresas = db.query("SELECT * FROM empresas ORDER BY nome") if g.usuario["perfil"] == "superadmin" else []
    return render_template("painel/unidades.html", unidades=unidades_visiveis(g.usuario), empresas=empresas, fusos=fusos.FUSOS_BRASIL)


@bp.post("/empresas/nova")
@exige("configurar_escola")
def empresa_nova():
    if g.usuario["perfil"] != "superadmin":
        abort(403)
    row = db.execute("INSERT INTO empresas (nome) VALUES (%s) RETURNING id", (request.form["nome"].strip(),), returning=True)
    auditar("criar", "empresa", row["id"], depois={"nome": request.form["nome"]})
    return redirect(url_for("painel.unidades"))


@bp.post("/unidades/nova")
@exige("configurar_escola")
def unidade_nova():
    f = request.form
    empresa_id = int(f["empresa_id"]) if g.usuario["perfil"] == "superadmin" else g.usuario["empresa_id"]
    if not empresa_id:
        abort(400)
    row = db.execute(
        "INSERT INTO unidades (empresa_id, nome, endereco, fuso) VALUES (%s,%s,%s,%s) RETURNING id",
        (empresa_id, f["nome"].strip(), f.get("endereco"), fusos.validar(f.get("fuso"))), returning=True,
    )
    from ..espelho.service import garantir_monitor_token

    garantir_monitor_token(row["id"])
    auditar("criar", "unidade", row["id"], depois={"nome": f["nome"]}, unidade_id=row["id"])
    return redirect(url_for("auth.trocar_unidade", unidade_id=row["id"]))


@bp.route("/unidades/<int:unidade_id>/config", methods=["GET", "POST"])
@exige("configurar_escola")
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
        # Nome DNS da catraca: só o superadmin troca, e só dentro do domínio (o vigia aponta esse
        # nome na Cloudflare; antes qualquer admin de escola escrevia o nome que quisesse).
        if g.usuario["perfil"] == "superadmin" and "dns_nome" in f:
            from ..espelho import failover

            nome = (f.get("dns_nome") or "").strip().lower()
            if nome and not failover.nome_valido(nome):
                flash(f"Nome DNS precisa terminar em .{failover.dominio() or '(FAILOVER_DOMINIO não configurado)'}", "danger")
                return redirect(url_for("painel.unidade_config", unidade_id=unidade_id))
            cfg["dns_nome"] = nome or failover.nome_padrao(unidade_id)
        db.execute("UPDATE unidades SET nome = %s, endereco = %s, fuso = %s, config = config || %s WHERE id = %s",
                   (f["nome"].strip(), f.get("endereco"), fusos.validar(f.get("fuso")), json.dumps(cfg), unidade_id))
        auditar("config", "unidade", unidade_id, antes=un["config"], depois=cfg)
        flash("Unidade atualizada", "success")
        return redirect(url_for("painel.unidade_config", unidade_id=unidade_id))
    from ..espelho import service as espelho_service
    from ..notificacoes import convites, email, telegram

    tem_logo = db.query("SELECT 1 FROM unidade_logos WHERE unidade_id = %s", (unidade_id,), one=True) is not None
    return render_template("painel/unidade_config.html", un=un, fusos=fusos.FUSOS_BRASIL, link_escola=telegram.link_escola(un["id"]),
                           convite_texto=convites.texto_da_escola(un), nome_convite=convites.nome_escola(un), tem_logo=tem_logo,
                           email_ok=email.configurado(), schema_nuvem=espelho_service.versao_schema(),
                           quarentena=db.query("SELECT count(*) AS n FROM espelho_quarentena WHERE unidade_id = %s", (unidade_id,), one=True)["n"])


LOGO_MAX_BYTES = 300 * 1024


@bp.post("/unidades/<int:unidade_id>/convite")
@exige("configurar_escola")
def unidade_convite(unidade_id):
    """Texto do convite aos responsáveis, nome da escola nas mensagens, e-mail de resposta e logotipo."""
    from ..notificacoes import convites

    un = unidade_atual_ou_403()
    if un["id"] != unidade_id:
        abort(404)
    f = request.form
    texto = (f.get("convite_texto") or "").replace("\r\n", "\n").strip()
    if texto and "{link}" not in texto:
        flash("O texto do convite precisa ter {link} — é onde entra o link de ativação de cada responsável.", "danger")
        return redirect(url_for("painel.unidade_config", unidade_id=unidade_id) + "#convite")
    cfg = {
        "convite_texto": None if not texto or texto == convites.TEXTO_PADRAO else texto,
        "nome_convite": (f.get("nome_convite") or "").strip()[:120] or None,
        "email_contato": (f.get("email_contato") or "").strip().lower()[:120] or None,
    }
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (json.dumps(cfg), unidade_id))
    logo = request.files.get("logo")
    if f.get("remover_logo") == "1":
        db.execute("DELETE FROM unidade_logos WHERE unidade_id = %s", (unidade_id,))
    elif logo and logo.filename:
        dados = logo.read()
        tipo = "image/png" if dados[:8] == b"\x89PNG\r\n\x1a\n" else ("image/jpeg" if dados[:2] == b"\xff\xd8" else None)
        if not tipo or len(dados) > LOGO_MAX_BYTES:
            flash("Logotipo precisa ser PNG ou JPG de até 300 KB", "danger")
            return redirect(url_for("painel.unidade_config", unidade_id=unidade_id) + "#convite")
        import psycopg2

        db.execute("INSERT INTO unidade_logos (unidade_id, conteudo, tipo) VALUES (%s,%s,%s) ON CONFLICT (unidade_id) "
                   "DO UPDATE SET conteudo = EXCLUDED.conteudo, tipo = EXCLUDED.tipo, atualizado_em = now()",
                   (unidade_id, psycopg2.Binary(dados), tipo))
    auditar("config_convite", "unidade", unidade_id, depois=cfg)
    flash("Convite atualizado", "success")
    return redirect(url_for("painel.unidade_config", unidade_id=unidade_id) + "#convite")


_EXPORTACAO = {
    "turmas.csv": "SELECT nome, turno, exige_responsavel_saida, tolerancia_entrada_min, entrada_inicio, entrada_fim, saida_inicio, saida_fim, ativo "
                  "FROM turmas WHERE unidade_id = %(u)s ORDER BY nome",
    "alunos.csv": "SELECT p.id, p.nome, t.nome AS turma, a.matricula, p.status, a.saida_autonoma, p.criado_em AT TIME ZONE %(f)s AS cadastrado_em, "
                  "p.desligado_em AT TIME ZONE %(f)s AS desligado_em FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id "
                  "LEFT JOIN turmas t ON t.id = a.turma_id WHERE p.unidade_id = %(u)s ORDER BY t.nome, p.nome",
    "responsaveis.csv": "SELECT p.id, p.nome, r.telefone, r.email, p.status, (r.telegram_chat_id IS NOT NULL) AS telegram_ativo "
                        "FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE p.unidade_id = %(u)s ORDER BY p.nome",
    "vinculos.csv": "SELECT pa.nome AS aluno, pr.nome AS responsavel, ar.parentesco, ar.regra, ar.dias_semana FROM aluno_responsavel ar "
                    "JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas pa ON pa.id = a.pessoa_id JOIN responsaveis r ON r.id = ar.responsavel_id "
                    "JOIN pessoas pr ON pr.id = r.pessoa_id WHERE pa.unidade_id = %(u)s ORDER BY pa.nome",
    "funcionarios.csv": "SELECT p.id, p.nome, f.cargo, p.status FROM funcionarios f JOIN pessoas p ON p.id = f.pessoa_id WHERE p.unidade_id = %(u)s ORDER BY p.nome",
    "consentimentos.csv": "SELECT p.nome AS pessoa, coalesce(c.escopo, 'biometria') AS escopo, c.aceito, c.canal, c.assinado_por, c.assinado_em, "
                          "c.criado_em AT TIME ZONE %(f)s AS registrado_em FROM consentimentos_lgpd c JOIN pessoas p ON p.id = c.pessoa_id "
                          "WHERE p.unidade_id = %(u)s ORDER BY c.id",
    "eventos_12_meses.csv": "SELECT e.ocorrido_em AT TIME ZONE %(f)s AS quando, p.nome AS pessoa, e.tipo, e.sentido, d.nome AS catraca, "
                            "pc.nome AS com_quem, e.justificativa, ua.nome AS autorizado_por FROM eventos e LEFT JOIN pessoas p ON p.id = e.pessoa_id "
                            "LEFT JOIN pessoas pc ON pc.id = e.correlacao_id LEFT JOIN dispositivos d ON d.id = e.dispositivo_id "
                            "LEFT JOIN usuarios_admin ua ON ua.id = e.autorizado_por WHERE e.unidade_id = %(u)s AND e.ocorrido_em > now() - interval '12 months' "
                            "ORDER BY e.ocorrido_em",
}


@bp.get("/unidades/<int:unidade_id>/exportar.zip")
@exige("configurar_escola")
def unidade_exportar(unidade_id):
    """Revisão (O11): a escola (ou o fim do contrato) precisa levar os dados dela. Planilhas
    CSV abertas no Excel; fotos ficam de fora (dado biométrico não sai do sistema)."""
    import csv
    import io
    import zipfile

    from flask import Response

    un = unidade_atual_ou_403()
    if un["id"] != unidade_id:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, sql in _EXPORTACAO.items():
            linhas = db.query(sql, {"u": unidade_id, "f": un["fuso"]})
            texto = io.StringIO()
            w = csv.writer(texto, delimiter=";")
            if linhas:
                w.writerow(linhas[0].keys())
                for r in linhas:
                    w.writerow(["" if v is None else v for v in r.values()])
            z.writestr(nome, "\ufeff" + texto.getvalue())  # BOM: o Excel abre com acento certo
    auditar("exportar_dados", "unidade", unidade_id)
    nome = "".join(c for c in un["nome"] if c.isalnum()) or "escola"
    return Response(buf.getvalue(), content_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=nexosix-{nome}.zip", "Cache-Control": "no-store"})


@bp.post("/unidades/<int:unidade_id>/codigo-instalacao")
@exige("gerir_dispositivos")
def codigo_instalacao(unidade_id):
    """Código de uso único (24 h) que o instalador do servidor da escola pede. Aparece uma vez só."""
    from ..espelho import instalacao

    un = unidade_atual_ou_403()
    if un["id"] != unidade_id:
        abort(404)
    codigo, expira = instalacao.gerar(unidade_id, g.usuario["id"])
    auditar("codigo_instalacao", "unidade", unidade_id)
    flash(codigo, "codigo")
    return redirect(url_for("painel.unidade_config", unidade_id=unidade_id))


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
        if bool(t["exige_responsavel_saida"]) != campos[2]:
            from ..dispositivos import service as dispositivos

            n = dispositivos.enfileirar_regras(turma_id=turma_id)
            flash(f"Regras de {n} aluno(s) reenviadas à catraca", "info")
    else:
        row = db.execute("INSERT INTO turmas (unidade_id, nome, turno, exige_responsavel_saida, tolerancia_entrada_min, entrada_inicio, entrada_fim, saida_inicio, saida_fim, ativo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", (un["id"],) + campos, returning=True)
        auditar("criar", "turma", row["id"], depois={"nome": campos[0]})
    flash("Turma salva", "success")
    return redirect(url_for("painel.turmas"))
