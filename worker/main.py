"""Processo worker (Railway: `python -m worker.main`).

Loop único: fila de sync → coleta de logs → Telegram → status; LGPD uma vez por hora.
Sem Redis: tudo em tabela PostgreSQL com SKIP LOCKED.
"""
import logging
import os
import time

from dotenv import load_dotenv

from app import db
from . import collect, lgpd, notify, sync

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


def rodada(adaptadores, contadores):
    ok, falhas = sync.processar_fila(adaptadores=adaptadores)
    if ok or falhas:
        log.info("sync: %d ok, %d falhas", ok, falhas)
    contadores["coleta"] += 1
    if contadores["coleta"] % int(os.environ.get("WORKER_COLETA_A_CADA", "6")) == 0:
        n = collect.coletar_todos(adaptadores)
        if n:
            log.info("coleta: %d logs novos", n)
        collect.verificar_status()
    enviados, falhas = notify.enviar_pendentes()
    if enviados or falhas:
        log.info("telegram: %d enviados, %d falhas", enviados, falhas)
    if time.time() - contadores["lgpd_em"] > 3600:
        n = lgpd.purgar_desligados()
        if n:
            log.info("lgpd: %d fotos purgadas", n)
        contadores["lgpd_em"] = time.time()


def main():
    db.init(os.environ["DATABASE_URL"], 1, 4)
    intervalo = int(os.environ.get("WORKER_INTERVALO_SEG", "5"))
    adaptadores = {}
    contadores = {"coleta": 0, "lgpd_em": 0}
    log.info("worker iniciado (intervalo %ss)", intervalo)
    while True:
        try:
            rodada(adaptadores, contadores)
        except Exception:  # noqa: BLE001 — o worker nunca morre por uma rodada ruim
            log.exception("rodada falhou")
            adaptadores.clear()
        time.sleep(intervalo)


if __name__ == "__main__":
    main()
