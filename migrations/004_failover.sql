-- Failover: se o servidor local some, a nuvem assume a unidade (reconfigura a catraca pela
-- VPN, libera escrita no painel dela) e registra o que mudou por unidade no outbox; quando o
-- local volta, puxa esse "reverso", aplica, e a nuvem devolve a catraca pra ele.

ALTER TABLE espelho_outbox ADD COLUMN unidade_id int;
CREATE INDEX espelho_outbox_unidade_idx ON espelho_outbox (unidade_id, id);

CREATE OR REPLACE FUNCTION espelho_registrar() RETURNS trigger AS $$
DECLARE
    pks   text[] := TG_ARGV;
    linha jsonb;
    chave jsonb;
    uni   int := NULLIF(current_setting('nexosix.unidade', true), '')::int;
BEGIN
    IF current_setting('nexosix.outbox', true) IS DISTINCT FROM 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        linha := to_jsonb(OLD);
        SELECT jsonb_object_agg(k, linha->k) INTO chave FROM unnest(pks) AS k;
        INSERT INTO espelho_outbox (tabela, operacao, chave, unidade_id) VALUES (TG_TABLE_NAME, 'delete', chave, uni);
    ELSE
        linha := to_jsonb(NEW);
        SELECT jsonb_object_agg(k, linha->k) INTO chave FROM unnest(pks) AS k;
        INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES (TG_TABLE_NAME, 'upsert', chave, linha, uni);
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;

-- Estado do servidor LOCAL (linha única, nunca espelhada):
--   normal      → opera e envia o espelho
--   recuperando → a nuvem está (ou esteve) no comando; local puxa o reverso antes de aceitar escrita
CREATE TABLE espelho_estado (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    estado      text NOT NULL DEFAULT 'normal' CHECK (estado IN ('normal','recuperando')),
    cursor_nuvem bigint NOT NULL DEFAULT 0,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);
INSERT INTO espelho_estado DEFAULT VALUES;
