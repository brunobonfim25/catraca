"""Coleta de access_logs pendentes direto da catraca (quando o Monitor não entregou:
catraca ficou offline, servidor reiniciou, deploy…). Dedupe fica em eventos.service."""
import logging
from datetime import timedelta

from app import db
from app.eventos import service as eventos
from app.eventos import tipos
from app.util import agora_utc
from integrations import DispositivoErro, adaptador_para

log = logging.getLogger(__name__)

LOTE = 200
# Monitor manda device_is_alive a cada 30 s; sem contato por 3 min = offline
OFFLINE_APOS = timedelta(minutes=3)


def coletar_dispositivo(dispositivo, adapter=None):
    """Varre os registros da catraca em sequência a partir do cursor da COLETA (não o do
    Monitor): o que o Monitor perdeu no meio também entra (C8). O dedupe descarta o resto."""
    adapter = adapter or adaptador_para(dispositivo)
    total = 0
    since = int(dispositivo.get("coleta_log_id") or 0)
    logs = adapter.fetch_logs(since, limit=LOTE)
    if not logs and since > 0:
        # Revisão (C5): numeração recomeçou (reset)? O maior número na catraca ficou abaixo do
        # nosso cursor: abre época nova e coleta do zero.
        maior = adapter.ultimo_log_id()
        if maior < since:
            eventos.nova_epoca(dispositivo, f"maior registro na catraca ({maior}) abaixo do já coletado ({since})")
            since = 0
            logs = adapter.fetch_logs(since, limit=LOTE)
    epoca = dispositivo.get("log_epoca")
    while logs:
        for item in logs:
            eventos.processar_access_log(dispositivo, item)
            if dispositivo.get("log_epoca") != epoca:
                return total  # o próprio registro revelou um reset: recomeça do zero na próxima volta
            since = max(since, int(item["id"]))
            total += 1
        db.execute("UPDATE dispositivos SET coleta_log_id = %s, ultimo_log_id = GREATEST(ultimo_log_id, %s) WHERE id = %s", (since, since, dispositivo["id"]))
        dispositivo["coleta_log_id"] = since
        if len(logs) < LOTE:
            break
        logs = adapter.fetch_logs(since, limit=LOTE)
    db.execute("UPDATE dispositivos SET status = CASE WHEN status = 'contingencia' THEN status ELSE 'online' END, ultimo_contato = now() WHERE id = %s", (dispositivo["id"],))
    return total


def coletar_todos(adaptadores=None):
    adaptadores = adaptadores if adaptadores is not None else {}
    total = 0
    for d in db.query("SELECT * FROM dispositivos WHERE ativo ORDER BY id"):
        try:
            adapter = adaptadores.get(d["id"])
            if adapter is None:
                adapter = adaptadores[d["id"]] = adaptador_para(d)
            total += coletar_dispositivo(d, adapter)
        except DispositivoErro as e:
            adaptadores.pop(d["id"], None)
            log.info("coleta em %s falhou: %s", d["nome"], e)
            _marcar_offline_se_quieto(d)
    return total


def _marcar_offline_se_quieto(d):
    if d["status"] == "offline":
        return
    quieto = d["ultimo_contato"] is None or (agora_utc() - d["ultimo_contato"]) > OFFLINE_APOS
    if quieto:
        db.execute("UPDATE dispositivos SET status = 'offline' WHERE id = %s", (d["id"],))
        eventos.registrar(d["unidade_id"], tipos.DISPOSITIVO_OFFLINE, agora_utc(), dispositivo_id=d["id"], origem="sistema")


def verificar_status():
    """Sem Monitor e sem coleta há 3 min → offline (aparece no painel)."""
    for d in db.query("SELECT * FROM dispositivos WHERE ativo AND status <> 'offline'"):
        _marcar_offline_se_quieto(d)
