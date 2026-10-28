"""Proteções transversais do painel (revisão de 27/10/2026, onda 5).

- CSRF (S13): todo POST de navegador leva um token da sessão. Sem isso, uma página qualquer
  aberta no PC da recepção podia disparar "liberar catraca" na sessão logada.
- Cabeçalhos (S19): o painel não pode ser embutido em outro site (clickjacking) etc.
- Chave de sessão (S18): a nuvem não sobe com a chave padrão — com ela qualquer um forja o
  cookie de superadmin.
"""
import hmac
import secrets

from flask import abort, request, session

# Quem chama sem navegador: catraca, agente da escola, Telegram. Têm autenticação própria.
SEM_CSRF = ("/api/notifications/", "/api/catraca/", "/api/espelho/", "/api/telegram/webhook", "/api/railway/webhook")
CHAVES_FRACAS = {"", "dev-insegura", "teste", "troque-esta-chave", "changeme"}


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def instalar(app):
    if app.config.get("MODO") == "nuvem" and not app.config.get("TESTING"):
        chave = app.config.get("SECRET_KEY") or ""
        if chave in CHAVES_FRACAS or len(chave) < 16:
            raise RuntimeError("SECRET_KEY fraca ou padrão: defina uma chave aleatória (32+ caracteres) na nuvem")

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _csrf():
        if not app.config.get("CSRF_ATIVO", True) or request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.path.startswith(SEM_CSRF):
            return None
        enviado = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        esperado = session.get("_csrf") or ""
        if not esperado or not hmac.compare_digest(enviado, esperado):
            abort(400, "Formulário expirado: recarregue a página e tente de novo.")
        return None

    @app.after_request
    def _cabecalhos(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        if app.config.get("MODO") == "nuvem":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp
