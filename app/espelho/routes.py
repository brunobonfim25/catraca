"""Nuvem: recebe o espelho; e trava escrita no painel (somente leitura)."""
import logging

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for

from .. import db
from ..auth.rbac import login_obrigatorio
from . import service

bp = Blueprint("espelho", __name__)
log = logging.getLogger(__name__)


@bp.post("/api/espelho/<token>")
def receber(token):
    if current_app.config.get("MODO") != "nuvem":
        abort(404)
    unidade = service.unidade_por_token(token)
    if not unidade:
        abort(403)
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get("itens") or []
    try:
        aplicados, rejeitados = service.aplicar_lote(unidade, itens)
    except Exception as e:  # noqa: BLE001 — o local vai reenviar; o que importa é logar o motivo
        log.exception("espelho da unidade %s falhou", unidade["id"])
        db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", ('{"espelho_erro": %s}' % _json_str(str(e)[:300]), unidade["id"]))
        return jsonify({"ok": False, "erro": str(e)[:300]}), 500
    db.execute("""UPDATE unidades SET config = config - 'espelho_erro' WHERE id = %s""", (unidade["id"],))
    return jsonify({"ok": True, "aplicados": aplicados, "rejeitados": rejeitados})


def _json_str(s):
    import json

    return json.dumps(s, ensure_ascii=False)


@bp.get("/api/espelho/status")
@login_obrigatorio
def status():
    rows = db.query("SELECT id, nome, config->>'espelho_ultimo_em' AS ultimo_em, config->>'espelho_erro' AS erro FROM unidades ORDER BY nome")
    return jsonify([dict(r) for r in rows])


# Escritas permitidas na nuvem: entrar/sair, senha, usuários da nuvem, criar empresa/unidade
# (é a nuvem quem define os ids que o servidor local usa).
ESCRITA_PERMITIDA = {
    "auth.login", "auth.logout", "auth.trocar_senha", "auth.usuario_novo", "auth.usuario_ativo",
    "painel.empresa_nova", "painel.unidade_nova", "espelho.receber",
}


def instalar_somente_leitura(app):
    @app.before_request
    def _bloquear_escrita():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if request.path.startswith("/api/") or request.endpoint in ESCRITA_PERMITIDA:
            return None
        flash("Painel da rede é somente leitura. Faça a alteração no servidor da escola.", "warning")
        return redirect(request.referrer or url_for("painel.dashboard"))
