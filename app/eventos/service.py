"""Ingestão de eventos: access_logs da catraca (Monitor ou coleta), decisões do modo Pro,
liberações manuais. Atualiza presença, espera de responsável e fila do Telegram.
"""
import json
import logging
from datetime import timedelta

from .. import db
from ..acesso import regras
from ..acesso import service as acesso
from ..dispositivos import service as dispositivos
from ..notificacoes import telegram
from ..util import agora_utc, de_unix, para_local, primeiro_nome
from . import tipos

log = logging.getLogger(__name__)

# Entrada de aluno até 60 s depois do responsável na mesma catraca = veio acompanhado
JANELA_ACOMPANHADO_SEG = 60


# ---------------------------------------------------------------- gravação

def registrar(unidade_id, tipo, ocorrido_em, dispositivo_id=None, pessoa_id=None, sentido=None, origem="dispositivo",
              log_id=None, evento_dispositivo=None, correlacao_id=None, autorizado_por=None, justificativa=None, detalhes=None):
    """Grava o evento. Devolve a linha, ou None se (dispositivo, época, log_id) já existia (dedupe).
    A época é a atual da catraca (muda quando ela reseta e recomeça a numeração)."""
    return db.execute(
        "INSERT INTO eventos (unidade_id, dispositivo_id, pessoa_id, tipo, sentido, ocorrido_em, origem, log_id_dispositivo, log_epoca, "
        "evento_dispositivo, correlacao_id, autorizado_por, justificativa, detalhes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
        "COALESCE((SELECT log_epoca FROM dispositivos WHERE id = %s), 0),%s,%s,%s,%s,%s) "
        "ON CONFLICT (dispositivo_id, log_epoca, log_id_dispositivo) WHERE log_id_dispositivo IS NOT NULL DO NOTHING RETURNING *",
        (unidade_id, dispositivo_id, pessoa_id, tipo, sentido, ocorrido_em, origem, log_id, dispositivo_id, evento_dispositivo,
         correlacao_id, autorizado_por, justificativa, json.dumps(detalhes or {})),
        returning=True,
    )


def atualizar_presenca(pessoa_id, unidade_id, dentro, evento_id, quando):
    db.execute(
        "INSERT INTO presenca (pessoa_id, unidade_id, dentro, desde, ultimo_evento_id) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (pessoa_id) DO UPDATE SET dentro = EXCLUDED.dentro, desde = EXCLUDED.desde, ultimo_evento_id = EXCLUDED.ultimo_evento_id "
        # Coleta atrasada pode trazer evento mais velho que o estado atual: não volta no tempo
        "WHERE presenca.desde <= EXCLUDED.desde",
        (pessoa_id, unidade_id, dentro, quando, evento_id),
    )


def pessoa_por_user_id(dispositivo_id, user_id):
    if not user_id:
        return None
    return db.query(
        "SELECT p.* FROM dispositivo_pessoa dp JOIN pessoas p ON p.id = dp.pessoa_id "
        "WHERE dp.dispositivo_id = %s AND dp.user_id_no_dispositivo = %s",
        (dispositivo_id, int(user_id)), one=True,
    )


def _unidade(unidade_id):
    return db.query("SELECT * FROM unidades WHERE id = %s", (unidade_id,), one=True)


def _turma_do_aluno(pessoa_id):
    return db.query(
        "SELECT t.*, a.id AS aluno_id FROM alunos a JOIN turmas t ON t.id = a.turma_id WHERE a.pessoa_id = %s",
        (pessoa_id,), one=True,
    )


# ---------------------------------------------------------------- responsável em espera

def registrar_espera(unidade, responsavel_pessoa, dispositivo=None, quando=None, log_id=None, evento_dispositivo=None):
    """Responsável se identificou no leitor de fora → fica em espera por N minutos.
    Responsável bloqueado/desligado não gera espera: devolve (None, evento com alerta)."""
    quando = quando or agora_utc()
    if responsavel_pessoa["status"] != "ativo":
        evento = registrar(unidade["id"], tipos.ACESSO_NEGADO, quando, dispositivo_id=dispositivo["id"] if dispositivo else None,
                           pessoa_id=responsavel_pessoa["id"], sentido="entrada", log_id=log_id, evento_dispositivo=evento_dispositivo,
                           detalhes={"motivo": f"responsável {responsavel_pessoa['status']}", "alerta": "responsável bloqueado tentou retirar"})
        return None, evento
    resp = db.query("SELECT * FROM responsaveis WHERE pessoa_id = %s", (responsavel_pessoa["id"],), one=True)
    minutos = int((unidade.get("config") or {}).get("espera_responsavel_min") or 15)
    expira = quando + timedelta(minutes=minutos)
    espera = db.execute(
        "INSERT INTO responsavel_em_espera (unidade_id, responsavel_id, identificado_em, expira_em, dispositivo_id) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (unidade["id"], resp["id"], quando, expira, dispositivo["id"] if dispositivo else None), returning=True,
    )
    alunos = db.query(
        "SELECT p.nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas p ON p.id = a.pessoa_id WHERE ar.responsavel_id = %s",
        (resp["id"],),
    )
    evento = registrar(
        unidade["id"], tipos.RESPONSAVEL_CHEGOU, quando, dispositivo_id=dispositivo["id"] if dispositivo else None,
        pessoa_id=responsavel_pessoa["id"], sentido="entrada", log_id=log_id, evento_dispositivo=evento_dispositivo,
        detalhes={"espera_id": espera["id"], "alunos": [a["nome"] for a in alunos]},
    )
    return espera, evento


