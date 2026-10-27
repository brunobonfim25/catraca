"""Processo worker (`python -m worker.main`). O que roda depende do MODO:

  unico → tudo: fila da catraca, coleta de logs, Telegram, LGPD (dev ou escola sem nuvem)
  local → agente da escola: puxa da nuvem, fila da catraca, coleta, sobe eventos, ping
  nuvem → Telegram, convites por e-mail, LGPD, limpeza do outbox (a catraca é do agente)

Sem Redis: tudo em tabela PostgreSQL com SKIP LOCKED.
"""
import logging
import os
import time

from dotenv import load_dotenv

from app import db
from . import collect, espelho, lgpd, notify, sync

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


def _a_cada(contadores, chave, segundos):
    if time.time() - contadores.get(chave, 0) >= segundos:
        contadores[chave] = time.time()
        return True
    return False


def rodada_catraca(adaptadores, contadores):
    ok, falhas = sync.processar_fila(adaptadores=adaptadores)
    if ok or falhas:
        log.info("sync: %d ok, %d falhas", ok, falhas)
    if _a_cada(contadores, "coleta", int(os.environ.get("WORKER_COLETA_SEG", "30"))):
        n = collect.coletar_todos(adaptadores)
        if n:
            log.info("coleta: %d logs novos", n)
        collect.verificar_status()


def rodada_nuvem(contadores):
    enviados, falhas = notify.enviar_pendentes()
    if enviados or falhas:
        log.info("telegram: %d enviados, %d falhas", enviados, falhas)
    if _a_cada(contadores, "convites", 30):  # e-mail de convite: lote pequeno, a cota grátis é por dia
        from app.notificacoes import convites

        enviados, falhas = convites.enviar_pendentes()
        if enviados or falhas:
            log.info("convites por e-mail: %d enviados, %d falhas", enviados, falhas)
    if os.environ.get("MODO") == "nuvem" and _a_cada(contadores, "vigia", 30):
        from app.espelho import failover

        for u, acao in failover.verificar():
            log.warning("vigia: failover %s em %s", acao, u["nome"])
    if _a_cada(contadores, "lgpd", 3600):
        n = lgpd.purgar_desligados()
        if n:
            log.info("lgpd: %d fotos purgadas", n)
        from app.espelho import service as esp

        esp.limpar_outbox_da_nuvem()


def _etapa(nome, fn, *args):
    """Cada etapa por si. Revisão (E1): uma exceção no puxar (nuvem fora, item quebrado)
    pulava a fila da catraca e o envio de eventos da rodada — com a internet ruim, a catraca
    ficava sem fotos novas e a nuvem sem eventos até a internet voltar."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("etapa %s falhou", nome)
        return FALHOU


FALHOU = object()


def rodada_agente(adaptadores, contadores):
    carregado = _etapa("carga inicial", espelho.garantir_carga_inicial)
    if carregado is True:
        n = _etapa("puxar", espelho.puxar)
        if n and n is not FALHOU:
            log.info("nuvem → agente: %d linhas", n)
    # A catraca é atendida mesmo sem a carga inicial: o que já está na fila local é executado
    if _etapa("catraca", rodada_catraca, adaptadores, contadores) is FALHOU:
        adaptadores.clear()
    n = _etapa("empurrar", espelho.empurrar)
    if n and n is not FALHOU:
        log.info("agente → nuvem: %d linhas", n)


def main():
    modo = os.environ.get("MODO", "unico")
    db.init(os.environ["DATABASE_URL"], 1, 4)
    db.gucs = {"nexosix.outbox": "on"} if modo in ("nuvem", "local") else {}
    # Marca de escola nas escritas do worker (o site faz isso pela sessão). Sem ela a exclusão
    # LGPD das fotos saía da nuvem sem escola e o PC da escola nunca apagava a foto.
    db.gucs_fn = [lambda: {"nexosix.unidade": db.contexto.unidade_id} if getattr(db.contexto, "unidade_id", None) else {}]
    if modo == "local":
        db.contexto.unidade_id = int(os.environ.get("UNIDADE_ID", "0") or 0) or None
    intervalo = int(os.environ.get("WORKER_INTERVALO_SEG", "3" if modo == "local" else "5"))
    adaptadores, contadores = {}, {}
    log.info("worker iniciado em MODO=%s (intervalo %ss)", modo, intervalo)
    if modo == "local":
        espelho.iniciar_ping()
    while True:
        try:
            if modo == "nuvem":
                rodada_nuvem(contadores)
            elif modo == "local":
                rodada_agente(adaptadores, contadores)
            else:
                rodada_catraca(adaptadores, contadores)
                rodada_nuvem(contadores)
        except Exception:  # noqa: BLE001 — o worker nunca morre por uma rodada ruim
            log.exception("rodada falhou")
            adaptadores.clear()
        time.sleep(intervalo)


if __name__ == "__main__":
    main()
