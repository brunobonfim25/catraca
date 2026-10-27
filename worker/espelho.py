"""Rodada do agente: puxa o que a nuvem mudou, sobe o que aconteceu aqui, dá sinal de vida."""
import logging
import os
import socket
import threading
from datetime import datetime, timezone

import requests

from app import db
from app.espelho import service

log = logging.getLogger(__name__)
VERSAO = "2"


def _cfg():
    return os.environ.get("NUVEM_URL", "").rstrip("/"), os.environ.get("ESPELHO_TOKEN", ""), int(os.environ.get("UNIDADE_ID", "0") or 0)


def garantir_carga_inicial():
    nuvem, token, unidade_id = _cfg()
    if not (nuvem and token and unidade_id):
        log.error("agente sem NUVEM_URL / ESPELHO_TOKEN / UNIDADE_ID")
        return False
    if not service.precisa_carga():
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
    except service.RecargaNecessaria as e:
        log.warning("nuvem pediu recarga: %s", e)
        service.marcar_recarga()
        return 0
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
    """IP deste PC na rede da escola (o que o DNS da catraca aponta em operação normal).
    Descoberto a cada ping: o roteador troca o IP do PC e o IP gravado na instalação envelhecia."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip if service.ip_privado(ip) else None
    except OSError:
        return None


def web_ok():
    """O site deste PC (o que a catraca chama) responde? O worker vivo com o site caído fazia a
    nuvem achar que estava tudo bem enquanto a catraca não tinha com quem falar."""
    porta = os.environ.get("PORT", "80")
    try:
        return requests.get(f"http://127.0.0.1:{porta}/health", timeout=5).ok
    except requests.RequestException:
        return False


def _catraca_contato():
    r = db.query("SELECT max(ultimo_contato) AS m FROM dispositivos WHERE ativo", one=True)
    return r["m"].isoformat() if r and r["m"] else None


def ping():
    nuvem, token, _ = _cfg()
    p = service.pendentes()
    est = service.estado_local() or {}
    resp = service.ping(nuvem, token, {
        "versao": VERSAO, "schema": service.versao_schema(), "base_url": os.environ.get("BASE_URL", ""), "ip_lan": ip_lan(),
        "outbox_pendentes": p["n"], "cursor": {"xid": est.get("cursor_xid"), "id": est.get("cursor_nuvem")},
        "web_ok": web_ok(), "catraca_contato": _catraca_contato(), "desvio_relogio_seg": est.get("desvio_relogio_seg"),
    })
    if resp and resp.get("agora"):
        desvio = int((datetime.fromisoformat(resp["agora"]) - datetime.now(timezone.utc)).total_seconds())
        db.execute("UPDATE espelho_estado SET desvio_relogio_seg = %s WHERE id = 1", (desvio,), outbox=False)
        if abs(desvio) > 60:
            log.error("relógio deste PC está %+d s diferente da nuvem: acerte a hora do Windows", desvio)
    if resp and resp.get("schema_nuvem") and resp["schema_nuvem"] != service.versao_schema():
        log.warning("banco deste agente (%s) difere da nuvem (%s): rode o instalador de novo para atualizar",
                    service.versao_schema(), resp["schema_nuvem"])
    return resp


def iniciar_ping(intervalo=30):
    """Sinal de vida numa thread própria. Revisão (E6): o ping ficava no fim da rodada; uma
    catraca lenta (timeout de foto, 3 mil alunos na fila) atrasava a rodada além dos 2 min e a
    nuvem assumia a catraca com o agente vivo."""
    parar = threading.Event()

    def _laco():
        while not parar.is_set():
            try:
                ping()
            except Exception:  # noqa: BLE001 — o ping nunca pode morrer
                log.exception("ping falhou")
            parar.wait(intervalo)

    threading.Thread(target=_laco, name="ping", daemon=True).start()
    return parar