def consumir_espera(espera_id, aluno_id, responsavel_id):
    """Chamar DEPOIS de atualizar a presença do aluno que saiu. A espera só se encerra quando
    nenhum outro filho vinculado a esse responsável continua dentro: antes o primeiro irmão
    consumia a espera e o segundo ficava preso na catraca."""
    restante = db.query(
        "SELECT 1 FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN presenca pr ON pr.pessoa_id = a.pessoa_id "
        "JOIN pessoas p ON p.id = a.pessoa_id WHERE ar.responsavel_id = %s AND a.id <> %s AND pr.dentro AND p.status = 'ativo' LIMIT 1",
        (responsavel_id, aluno_id), one=True,
    )
    db.execute("UPDATE responsavel_em_espera SET aluno_id = %s, consumido_em = CASE WHEN %s THEN consumido_em ELSE now() END WHERE id = %s",
               (aluno_id, restante is not None, espera_id))


# Responsável saiu pela catraca: a espera dele acaba em 2 min (dá tempo do filho passar logo
# atrás; não sobra espera da manhã para a criança sair sozinha depois).
SAIDA_RESPONSAVEL_GRACA_MIN = 2


def encerrar_esperas_do_responsavel(responsavel_pessoa_id, em_minutos=0):
    db.execute(
        "UPDATE responsavel_em_espera SET expira_em = LEAST(expira_em, now() + make_interval(mins => %s)) "
        "WHERE consumido_em IS NULL AND expira_em > now() AND responsavel_id = (SELECT id FROM responsaveis WHERE pessoa_id = %s)",
        (em_minutos, responsavel_pessoa_id),
    )


# ---------------------------------------------------------------- Telegram

SEM_RESPONSAVEL = "SAIDA_SEM_RESPONSAVEL"  # só texto: a catraca liberou sem o servidor concordar
ATRASO_AVISO_MIN = 10


def texto_evento(tipo, pessoa, turma_nome, quando_local, retirado_por=None, parentesco=None, justificativa=None):
    texto = _texto_evento(tipo, pessoa, turma_nome, quando_local, retirado_por, parentesco, justificativa)
    # Coleta depois de queda manda o aviso horas depois: sem a data e o "atrasado", o pai
    # achava que a criança tinha acabado de entrar.
    if texto and agora_utc() - quando_local > timedelta(minutes=ATRASO_AVISO_MIN):
        texto = texto.replace(f"às {quando_local:%H:%M}", f"em {quando_local:%d/%m} às {quando_local:%H:%M}", 1) + " (aviso atrasado)"
    return texto


def _texto_evento(tipo, pessoa, turma_nome, quando_local, retirado_por=None, parentesco=None, justificativa=None):
    nome = primeiro_nome(pessoa["nome"])
    turma = f" ({turma_nome})" if turma_nome else ""
    hora = quando_local.strftime("%H:%M")
    if tipo == SEM_RESPONSAVEL:
        return f"{nome}{turma} saiu às {hora} sem responsável registrado na catraca. A escola foi avisada."
    if tipo == tipos.ENTRADA:
        return f"{nome}{turma} entrou às {hora}."
    if tipo == tipos.ENTRADA_ATRASADA:
        return f"{nome}{turma} entrou às {hora} (após o horário de tolerância)."
    if tipo == tipos.SAIDA_ALUNO:
        quem = primeiro_nome(retirado_por) + (f" ({parentesco})" if parentesco else "")
        return f"{nome} saiu às {hora} com {quem}."
    if tipo == tipos.SAIDA_AUTONOMA:
        return f"{nome}{turma} saiu sozinho(a) às {hora}."
    if tipo == tipos.SAIDA_ANTECIPADA:
        j = f" Motivo: {justificativa}" if justificativa else ""
        return f"{nome}{turma} teve saída antecipada liberada pela escola às {hora}.{j}"
    if tipo == tipos.SAIDA_NEGADA:
        return f"{nome} tentou sair às {hora} e a catraca não liberou (sem responsável autorizado). A recepção foi avisada."
    if tipo == tipos.SAIDA:
        return f"{nome}{turma} saiu às {hora}."
    return None


