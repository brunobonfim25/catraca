"""Monitoramento em tempo real e incidentes (O2 da revisão de 27/10; ampliado em 28/10/2026).

O vigia do worker da nuvem roda `verificar()` a cada 30 s:
  - tudo que está errado agora vira um INCIDENTE (tabela `incidentes`), com início e fim;
  - o incidente crítico chega no Telegram (ALERTA_TELEGRAM_CHAT, o grupo da Nexosix) com o
    botão "Assumir" e é repetido a cada 15 min até alguém assumir — antes o alerta ia uma vez
    só e ninguém sabia se alguém tinha visto;
  - quando o problema some, o incidente fecha e avisa quanto tempo durou.
A tela /painel/monitor (superadmin) mostra o mesmo estado, ao vivo.
O que o sistema não consegue ver de dentro (a nuvem inteira fora) fica com o monitor externo
apontado para /health e /health/worker (ver README).
"""
import logging
import os
import time
from datetime import datetime, timezone

import requests

from . import db

log = logging.getLogger(__name__)

AGENTE_MUDO_SEG = 120        # igual ao FAILOVER_APOS_SEG: mudo assim, a nuvem já está assumindo
CATRACA_FORA_SEG = 180
WORKER_MUDO_SEG = 180
AVISO_ATRASADO_SEG = 300     # aviso aos pais parado na fila há mais de 5 min
REPETIR_MIN = 15
DESVIO_RELOGIO_SEG = 60
TELEGRAM_CHECAR_SEG = 300


def _agora():
    return datetime.now(timezone.utc)


def batimento(nome):
    db.execute("INSERT INTO sistema_status (chave, valor, atualizado_em) VALUES (%s, '{}', now()) "
               "ON CONFLICT (chave) DO UPDATE SET atualizado_em = now()", (f"worker:{nome}",), outbox=False)


def worker_visto(nome="nuvem"):
    r = db.query("SELECT atualizado_em FROM sistema_status WHERE chave = %s", (f"worker:{nome}",), one=True)
    return r["atualizado_em"] if r else None


def worker_ok(nome="nuvem"):
    visto = worker_visto(nome)
    return bool(visto and (_agora() - visto).total_seconds() < WORKER_MUDO_SEG)


def _segundos(iso_ou_dt):
    if not iso_ou_dt:
        return None
    dt = datetime.fromisoformat(iso_ou_dt) if isinstance(iso_ou_dt, str) else iso_ou_dt
    return int((_agora() - dt).total_seconds())


def _status(chave):
    return db.query("SELECT valor, atualizado_em FROM sistema_status WHERE chave = %s", (chave,), one=True)


def _gravar_status(chave, valor):
    from psycopg2.extras import Json

    db.execute("INSERT INTO sistema_status (chave, valor, atualizado_em) VALUES (%s, %s, now()) "
               "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now()", (chave, Json(valor)), outbox=False)


# ---------------------------------------------------------------- o que está errado agora

def _site_da_nuvem():
    """O worker confere o site (o painel e a API da catraca em failover). Duas falhas
    seguidas = incidente: um soluço de rede não acorda ninguém."""
    base = os.environ.get("BASE_URL", "").rstrip("/")
    if not base.startswith("http") or os.environ.get("MODO") != "nuvem":
        return None
    try:
        ok = requests.get(f"{base}/health", timeout=10).ok
    except requests.RequestException:
        ok = False
    st = _status("monitor:site")
    falhas = 0 if ok else int(((st or {}).get("valor") or {}).get("falhas", 0)) + 1
    _gravar_status("monitor:site", {"ok": ok, "falhas": falhas})
    return falhas


