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


def dominio():
    return (os.environ.get("FAILOVER_DOMINIO") or "").strip().lower().strip(".")


def nome_valido(nome):
    """Só nomes dentro do domínio da Nexosix. Revisão (S6): o nome era um campo livre na
    configuração da escola e o vigia, com o token da Cloudflare, apontava QUALQUER nome da zona
    — um admin de escola podia sequestrar o site principal escrevendo "www" ali."""
    d = dominio()
    nome = (nome or "").strip().lower().strip(".")
    return bool(d and nome.endswith("." + d) and all(c.isalnum() or c in "-." for c in nome))


def nome_padrao(unidade_id):
    d = dominio()
    return f"catraca-u{int(unidade_id)}.{d}" if d else None


def nome_da_unidade(unidade, gravar=True):
    """Nome DNS que a catraca desta escola usa. Calculado (catraca-u<id>.<domínio>); o
    superadmin pode trocar, desde que dentro do domínio. Gravado em unidades.config pra descer
    pro agente, que configura a catraca com ele."""
    cfg = unidade.get("config") or {}
    nome = cfg.get("dns_nome") if nome_valido(cfg.get("dns_nome")) else nome_padrao(unidade["id"])
    if nome and gravar and cfg.get("dns_nome") != nome:
        db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"dns_nome": nome}), unidade["id"]))
        unidade["config"] = {**cfg, "dns_nome": nome}
    return nome


def _marcar_failover(unidade, campos):
    atual = {**(((unidade.get("config") or {}).get("failover")) or {}), **campos}
    db.execute("UPDATE unidades SET config = config || %s WHERE id = %s", (Json({"failover": atual}), unidade["id"]), outbox=False)
    unidade["config"] = {**(unidade.get("config") or {}), "failover": atual}


def ativar(unidade, dns=None):
    """Agente mudo → nome da catraca passa a apontar pra nuvem."""
    alvo = os.environ.get("FAILOVER_NUVEM_ALVO", "")
    nome = nome_da_unidade(unidade)
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
    _marcar_failover(unidade, {"ativo": True, "desde": _agora(), "saudavel_desde": None, "ip_apontado": None})
    db.contexto.unidade_id = unidade["id"]
    try:
        eventos.registrar(unidade["id"], tipos.FAILOVER_NUVEM, eventos.agora_utc(), origem="sistema", detalhes={"dns": nome, "alvo": alvo})
    finally:
        db.contexto.unidade_id = None
    log.warning("FAILOVER: nuvem assumiu %s (%s → %s)", unidade["nome"], nome, alvo)
    return True


def _ip_lan(unidade):
    ip = ((unidade.get("config") or {}).get("agente") or {}).get("ip_lan")
    return ip if ip and espelho.ip_privado(ip) else None


def encerrar(unidade, dns=None):
    """Agente voltou → nome volta pro IP dele na LAN."""
    nome = nome_da_unidade(unidade)
    ip_lan = _ip_lan(unidade)
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
    _marcar_failover(unidade, {"ativo": False, "desde": None, "saudavel_desde": None, "encerrado_em": _agora(), "ip_apontado": ip_lan})
    db.contexto.unidade_id = unidade["id"]
    try:
        eventos.registrar(unidade["id"], tipos.FAILOVER_FIM, eventos.agora_utc(), origem="sistema", detalhes={"dns": nome, "alvo": ip_lan})
    finally:
        db.contexto.unidade_id = None
    log.warning("failover encerrado: %s voltou para o agente (%s)", unidade["nome"], ip_lan)
    return True


def em_failover(unidade):
    return bool(((unidade.get("config") or {}).get("failover") or {}).get("ativo"))


def _segundos_desde(iso):
    if not iso:
        return 0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def verificar(dns=None, apos_seg=None, volta_seg=None):
    """Roda no worker da nuvem. Devolve lista de (unidade, 'ativado'|'encerrado'|'ip').

    Revisão (E6/E12/E16):
      - o agente que dá sinal mas cujo site (o que a catraca chama) caiu também conta como fora;
      - voltar pro agente só depois de `volta_seg` saudável seguido — um PC que liga e desliga
        fazia o DNS pular a cada 30 s e a catraca ficava sem servidor no meio da troca;
      - em operação normal o registro acompanha o IP atual do PC (o roteador troca o IP)."""
    apos_seg = apos_seg or int(os.environ.get("FAILOVER_APOS_SEG", "120"))
    volta_seg = int(os.environ.get("FAILOVER_VOLTA_SEG", "120")) if volta_seg is None else volta_seg
    mudancas = []
    for u in db.query("SELECT * FROM unidades WHERE ativo AND config ? 'espelho_token'"):
        nome = nome_da_unidade(u)
        if not nome:
            continue
        agente = (u.get("config") or {}).get("agente") or {}
        web_fora = agente.get("web_ok") is False and _segundos_desde(agente.get("web_falha_desde")) > apos_seg
        doente = espelho.agente_silencioso(u, apos_seg) or web_fora
        fo = (u.get("config") or {}).get("failover") or {}
        if doente:
            if fo.get("saudavel_desde"):
                _marcar_failover(u, {"saudavel_desde": None})
            if not em_failover(u) and ativar(u, dns):
                mudancas.append((u, "ativado"))
            continue
        if em_failover(u):
            if not fo.get("saudavel_desde"):
                _marcar_failover(u, {"saudavel_desde": _agora()})
            if _segundos_desde(u["config"]["failover"]["saudavel_desde"]) >= volta_seg and encerrar(u, dns):
                mudancas.append((u, "encerrado"))
            continue
        ip = _ip_lan(u)
        if ip and fo.get("ip_apontado") != ip:
            try:
                if (dns or provedor()).apontar(nome, ip, "A"):
                    _marcar_failover(u, {"ip_apontado": ip})
                    log.info("DNS da catraca de %s agora aponta para %s", u["nome"], ip)
                    mudancas.append((u, "ip"))
            except Exception as e:  # noqa: BLE001
                log.error("DNS %s → %s falhou: %s", nome, ip, e)
    return mudancas
