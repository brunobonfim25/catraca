"""Fábrica de adaptadores: a partir da linha de `dispositivos` devolve quem fala com o hardware."""
import os

from .base_adapter import BaseAdapter, DispositivoErro, FotoRejeitada  # noqa: F401


def adaptador_para(dispositivo, simulador=None):
    """`dispositivo` é a linha da tabela. Com CONTROLID_SIMULADOR=1 (ou fabricante
    'simulador') o adaptador aponta pro simulador HTTP — útil em dev sem catraca."""
    fabricante = (dispositivo.get("fabricante") or "controlid").lower()
    if simulador is None:
        simulador = os.environ.get("CONTROLID_SIMULADOR", "0") == "1"
    if fabricante in ("controlid", "simulador"):
        from .controlid.adapter import ControlIdAdapter

        cfg = dispositivo.get("config") or {}
        host, porta = dispositivo.get("ip") or "127.0.0.1", dispositivo.get("porta") or 80
        # A nuvem só alcança a catraca pela VPN (endereço cadastrado em config.ip_vpn)
        if os.environ.get("MODO", "local") == "nuvem" and cfg.get("ip_vpn"):
            host, porta = cfg["ip_vpn"], int(cfg.get("porta_vpn") or porta)
        return ControlIdAdapter(
            host=host,
            porta=porta,
            usuario=dispositivo.get("usuario_api") or "admin",
            senha=dispositivo.get("senha_api") or "admin",
        )
    raise DispositivoErro(f"fabricante sem adaptador: {fabricante}")
