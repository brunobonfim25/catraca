"""Endpoints da sincronização (servidos pela NUVEM) e a página de status do AGENTE."""
import logging

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request

from .. import db
from ..auth.rbac import login_obrigatorio
from . import service

bp = Blueprint("espelho", __name__)
log = logging.getLogger(__name__)


def _unidade_ou_403(token):
    if current_app.config.get("MODO") != "nuvem":
        abort(404)
    unidade = service.unidade_por_token(token)
    if not unidade:
        abort(403)
    return unidade


@bp.post("/api/espelho/<token>")
def receber(token):
    """Agente → nuvem: eventos, presença, status."""
    unidade = _unidade_ou_403(token)
    itens = (request.get_json(silent=True) or {}).get("itens") or []
    try:
        aplicados, rejeitados = service.receber_do_agente(unidade, itens)
    except Exception as e:  # noqa: BLE001 — o agente reenvia; o que importa é registrar o motivo
        log.exception("espelho da unidade %s falhou", unidade["id"])
        service._marcar(unidade["id"], {"agente_erro": str(e)[:300]})
        return jsonify({"ok": False, "erro": str(e)[:300]}), 500
    return jsonify({"ok": True, "aplicados": aplicados, "rejeitados": rejeitados})


@bp.get("/api/espelho/<token>/snapshot")
def snapshot(token):
    unidade = _unidade_ou_403(token)
    return jsonify({"ok": True, **service.snapshot(unidade)})


@bp.get("/api/espelho/<token>/reverso")
def reverso(token):
    """Nuvem → agente: o que mudou pra esta unidade desde `desde`."""
    unidade = _unidade_ou_403(token)
    desde = request.args.get("desde", 0, type=int)
    limite = min(request.args.get("limite", 500, type=int), 1000)
    itens = service.lote_pendente(limite, unidade_id=unidade["id"], desde=desde)
    return jsonify({"ok": True, "itens": itens})


@bp.post("/api/espelho/<token>/reverso/confirmar")
def reverso_confirmar(token):
    unidade = _unidade_ou_403(token)
    ate = (request.get_json(silent=True) or {}).get("ate")
    if ate is not None:
        service.confirmar(unidade_id=unidade["id"], ate=int(ate))
    return jsonify({"ok": True})


@bp.post("/api/espelho/<token>/ping")
def ping(token):
    unidade = _unidade_ou_403(token)
    service.registrar_ping(unidade, request.get_json(silent=True) or {})
    return jsonify({"ok": True})


@bp.get("/api/espelho/status")
@login_obrigatorio
def status():
    rows = db.query("SELECT id, nome, config->>'agente_visto_em' AS visto_em, config->>'agente_erro' AS erro, config->'agente' AS agente FROM unidades ORDER BY nome")
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- agente (MODO=local)

@bp.get("/agente")
def agente_status():
    if current_app.config.get("MODO") != "local":
        abort(404)
    est = service.estado_local()
    pend = service.pendentes()
    disps = db.query("SELECT nome, status, ultimo_contato, modo FROM dispositivos WHERE ativo ORDER BY nome")
    un = db.query("SELECT nome FROM unidades WHERE id = %s", (current_app.config.get("UNIDADE_ID"),), one=True)
    fila = db.query("SELECT status, count(*) AS n FROM fila_sync GROUP BY status")
    return render_template("agente.html", estado=est, pendentes=pend, dispositivos=disps, unidade=un, fila=fila,
                           unidade_id=current_app.config.get("UNIDADE_ID"), nuvem=current_app.config.get("NUVEM_URL"))


def instalar_agente_local(app):
    """O agente não tem painel: tudo que não é API da catraca vai pra nuvem."""
    livres = ("/api/", "/static/", "/health", "/agente")

    @app.before_request
    def _so_catraca():
        if request.path == "/" or any(request.path.startswith(p) for p in livres):
            return None
        nuvem = app.config.get("NUVEM_URL")
        return redirect(nuvem + request.path) if nuvem else abort(404)
