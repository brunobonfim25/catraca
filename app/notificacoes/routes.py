"""Webhook do Telegram + tela de ativação (QR) por responsável."""
import hmac

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import db
from ..auth.rbac import exige, unidade_atual_ou_403
from ..escopo import da_unidade
from . import convites as convites_mod
from . import email as email_mod
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
    # Antes: sem escola selecionada (usuário da rede) passava qualquer responsável, de qualquer rede
    r = da_unidade("responsavel", responsavel_id)
    un = unidade_atual_ou_403()
    # mesmo texto do convite em massa (editável em Configuração › Escola)
    c = convites_mod.montar(un, responsavel_id)
    return render_template("notificacoes/ativar.html", responsavel=c["responsavel"], link=c["link"], bot=current_app.config.get("TELEGRAM_BOT_USERNAME"),
                           whatsapp=telegram.link_whatsapp(r["telefone"], c["texto"]), link_escola=telegram.link_escola(r["unidade_id"]))


@bp.get("/telegram/escola/<int:unidade_id>/qr.png")
@exige("ver_presenca")
def qr_escola(unidade_id):
    return Response(telegram.qr_png(telegram.link_escola(unidade_id)), content_type="image/png")


@bp.get("/telegram/ativar/<int:responsavel_id>/qr.png")
@exige("cadastrar_pessoas")
def qr(responsavel_id):
    r = da_unidade("responsavel", responsavel_id)
    if not r["telegram_token"]:
        abort(404)
    return Response(telegram.qr_png(telegram.link_ativacao(r["telegram_token"])), content_type="image/png")


@bp.post("/telegram/ativar/<int:responsavel_id>/novo-token")
@exige("cadastrar_pessoas")
def novo_token(responsavel_id):
    da_unidade("responsavel", responsavel_id)
    telegram.gerar_token(responsavel_id)
    from ..auditoria import auditar

    auditar("novo_link_telegram", "responsavel", responsavel_id)
    from flask import redirect, url_for

    return redirect(url_for("notificacoes.ativar", responsavel_id=responsavel_id))


# ---------------------------------------------------------------- convites em massa

def logo_url(unidade):
    """URL pública e absoluta do logotipo (cliente de e-mail não mostra imagem embutida)."""
    v = db.query("SELECT extract(epoch FROM atualizado_em)::bigint AS v FROM unidade_logos WHERE unidade_id = %s", (unidade["id"],), one=True)
    if not v:
        return None
    return f"{current_app.config.get('BASE_URL', '')}{url_for('notificacoes.logo_escola', unidade_id=unidade['id'])}?v={v['v']}"


@bp.get("/convites")
@exige("cadastrar_pessoas")
def convites():
    un = unidade_atual_ou_403()
    filtro = request.args.get("filtro") or None
    return render_template("notificacoes/convites.html", linhas=convites_mod.lista(un["id"], filtro), filtro=filtro,
                           resumo=convites_mod.resumo(un["id"]), email_ok=email_mod.configurado(), fuso=un["fuso"],
                           link_escola=telegram.link_escola(un["id"]))


@bp.post("/convites/<int:responsavel_id>/whatsapp")
@exige("cadastrar_pessoas")
def convite_whatsapp(responsavel_id):
    """O form abre em aba nova (target=_blank) e cai direto no WhatsApp com o texto pronto."""
    un = unidade_atual_ou_403()
    r = db.query("SELECT p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE r.id = %s", (responsavel_id,), one=True)
    if not r or r["unidade_id"] != un["id"]:
        abort(404)
    url = convites_mod.whatsapp(un, responsavel_id, g.usuario["id"])
    if not url:
        flash("Este responsável não tem celular válido no cadastro", "danger")
        return redirect(url_for("notificacoes.convites"))
    return redirect(url)


@bp.post("/convites/email")
@exige("cadastrar_pessoas")
def convites_email():
    un = unidade_atual_ou_403()
    if not email_mod.configurado():
        flash("O envio de e-mail ainda não está configurado (peça à Nexosix). Use o WhatsApp ou o link da escola.", "danger")
        return redirect(url_for("notificacoes.convites"))
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if request.form.get("todos") == "1":
        ids = [x["id"] for x in convites_mod.lista(un["id"], request.form.get("filtro") or None) if x["email"]]
    n, pulados = convites_mod.enfileirar_emails(un, ids, g.usuario["id"], logo_url(un))
    flash(f"{n} e-mail(s) na fila de envio" + (f" · {pulados} pulado(s) (sem e-mail ou já na fila)" if pulados else "") +
          ". Saem em poucos minutos.", "success" if n else "warning")
    return redirect(url_for("notificacoes.convites"))


@bp.get("/convites/<int:responsavel_id>/previa")
@exige("cadastrar_pessoas")
def convite_previa(responsavel_id):
    """Como o e-mail chega para este responsável (abre em aba nova)."""
    un = unidade_atual_ou_403()
    r = db.query("SELECT p.unidade_id FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE r.id = %s", (responsavel_id,), one=True)
    if not r or r["unidade_id"] != un["id"]:
        abort(404)
    c = convites_mod.montar(un, responsavel_id, logo_url(un))
    return Response(c["html"], content_type="text/html; charset=utf-8")


@bp.get("/escola/<int:unidade_id>/logo")
def logo_escola(unidade_id):
    """Público de propósito: é a imagem do topo do e-mail."""
    r = db.query("SELECT conteudo, tipo FROM unidade_logos WHERE unidade_id = %s", (unidade_id,), one=True)
    if not r:
        abort(404)
    return Response(bytes(r["conteudo"]), content_type=r["tipo"], headers={"Cache-Control": "public, max-age=86400"})
