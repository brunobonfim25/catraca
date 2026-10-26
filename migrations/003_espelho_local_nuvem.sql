-- Modelo 3 (decisão de 26/10/2026): o servidor LOCAL da escola é quem decide e grava;
-- a NUVEM recebe um espelho das linhas (backup vivo + painel consolidado da rede).
--
-- Mecanismo: outbox. Todo INSERT/UPDATE/DELETE em tabela de domínio vira uma linha em
-- espelho_outbox (só quando a sessão tem nexosix.outbox = 'on', ou seja, no servidor local).
-- O worker local empurra os lotes por HTTPS pra nuvem, que aplica upsert/delete.

ALTER TABLE dispositivos DROP CONSTRAINT dispositivos_status_check;
ALTER TABLE dispositivos ADD CONSTRAINT dispositivos_status_check
    CHECK (status IN ('online','offline','desconhecido','contingencia'));

CREATE TABLE espelho_outbox (
    id          bigserial PRIMARY KEY,
    tabela      text NOT NULL,
    operacao    text NOT NULL CHECK (operacao IN ('upsert','delete')),
    chave       jsonb NOT NULL,
    dados       jsonb,
    criado_em   timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION espelho_registrar() RETURNS trigger AS $$
DECLARE
    pks   text[] := TG_ARGV;
    linha jsonb;
    chave jsonb;
BEGIN
    IF current_setting('nexosix.outbox', true) IS DISTINCT FROM 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        linha := to_jsonb(OLD);
        SELECT jsonb_object_agg(k, linha->k) INTO chave FROM unnest(pks) AS k;
        INSERT INTO espelho_outbox (tabela, operacao, chave) VALUES (TG_TABLE_NAME, 'delete', chave);
    ELSE
        linha := to_jsonb(NEW);
        SELECT jsonb_object_agg(k, linha->k) INTO chave FROM unnest(pks) AS k;
        INSERT INTO espelho_outbox (tabela, operacao, chave, dados) VALUES (TG_TABLE_NAME, 'upsert', chave, linha);
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;

-- fila_sync fica de fora: é operacional e só faz sentido perto da catraca.
CREATE TRIGGER espelho_empresas            AFTER INSERT OR UPDATE OR DELETE ON empresas            FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_unidades            AFTER INSERT OR UPDATE OR DELETE ON unidades            FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_usuarios_admin      AFTER INSERT OR UPDATE OR DELETE ON usuarios_admin      FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_turmas              AFTER INSERT OR UPDATE OR DELETE ON turmas              FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_pessoas             AFTER INSERT OR UPDATE OR DELETE ON pessoas             FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_pessoa_fotos        AFTER INSERT OR UPDATE OR DELETE ON pessoa_fotos        FOR EACH ROW EXECUTE FUNCTION espelho_registrar('pessoa_id');
CREATE TRIGGER espelho_alunos              AFTER INSERT OR UPDATE OR DELETE ON alunos              FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_responsaveis        AFTER INSERT OR UPDATE OR DELETE ON responsaveis        FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_aluno_responsavel   AFTER INSERT OR UPDATE OR DELETE ON aluno_responsavel   FOR EACH ROW EXECUTE FUNCTION espelho_registrar('aluno_id','responsavel_id');
CREATE TRIGGER espelho_funcionarios        AFTER INSERT OR UPDATE OR DELETE ON funcionarios        FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_dispositivos        AFTER INSERT OR UPDATE OR DELETE ON dispositivos        FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_dispositivo_leitores AFTER INSERT OR UPDATE OR DELETE ON dispositivo_leitores FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_dispositivo_pessoa  AFTER INSERT OR UPDATE OR DELETE ON dispositivo_pessoa  FOR EACH ROW EXECUTE FUNCTION espelho_registrar('dispositivo_id','pessoa_id');
CREATE TRIGGER espelho_eventos             AFTER INSERT OR UPDATE OR DELETE ON eventos             FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_responsavel_em_espera AFTER INSERT OR UPDATE OR DELETE ON responsavel_em_espera FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_presenca            AFTER INSERT OR UPDATE OR DELETE ON presenca            FOR EACH ROW EXECUTE FUNCTION espelho_registrar('pessoa_id');
CREATE TRIGGER espelho_fila_notificacao    AFTER INSERT OR UPDATE OR DELETE ON fila_notificacao    FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_auditoria           AFTER INSERT OR UPDATE OR DELETE ON auditoria           FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_consentimentos_lgpd AFTER INSERT OR UPDATE OR DELETE ON consentimentos_lgpd FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
CREATE TRIGGER espelho_avisos_retirada     AFTER INSERT OR UPDATE OR DELETE ON avisos_retirada     FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');

-- Na nuvem um lote chega na ordem em que aconteceu, mas pode trazer filho antes do pai
-- dentro da mesma transação; FKs adiáveis deixam checar só no commit do lote.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT conname, conrelid::regclass AS tabela FROM pg_constraint
             WHERE contype = 'f' AND connamespace = 'public'::regnamespace LOOP
        EXECUTE format('ALTER TABLE %s ALTER CONSTRAINT %I DEFERRABLE INITIALLY IMMEDIATE', r.tabela, r.conname);
    END LOOP;
END $$;
