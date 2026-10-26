"""Rodada do espelho (servidor local → nuvem)."""
import logging
import os

from app.espelho import service

log = logging.getLogger(__name__)


def enviar():
    nuvem = os.environ.get("NUVEM_URL", "").rstrip("/")
    token = os.environ.get("ESPELHO_TOKEN", "")
    if not nuvem or not token:
        return 0
    try:
        return service.enviar_pendentes(nuvem, token)
    except service.EspelhoErro as e:
        p = service.pendentes()
        log.warning("espelho: %s (%s itens pendentes desde %s)", e, p["n"], p["desde"])
        return 0
