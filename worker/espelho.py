"""Rodada do agente: puxa o que a nuvem mudou, sobe o que aconteceu aqui, dá sinal de vida."""
import logging
import os

from app.espelho import service

log = logging.getLogger(__name__)


def _cfg():
    return os.environ.get("NUVEM_URL", "").rstrip("/"), os.environ.get("ESPELHO_TOKEN", ""), int(os.environ.get("UNIDADE_ID", "0") or 0)


def garantir_carga_inicial():
    nuvem, token, unidade_id = _cfg()
    if not (nuvem and token and unidade_id):
        log.error("agente sem NUVEM_URL / ESPELHO_TOKEN / UNIDADE_ID")
        return False
    if service.estado_local()["cursor_nuvem"] > 0:
        return True
    try:
        n = service.carga_inicial(nuvem, token, unidade_id)
        log.info("carga inicial: %d linhas da nuvem", n)
        return True
    except service.EspelhoErro as e:
        log.warning("carga inicial falhou: %s", e)
        return False


def puxar():
    nuvem, token, unidade_id = _cfg()
    try:
        return service.puxar(nuvem, token, unidade_id)
    except service.EspelhoErro as e:
        log.warning("puxar: %s", e)
        return 0


def empurrar():
    nuvem, token, _ = _cfg()
    try:
        return service.empurrar(nuvem, token)
    except service.EspelhoErro as e:
        p = service.pendentes()
        log.warning("empurrar: %s (%s itens pendentes desde %s)", e, p["n"], p["desde"])
        return 0


def ip_lan():
    """IP deste PC na rede da escola (o que o DNS da catraca aponta em operação normal)."""
    if os.environ.get("AGENTE_IP_LAN"):
        return os.environ["AGENTE_IP_LAN"]
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        return s.getsockname()[0]
    except OSError:
        return None


def ping():
    nuvem, token, _ = _cfg()
    p = service.pendentes()
    service.ping(nuvem, token, {"versao": "1", "base_url": os.environ.get("BASE_URL", ""), "ip_lan": ip_lan(),
                                "outbox_pendentes": p["n"], "cursor": service.estado_local()["cursor_nuvem"]})
