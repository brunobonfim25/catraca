"""Catracas/leitores: cadastro, teste de conexão, Monitor, modo online, fila."""
import json

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import db
from ..auditoria import auditar
from ..auth.rbac import exige, unidade_atual_ou_403
from . import service

bp = Blueprint("dispositivos", __name__, url_prefix="/dispositivos")


def _disp(dispositivo_id):
    un = unidade_atual_ou_403()
    d = db.query("SELECT * FROM dispositivos WHERE id = %s", (dispositivo_id,), one=True)
    if not d or d["unidade_id"] != un["id"]:
        abort(404)
    return un, d


@bp.get("/")
@exige("configurar_escola")  # a catraca é assunto da direção/TI; o resto vê o status dela no "Agora"
def lista():
    un = unidade_atual_ou_403()
    return render_template("dispositivos/lista.html", dispositivos=service.listar(un["id"], apenas_ativos=False), fila=service.resumo_fila(un["id"]))


def _salvar(d, f):
    campos = dict(
        nome=f["nome"].strip(), fabricante=f.get("fabricante") or "controlid", modelo=f.get("modelo"), ip=f.get("ip", "").strip(),
        porta=int(f.get("porta") or 80), usuario_api=f.get("usuario_api") or "admin", funcao=f.get("funcao") or "catraca",
        sentido=f.get("sentido") or "bidirecional", modo=f.get("modo") or "standalone", ativo=f.get("ativo") == "1",
    )
    cfg = {"allow_entrada": f.get("allow_entrada") or "clockwise", "allow_saida": f.get("allow_saida") or "anticlockwise",
           "ip_vpn": (f.get("ip_vpn") or "").strip() or None}
    if d:
        sets = ", ".join(f"{k} = %s" for k in campos)
        params = list(campos.values()) + [json.dumps(cfg), d["id"]]
        db.execute(f"UPDATE dispositivos SET {sets}, config = config || %s WHERE id = %s", params)
        if f.get("senha_api"):
            db.execute("UPDATE dispositivos SET senha_api = %s WHERE id = %s", (f["senha_api"], d["id"]))
        auditar("editar", "dispositivo", d["id"], depois=campos)
        return d["id"]
    row = db.execute(
        "INSERT INTO dispositivos (unidade_id, nome, fabricante, modelo, ip, porta, usuario_api, senha_api, funcao, sentido, modo, ativo, config) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (g.unidade["id"], campos["nome"], campos["fabricante"], campos["modelo"], campos["ip"], campos["porta"], campos["usuario_api"],
         f.get("senha_api") or None, campos["funcao"], campos["sentido"], campos["modo"], campos["ativo"], json.dumps(cfg)), returning=True,
    )
    service.criar_leitores_padrao(row["id"])
    auditar("criar", "dispositivo", row["id"], depois=campos)
    return row["id"]


@bp.route("/novo", methods=["GET", "POST"])
@exige("gerir_dispositivos")
def novo():
    unidade_atual_ou_403()
    if request.method == "POST":
        did = _salvar(None, request.form)
        n = service.enfileirar_todos_da_unidade(did, g.unidade["id"])
        flash(f"Catraca cadastrada. {n} pessoa(s) enfileirada(s) para sincronizar.", "success")
        return redirect(url_for("dispositivos.detalhe", dispositivo_id=did))
    return render_template("dispositivos/form.html", d=None)


@bp.route("/<int:dispositivo_id>/editar", methods=["GET", "POST"])
@exige("gerir_dispositivos")
def editar(dispositivo_id):
    un, d = _disp(dispositivo_id)
    if request.method == "POST":
        _salvar(d, request.form)
        flash("Catraca atualizada", "success")
        return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))
    return render_template("dispositivos/form.html", d=d)


@bp.get("/<int:dispositivo_id>")
@exige("configurar_escola")
def detalhe(dispositivo_id):
    un, d = _disp(dispositivo_id)
    leitores = db.query("SELECT * FROM dispositivo_leitores WHERE dispositivo_id = %s ORDER BY id", (dispositivo_id,))
    fila = db.query(
        "SELECT f.*, p.nome AS pessoa_nome FROM fila_sync f LEFT JOIN pessoas p ON p.id = f.pessoa_id WHERE f.dispositivo_id = %s "
        "ORDER BY CASE f.status WHEN 'erro' THEN 0 WHEN 'processando' THEN 1 WHEN 'pendente' THEN 2 ELSE 3 END, f.id DESC LIMIT 100", (dispositivo_id,),
    )
    pessoas = db.query(
        "SELECT dp.*, p.nome, p.tipo FROM dispositivo_pessoa dp JOIN pessoas p ON p.id = dp.pessoa_id WHERE dp.dispositivo_id = %s "
        "ORDER BY CASE dp.foto_sync_status WHEN 'erro' THEN 0 WHEN 'pendente' THEN 1 ELSE 2 END, p.nome LIMIT 300", (dispositivo_id,),
    )
    return render_template("dispositivos/detalhe.html", d=d, leitores=leitores, fila=fila, pessoas=pessoas, fuso=un["fuso"],
                           agente=(un["config"] or {}).get("agente") or {}, agente_visto_em=(un["config"] or {}).get("agente_visto_em"))


