-- Fase 2 do painel (27/10/2026): importar a planilha da escola e convidar os responsáveis
-- em massa. Sugestão do integrador Control iD: com a planilha importada a secretaria só
-- tira foto; o convite por e-mail chega em todo mundo de uma vez.

-- E-mail do responsável: terceiro canal de convite (além de WhatsApp e link da escola).
-- Viaja pro agente pelo trigger que já existe em responsaveis (to_jsonb pega a coluna nova).
ALTER TABLE responsaveis ADD COLUMN email text;

-- As tabelas abaixo são só da nuvem: sem trigger de espelho, o agente não precisa delas.

-- Planilha enviada: guardada crua para a secretaria conferir o mapeamento antes de gravar.
CREATE TABLE importacoes (
    id            bigserial PRIMARY KEY,
    unidade_id    int NOT NULL REFERENCES unidades(id) ON DELETE CASCADE,
    usuario_id    int REFERENCES usuarios_admin(id) ON DELETE SET NULL,
    arquivo_nome  text,
    cabecalho     jsonb NOT NULL,
    linhas        jsonb NOT NULL,
    linha_inicial int NOT NULL DEFAULT 2,    -- nº da linha do Excel da 1ª linha de dados (pra apontar erro)
    mapeamento    jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'rascunho' CHECK (status IN ('rascunho','importada')),
    resultado     jsonb,
    criado_em     timestamptz NOT NULL DEFAULT now(),
    importado_em  timestamptz
);
CREATE INDEX importacoes_unidade_idx ON importacoes (unidade_id, criado_em DESC);

-- Convite para ativar o Telegram. WhatsApp é registro (a secretaria envia do celular dela);
-- e-mail é fila do worker da nuvem (corpo já montado na hora de enfileirar).
CREATE TABLE convites (
    id             bigserial PRIMARY KEY,
    unidade_id     int NOT NULL REFERENCES unidades(id) ON DELETE CASCADE,
    responsavel_id int NOT NULL REFERENCES responsaveis(id) ON DELETE CASCADE,
    canal          text NOT NULL CHECK (canal IN ('whatsapp','email')),
    destino        text,
    assunto        text,
    corpo_html     text,
    corpo_texto    text,
    status         text NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente','enviado','erro')),
    tentativas     int NOT NULL DEFAULT 0,
    proximo_em     timestamptz NOT NULL DEFAULT now(),
    erro           text,
    enviado_em     timestamptz,
    criado_por     int REFERENCES usuarios_admin(id) ON DELETE SET NULL,
    criado_em      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX convites_pendentes_idx ON convites (status, proximo_em) WHERE status = 'pendente';
CREATE INDEX convites_responsavel_idx ON convites (responsavel_id, criado_em DESC);

-- Logotipo da escola no e-mail de convite (servido por URL pública: cliente de e-mail não
-- mostra imagem embutida em data: URI).
CREATE TABLE unidade_logos (
    unidade_id    int PRIMARY KEY REFERENCES unidades(id) ON DELETE CASCADE,
    conteudo      bytea NOT NULL,
    tipo          text NOT NULL,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);