def notificar_responsaveis(aluno_pessoa, texto, evento_id, unidade_id):
    if not texto:
        return 0
    # só responsável ativo: desligado (ex.: disputa de guarda) não recebe mais nada
    resps = db.query(
        "SELECT r.id FROM aluno_responsavel ar JOIN responsaveis r ON r.id = ar.responsavel_id "
        "JOIN pessoas rp ON rp.id = r.pessoa_id JOIN alunos a ON a.id = ar.aluno_id WHERE a.pessoa_id = %s AND rp.status = 'ativo'",
        (aluno_pessoa["id"],),
    )
    for r in resps:
        telegram.enfileirar(unidade_id, r["id"], texto, evento_id)
    return len(resps)


NEGADA_INTERVALO_MIN = 10


def _avisar_negada(pessoa, evento, unidade, ocorrido):
    """Um aviso de "tentou sair" por aluno a cada 10 min: criança brincando perto do leitor
    gerava dezenas de mensagens alarmantes para os pais."""
    recente = db.query(
        "SELECT 1 FROM eventos WHERE pessoa_id = %s AND tipo = %s AND id <> %s AND ocorrido_em > %s LIMIT 1",
        (pessoa["id"], tipos.SAIDA_NEGADA, evento["id"], ocorrido - timedelta(minutes=NEGADA_INTERVALO_MIN)), one=True,
    )
    if recente:
        return
    turma = _turma_do_aluno(pessoa["id"])
    notificar_responsaveis(pessoa, texto_evento(tipos.SAIDA_NEGADA, pessoa, turma and turma["nome"], para_local(ocorrido, unidade["fuso"])), evento["id"], unidade["id"])


# ---------------------------------------------------------------- access_logs (Standalone / coleta / espelho do Pro)

TIPO_POR_CODIGO = {
    3: tipos.NAO_IDENTIFICADO, 5: tipos.NAO_IDENTIFICADO, 10: tipos.LIBERACAO_MANUAL,
    11: tipos.BOTOEIRA, 12: tipos.LIBERACAO_MANUAL, 13: tipos.DESISTENCIA,
}


def _evento_pro_sem_log(dispositivo_id, pessoa_id, ocorrido_em, codigo):
    """No modo Pro o servidor já gravou o evento ao decidir; quando o access_log chega pelo
    Monitor, só anexa o log_id em vez de duplicar."""
    return db.query(
        "SELECT id FROM eventos WHERE dispositivo_id = %s AND pessoa_id IS NOT DISTINCT FROM %s AND log_id_dispositivo IS NULL "
        "AND evento_dispositivo = %s AND ocorrido_em BETWEEN %s AND %s ORDER BY id DESC LIMIT 1",
        (dispositivo_id, pessoa_id, codigo, ocorrido_em - timedelta(seconds=10), ocorrido_em + timedelta(seconds=10)), one=True,
    )


# Mesmo número de registro com hora diferente disso = não é o mesmo registro: a catraca resetou
RESET_DIFERENCA = timedelta(minutes=2)


def nova_epoca(dispositivo, motivo):
    """A catraca recomeçou a numeração dos registros (reset, troca do equipamento)."""
    linha = db.execute("UPDATE dispositivos SET log_epoca = log_epoca + 1, ultimo_log_id = 0, coleta_log_id = 0 WHERE id = %s RETURNING log_epoca",
                       (dispositivo["id"],), returning=True)
    dispositivo.update({"log_epoca": linha["log_epoca"], "ultimo_log_id": 0, "coleta_log_id": 0})
    registrar(dispositivo["unidade_id"], tipos.DISPOSITIVO_RESET, agora_utc(), dispositivo_id=dispositivo["id"], origem="sistema",
              detalhes={"motivo": motivo, "epoca": linha["log_epoca"], "alerta": "catraca resetada: cadastro sendo conferido"})
    # Depois de reset os números dos usuários na catraca também mudaram (C4)
    dispositivos.enfileirar(dispositivo["id"], "reconciliar")
    log.warning("catraca %s recomeçou a numeração (%s): época %s", dispositivo["id"], motivo, linha["log_epoca"])


