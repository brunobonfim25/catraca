"""Convite em massa para o responsável ativar os avisos do Telegram.

O Telegram não deixa o bot mandar a primeira mensagem só sabendo o celular: o responsável
tem que tocar num link e apertar Iniciar (o integrador confirmou: é assim no sistema dele
também). Então o trabalho é fazer o link chegar em todo mundo, sem custo por mensagem:

- WhatsApp: a secretaria manda do WhatsApp dela (wa.me com o texto pronto), um clique por família;
- e-mail: fila do worker da nuvem (Brevo/Resend), todos de uma vez;
- link da escola (cartaz, grupo da turma): telegram.link_escola, ativa pelo telefone.

O link de cada convite é o token individual do responsável, REAPROVEITADO se já existe: gerar
um novo a cada convite invalidaria o que foi mandado antes pelo outro canal.
"""
import html
import logging
from datetime import timedelta

from .. import db
from ..util import primeiro_nome
from . import email as email_mod
from . import telegram

log = logging.getLogger(__name__)

TEXTO_PADRAO = (
    "Olá, {responsavel}!\n\n"
    "Você vai receber no Telegram, na hora, cada entrada e saída de {alunos}. Os avisos são enviados pela escola ({escola}).\n\n"
    "Para ativar (leva 1 minuto):\n"
    "1. Instale o Telegram no celular, se ainda não tiver.\n"
    "2. Toque no link abaixo e depois em \"Iniciar\".\n"
    "3. Responda, no próprio Telegram, se autoriza o uso da foto na catraca.\n\n"
    "{link}\n\n"
    "É gratuito. Só a escola envia mensagens por esse canal."
)
ASSUNTO = "{escola}: avisos de entrada e saída de {alunos}"
MAX_TENTATIVAS = 4
BACKOFF_SEG = (60, 600, 3600, 6 * 3600)


def nome_escola(unidade):
    return ((unidade.get("config") or {}).get("nome_convite") or "").strip() or unidade["nome"]


def texto_da_escola(unidade):
    return ((unidade.get("config") or {}).get("convite_texto") or "").strip() or TEXTO_PADRAO


def _lista_nomes(nomes):
    nomes = [primeiro_nome(n) for n in nomes] or ["seu filho(a)"]
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]


def preencher(modelo, responsavel, alunos, escola, link):
    # replace e não format(): chave digitada errada no texto da escola não pode derrubar o envio
    return (modelo.replace("{responsavel}", primeiro_nome(responsavel)).replace("{alunos}", _lista_nomes(alunos))
            .replace("{escola}", escola).replace("{link}", link))


def _responsavel(responsavel_id):
    r = db.query("SELECT r.*, p.nome, p.unidade_id, p.status FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE r.id = %s",
                 (responsavel_id,), one=True)
    if not r:
        return None, []
    alunos = [a["nome"] for a in db.query(
        "SELECT pa.nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id JOIN pessoas pa ON pa.id = a.pessoa_id "
        "WHERE ar.responsavel_id = %s AND pa.status <> 'desligado' ORDER BY pa.nome", (responsavel_id,))]
    return r, alunos


def link_do(r):
    return telegram.link_ativacao(r["telegram_token"] or telegram.gerar_token(r["id"]))


def montar(unidade, responsavel_id, logo_url=None):
    """Texto, assunto e HTML do convite deste responsável, com o link individual dele."""
    r, alunos = _responsavel(responsavel_id)
    link = link_do(r)
    escola = nome_escola(unidade)
    texto = preencher(texto_da_escola(unidade), r["nome"], alunos, escola, link)
    assunto = preencher(ASSUNTO, r["nome"], alunos, escola, link)
    return {"responsavel": r, "alunos": alunos, "link": link, "texto": texto, "assunto": assunto,
            "html": montar_html(escola, texto, link, logo_url)}


