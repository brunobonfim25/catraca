"""Endpoints da sincronização (servidos pela NUVEM) e a página de status do AGENTE."""
import hmac
import json
import logging
import os

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request

from .. import db
from ..auditoria import auditar
from ..auth.rbac import login_obrigatorio
from . import service

bp = Blueprint("espelho", __name__)
log = logging.getLogger(__name__)


def _unidade_do_agente():
    """Escola do agente pelo cabeçalho X-Espelho-Token (nunca pela URL: vai pra log de acesso)."""
    if current_app.config.get("MODO") != "nuvem":
        abort(404)
    token = request.headers.get("X-Espelho-Token", "")
    unidade = service.unidade_por_token(token) if token else None
    if not unidade or not hmac.compare_digest((unidade.get("config") or {}).get("espelho_token", ""), token):
        abort(403)
    try:
        service.conferir_agente(unidade, request.headers.get("X-Agente-Id"))
    except service.OutroAgente as e:
        abort(Response(json.dumps({"ok": False, "erro": str(e)}), status=409, content_type="application/json"))
    return unidade


@bp.post("/api/espelho/receber")
def receber():
    """Agente → nuvem: eventos, presença, status."""
    unidade = _unidade_do_agente()
    itens = (request.get_json(silent=True) or {}).get("itens") or []
    try:
        aplicados, rejeitados = service.receber_do_agente(unidade, itens)
    except Exception as e:  # noqa: BLE001 — o agente reenvia; o que importa é registrar o motivo
        log.exception("espelho da unidade %s falhou", unidade["id"])
        service._marcar(unidade["id"], {"agente_erro": str(e)[:300]})
        return jsonify({"ok": False, "erro": str(e)[:300]}), 500
    return jsonify({"ok": True, "aplicados": aplicados, "rejeitados": rejeitados})


@bp.get("/api/espelho/snapshot")
def snapshot():
    unidade = _unidade_do_agente()
    return jsonify({"ok": True, **service.snapshot(unidade)})


@bp.get("/api/espelho/fotos")
def fotos():
    unidade = _unidade_do_agente()
    return jsonify({"ok": True, "itens": service.fotos(unidade, request.args.get("apos", 0, type=int), request.args.get("limite", 50, type=int))})


@bp.get("/api/espelho/reverso")
def reverso():
    """Nuvem → agente: o que mudou pra esta unidade depois do cursor (xid, id)."""
    unidade = _unidade_do_agente()
    limite = max(1, min(request.args.get("limite", 500, type=int), 1000))
    try:
        lote = service.lote_para_agente(unidade, request.args.get("desde_xid", "0"), request.args.get("desde", 0, type=int), limite)
    except service.RecargaNecessaria as e:
        return jsonify({"ok": False, "recarga": True, "erro": str(e)}), 409
    return jsonify({"ok": True, **lote})


@bp.post("/api/espelho/reverso/confirmar")
def reverso_confirmar():
    unidade = _unidade_do_agente()
    ate = (request.get_json(silent=True) or {}).get("ate") or {}
    if isinstance(ate, dict) and ate.get("xid") is not None and ate.get("id") is not None:
        service.confirmar_ate(unidade["id"], ate["xid"], ate["id"])
    return jsonify({"ok": True})


@bp.post("/api/espelho/ping")
def ping():
    unidade = _unidade_do_agente()
    return jsonify({"ok": True, **service.registrar_ping(unidade, request.get_json(silent=True) or {})})


@bp.get("/api/espelho/status")
@login_obrigatorio
def status():
    """Situação dos servidores de escola: só das escolas que o usuário pode ver (antes listava
    IP e erros de todas as escolas de todos os clientes para qualquer usuário logado)."""
    from flask import g

    from ..auth.rbac import tem_permissao, unidades_visiveis

    if not tem_permissao(g.usuario, "configurar_escola"):
        return jsonify([]), 403
    ids = [u["id"] for u in unidades_visiveis(g.usuario)]
    rows = db.query("SELECT id, nome, config->>'agente_visto_em' AS visto_em, config->>'agente_erro' AS erro, config->'agente' AS agente "
                    "FROM unidades WHERE id = ANY(%s) ORDER BY nome", (ids,))
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- instalador do agente

# O .exe é um só para todas as escolas (publicado pelo GitHub Actions, ver deploy/windows/agente).
# Se o repositório virar privado, publique o .exe em outro lugar e aponte AGENTE_EXE_URL pra lá.
AGENTE_EXE_PADRAO = "https://github.com/brunobonfim25/catraca/releases/latest/download/NexosixAgente-Setup.exe"


@bp.get("/unidades/<int:unidade_id>/instalador.exe")
@login_obrigatorio
def instalador_exe(unidade_id):
    from flask import g

    from ..auth.rbac import tem_permissao, unidade_atual_ou_403

    un = unidade_atual_ou_403()
    if un["id"] != unidade_id or not tem_permissao(g.usuario, "gerir_dispositivos"):
        abort(404)
    auditar("baixar_instalador", "unidade", un["id"])
    return redirect(os.environ.get("AGENTE_EXE_URL") or AGENTE_EXE_PADRAO)


def _json_utf8(dados, status=200):
    """Acentos em UTF-8 de verdade (sem \\u00ed): a tela do instalador lê o nome da escola sem
    decodificar escape."""
    return Response(json.dumps(dados, ensure_ascii=False), status=status, content_type="application/json; charset=utf-8")


@bp.post("/api/espelho/ativar")
def ativar():
    """Instalador → nuvem: troca o código de instalação pelo token do agente.

    {"codigo": "ABCD-EFGH", "so_verificar": true} só confere e devolve o nome da escola (a tela
    do instalador mostra antes de instalar). Sem so_verificar, consome o código.
    Erros contam como tentativa de login (mesmo bloqueio progressivo por IP): o código tem 8
    letras, mas sem limite daria pra tentar à vontade."""
    from ..auth.routes import _bloqueado, _falhou, _ip
    from . import instalacao

    if current_app.config.get("MODO") != "nuvem":
        abort(404)
    chaves = [f"ativar:{_ip()}"]
    if _bloqueado(chaves):
        return _json_utf8({"ok": False, "erro": "Muitas tentativas com código errado. Espere alguns minutos."}, 429)
    dados = request.get_json(silent=True) or {}
    codigo = str(dados.get("codigo") or "")
    if dados.get("so_verificar"):
        un = instalacao.verificar(codigo)
        if not un:
            _falhou(chaves)
            return _json_utf8({"ok": False, "erro": "Código inválido ou vencido. Gere outro no painel."}, 404)
        return _json_utf8({"ok": True, "unidade_id": un["id"], "unidade_nome": un["nome"]})
    un, token = instalacao.ativar(codigo)
    if not un:
        _falhou(chaves)
        return _json_utf8({"ok": False, "erro": "Código inválido ou vencido. Gere outro no painel."}, 404)
    maquina = str(dados.get("maquina") or "")[:80]
    auditar("ativar_servidor_escola", "unidade", un["id"], depois={"ip": _ip(), "maquina": maquina}, unidade_id=un["id"])
    log.warning("servidor da escola %s ativado (máquina %s)", un["id"], maquina)
    return _json_utf8({"ok": True, "unidade_id": un["id"], "unidade_nome": un["nome"],
                       "nuvem_url": current_app.config["BASE_URL"], "token": token})


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
    quarentena = db.query("SELECT count(*) AS n FROM espelho_quarentena", one=True)["n"]
    return render_template("agente.html", estado=est, pendentes=pend, dispositivos=disps, unidade=un, fila=fila, quarentena=quarentena,
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
