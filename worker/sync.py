"""Fila de sincronização → catracas. Pega itens com SELECT … FOR UPDATE SKIP LOCKED, então
pode rodar mais de um worker sem duplicar envio."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app import db
from integrations import DispositivoErro, DispositivoInacessivel, FotoRejeitada, adaptador_para

log = logging.getLogger(__name__)

MAX_TENTATIVAS = 8
BACKOFF_SEG = (10, 30, 60, 120, 300, 600, 1200, 1800)

# Regra única "liberado 24h" por catraca: em Standalone a catraca precisa de alguma regra
# pra abrir; quem decide janela/atraso/responsável é o servidor (ou o modo Pro na saída).
NOME_REGRA_LIBERADO = "Nexosix - liberado"


class ComandoVencido(Exception):
    """Comando que perdeu a validade antes de chegar à catraca (não repete)."""


def reservar(limite=20):
    """Marca até `limite` itens como processando e devolve. Outro worker não vê os mesmos."""
    with db.transacao() as cur:
        # Comandos e bloqueios antes das fotos: um "liberar" ou a remoção de um desligado não
        # pode esperar atrás de 900 fotos da carga inicial.
        # Revisão (C10): item em 'processando' de um worker que morreu no meio ficava parado pra
        # sempre. Reservar agora é um "aluguel" de 10 min (proximo_em): vencido, volta pra fila.
        cur.execute(
            "SELECT id FROM fila_sync WHERE status IN ('pendente','processando') AND proximo_em <= now() "
            "ORDER BY CASE WHEN tipo IN ('liberar','remove_user','set_rules','testar','modo','configurar_monitor','set_clock','reconciliar') THEN 0 ELSE 1 END, "
            "proximo_em, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limite,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return []
        cur.execute("UPDATE fila_sync SET status = 'processando', tentativas = tentativas + 1, proximo_em = now() + interval '10 minutes' WHERE id = ANY(%s)", (ids,))
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


def adiar(item, erro, segundos=60):
    """Catraca fora: volta pra fila sem gastar tentativa."""
    db.execute("UPDATE fila_sync SET status = 'pendente', tentativas = GREATEST(tentativas - 1, 0), erro = %s, proximo_em = now() + %s WHERE id = %s",
               (str(erro)[:500], timedelta(seconds=segundos), item["id"]))


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


NOME_REGRA_SO_ENTRADA = "Nexosix - so entrada"
DESVIO_MAX_SEG = 60


def _regra(adapter, dispositivo, chave, nome, portais=None):
    """Garante a regra na catraca (cria uma vez, guarda o id em dispositivos.config)."""
    cfg = dispositivo.get("config") or {}
    if cfg.get(chave):
        return int(cfg[chave])
    achadas = adapter.load_objects("access_rules", where={"access_rules": {"name": nome}}, fields=["id"])
    if achadas:
        regra_id = int(achadas[0]["id"])
    else:
        regra_id = adapter.criar_regra_horario(nome, 0, 86399, dias=(0, 1, 2, 3, 4, 5, 6), portais=portais)
    db.execute("UPDATE dispositivos SET config = config || %s WHERE id = %s", (json.dumps({chave: regra_id}), dispositivo["id"]))
    dispositivo["config"] = {**cfg, chave: regra_id}
    return regra_id


def _regra_liberado(adapter, dispositivo):
    return _regra(adapter, dispositivo, "regra_liberado_id", NOME_REGRA_LIBERADO)


def _regra_so_entrada(adapter, dispositivo):
    """Contingência (catraca sem servidor): aluno que só sai com responsável NÃO tem regra no
    portal de saída. Offline a catraca nega a saída e a recepção libera pelo painel/portão PCD.

    Revisão (C6): a regra ficava presa aos portais do primeiro dia. Corrigido o mapeamento
    (descobriu-se que o portal 1 é a saída), a catraca seguia com a regra velha: offline os
    alunos "só com responsável" saíam livres e não entravam. Agora, se os portais de entrada
    mudaram, a regra é apagada e recriada (e o painel reenvia as regras de todos)."""
    rows = db.query("SELECT portal_id FROM dispositivo_leitores WHERE dispositivo_id = %s AND sentido = 'entrada' AND portal_id IS NOT NULL ORDER BY portal_id", (dispositivo["id"],))
    portais = sorted({r["portal_id"] for r in rows}) or [1]  # [SUP] portal 1 = entrada no idBlock Next
    cfg = dispositivo.get("config") or {}
    if cfg.get("regra_so_entrada_id") and cfg.get("regra_so_entrada_portais") != portais:
        try:
            adapter.destruir_regra(int(cfg["regra_so_entrada_id"]))
        except DispositivoInacessivel:
            raise
        except DispositivoErro as e:
            log.warning("não consegui apagar a regra velha %s: %s", cfg["regra_so_entrada_id"], e)
        db.execute("UPDATE dispositivos SET config = config - 'regra_so_entrada_id' WHERE id = %s", (dispositivo["id"],))
        dispositivo["config"] = {k: v for k, v in cfg.items() if k != "regra_so_entrada_id"}
        # a busca por nome acharia a regra antiga se ela não pôde ser apagada: cria outra
        regra_id = adapter.criar_regra_horario(NOME_REGRA_SO_ENTRADA, 0, 86399, dias=(0, 1, 2, 3, 4, 5, 6), portais=portais)
        db.execute("UPDATE dispositivos SET config = config || %s WHERE id = %s",
                   (json.dumps({"regra_so_entrada_id": regra_id, "regra_so_entrada_portais": portais}), dispositivo["id"]))
        dispositivo["config"] = {**dispositivo["config"], "regra_so_entrada_id": regra_id, "regra_so_entrada_portais": portais}
        return regra_id
    regra_id = _regra(adapter, dispositivo, "regra_so_entrada_id", NOME_REGRA_SO_ENTRADA, portais=portais)
    if cfg.get("regra_so_entrada_portais") != portais:
        db.execute("UPDATE dispositivos SET config = config || %s WHERE id = %s", (json.dumps({"regra_so_entrada_portais": portais}), dispositivo["id"]))
        dispositivo["config"] = {**(dispositivo.get("config") or {}), "regra_so_entrada_portais": portais}
    return regra_id


def reconciliar(adapter, dispositivo):
    """Refaz o vínculo pessoa ↔ número na catraca a partir do que está NELA (campo
    registration = id da pessoa no Nexosix). Revisão (C4): depois de reset ou troca, dois
    registros nossos apontavam pro mesmo número da catraca — face, regra e aviso iam pra
    pessoa errada. Devolve um resumo."""
    na_catraca = adapter.usuarios()
    por_registro, sobra = {}, []
    for u in na_catraca:
        reg = str(u.get("registration") or "")
        if reg.isdigit() and reg not in por_registro:
            por_registro[reg] = int(u["id"])
        else:
            sobra.append(int(u["id"]))  # sem registro nosso ou duplicado
    ativos = {str(r["id"]): r for r in db.query("SELECT id, status FROM pessoas WHERE unidade_id = %s", (dispositivo["unidade_id"],))}
    resumo = {"na_catraca": len(na_catraca), "corrigidos": 0, "reenviados": 0, "removidos": 0}
    for reg, user_id in list(por_registro.items()):
        p = ativos.get(reg)
        if p is None or p["status"] == "desligado":
            sobra.append(user_id)
            del por_registro[reg]
    for user_id in sobra:
        adapter.remove_user(user_id)
        resumo["removidos"] += 1
    from app.dispositivos import service as dispositivos

    com_foto = set(int(i) for i in adapter.list_user_images())
    for dp in db.query("SELECT dp.*, p.status, (pf.pessoa_id IS NOT NULL) AS tem_foto FROM dispositivo_pessoa dp JOIN pessoas p ON p.id = dp.pessoa_id "
                       "LEFT JOIN pessoa_fotos pf ON pf.pessoa_id = dp.pessoa_id WHERE dp.dispositivo_id = %s", (dispositivo["id"],)):
        certo = por_registro.get(str(dp["pessoa_id"]))
        if certo != dp["user_id_no_dispositivo"]:
            db.execute("UPDATE dispositivo_pessoa SET user_id_no_dispositivo = %s, foto_sync_status = 'pendente', regras_sync_status = 'pendente', "
                       "atualizado_em = now() WHERE dispositivo_id = %s AND pessoa_id = %s", (certo, dispositivo["id"], dp["pessoa_id"]))
            resumo["corrigidos"] += 1
        if dp["status"] != "desligado" and (certo is None or certo != dp["user_id_no_dispositivo"]):
            # número novo ou nenhum: manda cadastro, foto e regra de novo
            for tipo in ("sync_user", "set_photo", "set_rules"):
                dispositivos.enfileirar(dispositivo["id"], tipo, dp["pessoa_id"])
            resumo["reenviados"] += 1
        elif dp["status"] != "desligado":
            # número certo, mas a catraca resetada pode ter perdido a foto e a regra
            if dp["tem_foto"] and certo not in com_foto:
                dispositivos.enfileirar(dispositivo["id"], "set_photo", dp["pessoa_id"])
                resumo["reenviados"] += 1
            dispositivos.enfileirar(dispositivo["id"], "set_rules", dp["pessoa_id"])
    return resumo


def _precisa_responsavel(pessoa):
    if pessoa["tipo"] != "aluno":
        return False
    r = db.query("SELECT a.saida_autonoma, t.exige_responsavel_saida FROM alunos a JOIN turmas t ON t.id = a.turma_id WHERE a.pessoa_id = %s", (pessoa["id"],), one=True)
    if not r:
        return False
    if r["saida_autonoma"] is not None:
        return not r["saida_autonoma"]
    return bool(r["exige_responsavel_saida"])


def destino_da_catraca(dispositivo):
    """(host, porta, token) que a catraca usa pra chamar o servidor (Monitor e modo Pro).

    Com redundância, o NOME DNS da escola na porta 80 — o mesmo que a nuvem atende quando
    assume — e o token da escola (a nuvem só aceita o token da própria escola). Sem nome, o
    endereço deste servidor (BASE_URL) e, sem token da escola, o MONITOR_TOKEN do .env."""
    un = db.query("SELECT config FROM unidades WHERE id = %s", (dispositivo["unidade_id"],), one=True)
    cfg = (un or {}).get("config") or {}
    token = cfg.get("monitor_token") or os.environ.get("MONITOR_TOKEN", "")
    if cfg.get("dns_nome"):
        return cfg["dns_nome"], 80, token
    base = urlparse(os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    return base.hostname, base.port or (443 if base.scheme == "https" else 80), token


def processar_item(item, adapter, dispositivo):
    pessoa = db.query("SELECT * FROM pessoas WHERE id = %s", (item["pessoa_id"],), one=True) if item["pessoa_id"] else None
    tipo = item["tipo"]
    payload = item.get("payload") or {}

    # ---- comandos de catraca (vêm do painel na nuvem; só o agente alcança o equipamento)
    if tipo == "set_clock":
        # A hora vai DESTE PC pra catraca. Revisão (E16): com o relógio do PC errado (bateria da
        # BIOS, Windows sem sincronizar) o "acertar hora" desacertava a catraca e as entradas
        # caíam fora da janela do turno. Só acerta se o PC bate com a nuvem.
        est = db.query("SELECT desvio_relogio_seg FROM espelho_estado WHERE id = 1", one=True)
        desvio = (est or {}).get("desvio_relogio_seg")
        if desvio is not None and abs(desvio) > DESVIO_MAX_SEG:
            raise ComandoVencido(f"relógio deste servidor está {desvio:+d} s diferente da nuvem; acerte a hora do Windows antes de acertar a catraca")
        adapter.set_clock(fuso=payload.get("fuso", "America/Sao_Paulo"))
        return
    if tipo == "liberar":
        expira = payload.get("expira_em")
        if expira and datetime.fromisoformat(expira) < datetime.now(timezone.utc):
            raise ComandoVencido("liberação vencida: passou de 60 s até chegar à catraca; a recepção deve liberar de novo")
        adapter.release(payload.get("sentido", "ambos"))
        return
    if tipo == "reconciliar":
        resumo = reconciliar(adapter, dispositivo)
        db.execute("UPDATE fila_sync SET payload = payload || %s WHERE id = %s", (json.dumps({"resumo": resumo}), item["id"]))
        log.info("reconciliação de %s: %s", dispositivo["nome"], resumo)
        return
    if tipo == "testar":
        info = adapter.identify_device()
        trocou = (dispositivo.get("serial") and info.get("serial") and info["serial"] != dispositivo["serial"]) or (
            dispositivo.get("device_id_fabricante") and info.get("device_id") and int(info["device_id"]) != int(dispositivo["device_id_fabricante"]))
        if trocou:
            # outra catraca no lugar (troca na garantia): os números de usuário dela são outros
            from app.dispositivos import service as dispositivos

            dispositivos.enfileirar(dispositivo["id"], "reconciliar")
        db.execute(
            "UPDATE dispositivos SET status = 'online', ultimo_contato = now(), firmware = %s, serial = COALESCE(%s, serial), "
            "device_id_fabricante = COALESCE(%s, device_id_fabricante) WHERE id = %s",
            (info.get("firmware"), info.get("serial"), int(info["device_id"]) if info.get("device_id") else None, dispositivo["id"]),
        )
        return
    if tipo == "configurar_monitor":
        host, porta, token = destino_da_catraca(dispositivo)
        adapter.configurar_monitor(host, porta, f"api/notifications/{token}")
        return
    if tipo == "modo":
        host, porta, token = destino_da_catraca(dispositivo)
        url = f"http://{host}{'' if porta == 80 else f':{porta}'}/api/catraca/{token}"
        novo = payload.get("modo", "standalone")
        adapter.configurar_modo_online(url, ativar=(novo == "pro"))
        db.execute("UPDATE dispositivos SET modo = %s WHERE id = %s", (novo, dispositivo["id"]))
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
        elif _precisa_responsavel(pessoa):
            adapter.set_access_rules(user_id, [_regra_so_entrada(adapter, dispositivo)])
        else:
            adapter.set_access_rules(user_id, [_regra_liberado(adapter, dispositivo)])
        db.execute("UPDATE dispositivo_pessoa SET regras_sync_status = 'sincronizado', atualizado_em = now() WHERE dispositivo_id = %s AND pessoa_id = %s", (dispositivo["id"], pessoa["id"]))
        return

    raise DispositivoErro(f"tipo de item desconhecido: {tipo}")


def processar_fila(limite=20, adaptadores=None):
    """Uma rodada. Devolve (ok, falhas). `adaptadores` é cache {dispositivo_id: adapter}."""
    adaptadores = adaptadores if adaptadores is not None else {}
    ok = falhas = 0
    fora = {}  # catraca que não respondeu nesta volta: o resto dos itens dela nem tenta
    for item in reservar(limite):
        dispositivo = db.query("SELECT * FROM dispositivos WHERE id = %s", (item["dispositivo_id"],), one=True)
        if not dispositivo or not dispositivo["ativo"]:
            falhar(item, "dispositivo inativo", definitivo=True)
            falhas += 1
            continue
        if dispositivo["id"] in fora and item["tipo"] != "liberar":
            adiar(item, fora[dispositivo["id"]])
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
        except (FotoRejeitada, ComandoVencido) as e:
            falhar(item, e, definitivo=True)
            falhas += 1
        except DispositivoInacessivel as e:
            adaptadores.pop(dispositivo["id"], None)
            fora[dispositivo["id"]] = f"catraca sem resposta: {e}"
            if item["tipo"] == "liberar":
                falhar(item, e, definitivo=True)  # nunca repete giro
            else:
                adiar(item, fora[dispositivo["id"]])
            falhas += 1
        except DispositivoErro as e:
            log.warning("sync %s/%s em %s falhou: %s", item["tipo"], item["pessoa_id"], dispositivo["nome"], e)
            adaptadores.pop(dispositivo["id"], None)
            # "liberar" nunca é repetido: um timeout pode ter girado a catraca sem a resposta voltar
            falhar(item, e, definitivo=item["tipo"] == "liberar")
            falhas += 1
        except Exception as e:  # noqa: BLE001 — bug nosso não pode travar a fila inteira
            log.exception("erro inesperado no item %s", item["id"])
            falhar(item, f"erro interno: {e}")
            falhas += 1
    return ok, falhas