def montar_html(escola, texto, link, logo_url=None):
    """E-mail simples em tabela (Outlook/Gmail), com o link virando botão."""
    blocos = []
    for paragrafo in texto.split("\n\n"):
        if link in paragrafo:
            antes, _, depois = paragrafo.partition(link)
            if antes.strip():
                blocos.append(f'<p style="margin:0 0 14px">{html.escape(antes.strip())}</p>')
            blocos.append(
                f'<p style="margin:18px 0;text-align:center"><a href="{html.escape(link)}" style="background:#229ED9;color:#fff;'
                f'text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:bold;display:inline-block">Ativar avisos no Telegram</a></p>'
                f'<p style="margin:0 0 14px;font-size:12px;color:#64748b;text-align:center">Se o botão não abrir, copie: {html.escape(link)}</p>')
            if depois.strip():
                blocos.append(f'<p style="margin:0 0 14px">{html.escape(depois.strip())}</p>')
        else:
            blocos.append(f'<p style="margin:0 0 14px">{"<br>".join(html.escape(ln) for ln in paragrafo.splitlines())}</p>')
    topo = (f'<img src="{html.escape(logo_url)}" alt="{html.escape(escola)}" style="max-height:64px;max-width:220px">' if logo_url
            else f'<b style="font-size:18px">{html.escape(escola)}</b>')
    return (
        '<!doctype html><html><body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#1e293b">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:10px">'
        f'<tr><td style="padding:20px 28px;border-bottom:1px solid #e2e8f0">{topo}</td></tr>'
        f'<tr><td style="padding:24px 28px;font-size:15px;line-height:1.5">{"".join(blocos)}</td></tr>'
        '<tr><td style="padding:14px 28px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8">'
        'Você recebeu este e-mail porque está cadastrado como responsável na escola. Avisos por Nexosix Acesso.</td></tr>'
        '</table></td></tr></table></body></html>'
    )


# ---------------------------------------------------------------- WhatsApp (um clique por família)

def whatsapp(unidade, responsavel_id, usuario_id=None):
    """Link wa.me com o convite pronto + registro de que foi enviado. None se não tem celular."""
    c = montar(unidade, responsavel_id)
    url = telegram.link_whatsapp(c["responsavel"]["telefone"], c["texto"])
    if not url:
        return None
    db.execute("INSERT INTO convites (unidade_id, responsavel_id, canal, destino, status, enviado_em, criado_por) "
               "VALUES (%s,%s,'whatsapp',%s,'enviado',now(),%s)", (unidade["id"], responsavel_id, c["responsavel"]["telefone"], usuario_id))
    return url


# ---------------------------------------------------------------- e-mail (fila)

def enfileirar_emails(unidade, responsavel_ids, usuario_id=None, logo_url=None):
    """Põe na fila um e-mail por responsável. Pula quem não tem e-mail, já ativou ou já tem
    e-mail na fila. Devolve (enfileirados, pulados)."""
    n = pulados = 0
    with db.lote():
        for rid in responsavel_ids:
            r, _ = _responsavel(rid)
            if (not r or r["unidade_id"] != unidade["id"] or not r["email"] or r["telegram_chat_id"] or r["status"] != "ativo"
                    or db.query("SELECT 1 FROM convites WHERE responsavel_id = %s AND canal = 'email' AND status = 'pendente'", (rid,), one=True)):
                pulados += 1
                continue
            c = montar(unidade, rid, logo_url)
            db.execute("INSERT INTO convites (unidade_id, responsavel_id, canal, destino, assunto, corpo_html, corpo_texto, criado_por) "
                       "VALUES (%s,%s,'email',%s,%s,%s,%s,%s)", (unidade["id"], rid, r["email"], c["assunto"], c["html"], c["texto"], usuario_id))
            n += 1
    return n, pulados


def _reservar(limite):
    with db.transacao() as cur:
        cur.execute("SELECT id FROM convites WHERE status = 'pendente' AND canal = 'email' AND proximo_em <= now() "
                    "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED", (limite,))
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return []
        cur.execute("UPDATE convites SET tentativas = tentativas + 1 WHERE id = ANY(%s)", (ids,))
        cur.execute("SELECT c.*, u.nome AS unidade_nome, u.config AS unidade_config FROM convites c JOIN unidades u ON u.id = c.unidade_id "
                    "WHERE c.id = ANY(%s) ORDER BY c.id", (ids,))
        return cur.fetchall()