def _telegram():
    """Webhook do bot (é por ele que os pais ativam) — conferido a cada 5 min."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return None
    st = _status("monitor:telegram")
    if st and _segundos(st["atualizado_em"]) < TELEGRAM_CHECAR_SEG:
        return st["valor"]
    from .notificacoes import telegram

    try:
        info = telegram._chamar("getWebhookInfo", {}).get("result") or {}
        valor = {"ok": True, "pendentes": info.get("pending_update_count", 0), "ultimo_erro": info.get("last_error_message"),
                 "erro_em": info.get("last_error_date")}
    except Exception as e:  # noqa: BLE001
        valor = {"ok": False, "erro": str(e)[:200]}
    _gravar_status("monitor:telegram", valor)
    return valor


def verificacoes():
    """Lista de {chave, texto, gravidade, unidade_id} de tudo que está errado agora."""
    out = []

    def add(chave, texto, gravidade="critico", unidade_id=None):
        out.append({"chave": chave, "texto": texto, "gravidade": gravidade, "unidade_id": unidade_id})

    schema = db.query("SELECT max(versao) AS v FROM schema_migrations", one=True)["v"]
    for u in db.query("SELECT * FROM unidades WHERE ativo AND config ? 'espelho_token'"):
        cfg = u["config"] or {}
        ag = cfg.get("agente") or {}
        mudo = _segundos(cfg.get("agente_visto_em"))
        if mudo is not None and mudo > AGENTE_MUDO_SEG:
            add(f"agente:{u['id']}", f"Servidor da escola {u['nome']} sem sinal há {mudo // 60} min", unidade_id=u["id"])
        elif mudo is not None:
            if ag.get("web_ok") is False:
                add(f"agente_web:{u['id']}", f"Site do servidor da escola {u['nome']} fora do ar (a catraca não consegue falar com ele)", unidade_id=u["id"])
            if ag.get("schema") and schema and ag["schema"] != schema:
                add(f"agente_versao:{u['id']}", f"Servidor da escola {u['nome']} desatualizado ({ag['schema'][:3]} × {schema[:3]}): rodar o instalador", "atencao", u["id"])
            desvio = ag.get("desvio_relogio_seg")
            if desvio is not None and abs(desvio) > DESVIO_RELOGIO_SEG:
                add(f"agente_relogio:{u['id']}", f"Relógio do PC da escola {u['nome']} {desvio:+d} s diferente da nuvem", "atencao", u["id"])
    for d in db.query("SELECT d.id, d.nome, d.status, d.ultimo_contato, d.unidade_id, u.nome AS escola FROM dispositivos d JOIN unidades u ON u.id = d.unidade_id "
                      "WHERE d.ativo AND u.ativo AND d.status IN ('offline','contingencia') "
                      "AND (d.ultimo_contato IS NULL OR d.ultimo_contato < now() - make_interval(secs => %s))", (CATRACA_FORA_SEG,)):
        estado = "em contingência (nega a saída de quem precisa de responsável)" if d["status"] == "contingencia" else "offline"
        add(f"catraca:{d['id']}", f"Catraca {d['nome']} ({d['escola']}) {estado}", unidade_id=d["unidade_id"])
    for f in db.query("SELECT d.unidade_id, u.nome, count(*) AS n FROM fila_sync f JOIN dispositivos d ON d.id = f.dispositivo_id JOIN unidades u ON u.id = d.unidade_id "
                      "WHERE f.status = 'erro' AND f.criado_em > now() - interval '1 day' GROUP BY d.unidade_id, u.nome"):
        add(f"fila_catraca:{f['unidade_id']}", f"{f['n']} envio(s) para a catraca de {f['nome']} com erro nas últimas 24 h", "atencao", f["unidade_id"])
    atraso = db.query("SELECT count(*) AS n, min(criado_em) AS desde FROM fila_notificacao WHERE status = 'pendente' "
                      "AND criado_em < now() - make_interval(secs => %s)", (AVISO_ATRASADO_SEG,), one=True)
    if atraso["n"]:
        add("avisos_atrasados", f"{atraso['n']} aviso(s) aos pais parados na fila (o mais velho há {_segundos(atraso['desde']) // 60} min)")
    n = db.query("SELECT count(*) AS n FROM fila_notificacao WHERE status = 'erro' AND criado_em > now() - interval '1 hour'", one=True)["n"]
    if n >= 5:
        add("telegram", f"{n} avisos aos pais falharam na última hora")
    tg = _telegram()
    if tg and not tg.get("ok"):
        add("telegram_api", f"Bot do Telegram não responde: {tg.get('erro', '')[:120]}")
    elif tg and tg.get("ultimo_erro") and tg.get("erro_em") and time.time() - int(tg["erro_em"]) < 600:
        add("telegram_webhook", f"Telegram não consegue entregar mensagens ao sistema: {tg['ultimo_erro'][:120]}")
    falhas = _site_da_nuvem()
    if falhas and falhas >= 2:
        add("site_nuvem", "Site da nuvem não responde (painel e reserva das catracas fora)")
    from . import backup

    if all(backup.configuracao()):
        b = backup.ultimo()
        erro = b and (b["valor"] or {}).get("erro")
        if not b or erro or (_agora() - b["atualizado_em"]).days >= backup.INTERVALO_DIAS + 1:
            add("backup", f"Cópia do banco não saiu{': ' + erro[:120] if erro else ''}", "atencao")
    for q in db.query("SELECT q.unidade_id, u.nome, count(*) AS n FROM espelho_quarentena q LEFT JOIN unidades u ON u.id = q.unidade_id GROUP BY q.unidade_id, u.nome"):
        add(f"quarentena:{q['unidade_id']}", f"{q['n']} alteração(ões) de {q['nome'] or q['unidade_id']} presas na sincronização", "atencao", q["unidade_id"])
    # incidentes que vêm de fora (deploy que falhou) ficam abertos até o webhook dizer que voltou
    for i in db.query("SELECT chave, texto, gravidade, unidade_id FROM incidentes WHERE fim IS NULL AND chave LIKE 'deploy:%%'"):
        add(i["chave"], i["texto"], i["gravidade"], i["unidade_id"])
    return out


def problemas():
    """{chave: texto} — usado no painel da rede."""
    return {p["chave"]: p["texto"] for p in verificacoes()}


# ---------------------------------------------------------------- incidentes

def _botao(inc_id):
    return {"inline_keyboard": [[{"text": "🙋 Assumir", "callback_data": f"inc:{inc_id}"}]]}


def _duracao(seg):
    seg = int(seg)
    if seg < 3600:
        return f"{max(1, seg // 60)} min"
    return f"{seg // 3600} h {seg % 3600 // 60:02d} min"


def _mandar(enviar, chat, texto, markup=None):
    if not chat:
        return
    try:
        enviar(chat, texto, reply_markup=markup) if markup else enviar(chat, texto)
    except Exception as e:  # noqa: BLE001 — o alerta não pode derrubar o vigia
        log.warning("alerta não enviado: %s", e)


def abrir(chave, texto, gravidade="critico", unidade_id=None):
    """Abre (se ainda não houver aberto). Devolve o id do incidente novo ou None."""
    r = db.execute("INSERT INTO incidentes (chave, texto, gravidade, unidade_id) VALUES (%s,%s,%s,%s) "
                   "ON CONFLICT (chave) WHERE fim IS NULL DO NOTHING RETURNING id", (chave, texto, gravidade, unidade_id),
                   returning=True, outbox=False)
    return r and r["id"]


def fechar(chave):
    return db.execute("UPDATE incidentes SET fim = now() WHERE chave = %s AND fim IS NULL RETURNING *", (chave,), returning=True, outbox=False)


def verificar(enviar=None):
    """Uma volta do vigia. Devolve as mensagens mandadas (ou que seriam, sem chat)."""
    from .notificacoes import telegram

    enviar = enviar or telegram.enviar
    chat = os.environ.get("ALERTA_TELEGRAM_CHAT", "")
    agora = {p["chave"]: p for p in verificacoes()}
    abertos = {i["chave"]: i for i in db.query("SELECT * FROM incidentes WHERE fim IS NULL")}
    msgs = []
    for chave, p in agora.items():
        if chave in abertos:
            continue
        inc_id = abrir(chave, p["texto"], p["gravidade"], p["unidade_id"])
        if inc_id:
            texto = ("🚨 " if p["gravidade"] == "critico" else "⚠️ ") + p["texto"]
            msgs.append(texto)
            _mandar(enviar, chat, texto, _botao(inc_id) if p["gravidade"] == "critico" else None)
            db.execute("UPDATE incidentes SET ultimo_aviso = now(), avisos = 1 WHERE id = %s", (inc_id,), outbox=False)
    for chave, i in abertos.items():
        if chave not in agora:
            fechado = fechar(chave)
            if fechado:
                texto = f"✅ Resolvido em {_duracao((fechado['fim'] - fechado['inicio']).total_seconds())}: {i['texto']}"
                msgs.append(texto)
                _mandar(enviar, chat, texto)
        elif (i["gravidade"] == "critico" and not i["reconhecido_em"] and i["ultimo_aviso"]
              and _segundos(i["ultimo_aviso"]) >= REPETIR_MIN * 60):
            # ninguém assumiu: insiste
            texto = f"⏰ Sem resposta há {_duracao(_segundos(i['inicio']))}: {agora[chave]['texto']}"
            msgs.append(texto)
            _mandar(enviar, chat, texto, _botao(i["id"]))
            db.execute("UPDATE incidentes SET ultimo_aviso = now(), avisos = avisos + 1, texto = %s WHERE id = %s",
                       (agora[chave]["texto"], i["id"]), outbox=False)
    for m in msgs:
        log.warning("alerta: %s", m)
    return msgs


def reconhecer(inc_id, quem):
    r = db.execute("UPDATE incidentes SET reconhecido_por = %s, reconhecido_em = now() WHERE id = %s AND reconhecido_em IS NULL RETURNING *",
                   (quem[:80], int(inc_id)), returning=True, outbox=False)
    return r


def responder_botao(chat_id, inc_id, quem):
    """Clique em "Assumir" no Telegram. Só vale no chat de alertas da Nexosix."""
    if str(chat_id) != os.environ.get("ALERTA_TELEGRAM_CHAT", "") or not str(inc_id).isdigit():
        return "Este botão não vale aqui."
    r = reconhecer(inc_id, quem)
    if not r:
        i = db.query("SELECT reconhecido_por, fim FROM incidentes WHERE id = %s", (int(inc_id),), one=True)
        if i and i["fim"]:
            return "Já foi resolvido."
        return f"Já assumido por {i['reconhecido_por']}." if i else "Incidente não encontrado."
    return f"Assumido por {quem}."


def railway_evento(payload):
    """Webhook do Railway (deploy que falhou/caiu → incidente; deploy que subiu → fecha).
    [SUP] formato do payload conferido com o test-webhook do Railway; os campos são lidos
    com cuidado porque o formato não é documentado em detalhe."""
    tipo = str(payload.get("type") or "")
    servico = (((payload.get("resource") or {}).get("service") or {}).get("name") or (payload.get("service") or {}).get("name")
               or (payload.get("details") or {}).get("serviceName") or "serviço")
    chave = f"deploy:{servico}"
    if tipo in ("Deployment.failed", "Deployment.crashed", "Deployment.oom_killed"):
        motivo = {"Deployment.failed": "deploy falhou", "Deployment.crashed": "caiu", "Deployment.oom_killed": "sem memória"}[tipo]
        abrir(chave, f"Railway: {servico} {motivo}")
        return "aberto"
    if tipo in ("Deployment.deployed", "Deployment.redeployed", "Deployment.resumed", "Deployment.restarted"):
        return "fechado" if fechar(chave) else "nada"
    return "ignorado"


# ---------------------------------------------------------------- tela do monitor

def estado(modo="nuvem"):
    """Tudo o que a tela /painel/monitor mostra, numa consulta por bloco."""
    t0 = time.time()
    db.query("SELECT 1")
    banco_ms = int((time.time() - t0) * 1000)
    w = worker_visto(modo)
    tg = (_status("monitor:telegram") or {}).get("valor") or {}
    site = (_status("monitor:site") or {}).get("valor") or {}
    from . import backup

    b = backup.ultimo()
    nuvem = {
        "worker_seg": _segundos(w), "worker_ok": worker_ok(modo), "banco_ms": banco_ms,
        "site_ok": site.get("ok", True), "telegram": tg,
        "backup_em": b["atualizado_em"].isoformat() if b else None, "backup_erro": b and (b["valor"] or {}).get("erro"),
        "backup_configurado": all(backup.configuracao()),
        "avisos": db.query("SELECT count(*) FILTER (WHERE status = 'pendente') AS pendentes, "
                           "count(*) FILTER (WHERE status = 'enviado' AND enviado_em > now() - interval '1 hour') AS enviados_1h, "
                           "count(*) FILTER (WHERE status = 'erro' AND criado_em > now() - interval '1 hour') AS erros_1h "
                           "FROM fila_notificacao", one=True),
    }
    escolas = []
    for u in db.query("SELECT * FROM unidades WHERE ativo ORDER BY nome"):
        cfg = u["config"] or {}
        ag = cfg.get("agente") or {}
        disps = db.query("SELECT d.id, d.nome, d.status, d.modo, d.ultimo_contato, "
                         "(SELECT count(*) FROM fila_sync f WHERE f.dispositivo_id = d.id AND f.status IN ('pendente','processando')) AS fila, "
                         "(SELECT count(*) FROM fila_sync f WHERE f.dispositivo_id = d.id AND f.status = 'erro' AND f.criado_em > now() - interval '1 day') AS erros "
                         "FROM dispositivos d WHERE d.unidade_id = %s AND d.ativo ORDER BY d.nome", (u["id"],))
        ult = db.query("SELECT max(ocorrido_em) AS em FROM eventos WHERE unidade_id = %s AND ocorrido_em > now() - interval '7 days'", (u["id"],), one=True)["em"]
        escolas.append({
            "id": u["id"], "nome": u["nome"], "tem_agente": bool(cfg.get("espelho_token")),
            "agente_seg": _segundos(cfg.get("agente_visto_em")), "web_ok": ag.get("web_ok"), "schema": ag.get("schema"),
            "desvio": ag.get("desvio_relogio_seg"), "outbox": ag.get("outbox_pendentes"), "ip": ag.get("ip_lan"),
            "failover": bool((cfg.get("failover") or {}).get("ativo")),
            "quarentena": db.query("SELECT count(*) AS n FROM espelho_quarentena WHERE unidade_id = %s", (u["id"],), one=True)["n"],
            "catracas": [{**dict(d), "contato_seg": _segundos(d["ultimo_contato"]), "ultimo_contato": None} for d in disps],
            "ultimo_evento_seg": _segundos(ult),
        })
    abertos = db.query("SELECT i.*, u.nome AS escola FROM incidentes i LEFT JOIN unidades u ON u.id = i.unidade_id WHERE i.fim IS NULL "
                       "ORDER BY (i.gravidade = 'critico') DESC, i.inicio")
    recentes = db.query("SELECT i.*, u.nome AS escola FROM incidentes i LEFT JOIN unidades u ON u.id = i.unidade_id WHERE i.fim IS NOT NULL "
                        "ORDER BY i.fim DESC LIMIT 20")

    def _inc(i):
        return {"id": i["id"], "texto": i["texto"], "gravidade": i["gravidade"], "escola": i["escola"],
                "ha": _duracao(_segundos(i["inicio"])), "inicio": i["inicio"].isoformat(),
                "duracao": _duracao((i["fim"] - i["inicio"]).total_seconds()) if i["fim"] else None,
                "assumido_por": i["reconhecido_por"], "avisos": i["avisos"]}

    return {"nuvem": nuvem, "escolas": escolas, "abertos": [_inc(i) for i in abertos], "recentes": [_inc(i) for i in recentes],
            "agora": _agora().isoformat()}