def processar_access_log(dispositivo, item):
    """item = linha de access_logs da Control iD (id, time, event, user_id, portal_id…).
    Idempotente: o mesmo log pelo Monitor e pela coleta vira um evento só."""
    log_id = int(item["id"])
    codigo = int(item.get("event") or 0)
    user_id = int(item.get("user_id") or 0)
    ocorrido = de_unix(item["time"]) if item.get("time") else agora_utc()
    epoca = int(dispositivo.get("log_epoca") or 0)
    ja = db.query("SELECT id, ocorrido_em FROM eventos WHERE dispositivo_id = %s AND log_epoca = %s AND log_id_dispositivo = %s",
                  (dispositivo["id"], epoca, log_id), one=True)
    if ja:
        # Revisão (C5): depois de um reset o número 1, 2, 3… volta, e tudo era descartado como
        # repetido. Mesmo número com hora bem diferente (e mais nova) é registro novo.
        if not item.get("time") or abs(ocorrido - ja["ocorrido_em"]) <= RESET_DIFERENCA or ocorrido < ja["ocorrido_em"]:
            return None
        nova_epoca(dispositivo, f"registro {log_id} repetido com outra hora")
    unidade = _unidade(dispositivo["unidade_id"])
    sentido = dispositivos.sentido_do_leitor(dispositivo, portal_id=item.get("portal_id"))
    pessoa = pessoa_por_user_id(dispositivo["id"], user_id)

    pendente = _evento_pro_sem_log(dispositivo["id"], pessoa["id"] if pessoa else None, ocorrido, codigo) if codigo in (6, 7) else None
    if pendente:
        db.execute("UPDATE eventos SET log_id_dispositivo = %s, log_epoca = %s WHERE id = %s", (log_id, int(dispositivo.get("log_epoca") or 0), pendente["id"]))
        _avancar_ultimo_log(dispositivo, log_id)
        return db.query("SELECT * FROM eventos WHERE id = %s", (pendente["id"],), one=True)

    base = dict(dispositivo_id=dispositivo["id"], pessoa_id=pessoa["id"] if pessoa else None, sentido=sentido,
                log_id=log_id, evento_dispositivo=codigo, detalhes={"user_id": user_id, "portal_id": item.get("portal_id")})
    evento = None
    if codigo == 7:
        evento = _processar_liberado(unidade, dispositivo, pessoa, sentido, ocorrido, base)
    elif codigo == 6:
        tipo = tipos.SAIDA_NEGADA if sentido == "saida" else tipos.ACESSO_NEGADO
        evento = registrar(unidade["id"], tipo, ocorrido, **base)
        if evento and pessoa and pessoa["tipo"] == "aluno" and sentido == "saida":
            _avisar_negada(pessoa, evento, unidade, ocorrido)
    elif codigo == 13:
        evento = registrar(unidade["id"], tipos.DESISTENCIA, ocorrido, **base)
        if evento and pessoa and sentido:
            desistencia(unidade, dispositivo, pessoa, sentido, ocorrido, evento)
    else:
        tipo = TIPO_POR_CODIGO.get(codigo, f"DISPOSITIVO_EVENTO_{codigo}")
        evento = registrar(unidade["id"], tipo, ocorrido, **base)
    _avancar_ultimo_log(dispositivo, log_id)
    return evento


def _avancar_ultimo_log(dispositivo, log_id):
    db.execute("UPDATE dispositivos SET ultimo_log_id = GREATEST(ultimo_log_id, %s) WHERE id = %s", (log_id, dispositivo["id"]))


def desistencia(unidade, dispositivo, pessoa, sentido, quando, evento=None):
    """Liberou e a pessoa não girou. Revisão (C9): a espera já tinha sido consumida e o "saiu"
    enviado; na segunda tentativa a criança era negada e os pais recebiam "tentou sair".
    Agora: presença volta, a espera reabre e os pais recebem a correção."""
    decisao = db.query(
        "SELECT * FROM eventos WHERE dispositivo_id = %s AND pessoa_id = %s AND sentido = %s AND evento_dispositivo = 7 "
        "AND ocorrido_em BETWEEN %s AND %s AND NOT (detalhes ? 'desistiu') ORDER BY ocorrido_em DESC, id DESC LIMIT 1",
        (dispositivo["id"], pessoa["id"], sentido, quando - timedelta(minutes=2), quando + timedelta(seconds=30)), one=True,
    )
    atualizar_presenca(pessoa["id"], unidade["id"], sentido != "entrada", evento["id"] if evento else (decisao and decisao["id"]), quando)
    if not decisao:
        return
    db.execute("UPDATE eventos SET detalhes = detalhes || '{\"desistiu\": true}' WHERE id = %s", (decisao["id"],))
    espera_id = (decisao.get("detalhes") or {}).get("espera_id")
    if espera_id:
        db.execute("UPDATE responsavel_em_espera SET consumido_em = NULL WHERE id = %s AND expira_em > %s", (espera_id, quando))
    turma = _turma_do_aluno(pessoa["id"]) if pessoa["tipo"] == "aluno" else None
    if turma and decisao["tipo"] in tipos.AVISA_SAIDA_ENTRADA:
        hora = para_local(decisao["ocorrido_em"], unidade["fuso"]).strftime("%H:%M")
        acao = "saiu" if sentido == "saida" else "entrou"
        texto = f"Correção: {primeiro_nome(pessoa['nome'])} não {acao} às {hora} — desistiu na catraca e continua {'na escola' if sentido == 'saida' else 'fora'}."
        notificar_responsaveis(pessoa, texto, evento["id"] if evento else decisao["id"], unidade["id"])


