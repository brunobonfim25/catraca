"""LGPD: foto (dado biométrico) some do banco até 30 dias após o desligamento.
A remoção da catraca é imediata (fila remove_user no desligamento)."""
import logging

from app import db
from app.auditoria import auditar

log = logging.getLogger(__name__)

DIAS_RETENCAO = 30


def purgar_desligados():
    rows = db.query(
        "SELECT p.id, p.unidade_id FROM pessoas p JOIN pessoa_fotos f ON f.pessoa_id = p.id "
        "WHERE p.status = 'desligado' AND p.desligado_em < now() - make_interval(days => %s)",
        (DIAS_RETENCAO,),
    )
    for r in rows:
        db.contexto.unidade_id = r["unidade_id"]  # a exclusão desce pro agente da escola certa
        try:
            db.execute("DELETE FROM pessoa_fotos WHERE pessoa_id = %s", (r["id"],))
            db.execute("UPDATE pessoas SET foto_atualizada_em = NULL WHERE id = %s", (r["id"],))
            auditar("purga_lgpd", "pessoa", r["id"], depois={"motivo": f"{DIAS_RETENCAO} dias após desligamento"})
        finally:
            db.contexto.unidade_id = None
    # Esperas de responsável vencidas há mais de um dia não servem pra nada
    db.execute("DELETE FROM responsavel_em_espera WHERE expira_em < now() - interval '1 day'")
    return len(rows)
