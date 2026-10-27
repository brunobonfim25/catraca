"""Redundância: a catraca sempre consulta o agente da escola; se o agente some, a nuvem assume.

Como a catraca tem UM endereço de servidor (Monitor e modo Pro), ela é configurada com um
NOME (ex.: catraca-recreio.nexosix.com.br), não com IP:
  normal   → o nome resolve para o IP do agente na LAN (A 192.168.x.x, TTL 60 s)
  failover → o nome aponta para a nuvem (CNAME do Railway); a catraca, que é quem inicia as
             conexões, passa a mandar Monitor e perguntar o modo Pro para a nuvem sem VPN
Quando o agente volta a dar sinal, o DNS volta pra LAN e o agente puxa da nuvem tudo que
aconteceu enquanto esteve fora (outbox marcado com a unidade).

[HW] confirmar na catraca: resolução de nome em devices.ip / monitor.hostname (documentado),
     aceitar HTTP na porta 80 pela internet (Cloudflare com "Always use HTTPS" desligado),
     e TTL do cache de DNS do equipamento.
"""
import logging
import os
from datetime import datetime, timezone

import requests
from psycopg2.extras import Json

from .. import db
from ..eventos import service as eventos
from ..eventos import tipos
from . import service as espelho

log = logging.getLogger(__name__)


class DnsNulo:
    """Sem provedor configurado: só registra o que faria (útil em dev e enquanto não há domínio)."""

    def __init__(self):
        self.chamadas = []

    def apontar(self, nome, alvo, tipo):
        self.chamadas.append((nome, alvo, tipo))
        log.warning("DNS não configurado: %s deveria apontar para %s (%s)", nome, alvo, tipo)
        return True


class DnsCloudflare:
    """Troca o conteúdo do registro `nome` na zona (A ↔ CNAME). API token com Zone.DNS:Edit."""

    def __init__(self, token, zona, http=None):
        self.token, self.zona = token, zona
        self.http = http or requests.Session()
        self.base = f"https://api.cloudflare.com/client/v4/zones/{zona}/dns_records"
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def apontar(self, nome, alvo, tipo):
        r = self.http.get(self.base, headers=self.h, params={"name": nome}, timeout=15)
        r.raise_for_status()
        registros = r.json().get("result") or []
        # Em failover o nome passa pela proxy da Cloudflare (aceita HTTP e fala HTTPS com o Railway);
        # em operação normal é só DNS, com o IP privado da LAN.
        corpo = {"type": tipo, "name": nome, "content": alvo, "ttl": 60, "proxied": tipo == "CNAME"}
        if registros:
            r = self.http.put(f"{self.base}/{registros[0]['id']}", headers=self.h, json=corpo, timeout=15)
        else:
            r = self.http.post(self.base, headers=self.h, json=corpo, timeout=15)
        r.raise_for_status()
        return bool(r.json().get("success"))


def provedor():
    token, zona = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ZONE_ID")
    return DnsCloudflare(token, zona) if token and zona else DnsNulo()


def _agora():
    return datetime.now(timezone.utc).isoformat()


def ativar(unidade, dns=None):
    """Agente mudo → nome da catraca passa a apontar pra nuvem."""
    cfg = unidade.get("config") or {}
    alvo = os.environ.get("FAILOVER_NUVEM_ALVO", "")
    nome = cfg.get("dns_nome")
    if not nome or not alvo:
        return False
    dns = dns or provedor()
    try:
        ok = dns.apontar(nome, alvo, "CNAME")
    except Exception as e:  # noqa: BLE001 — provedor fora do ar: tenta na próxima rodada
        log.error("failover %s: DNS falhou: %s", unidade["nome"], e)
        return False
    if not ok:
        return False
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"failover": {"ativo": True, "desde": _agora()}}), unidade["id"]), outbox=False)
    db.contexto.unidade_id = unidade["id"]
    try:
        eventos.registrar(unidade["id"], tipos.FAILOVER_NUVEM, eventos.agora_utc(), origem="sistema", detalhes={"dns": nome, "alvo": alvo})
    finally:
        db.contexto.unidade_id = None
    log.warning("FAILOVER: nuvem assumiu %s (%s → %s)", unidade["nome"], nome, alvo)
    return True


def encerrar(unidade, dns=None):
    """Agente voltou → nome volta pro IP dele na LAN."""
    cfg = unidade.get("config") or {}
    nome = cfg.get("dns_nome")
    ip_lan = (cfg.get("agente") or {}).get("ip_lan")
    if not nome or not ip_lan:
        return False
    dns = dns or provedor()
    try:
        ok = dns.apontar(nome, ip_lan, "A")
    except Exception as e:  # noqa: BLE001
        log.error("fim do failover %s: DNS falhou: %s", unidade["nome"], e)
        return False
    if not ok:
        return False
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"failover": {"ativo": False, "desde": None, "encerrado_em": _agora()}}), unidade["id"]), outbox=False)
    db.contexto.unidade_id = unidade["id"]
    try:
        eventos.registrar(unidade["id"], tipos.FAILOVER_FIM, eventos.agora_utc(), origem="sistema", detalhes={"dns": nome, "alvo": ip_lan})
    finally:
        db.contexto.unidade_id = None
    log.warning("failover encerrado: %s voltou para o agente (%s)", unidade["nome"], ip_lan)
    return True


def em_failover(unidade):
    return bool(((unidade.get("config") or {}).get("failover") or {}).get("ativo"))


def verificar(dns=None, apos_seg=None):
    """Roda no worker da nuvem. Devolve lista de (unidade, 'ativado'|'encerrado')."""
    apos_seg = apos_seg or int(os.environ.get("FAILOVER_APOS_SEG", "120"))
    mudancas = []
    for u in db.query("SELECT * FROM unidades WHERE ativo AND config ? 'dns_nome'"):
        mudo = espelho.agente_silencioso(u, apos_seg)
        if mudo and not em_failover(u):
            if ativar(u, dns):
                mudancas.append((u, "ativado"))
        elif not mudo and em_failover(u):
            if encerrar(u, dns):
                mudancas.append((u, "encerrado"))
    return mudancas
