-- Revisão de 27/10/2026, onda 2: sincronização nuvem ↔ agente que não perde nem mistura linha.
--
-- 1) Cada linha do outbox passa a saber de qual(is) escola(s) ela é pela PRÓPRIA linha
--    (pessoa → unidade, dispositivo → unidade…), não só pelo parâmetro da sessão. Antes, uma
--    escrita feita sem a marca (worker, rota sem escola na sessão, admin da rede) saía com
--    unidade NULL e nunca descia pro agente; mudança de empresa/superadmin descia pra nenhuma.
--    Linha que vale pra várias escolas (empresa, usuário da rede) vira uma linha por escola.
-- 2) `xid`: a transação que escreveu. O agente lia "id > cursor"; um id menor que só fica
--    visível depois (transação mais lenta) era pulado pra sempre. Agora a nuvem só entrega o
--    que veio de transações já terminadas (xid < xmin do snapshot) e o cursor é (xid, id).
-- 3) Quarentena: um item que não aplica (FK, tipo) ia derrubar o lote inteiro e travar a
--    sincronização da escola pra sempre. Agora ele fica guardado com o erro e é tentado de novo.

ALTER TABLE espelho_outbox ADD COLUMN xid xid8 NOT NULL DEFAULT pg_current_xact_id();
CREATE INDEX espelho_outbox_cursor_idx ON espelho_outbox (unidade_id, xid, id);

ALTER TABLE espelho_estado
    ADD COLUMN cursor_xid text NOT NULL DEFAULT '0',
    ADD COLUMN carga_em timestamptz,
    ADD COLUMN agente_id text;
-- Agente já instalado com cursor > 0 já fez a carga inicial
UPDATE espelho_estado SET carga_em = now() WHERE cursor_nuvem > 0;

CREATE TABLE espelho_quarentena (
    id          bigserial PRIMARY KEY,
    lado        text NOT NULL,           -- quem tentou aplicar: 'nuvem' ou 'agente'
    unidade_id  int,
    tabela      text,
    operacao    text,
    chave       jsonb,
    dados       jsonb,
    erro        text,
    tentativas  int NOT NULL DEFAULT 1,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    tentado_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX espelho_quarentena_idx ON espelho_quarentena (lado, unidade_id, id);

-- Escola(s) a que uma linha pertence. NULL/vazio = não dá pra saber (cai no parâmetro da sessão).
CREATE OR REPLACE FUNCTION espelho_unidades_da_linha(tabela text, linha jsonb) RETURNS int[] AS $$
DECLARE
    r int[];
BEGIN
    IF tabela = 'unidades' THEN
        RETURN ARRAY[(linha->>'id')::int];
    ELSIF tabela = 'empresas' THEN
        SELECT array_agg(id) INTO r FROM unidades WHERE empresa_id = (linha->>'id')::int;
        RETURN r;
    ELSIF tabela = 'usuarios_admin' THEN
        -- o agente conhece todos os usuários da rede (eventos.autorizado_por aponta pra eles)
        IF linha->>'empresa_id' IS NOT NULL THEN
            SELECT array_agg(id) INTO r FROM unidades WHERE empresa_id = (linha->>'empresa_id')::int;
        ELSE
            SELECT array_agg(id) INTO r FROM unidades;  -- superadmin Nexosix
        END IF;
        RETURN r;
    ELSIF linha->>'unidade_id' IS NOT NULL THEN
        RETURN ARRAY[(linha->>'unidade_id')::int];
    ELSIF tabela IN ('pessoa_fotos', 'alunos', 'responsaveis', 'funcionarios', 'consentimentos_lgpd') THEN
        SELECT array_agg(unidade_id) INTO r FROM pessoas WHERE id = (linha->>'pessoa_id')::int;
        RETURN r;
    ELSIF tabela = 'aluno_responsavel' THEN
        SELECT array_agg(p.unidade_id) INTO r FROM alunos a JOIN pessoas p ON p.id = a.pessoa_id WHERE a.id = (linha->>'aluno_id')::int;
        RETURN r;
    ELSIF tabela IN ('dispositivo_leitores', 'dispositivo_pessoa', 'fila_sync') THEN
        SELECT array_agg(unidade_id) INTO r FROM dispositivos WHERE id = (linha->>'dispositivo_id')::int;
        RETURN r;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION espelho_registrar() RETURNS trigger AS $$
DECLARE
    pks   text[] := TG_ARGV;
    linha jsonb;
    chave jsonb;
    unis  int[];
    uni   int;
    sessao int := NULLIF(current_setting('nexosix.unidade', true), '')::int;
BEGIN
    IF current_setting('nexosix.outbox', true) IS DISTINCT FROM 'on' THEN
        RETURN NULL;
    END IF;
    linha := to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END);
    SELECT jsonb_object_agg(k, linha->k) INTO chave FROM unnest(pks) AS k;
    unis := espelho_unidades_da_linha(TG_TABLE_NAME, linha);
    IF unis IS NULL OR cardinality(unis) = 0 THEN
        unis := ARRAY[sessao];  -- linha apagada em cascata (o pai já sumiu): vale a sessão
    END IF;
    IF TG_TABLE_NAME = 'usuarios_admin' THEN
        linha := linha - 'senha_hash';  -- hash de senha não sai da nuvem (PC da escola pode ser roubado)
    END IF;
    FOREACH uni IN ARRAY unis LOOP
        IF TG_OP = 'DELETE' THEN
            INSERT INTO espelho_outbox (tabela, operacao, chave, unidade_id) VALUES (TG_TABLE_NAME, 'delete', chave, uni);
        ELSE
            INSERT INTO espelho_outbox (tabela, operacao, chave, dados, unidade_id) VALUES (TG_TABLE_NAME, 'upsert', chave, linha, uni);
        END IF;
    END LOOP;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;

-- Relógio do PC da escola comparado ao da nuvem (a cada ping). Acertar a hora da catraca com
-- um PC atrasado quebraria a janela de entrada e a ordem dos eventos.
ALTER TABLE espelho_estado ADD COLUMN desvio_relogio_seg int;

-- Token da catraca por escola. Antes era um só (MONITOR_TOKEN) pra todas: quem tinha o .env de
-- uma escola podia mandar evento "da catraca" de qualquer outra para a nuvem.
UPDATE unidades SET config = config || jsonb_build_object('monitor_token', replace(gen_random_uuid()::text, '-', ''))
 WHERE NOT config ? 'monitor_token';