def enviar_pendentes(limite=30, enviar=None):
    """Worker da nuvem. `enviar(para, assunto, html, texto, nome_remetente, responder_para)`
    injetável nos testes. Devolve (enviados, falhas)."""
    enviar = enviar or email_mod.enviar
    enviados = falhas = 0
    for c in _reservar(limite):
        cfg = c["unidade_config"] or {}
        try:
            enviar(c["destino"], c["assunto"], c["corpo_html"], c["corpo_texto"],
                   nome_remetente=nome_escola({"nome": c["unidade_nome"], "config": cfg}), responder_para=cfg.get("email_contato") or None)
            db.execute("UPDATE convites SET status = 'enviado', enviado_em = now(), erro = NULL WHERE id = %s", (c["id"],))
            enviados += 1
        except Exception as e:  # noqa: BLE001 — provedor fora/cota: tenta de novo mais tarde
            falhas += 1
            if getattr(e, "cota", False):
                # Revisão (A19): a cota do dia virava erro permanente e os convites acima de 300
                # nunca saíam. Fica pra amanhã cedo, sem gastar tentativa — e o resto do lote também.
                db.execute("UPDATE convites SET erro = %s, tentativas = GREATEST(tentativas - 1, 0), "
                           "proximo_em = date_trunc('day', now()) + interval '1 day 6 hours' WHERE status = 'pendente' AND canal = 'email'",
                           (f"cota diária do e-mail: sai amanhã ({str(e)[:200]})",))
                log.warning("cota diária de e-mail estourada: convites ficam para amanhã")
                break
            definitivo = c["tentativas"] >= MAX_TENTATIVAS or "não configurado" in str(e)
            if definitivo:
                db.execute("UPDATE convites SET status = 'erro', erro = %s WHERE id = %s", (str(e)[:300], c["id"]))
            else:
                espera = BACKOFF_SEG[min(c["tentativas"], len(BACKOFF_SEG)) - 1]
                db.execute("UPDATE convites SET erro = %s, proximo_em = now() + %s WHERE id = %s", (str(e)[:300], timedelta(seconds=espera), c["id"]))
            log.warning("convite por e-mail %s falhou: %s", c["id"], e)
    return enviados, falhas


# ---------------------------------------------------------------- tela

def lista(unidade_id, filtro=None):
    """Responsáveis ativos que ainda não ativaram o Telegram, com o último convite."""
    sql = (
        "SELECT r.id, r.telefone, r.email, p.id AS pessoa_id, p.nome, "
        "(SELECT string_agg(pa.nome, ', ' ORDER BY pa.nome) FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id "
        " JOIN pessoas pa ON pa.id = a.pessoa_id WHERE ar.responsavel_id = r.id AND pa.status <> 'desligado') AS alunos, "
        "c.canal AS ult_canal, c.status AS ult_status, c.criado_em AS ult_em, c.erro AS ult_erro, "
        "(SELECT count(*) FROM convites c2 WHERE c2.responsavel_id = r.id) AS n_convites "
        "FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id "
        "LEFT JOIN LATERAL (SELECT * FROM convites c WHERE c.responsavel_id = r.id ORDER BY c.criado_em DESC LIMIT 1) c ON true "
        "WHERE p.unidade_id = %s AND p.status = 'ativo' AND r.telegram_chat_id IS NULL"
    )
    if filtro == "nunca":
        sql += " AND c.id IS NULL"
    elif filtro == "convidados":
        sql += " AND c.id IS NOT NULL"
    return db.query(sql + " ORDER BY (c.id IS NOT NULL), p.nome", (unidade_id,))


def resumo(unidade_id):
    return db.query(
        "SELECT count(*) FILTER (WHERE r.telegram_chat_id IS NOT NULL) AS ativos, count(*) FILTER (WHERE r.telegram_chat_id IS NULL) AS faltam, "
        "count(*) FILTER (WHERE r.telegram_chat_id IS NULL AND NOT EXISTS (SELECT 1 FROM convites c WHERE c.responsavel_id = r.id)) AS nunca, "
        "count(*) FILTER (WHERE r.telegram_chat_id IS NULL AND r.email IS NOT NULL) AS com_email, "
        "(SELECT count(*) FROM convites c WHERE c.unidade_id = %s AND c.canal = 'email' AND c.status = 'pendente') AS na_fila "
        "FROM responsaveis r JOIN pessoas p ON p.id = r.pessoa_id WHERE p.unidade_id = %s AND p.status = 'ativo'",
        (unidade_id, unidade_id), one=True,
    )