def _processar_liberado(unidade, dispositivo, pessoa, sentido, ocorrido, base):
    """Evento 7 (catraca liberou) em Standalone: classifica e atualiza presença."""
    local = para_local(ocorrido, unidade["fuso"])
    if pessoa is None:
        return registrar(unidade["id"], tipos.ENTRADA if sentido == "entrada" else tipos.SAIDA, ocorrido, **base)

    if pessoa["tipo"] == "responsavel":
        if sentido == "entrada":
            _, evento = registrar_espera(unidade, pessoa, dispositivo, ocorrido, log_id=base["log_id"], evento_dispositivo=7)
            return evento
        encerrar_esperas_do_responsavel(pessoa["id"], SAIDA_RESPONSAVEL_GRACA_MIN)
        return registrar(unidade["id"], tipos.SAIDA, ocorrido, **base)

    turma = _turma_do_aluno(pessoa["id"]) if pessoa["tipo"] == "aluno" else None
    if sentido == "entrada" and pessoa["tipo"] == "funcionario":
        # Revisão (C14): em Standalone a catraca deixa o funcionário entrar a qualquer hora (a
        # escala só valia no modo Pro). Não dá pra travar sem regra de horário na catraca, mas
        # fica o alerta no painel.
        d = acesso.decidir(pessoa["id"], "entrada", unidade, ocorrido)
        if not d.liberar:
            base["detalhes"]["alerta"] = f"entrada liberada pela catraca: {d.motivo}"
    if sentido == "entrada":
        tipo = regras.classificar_entrada(turma, local) if turma else tipos.ENTRADA
        acompanhado = _responsavel_acompanhando(unidade["id"], dispositivo["id"], pessoa, ocorrido) if turma else None
        if acompanhado:
            base["correlacao_id"] = acompanhado["pessoa_id"]
            base["detalhes"]["acompanhado_por"] = acompanhado["nome"]
        evento = registrar(unidade["id"], tipo, ocorrido, **base)
        if evento:
            atualizar_presenca(pessoa["id"], unidade["id"], True, evento["id"], ocorrido)
            if turma:
                notificar_responsaveis(pessoa, texto_evento(tipo, pessoa, turma["nome"], local), evento["id"], unidade["id"])
        return evento

    # saída liberada pela catraca (Standalone): registra o que o servidor teria decidido
    tipo, correlacao, detalhes_extra, parentesco, retirado_por, decisao = tipos.SAIDA, None, {}, None, None, None
    if turma:
        decisao = acesso.decidir(pessoa["id"], "saida", unidade, ocorrido)
        if decisao.liberar:
            tipo = decisao.tipo
            if decisao.espera_id:
                resp = db.query("SELECT p.id, p.nome, ar.parentesco FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                                "LEFT JOIN aluno_responsavel ar ON ar.responsavel_id = r.id AND ar.aluno_id = %s WHERE r.id = %s",
                                (turma["aluno_id"], decisao.responsavel_id), one=True)
                correlacao, retirado_por, parentesco = resp["id"], resp["nome"], resp["parentesco"]
        else:
            # A catraca liberou sem o servidor concordar (Standalone na saída): fica visível no painel
            detalhes_extra = {"alerta": f"saída liberada pela catraca: {decisao.motivo}"}
    base["correlacao_id"] = correlacao
    base["detalhes"].update(detalhes_extra)
    if decisao and decisao.liberar and decisao.espera_id:
        base["detalhes"]["espera_id"] = decisao.espera_id
    evento = registrar(unidade["id"], tipo, ocorrido, **base)
    if evento:
        atualizar_presenca(pessoa["id"], unidade["id"], False, evento["id"], ocorrido)
        if decisao and decisao.liberar and decisao.espera_id:
            consumir_espera(decisao.espera_id, turma["aluno_id"], decisao.responsavel_id)
        if turma:
            texto_tipo = SEM_RESPONSAVEL if detalhes_extra else tipo
            notificar_responsaveis(pessoa, texto_evento(texto_tipo, pessoa, turma["nome"], local, retirado_por, parentesco), evento["id"], unidade["id"])
    return evento


