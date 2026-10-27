"""Pessoas: alunos, responsáveis, funcionários, visitantes, vínculos e fotos."""
import json

from .. import db
from ..auditoria import auditar
from ..dispositivos import service as dispositivos

MAX_RESPONSAVEIS = 5
FOTO_MAX_BYTES = 1024 * 1024  # a catraca aceita 2 MB; 1 MB já sobra pra uma face de webcam


class FotoInvalida(ValueError):
    pass


def validar_jpeg(dados: bytes):
    if not dados or len(dados) < 100:
        raise FotoInvalida("foto vazia")
    if dados[:2] != b"\xff\xd8":
        raise FotoInvalida("a foto precisa ser JPEG")
    if len(dados) > FOTO_MAX_BYTES:
        raise FotoInvalida("foto maior que 1 MB")


def _criar_pessoa(unidade_id, tipo, nome, documento=None):
    return db.execute(
        "INSERT INTO pessoas (unidade_id, tipo, nome, documento) VALUES (%s,%s,%s,%s) RETURNING id",
        (unidade_id, tipo, nome.strip(), (documento or "").strip() or None), returning=True,
    )["id"]


def criar_aluno(unidade_id, nome, turma_id, matricula=None, saida_autonoma=None, documento=None):
    pessoa_id = _criar_pessoa(unidade_id, "aluno", nome, documento)
    aluno_id = db.execute(
        "INSERT INTO alunos (pessoa_id, turma_id, matricula, saida_autonoma) VALUES (%s,%s,%s,%s) RETURNING id",
        (pessoa_id, turma_id, matricula or None, saida_autonoma), returning=True,
    )["id"]
    auditar("criar", "aluno", aluno_id, depois={"nome": nome, "turma_id": turma_id}, unidade_id=unidade_id)
    dispositivos.enfileirar_pessoa(pessoa_id, unidade_id, com_foto=False)
    return pessoa_id, aluno_id


