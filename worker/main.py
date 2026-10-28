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
    if _a_cada(contadores, "batimento", 30):
        from app import alertas

        alertas.batimento(os.environ.get("MODO", "unico"))
    enviados, falhas = notify.enviar_pendentes()
    if enviados or falhas:
        log.info("telegram: %d enviados, %d falhas", enviados, falhas)
    if _a_cada(contadores, "convites", 30):  # e-mail de convite: lote pequeno, a cota grátis é por dia
        from app.notificacoes import convites

        enviados, falhas = convites.enviar_pendentes()
        if enviados or falhas:
            log.info("convites por e-mail: %d enviados, %d falhas", enviados, falhas)
    if _a_cada(contadores, "lgpd", 3600):
        n = lgpd.purgar_desligados()
        if n:
            log.info("lgpd: %d fotos purgadas", n)
        from app.espelho import service as esp

        esp.limpar_outbox_da_nuvem()
        notify.limpar_antigos()


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


def acertar_relogios():
    """Revisão (C12): ninguém acertava a hora da catraca; relógio adiantado errava atraso,
    hora do aviso e validade da espera. Uma vez por dia (e ao ligar) o agente acerta todas —
    desde que o próprio PC esteja certo (o set_clock confere o desvio com a nuvem)."""
    from app.dispositivos import service as dispositivos

    for d in db.query("SELECT d.id, u.fuso FROM dispositivos d JOIN unidades u ON u.id = d.unidade_id WHERE d.ativo"):
        dispositivos.enfileirar(d["id"], "set_clock", payload={"fuso": d["fuso"]})


def _laco(nome, intervalo, fn, unidade_id):
    """Uma tarefa do agente numa thread própria. Revisão (C11): era uma volta só e em série —
    900 fotos ou uma catraca em timeout atrasavam bloqueio e liberação feitos no painel em
    minutos. Agora a fila da catraca, a coleta e a sincronização com a nuvem andam separadas."""
    import threading

    def _rodar():
        db.contexto.unidade_id = unidade_id  # contexto é por thread: sem isso o outbox sai sem escola
        while True:
            _etapa(nome, fn)
            time.sleep(intervalo)

    t = threading.Thread(target=_rodar, name=nome, daemon=True)
    t.start()
    return t


def iniciar_agente(unidade_id):
    fila_adapt, coleta_adapt, contadores = {}, {}, {}

    def espelho_():
        carregado = _etapa("carga inicial", espelho.garantir_carga_inicial)
        if carregado is True:
            n = _etapa("puxar", espelho.puxar)
            if n and n is not FALHOU:
                log.info("nuvem → agente: %d linhas", n)
        n = _etapa("empurrar", espelho.empurrar)
        if n and n is not FALHOU:
            log.info("agente → nuvem: %d linhas", n)

    def fila_():
        ok, falhas = sync.processar_fila(limite=10, adaptadores=fila_adapt)
        if ok or falhas:
            log.info("sync: %d ok, %d falhas", ok, falhas)

    def coleta_():
        if _a_cada(contadores, "relogio", 86400):
            acertar_relogios()
        n = collect.coletar_todos(coleta_adapt)
        if n:
            log.info("coleta: %d logs novos", n)
        collect.verificar_status()

    espelho.iniciar_ping()
    return [
        _laco("espelho", int(os.environ.get("WORKER_INTERVALO_SEG", "3")), espelho_, unidade_id),
        _laco("fila", 1, fila_, unidade_id),
        _laco("coleta", int(os.environ.get("WORKER_COLETA_SEG", "30")), coleta_, unidade_id),
    ]


def vigia():
    from app import alertas
    from app.espelho import failover

    for u, acao in failover.verificar():
        log.warning("vigia: failover %s em %s", acao, u["nome"])
    alertas.verificar()


def conectar(url, maxconn, espera_max=60):
    """Revisão (A19): banco lento na partida (Railway subindo os dois juntos) fazia o worker
    desistir e ficar parado. Agora tenta em laço, sem limite."""
    espera = 2
    while True:
        try:
            db.init(url, 1, maxconn)
            db.query("SELECT 1")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("banco indisponível (%s); nova tentativa em %ss", e, espera)
            db.fechar()
            time.sleep(espera)
            espera = min(espera * 2, espera_max)


def main():
    modo = os.environ.get("MODO", "unico")
    conectar(os.environ["DATABASE_URL"], 8)
    db.gucs = {"nexosix.outbox": "on"} if modo in ("nuvem", "local") else {}
    # O6: o worker não começa antes do banco estar na versão do código (trava compartilhada com o site)
    db.migrar(log=log.info)
    # Marca de escola nas escritas do worker (o site faz isso pela sessão). Sem ela a exclusão
    # LGPD das fotos saía da nuvem sem escola e o PC da escola nunca apagava a foto.
    db.gucs_fn = [lambda: {"nexosix.unidade": db.contexto.unidade_id} if getattr(db.contexto, "unidade_id", None) else {}]
    if modo == "local":
        db.contexto.unidade_id = int(os.environ.get("UNIDADE_ID", "0") or 0) or None
    intervalo = int(os.environ.get("WORKER_INTERVALO_SEG", "3" if modo == "local" else "5"))
    adaptadores, contadores = {}, {}
    log.info("worker iniciado em MODO=%s (intervalo %ss)", modo, intervalo)
    if modo == "local":
        threads = iniciar_agente(db.contexto.unidade_id)
        while all(t.is_alive() for t in threads):
            time.sleep(10)
        log.error("uma tarefa do agente parou; saindo pro serviço (NSSM/systemd) reiniciar")
        raise SystemExit(1)
    if modo == "nuvem":
        from app import backup

        # cópia semanal criptografada fora do Railway (em thread própria: leva ~1 min)
        _laco("backup", 600, backup.talvez_enviar, None)
        # Revisão (A9): o vigia da redundância ficava no mesmo laço do envio; no pico das 7h,
        # 2 mil mensagens atrasavam a troca de DNS de uma escola com o servidor caído.
        _laco("vigia", 30, vigia, None)
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