def _responsavel_acompanhando(unidade_id, dispositivo_id, aluno_pessoa, ocorrido):
    """Responsável do aluno que se identificou na mesma catraca nos 60 s anteriores."""
    return db.query(
        "SELECT p.id AS pessoa_id, p.nome FROM eventos e JOIN pessoas p ON p.id = e.pessoa_id "
        "JOIN responsaveis r ON r.pessoa_id = p.id JOIN aluno_responsavel ar ON ar.responsavel_id = r.id "
        "JOIN alunos a ON a.id = ar.aluno_id WHERE a.pessoa_id = %s AND e.dispositivo_id = %s AND e.unidade_id = %s "
        "AND e.tipo = %s AND e.ocorrido_em BETWEEN %s AND %s ORDER BY e.ocorrido_em DESC LIMIT 1",
        (aluno_pessoa["id"], dispositivo_id, unidade_id, tipos.RESPONSAVEL_CHEGOU, ocorrido - timedelta(seconds=JANELA_ACOMPANHADO_SEG), ocorrido), one=True,
    )


# ---------------------------------------------------------------- modo Pro (decisão online)

# Relógio da catraca: ninguém acerta a hora dela, e se o firmware mandar hora local como se
# fosse UTC tudo sai 3 h errado. Até 10 min de diferença vale a hora dela (mantém a ordem real
# dos eventos); acima disso decide pela hora do servidor e deixa o alerta no evento.
TOLERANCIA_RELOGIO = timedelta(minutes=10)