def atualizar_aluno(pessoa_id, nome, turma_id, matricula=None, saida_autonoma=None, documento=None):
    antes = db.query("SELECT p.nome, a.turma_id, a.matricula, a.saida_autonoma FROM pessoas p JOIN alunos a ON a.pessoa_id = p.id WHERE p.id = %s", (pessoa_id,), one=True)
    db.execute("UPDATE pessoas SET nome = %s, documento = %s, atualizado_em = now() WHERE id = %s", (nome.strip(), documento or None, pessoa_id))
    db.execute("UPDATE alunos SET turma_id = %s, matricula = %s, saida_autonoma = %s WHERE pessoa_id = %s", (turma_id, matricula or None, saida_autonoma, pessoa_id))
    auditar("editar", "aluno", pessoa_id, antes=antes, depois={"nome": nome, "turma_id": turma_id, "matricula": matricula, "saida_autonoma": saida_autonoma})
    p = db.query("SELECT unidade_id FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    dispositivos.enfileirar_pessoa(pessoa_id, p["unidade_id"], com_foto=False)


def _email(valor):
    v = (valor or "").strip().lower()
    return v if "@" in v and "." in v.split("@")[-1] else None


def criar_responsavel(unidade_id, nome, telefone=None, documento=None, email=None):
    pessoa_id = _criar_pessoa(unidade_id, "responsavel", nome, documento)
    resp_id = db.execute(
        "INSERT INTO responsaveis (pessoa_id, telefone, email) VALUES (%s,%s,%s) RETURNING id",
        (pessoa_id, (telefone or "").strip() or None, _email(email)), returning=True,
    )["id"]
    auditar("criar", "responsavel", resp_id, depois={"nome": nome}, unidade_id=unidade_id)
    dispositivos.enfileirar_pessoa(pessoa_id, unidade_id, com_foto=False)
    return pessoa_id, resp_id


def atualizar_responsavel(pessoa_id, nome, telefone=None, documento=None, email=None):
    db.execute("UPDATE pessoas SET nome = %s, documento = %s, atualizado_em = now() WHERE id = %s", (nome.strip(), documento or None, pessoa_id))
    db.execute("UPDATE responsaveis SET telefone = %s, email = %s WHERE pessoa_id = %s", ((telefone or "").strip() or None, _email(email), pessoa_id))
    auditar("editar", "responsavel", pessoa_id, depois={"nome": nome, "telefone": telefone, "email": _email(email)})
    p = db.query("SELECT unidade_id FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    dispositivos.enfileirar_pessoa(pessoa_id, p["unidade_id"], com_foto=False)


def criar_funcionario(unidade_id, nome, cargo=None, escala=None, documento=None):
    pessoa_id = _criar_pessoa(unidade_id, "funcionario", nome, documento)
    func_id = db.execute(
        "INSERT INTO funcionarios (pessoa_id, cargo, escala) VALUES (%s,%s,%s) RETURNING id",
        (pessoa_id, cargo or None, json.dumps(escala or {})), returning=True,
    )["id"]
    auditar("criar", "funcionario", func_id, depois={"nome": nome, "cargo": cargo, "escala": escala}, unidade_id=unidade_id)
    dispositivos.enfileirar_pessoa(pessoa_id, unidade_id, com_foto=False)
    return pessoa_id, func_id


def atualizar_funcionario(pessoa_id, nome, cargo=None, escala=None, documento=None):
    db.execute("UPDATE pessoas SET nome = %s, documento = %s, atualizado_em = now() WHERE id = %s", (nome.strip(), documento or None, pessoa_id))
    db.execute("UPDATE funcionarios SET cargo = %s, escala = %s WHERE pessoa_id = %s", (cargo or None, json.dumps(escala or {}), pessoa_id))
    auditar("editar", "funcionario", pessoa_id, depois={"nome": nome, "cargo": cargo, "escala": escala})
    p = db.query("SELECT unidade_id FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    dispositivos.enfileirar_pessoa(pessoa_id, p["unidade_id"], com_foto=False)


def criar_visitante(unidade_id, nome, documento=None):
    """Visitante não vai pra catraca: entra por liberação manual registrada."""
    pessoa_id = _criar_pessoa(unidade_id, "visitante", nome, documento)
    auditar("criar", "visitante", pessoa_id, depois={"nome": nome, "documento": documento}, unidade_id=unidade_id)
    return pessoa_id


def vincular(aluno_id, responsavel_id, parentesco=None, regra="sempre", dias_semana=None):
    n = db.query("SELECT count(*) AS n FROM aluno_responsavel WHERE aluno_id = %s", (aluno_id,), one=True)["n"]
    ja = db.query("SELECT 1 FROM aluno_responsavel WHERE aluno_id = %s AND responsavel_id = %s", (aluno_id, responsavel_id), one=True)
    if not ja and n >= MAX_RESPONSAVEIS:
        raise ValueError(f"aluno já tem {MAX_RESPONSAVEIS} responsáveis")
    if regra not in ("sempre", "dias", "aviso"):
        raise ValueError("regra inválida")
    db.execute(
        "INSERT INTO aluno_responsavel (aluno_id, responsavel_id, parentesco, regra, dias_semana) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (aluno_id, responsavel_id) DO UPDATE SET parentesco = EXCLUDED.parentesco, regra = EXCLUDED.regra, dias_semana = EXCLUDED.dias_semana",
        (aluno_id, responsavel_id, parentesco or None, regra, list(dias_semana or [])),
    )
    auditar("vincular", "aluno_responsavel", f"{aluno_id}:{responsavel_id}", depois={"parentesco": parentesco, "regra": regra, "dias": dias_semana})


def desvincular(aluno_id, responsavel_id):
    db.execute("DELETE FROM aluno_responsavel WHERE aluno_id = %s AND responsavel_id = %s", (aluno_id, responsavel_id))
    auditar("desvincular", "aluno_responsavel", f"{aluno_id}:{responsavel_id}")


def salvar_foto(pessoa_id, jpeg: bytes):
    validar_jpeg(jpeg)
    p = db.query("SELECT unidade_id, tipo FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    if not p:
        raise ValueError("pessoa não existe")
    db.execute(
        "INSERT INTO pessoa_fotos (pessoa_id, jpeg) VALUES (%s,%s) ON CONFLICT (pessoa_id) DO UPDATE SET jpeg = EXCLUDED.jpeg, atualizado_em = now()",
        (pessoa_id, psycopg_bytes(jpeg)),
    )
    db.execute("UPDATE pessoas SET foto_atualizada_em = now(), atualizado_em = now() WHERE id = %s", (pessoa_id,))
    db.execute("UPDATE dispositivo_pessoa SET foto_sync_status = 'pendente', erro = NULL WHERE pessoa_id = %s", (pessoa_id,))
    auditar("foto", "pessoa", pessoa_id, depois={"bytes": len(jpeg)})
    if p["tipo"] != "visitante":
        dispositivos.enfileirar_pessoa(pessoa_id, p["unidade_id"], com_foto=True)


def psycopg_bytes(dados):
    import psycopg2

    return psycopg2.Binary(dados)


def obter_foto(pessoa_id):
    r = db.query("SELECT jpeg FROM pessoa_fotos WHERE pessoa_id = %s", (pessoa_id,), one=True)
    return bytes(r["jpeg"]) if r else None


def bloquear(pessoa_id, bloquear=True):
    novo = "bloqueado" if bloquear else "ativo"
    p = db.query("SELECT status, unidade_id FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    db.execute("UPDATE pessoas SET status = %s, atualizado_em = now() WHERE id = %s", (novo, pessoa_id))
    auditar("bloquear" if bloquear else "desbloquear", "pessoa", pessoa_id, antes={"status": p["status"]}, depois={"status": novo})
    # Bloqueio tem que chegar na catraca (Standalone decide sozinha): tira as regras lá.
    for d in dispositivos.listar(p["unidade_id"]):
        dispositivos.enfileirar(d["id"], "set_rules", pessoa_id)


def desligar(pessoa_id):
    """Desligamento: tira das catracas agora; foto some do banco em até 30 dias (worker/lgpd.py)."""
    p = db.query("SELECT status, unidade_id FROM pessoas WHERE id = %s", (pessoa_id,), one=True)
    db.execute("UPDATE pessoas SET status = 'desligado', desligado_em = now(), atualizado_em = now() WHERE id = %s", (pessoa_id,))
    db.execute("DELETE FROM presenca WHERE pessoa_id = %s", (pessoa_id,))
    auditar("desligar", "pessoa", pessoa_id, antes={"status": p["status"]}, depois={"status": "desligado"})
    dispositivos.enfileirar_remocao(pessoa_id, p["unidade_id"])


def apagar_dados_biometricos(pessoa_id):
    """Exclusão LGPD: foto fora do banco. Mantém o cadastro (nome) pelo histórico de eventos."""
    db.execute("DELETE FROM pessoa_fotos WHERE pessoa_id = %s", (pessoa_id,))
    db.execute("UPDATE pessoas SET foto_atualizada_em = NULL WHERE id = %s", (pessoa_id,))
    auditar("apagar_foto_lgpd", "pessoa", pessoa_id)


def buscar(unidade_id, termo=None, tipo=None, turma_id=None, incluir_desligados=False, limite=200):
    sql = (
        "SELECT p.*, a.id AS aluno_id, a.turma_id, a.matricula, t.nome AS turma_nome, r.id AS responsavel_id, "
        "r.telegram_chat_id, f.cargo, (pf.pessoa_id IS NOT NULL) AS tem_foto "
        "FROM pessoas p LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id "
        "LEFT JOIN responsaveis r ON r.pessoa_id = p.id LEFT JOIN funcionarios f ON f.pessoa_id = p.id "
        "LEFT JOIN pessoa_fotos pf ON pf.pessoa_id = p.id WHERE p.unidade_id = %s"
    )
    params = [unidade_id]
    if not incluir_desligados:
        sql += " AND p.status <> 'desligado'"
    if tipo:
        sql += " AND p.tipo = %s"
        params.append(tipo)
    if turma_id:
        sql += " AND a.turma_id = %s"
        params.append(turma_id)
    if termo:
        sql += " AND (lower(p.nome) LIKE %s OR a.matricula = %s OR r.telefone LIKE %s)"
        params += [f"%{termo.lower()}%", termo, f"%{termo}%"]
    sql += " ORDER BY p.nome LIMIT %s"
    params.append(limite)
    return db.query(sql, params)


def detalhe(pessoa_id):
    return db.query(
        "SELECT p.*, a.id AS aluno_id, a.turma_id, a.matricula, a.saida_autonoma, t.nome AS turma_nome, "
        "r.id AS responsavel_id, r.telefone, r.email, r.telegram_chat_id, r.telegram_ativado_em, "
        "f.id AS funcionario_id, f.cargo, f.escala, (pf.pessoa_id IS NOT NULL) AS tem_foto "
        "FROM pessoas p LEFT JOIN alunos a ON a.pessoa_id = p.id LEFT JOIN turmas t ON t.id = a.turma_id "
        "LEFT JOIN responsaveis r ON r.pessoa_id = p.id LEFT JOIN funcionarios f ON f.pessoa_id = p.id "
        "LEFT JOIN pessoa_fotos pf ON pf.pessoa_id = p.id WHERE p.id = %s",
        (pessoa_id,), one=True,
    )


def responsaveis_do_aluno(aluno_id):
    return db.query(
        "SELECT ar.*, r.pessoa_id, r.telefone, r.telegram_chat_id, p.nome, p.status FROM aluno_responsavel ar "
        "JOIN responsaveis r ON r.id = ar.responsavel_id JOIN pessoas p ON p.id = r.pessoa_id WHERE ar.aluno_id = %s ORDER BY p.nome",
        (aluno_id,),
    )


def alunos_do_responsavel(responsavel_id):
    return db.query(
        "SELECT ar.*, a.pessoa_id, p.nome, t.nome AS turma_nome FROM aluno_responsavel ar JOIN alunos a ON a.id = ar.aluno_id "
        "JOIN pessoas p ON p.id = a.pessoa_id JOIN turmas t ON t.id = a.turma_id WHERE ar.responsavel_id = %s ORDER BY p.nome",
        (responsavel_id,),
    )


def registrar_consentimento(pessoa_id, assinado_por, assinado_em, arquivo_nome=None, arquivo=None, observacao=None):
    db.execute(
        "INSERT INTO consentimentos_lgpd (pessoa_id, assinado_por, assinado_em, arquivo_nome, arquivo, observacao) VALUES (%s,%s,%s,%s,%s,%s)",
        (pessoa_id, assinado_por, assinado_em, arquivo_nome, psycopg_bytes(arquivo) if arquivo else None, observacao),
    )
    auditar("consentimento", "pessoa", pessoa_id, depois={"assinado_por": assinado_por, "assinado_em": str(assinado_em)})


def tem_consentimento(pessoa_id):
    return db.query("SELECT 1 FROM consentimentos_lgpd WHERE pessoa_id = %s LIMIT 1", (pessoa_id,), one=True) is not None


# ---------------------------------------------------------------- pronto para a catraca?

SITUACOES = {
    "falta_foto": ("falta", "Falta foto"),
    "falta_responsavel": ("falta", "Falta responsável"),
    "erro_catraca": ("falta", "Erro na catraca — refazer foto"),
    "responsavel_sem_foto": ("atencao", "Responsável sem foto"),
    "enviando": ("atencao", "Enviando para a catraca"),
    "pronto": ("pronto", "Pronto"),
}


def _situacao(r):
    """Ordem = o que trava primeiro. Responsável sem foto conta porque, na saída "só com
    responsável", é a face dele no leitor de fora que libera a criança."""
    precisa = (not r["saida_autonoma"]) if r["saida_autonoma"] is not None else bool(r["exige_responsavel_saida"])
    if not r["tem_foto"]:
        chave = "falta_foto"
    elif precisa and not r["n_resp"]:
        chave = "falta_responsavel"
    elif r["disp_erro"]:
        chave = "erro_catraca"
    elif precisa and not r["n_resp_foto"]:
        chave = "responsavel_sem_foto"
    elif r["disp_pendente"]:
        chave = "enviando"
    else:
        chave = "pronto"
    classe, texto = SITUACOES[chave]
    if chave == "pronto" and not r["n_disp"]:
        texto = "Pronto (falta instalar a catraca)"
    return {"chave": chave, "classe": classe, "texto": texto, "erro": r["msg_erro"], "precisa_responsavel": precisa,
            "n_resp": r["n_resp"], "n_resp_telegram": r["n_resp_telegram"]}


def situacao_alunos(unidade_id):
    """{pessoa_id: situação} de todos os alunos da escola, numa consulta só."""
    rows = db.query(
        "SELECT a.pessoa_id, a.saida_autonoma, t.exige_responsavel_saida, (pf.pessoa_id IS NOT NULL) AS tem_foto, "
        "coalesce(rs.n, 0) AS n_resp, coalesce(rs.n_foto, 0) AS n_resp_foto, coalesce(rs.n_tg, 0) AS n_resp_telegram, "
        "coalesce(ds.n, 0) AS n_disp, coalesce(ds.erro, 0) AS disp_erro, coalesce(ds.pendente, 0) AS disp_pendente, ds.msg_erro "
        "FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id JOIN turmas t ON t.id = a.turma_id "
        "LEFT JOIN pessoa_fotos pf ON pf.pessoa_id = p.id "
        "LEFT JOIN LATERAL (SELECT count(*) AS n, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM pessoa_fotos f WHERE f.pessoa_id = rp.id)) AS n_foto, "
        "  count(*) FILTER (WHERE r.telegram_chat_id IS NOT NULL) AS n_tg FROM aluno_responsavel ar "
        "  JOIN responsaveis r ON r.id = ar.responsavel_id JOIN pessoas rp ON rp.id = r.pessoa_id "
        "  WHERE ar.aluno_id = a.id AND rp.status = 'ativo') rs ON true "
        "LEFT JOIN LATERAL (SELECT count(*) AS n, count(*) FILTER (WHERE dp.foto_sync_status = 'erro') AS erro, "
        "  count(*) FILTER (WHERE dp.foto_sync_status = 'pendente') AS pendente, max(dp.erro) AS msg_erro "
        "  FROM dispositivo_pessoa dp JOIN dispositivos d ON d.id = dp.dispositivo_id WHERE dp.pessoa_id = p.id AND d.ativo) ds ON true "
        "WHERE p.unidade_id = %s", (unidade_id,),
    )
    return {r["pessoa_id"]: _situacao(r) for r in rows}