# Quem fala com a catraca é o worker (na escola). O painel só enfileira o comando e o
# resultado aparece em segundos no status do dispositivo / na fila.
@bp.post("/<int:dispositivo_id>/testar")
@exige("gerir_dispositivos")
def testar(dispositivo_id):
    _disp(dispositivo_id)
    service.enfileirar(dispositivo_id, "testar")
    flash("Teste de conexão enviado ao servidor da escola. Atualize em instantes: serial/firmware/device_id aparecem aqui.", "info")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/monitor")
@exige("gerir_dispositivos")
def configurar_monitor(dispositivo_id):
    _disp(dispositivo_id)
    service.enfileirar(dispositivo_id, "configurar_monitor")
    auditar("configurar_monitor", "dispositivo", dispositivo_id)
    flash("Configuração do Monitor enfileirada (a catraca passa a avisar o servidor da escola).", "info")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/modo")
@exige("gerir_dispositivos")
def modo(dispositivo_id):
    un, d = _disp(dispositivo_id)
    novo = request.form.get("modo")
    if novo not in ("standalone", "pro"):
        abort(400)
    service.enfileirar(dispositivo_id, "modo", payload={"modo": novo})
    auditar("modo", "dispositivo", dispositivo_id, antes={"modo": d["modo"]}, depois={"modo": novo})
    flash(f"Mudança para {novo} enfileirada.", "info")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/reconciliar")
@exige("gerir_dispositivos")
def reconciliar(dispositivo_id):
    """Depois de reset ou troca da catraca: confere quem é quem nela e reenvia o que faltar."""
    _disp(dispositivo_id)
    service.enfileirar(dispositivo_id, "reconciliar")
    auditar("reconciliar", "dispositivo", dispositivo_id)
    flash("Reconciliação enfileirada: o servidor da escola confere o cadastro da catraca e reenvia o que faltar.", "info")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/hora")
@exige("gerir_dispositivos")
def acertar_hora(dispositivo_id):
    un, d = _disp(dispositivo_id)
    service.enfileirar(dispositivo_id, "set_clock", payload={"fuso": un["fuso"]})
    flash("Acerto de hora enfileirado", "success")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/sincronizar-todos")
@exige("gerir_dispositivos")
def sincronizar_todos(dispositivo_id):
    un, d = _disp(dispositivo_id)
    n = service.enfileirar_todos_da_unidade(dispositivo_id, un["id"])
    flash(f"{n} pessoa(s) enfileirada(s)", "success")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/fila/<int:item_id>/reenviar")
@exige("gerir_dispositivos")
def reenviar(dispositivo_id, item_id):
    _disp(dispositivo_id)
    db.execute("UPDATE fila_sync SET status = 'pendente', tentativas = 0, proximo_em = now(), erro = NULL WHERE id = %s AND dispositivo_id = %s", (item_id, dispositivo_id))
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))


@bp.post("/<int:dispositivo_id>/leitores")
@exige("gerir_dispositivos")
def leitores(dispositivo_id):
    """Mapeia component_id/portal_id → entrada/saída (validar no hardware)."""
    _disp(dispositivo_id)
    f = request.form
    db.execute("DELETE FROM dispositivo_leitores WHERE dispositivo_id = %s", (dispositivo_id,))
    for i in range(int(f.get("n") or 0)):
        comp, portal, sentido = f.get(f"component_{i}") or None, f.get(f"portal_{i}"), f.get(f"sentido_{i}")
        if sentido in ("entrada", "saida") and (comp or portal):
            db.execute("INSERT INTO dispositivo_leitores (dispositivo_id, component_id, portal_id, sentido, descricao) VALUES (%s,%s,%s,%s,%s)",
                       (dispositivo_id, comp, int(portal) if portal else None, sentido, f.get(f"descricao_{i}")))
    auditar("leitores", "dispositivo", dispositivo_id)
    # A regra de contingência usa os portais de entrada: mudou o mapeamento, reenvia as regras (C6)
    n = service.enfileirar_regras(dispositivo_id=dispositivo_id)
    flash(f"Mapeamento de leitores salvo; regras de {n} pessoa(s) reenviadas à catraca", "success")
    return redirect(url_for("dispositivos.detalhe", dispositivo_id=dispositivo_id))
