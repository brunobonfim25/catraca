"""Fila de sincronização → catracas. Pega itens com SELECT … FOR UPDATE SKIP LOCKED, então
pode rodar mais de um worker sem duplicar envio."""
import json
import logging
from datetime import timedelta

from app import db
from integrations import DispositivoErro, FotoRejeitada, adaptador_para

log = logging.getLogger(__name__)

MAX_TENTATIVAS = 8
BACKOFF_SEG = (10, 30, 60, 120, 300, 600, 1200, 1800)

# Regra única "liberado 24h" por catraca: em Standalone a catraca precisa de alguma regra
# pra abrir; quem decide janela/atraso/responsável é o servidor (ou o modo Pro na saída).
NOME_REGRA_LIBERADO = "Nexosix - liberado"


def reservar(limite=20):
    """Marca até `limite` itens como processando e devolve. Outro worker não vê os mesmos."""
    with db.transacao() as cur:
        cur.execute(
            "SELECT id FROM fila_sync WHERE status = 'pendente' AND proximo_em <= now() "
            "ORDER BY proximo_em, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limite,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return []
        cur.execute("UPDATE fila_sync SET status = 'processando', tentativas = tentativas + 1 WHERE id = ANY(%s)", (ids,))
        cur.execute(
            "SELECT f.*, d.unidade_id FROM fila_sync f JOIN dispositivos d ON d.id = f.dispositivo_id WHERE f.id = ANY(%s) ORDER BY f.id",
            (ids,),
        )
        return cur.fetchall()


def concluir(item_id):
    db.execute("UPDATE fila_sync SET status = 'concluido', concluido_em = now(), erro = NULL WHERE id = %s", (item_id,))


def falhar(item, erro, definitivo=False):
    tentativas = item["tentativas"]
    if definitivo or tentativas >= MAX_TENTATIVAS:
        db.execute("UPDATE fila_sync SET status = 'erro', erro = %s WHERE id = %s", (str(erro)[:500], item["id"]))
        return
    espera = BACKOFF_SEG[min(tentativas, len(BACKOFF_SEG)) - 1]
    db.execute(
        "UPDATE fila_sync SET status = 'pendente', erro = %s, proximo_em = now() + %s WHERE id = %s",
        (str(erro)[:500], timedelta(seconds=espera), item["id"]),
    )


def _dp(dispositivo_id, pessoa_id):
    return db.query("SELECT * FROM dispositivo_pessoa WHERE dispositivo_id = %s AND pessoa_id = %s", (dispositivo_id, pessoa_id), one=True)


def _garantir_user_id(adapter, dispositivo, pessoa):
    dp = _dp(dispositivo["id"], pessoa["id"])
    if dp and dp["user_id_no_dispositivo"]:
        return dp["user_id_no_dispositivo"]
    user_id = adapter.sync_user({"id": pessoa["id"], "nome": pessoa["nome"]})
    db.execute(
        "INSERT INTO dispositivo_pessoa (dispositivo_id, pessoa_id, user_id_no_dispositivo) VALUES (%s,%s,%s) "
        "ON CONFLICT (dispositivo_id, pessoa_id) DO UPDATE SET user_id_no_dispositivo = EXCLUDED.user_id_no_dispositivo, atualizado_em = now()",
        (dispositivo["id"], pessoa["id"], user_id),
    )
    return user_id


def _regra_liberado(adapter, dispositivo):
    cfg = dispositivo.get("config") or {}
    if cfg.get("regra_liberado_id"):
        return int(cfg["regra_liberado_id"])
    achadas = adapter.load_objects("access_rules", where={"access_rules": {"name": NOME_REGRA_LIBERADO}}, fields=["id"])
    if achadas:
        regra_id = int(achadas[0]["id"])
    else:
        regra_id = adapter.criar_regra_horario(NOME_REGRA_LIBERADO, 0, 86399, dias=(0, 1, 2, 3, 4, 5, 6))
    db.execute("UPDATE dispositivos SET config = config || %s WHERE id = %s", (json.dumps({"regra_liberado_id": regra_id}), dispositivo["id"]))
    dispositivo["config"] = {**cfg, "regra_liberado_id": regra_id}
    return regra_id


def processar_item(item, adapter, dispositivo):
    pessoa = db.query("SELECT * FROM pessoas WHERE id = %s", (item["pessoa_id"],), one=True) if item["pessoa_id"] else None
    tipo = item["tipo"]

    if tipo == "set_clock":
        adapter.set_clock(fuso=item["payload"].get("fuso", "America/Sao_Paulo"))
        return

    if tipo == "remove_user":
        dp = _dp(dispositivo["id"], item["pessoa_id"])
        if dp and dp["user_id_no_dispositivo"]:
            adapter.remove_user(dp["user_id_no_dispositivo"])
        db.execute("DELETE FROM dispositivo_pessoa WHERE dispositivo_id = %s AND pessoa_id = %s", (dispositivo["id"], item["pessoa_id"]))
        return

    if pessoa is None or pessoa["status"] == "desligado":
        return  # pessoa some da catraca pelo remove_user; nada a sincronizar

    if tipo == "sync_user":
        dp = _dp(dispositivo["id"], pessoa["id"])
        user_id = adapter.sync_user({"id": pessoa["id"], "nome": pessoa["nome"], "user_id_no_dispositivo": dp and dp["user_id_no_dispositivo"]})
        db.execute(
            "INSERT INTO dispositivo_pessoa (dispositivo_id, pessoa_id, user_id_no_dispositivo) VALUES (%s,%s,%s) "
            "ON CONFLICT (dispositivo_id, pessoa_id) DO UPDATE SET user_id_no_dispositivo = EXCLUDED.user_id_no_dispositivo, atualizado_em = now()",
            (dispositivo["id"], pessoa["id"], user_id),
        )
        return

    user_id = _garantir_user_id(adapter, dispositivo, pessoa)

    if tipo == "set_photo":
        foto = db.query("SELECT jpeg FROM pessoa_fotos WHERE pessoa_id = %s", (pessoa["id"],), one=True)
        if not foto:
            db.execute("UPDATE dispositivo_pessoa SET foto_sync_status = 'pendente', erro = 'sem foto' WHERE dispositivo_id = %s AND pessoa_id = %s", (dispositivo["id"], pessoa["id"]))
            return
        try:
            adapter.set_user_photo(user_id, bytes(foto["jpeg"]), match=True)
        except FotoRejeitada as e:
            # Erro de foto não adianta repetir: volta pra secretaria com "refazer foto"
            db.execute("UPDATE dispositivo_pessoa SET foto_sync_status = 'erro', erro = %s, atualizado_em = now() WHERE dispositivo_id = %s AND pessoa_id = %s", (str(e)[:300], dispositivo["id"], pessoa["id"]))
            raise
        db.execute("UPDATE dispositivo_pessoa SET foto_sync_status = 'sincronizado', erro = NULL, atualizado_em = now() WHERE dispositivo_id = %s AND pessoa_id = %s", (dispositivo["id"], pessoa["id"]))
        return

    if tipo == "set_rules":
        if pessoa["status"] != "ativo" or pessoa["tipo"] == "visitante":
            adapter.revoke(user_id)
        else:
            adapter.set_access_rules(user_id, [_regra_liberado(adapter, dispositivo)])
        db.execute("UPDATE dispositivo_pessoa SET regras_sync_status = 'sincronizado', atualizado_em = now() WHERE dispositivo_id = %s AND pessoa_id = %s", (dispositivo["id"], pessoa["id"]))
        return

    raise DispositivoErro(f"tipo de item desconhecido: {tipo}")


def processar_fila(limite=20, adaptadores=None):
    """Uma rodada. Devolve (ok, falhas). `adaptadores` é cache {dispositivo_id: adapter}."""
    adaptadores = adaptadores if adaptadores is not None else {}
    ok = falhas = 0
    for item in reservar(limite):
        dispositivo = db.query("SELECT * FROM dispositivos WHERE id = %s", (item["dispositivo_id"],), one=True)
        if not dispositivo or not dispositivo["ativo"]:
            falhar(item, "dispositivo inativo", definitivo=True)
            falhas += 1
            continue
        try:
            adapter = adaptadores.get(dispositivo["id"])
            if adapter is None:
                adapter = adaptadores[dispositivo["id"]] = adaptador_para(dispositivo)
            processar_item(item, adapter, dispositivo)
            concluir(item["id"])
            db.execute("UPDATE dispositivos SET status = 'online', ultimo_contato = now() WHERE id = %s", (dispositivo["id"],))
            ok += 1
        except FotoRejeitada as e:
            falhar(item, e, definitivo=True)
            falhas += 1
        except DispositivoErro as e:
            log.warning("sync %s/%s em %s falhou: %s", item["tipo"], item["pessoa_id"], dispositivo["nome"], e)
            adaptadores.pop(dispositivo["id"], None)
            falhar(item, e)
            falhas += 1
        except Exception as e:  # noqa: BLE001 — bug nosso não pode travar a fila inteira
            log.exception("erro inesperado no item %s", item["id"])
            falhar(item, f"erro interno: {e}")
            falhas += 1
    return ok, falhas