def hora_da_decisao(time_catraca):
    """(hora a usar, diferença em minutos se o relógio da catraca está fora da tolerância)."""
    agora = agora_utc()
    if not time_catraca:
        return agora, None
    catraca = de_unix(time_catraca)
    if abs(catraca - agora) <= TOLERANCIA_RELOGIO:
        return catraca, None
    return agora, int((catraca - agora).total_seconds() // 60)


def decidir_online(dispositivo, campos):
    """Catraca em modo Pro identificou alguém e pergunta se libera.
    Devolve (resposta_para_catraca, evento). Formato [DOC]: {"result": {event, user_id, user_name, actions[], message}}."""
    uuid = str(campos.get("uuid") or "")[:80] or None
    if uuid:
        # Revisão (C9): a catraca repete o pedido (mesmo uuid) quando a resposta demora. Decidir
        # de novo consumia a espera duas vezes: a 2ª negava e os pais recebiam "tentou sair".
        anterior = db.query("SELECT * FROM eventos WHERE dispositivo_id = %s AND detalhes->>'uuid' = %s AND criado_em > now() - interval '10 minutes' "
                            "ORDER BY id DESC LIMIT 1", (dispositivo["id"], uuid), one=True)
        if anterior and (anterior.get("detalhes") or {}).get("resposta"):
            return anterior["detalhes"]["resposta"], anterior
    resposta, ev = _decidir_online(dispositivo, campos, uuid)
    if ev:
        db.execute("UPDATE eventos SET detalhes = detalhes || %s WHERE id = %s", (json.dumps({"resposta": resposta}), ev["id"]))
    return resposta, ev


def _decidir_online(dispositivo, campos, uuid):
    unidade = _unidade(dispositivo["unidade_id"])
    user_id = int(campos.get("user_id") or 0)
    ocorrido, diferenca = hora_da_decisao(campos.get("time"))
    sentido = dispositivos.sentido_do_leitor(dispositivo, component_id=campos.get("component_id"), portal_id=campos.get("portal_id"))
    pessoa = pessoa_por_user_id(dispositivo["id"], user_id)
    cfg = dispositivo.get("config") or {}
    allow = {"entrada": cfg.get("allow_entrada", "clockwise"), "saida": cfg.get("allow_saida", "anticlockwise")}.get(sentido)
    detalhes = {"user_id": user_id, "component_id": campos.get("component_id"), "portal_id": campos.get("portal_id"), "modo": "pro"}
    if uuid:
        detalhes["uuid"] = uuid
    if diferenca is not None:
        detalhes["alerta"] = f"relógio da catraca com {diferenca} min de diferença: decidido pela hora do servidor"

    def negar(tipo, motivo, msg):
        ev = registrar(unidade["id"], tipo, ocorrido, dispositivo_id=dispositivo["id"], pessoa_id=pessoa["id"] if pessoa else None,
                       sentido=sentido, evento_dispositivo=6, detalhes={**detalhes, "motivo": motivo})
        return {"result": {"event": 6, "user_id": user_id, "user_name": pessoa["nome"] if pessoa else "", "message": msg[:60]}}, ev

    if pessoa is None:
        return negar(tipos.ACESSO_NEGADO, "user_id sem pessoa", "Não cadastrado")
    if sentido is None:
        # leitor sem sentido mapeado: antes virava "saída liberada para os dois lados"
        return negar(tipos.ACESSO_NEGADO, "leitor sem sentido mapeado", "Procure a recepção")

    if pessoa["tipo"] == "responsavel" and sentido == "entrada":
        if pessoa["status"] != "ativo":
            return negar(tipos.ACESSO_NEGADO, f"responsável {pessoa['status']}", "Procure a recepção")
        espera, ev = registrar_espera(unidade, pessoa, dispositivo, ocorrido, evento_dispositivo=7)
        if uuid and ev:
            db.execute("UPDATE eventos SET detalhes = detalhes || %s WHERE id = %s", (json.dumps({"uuid": uuid}), ev["id"]))
        alunos = ", ".join(primeiro_nome(a) for a in (ev["detalhes"].get("alunos") or [])) or "aluno"
        modo = (unidade.get("config") or {}).get("responsavel_resposta", "aguardar")
        # [SUP] event 7 sem actions = tela verde sem girar; ver README do adaptador, item 6
        resultado = {"event": 7, "user_id": user_id, "user_name": pessoa["nome"], "message": f"Aguarde {alunos}"[:60]}
        if modo == "liberar":
            resultado["actions"] = [{"action": "catra", "parameters": f"allow={allow}"}]
        elif modo == "negar":
            resultado["event"] = 6
        return {"result": resultado}, ev

    decisao = acesso.decidir(pessoa["id"], sentido, unidade, ocorrido)
    if not decisao.liberar:
        msg = {"saida": "Aguardando responsável", "entrada": "Acesso negado"}.get(sentido, "Negado")
        resp, ev = negar(decisao.tipo, decisao.motivo, msg)
        if pessoa["tipo"] == "aluno" and sentido == "saida" and ev:
            _avisar_negada(pessoa, ev, unidade, ocorrido)
        return resp, ev

    correlacao, retirado_por, parentesco = None, None, None
    turma = _turma_do_aluno(pessoa["id"]) if pessoa["tipo"] == "aluno" else None
    if decisao.espera_id and turma:
        r = db.query("SELECT p.id, p.nome, ar.parentesco FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
                     "LEFT JOIN aluno_responsavel ar ON ar.responsavel_id = r.id AND ar.aluno_id = %s WHERE r.id = %s",
                     (turma["aluno_id"], decisao.responsavel_id), one=True)
        correlacao, retirado_por, parentesco = r["id"], r["nome"], r["parentesco"]
    ev = registrar(unidade["id"], decisao.tipo, ocorrido, dispositivo_id=dispositivo["id"], pessoa_id=pessoa["id"], sentido=sentido,
                   evento_dispositivo=7, correlacao_id=correlacao,
                   detalhes={**detalhes, "motivo": decisao.motivo, **({"espera_id": decisao.espera_id} if decisao.espera_id else {})})
    if ev:
        atualizar_presenca(pessoa["id"], unidade["id"], sentido == "entrada", ev["id"], ocorrido)
        if decisao.espera_id and turma:
            consumir_espera(decisao.espera_id, turma["aluno_id"], decisao.responsavel_id)
        if pessoa["tipo"] == "responsavel" and sentido == "saida":
            encerrar_esperas_do_responsavel(pessoa["id"], SAIDA_RESPONSAVEL_GRACA_MIN)
        if turma:
            notificar_responsaveis(pessoa, texto_evento(decisao.tipo, pessoa, turma["nome"], para_local(ocorrido, unidade["fuso"]), retirado_por, parentesco), ev["id"], unidade["id"])
    mensagem = {"entrada": "Bem-vindo(a)", "saida": "Até amanhã"}.get(sentido, "Liberado")
    return {"result": {"event": 7, "user_id": user_id, "user_name": pessoa["nome"], "message": mensagem,
                       "actions": [{"action": "catra", "parameters": f"allow={allow}"}]}}, ev


# ---------------------------------------------------------------- liberação manual

def liberar_manual(unidade, usuario_id, sentido, justificativa, pessoa_id=None, dispositivo_id=None, nome_visitante=None, girar_catraca=True):
    """Recepção libera com justificativa. Fica no log com quem autorizou; pais recebem aviso."""
    quando = agora_utc()
    pessoa = db.query("SELECT * FROM pessoas WHERE id = %s AND unidade_id = %s", (pessoa_id, unidade["id"]), one=True) if pessoa_id else None
    if pessoa_id and not pessoa:
        raise ValueError("pessoa não é desta escola")
    if dispositivo_id and not db.query("SELECT 1 FROM dispositivos WHERE id = %s AND unidade_id = %s", (dispositivo_id, unidade["id"]), one=True):
        raise ValueError("catraca não é desta escola")
    tipo = tipos.LIBERACAO_MANUAL
    turma = None
    if pessoa and pessoa["tipo"] == "aluno":
        turma = _turma_do_aluno(pessoa["id"])
        tipo = tipos.SAIDA_ANTECIPADA if sentido == "saida" else tipos.ENTRADA
    detalhes = {"manual": True}
    if nome_visitante:
        detalhes["visitante"] = nome_visitante
    evento = registrar(unidade["id"], tipo, quando, dispositivo_id=dispositivo_id, pessoa_id=pessoa_id, sentido=sentido, origem="manual",
                       autorizado_por=usuario_id, justificativa=justificativa, detalhes=detalhes)
    if pessoa:
        atualizar_presenca(pessoa["id"], unidade["id"], sentido == "entrada", evento["id"], quando)
        if turma:
            notificar_responsaveis(pessoa, texto_evento(tipo, pessoa, turma["nome"], para_local(quando, unidade["fuso"]), justificativa=justificativa), evento["id"], unidade["id"])
    erro = None
    if girar_catraca and dispositivo_id:
        erro = _liberar_na_catraca(dispositivo_id, sentido)
        if erro:
            db.execute("UPDATE eventos SET detalhes = detalhes || %s WHERE id = %s", (json.dumps({"erro_catraca": erro}), evento["id"]))
    return evento, erro


def _liberar_na_catraca(dispositivo_id, sentido):
    """Quem gira a catraca é o worker (na escola): entra na fila como comando 'liberar'."""
    d = db.query("SELECT id FROM dispositivos WHERE id = %s AND ativo", (dispositivo_id,), one=True)
    if not d:
        return "dispositivo não encontrado"
    # Validade de 60 s: sem internet a recepção clicava 3 vezes e, quando o servidor da escola
    # voltava, a catraca dava 3 giros para quem estivesse na frente.
    db.execute(
        "INSERT INTO fila_sync (dispositivo_id, tipo, payload) VALUES (%s, 'liberar', %s)",
        (dispositivo_id, json.dumps({"sentido": {"entrada": "entrada", "saida": "saida"}.get(sentido, "ambos"),
                                     "expira_em": (agora_utc() + timedelta(seconds=LIBERAR_VALIDADE_SEG)).isoformat()})),
    )
    return None


LIBERAR_VALIDADE_SEG = 60


# ---------------------------------------------------------------- Monitor: outros endpoints

def processar_catra_event(dispositivo, payload):
    """[DOC] confirmação de giro (TURN_LEFT/TURN_RIGHT) ou desistência (GIVE_UP) de um access_event_id."""
    ev = payload.get("event") or {}
    aid = payload.get("access_event_id")
    if not aid:
        return
    epoca = int(dispositivo.get("log_epoca") or 0)
    db.execute("UPDATE eventos SET detalhes = detalhes || %s WHERE dispositivo_id = %s AND log_epoca = %s AND log_id_dispositivo = %s",
               (json.dumps({"giro": ev.get("name")}), dispositivo["id"], epoca, int(aid)))
    if "GIVE" in str(ev.get("name") or "").upper():
        alvo = db.query("SELECT * FROM eventos WHERE dispositivo_id = %s AND log_epoca = %s AND log_id_dispositivo = %s",
                        (dispositivo["id"], epoca, int(aid)), one=True)
        if alvo and alvo["pessoa_id"] and alvo["sentido"] and not (alvo.get("detalhes") or {}).get("desistiu"):
            pessoa = db.query("SELECT * FROM pessoas WHERE id = %s", (alvo["pessoa_id"],), one=True)
            desistencia(_unidade(dispositivo["unidade_id"]), dispositivo, pessoa, alvo["sentido"], alvo["ocorrido_em"])


def processar_operation_mode(dispositivo, payload):
    om = payload.get("operation_mode") or {}
    nome = str(om.get("mode_name") or "").lower()
    contingencia = "conting" in nome
    tipo = tipos.DISPOSITIVO_CONTINGENCIA if contingencia else tipos.DISPOSITIVO_ONLINE
    registrar(dispositivo["unidade_id"], tipo, agora_utc(), dispositivo_id=dispositivo["id"], origem="sistema", detalhes=om)
    # Fica vermelho no painel até a catraca avisar que voltou ao modo online
    db.execute("UPDATE dispositivos SET status = %s, ultimo_contato = now() WHERE id = %s", ("contingencia" if contingencia else "online", dispositivo["id"]))
