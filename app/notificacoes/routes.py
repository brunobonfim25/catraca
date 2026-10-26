"""Webhook do Telegram + tela de ativação (QR) por responsável."""
import hmac

from flask import Blueprint, Response, abort, current_app, g, render_template, request

from .. import db
from ..auth.rbac import exige
from . import telegram

bp = Blueprint("notificacoes", __name__)


@bp.post("/api/telegram/webhook")
def webhook():
    segredo = current_app.config.get("TELEGRAM_WEBHOOK_SECRET") or ""
    recebido = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not segredo or not hmac.compare_digest(segredo, recebido):
        abort(403)
    telegram.responder_update(request.get_json(silent=True) or {})
    return {"ok": True}


@bp.get("/telegram/ativar/<int:responsavel_id>")
@exige("cadastrar_pessoas")
def ativar(responsavel_id):
    r = db.query("SELECT r.*, p.nome, p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE r.id = %s", (responsavel_id,), one=True)
    if not r or (g.unidade and r["unidade_id"] != g.unidade["id"]):
        abort(404)
    token = r["telegram_token"] or telegram.gerar_token(responsavel_id)
    link = telegram.link_ativacao(token)
    return render_template("notificacoes/ativar.html", responsavel=r, link=link, bot=current_app.config.get("TELEGRAM_BOT_USERNAME"))


@bp.get("/telegram/ativar/<int:responsavel_id>/qr.png")
@exige("cadastrar_pessoas")
def qr(responsavel_id):
    r = db.query("SELECT telegram_token FROM responsaveis WHERE id = %s", (responsavel_id,), one=True)
    if not r or not r["telegram_token"]:
        abort(404)
    return Response(telegram.qr_png(telegram.link_ativacao(r["telegram_token"])), content_type="image/png")


@bp.post("/telegram/ativar/<int:responsavel_id>/novo-token")
@exige("cadastrar_pessoas")
def novo_token(responsavel_id):
    telegram.gerar_token(responsavel_id)
    from flask import redirect, url_for

    return redirect(url_for("notificacoes.ativar", responsavel_id=responsavel_id))
