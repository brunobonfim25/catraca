"""Envio de e-mail por API HTTP (Brevo ou Resend).

Por que API e não SMTP: o Railway bloqueia SMTP de saída nos planos Hobby/Trial, e a API
devolve erro claro (remetente não verificado, cota estourada). Formatos conferidos na doc
oficial em 27/10/2026:
- Brevo:  POST https://api.brevo.com/v3/smtp/email, header api-key, 201 {messageId}
          (grátis: 300 e-mails/dia — dá para convidar uma escola inteira em 1–2 dias)
- Resend: POST https://api.resend.com/emails, Authorization: Bearer, {id}

Variáveis: BREVO_API_KEY ou RESEND_API_KEY, e EMAIL_REMETENTE (endereço verificado no
provedor, ex.: avisos@nexosix.com.br).
"""
import os

import requests


def _cfg(chave):
    try:
        from flask import current_app

        return current_app.config.get(chave) or os.environ.get(chave, "")
    except RuntimeError:  # worker, fora do app
        return os.environ.get(chave, "")


def provedor():
    if _cfg("BREVO_API_KEY"):
        return "brevo"
    if _cfg("RESEND_API_KEY"):
        return "resend"
    return None


def configurado():
    return bool(provedor() and _cfg("EMAIL_REMETENTE"))


class EmailErro(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status

    @property
    def cota(self):
        """Cota do dia estourada (Brevo grátis: 300/dia) — não é erro do convite."""
        t = str(self).lower()
        return self.status == 429 or "quota" in t or "limit" in t or "cota" in t


def enviar(para, assunto, html, texto, nome_remetente="Nexosix Acesso", responder_para=None):
    """Manda um e-mail. Levanta EmailErro com a mensagem do provedor."""
    remetente = _cfg("EMAIL_REMETENTE")
    qual = provedor()
    if not qual or not remetente:
        raise EmailErro("envio de e-mail não configurado (BREVO_API_KEY/RESEND_API_KEY e EMAIL_REMETENTE)")
    if qual == "brevo":
        corpo = {"sender": {"name": nome_remetente, "email": remetente}, "to": [{"email": para}],
                 "subject": assunto, "htmlContent": html, "textContent": texto}
        if responder_para:
            corpo["replyTo"] = {"email": responder_para}
        r = requests.post("https://api.brevo.com/v3/smtp/email", json=corpo, timeout=15,
                          headers={"api-key": _cfg("BREVO_API_KEY"), "accept": "application/json"})
    else:
        corpo = {"from": f"{nome_remetente} <{remetente}>", "to": [para], "subject": assunto, "html": html, "text": texto}
        if responder_para:
            corpo["reply_to"] = responder_para
        r = requests.post("https://api.resend.com/emails", json=corpo, timeout=15,
                          headers={"Authorization": f"Bearer {_cfg('RESEND_API_KEY')}"})
    if r.status_code >= 300:
        try:
            msg = r.json().get("message") or r.text[:200]
        except ValueError:
            msg = r.text[:200]
        raise EmailErro(f"{qual} {r.status_code}: {msg}", r.status_code)
    return r.json() if r.content else {}
