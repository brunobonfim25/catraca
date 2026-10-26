-- Base multi-tenant: empresa → unidades → tudo o mais carrega unidade_id.
-- Timestamps sempre timestamptz (UTC no banco; a exibição converte pro fuso da unidade).

CREATE TABLE empresas (
    id          serial PRIMARY KEY,
    nome        text NOT NULL,
    criado_em   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE unidades (
    id          serial PRIMARY KEY,
    empresa_id  int NOT NULL REFERENCES empresas(id) ON DELETE RESTRICT,
    nome        text NOT NULL,
    endereco    text,
    fuso        text NOT NULL DEFAULT 'America/Sao_Paulo',
    -- parâmetros operacionais por escola (minutos de espera do responsável etc.)
    config      jsonb NOT NULL DEFAULT '{}'::jsonb,
    ativo       boolean NOT NULL DEFAULT true,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (empresa_id, nome)
);
CREATE INDEX unidades_empresa_idx ON unidades (empresa_id);

CREATE TABLE usuarios_admin (
    id          serial PRIMARY KEY,
    empresa_id  int REFERENCES empresas(id) ON DELETE RESTRICT,
    -- superadmin (Nexosix) tem empresa e unidade nulas; admin da rede tem só empresa;
    -- recepção/coordenação ficam presas a uma unidade.
    unidade_id  int REFERENCES unidades(id) ON DELETE RESTRICT,
    nome        text NOT NULL,
    email       text NOT NULL UNIQUE,
    senha_hash  text NOT NULL,
    perfil      text NOT NULL CHECK (perfil IN ('superadmin','admin_unidade','recepcao','coordenacao','auditor')),
    ativo       boolean NOT NULL DEFAULT true,
    ultimo_login timestamptz,
    criado_em   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE turmas (
    id          serial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    nome        text NOT NULL,
    turno       text NOT NULL CHECK (turno IN ('manha','tarde','integral')),
    -- regra de saída: exige responsável identificado no leitor de fora
    exige_responsavel_saida boolean NOT NULL DEFAULT true,
    tolerancia_entrada_min  int NOT NULL DEFAULT 15,
    entrada_inicio time NOT NULL DEFAULT '06:30',
    entrada_fim    time NOT NULL DEFAULT '08:00',
    saida_inicio   time NOT NULL DEFAULT '16:30',
    saida_fim      time NOT NULL DEFAULT '18:30',
    ativo       boolean NOT NULL DEFAULT true,
    UNIQUE (unidade_id, nome)
);
CREATE INDEX turmas_unidade_idx ON turmas (unidade_id);

CREATE TABLE pessoas (
    id          serial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    tipo        text NOT NULL CHECK (tipo IN ('aluno','responsavel','funcionario','visitante')),
    nome        text NOT NULL,
    documento   text,
    status      text NOT NULL DEFAULT 'ativo' CHECK (status IN ('ativo','bloqueado','desligado')),
    foto_atualizada_em timestamptz,
    -- LGPD: desligado_em dispara a exclusão de foto/template em até 30 dias
    desligado_em timestamptz,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    atualizado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pessoas_unidade_idx ON pessoas (unidade_id, tipo);
CREATE INDEX pessoas_nome_idx ON pessoas (unidade_id, lower(nome));

-- Foto fica no banco (Railway não tem disco persistente no web). Tabela separada pra não
-- carregar bytea em toda listagem.
CREATE TABLE pessoa_fotos (
    pessoa_id   int PRIMARY KEY REFERENCES pessoas(id) ON DELETE CASCADE,
    jpeg        bytea NOT NULL,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alunos (
    id          serial PRIMARY KEY,
    pessoa_id   int NOT NULL UNIQUE REFERENCES pessoas(id) ON DELETE RESTRICT,
    turma_id    int NOT NULL REFERENCES turmas(id) ON DELETE RESTRICT,
    matricula   text,
    -- NULL = segue a turma; true/false = autorização individual dos pais
    saida_autonoma boolean
);
CREATE INDEX alunos_turma_idx ON alunos (turma_id);

CREATE TABLE responsaveis (
    id          serial PRIMARY KEY,
    pessoa_id   int NOT NULL UNIQUE REFERENCES pessoas(id) ON DELETE RESTRICT,
    telefone    text,
    telegram_chat_id bigint,
    telegram_token   text UNIQUE,
    telegram_ativado_em timestamptz
);

CREATE TABLE aluno_responsavel (
    aluno_id        int NOT NULL REFERENCES alunos(id) ON DELETE CASCADE,
    responsavel_id  int NOT NULL REFERENCES responsaveis(id) ON DELETE CASCADE,
    parentesco      text,
    regra           text NOT NULL DEFAULT 'sempre' CHECK (regra IN ('sempre','dias','aviso')),
    -- 0=domingo … 6=sábado (padrão do PostgreSQL extract(dow))
    dias_semana     int[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (aluno_id, responsavel_id)
);

CREATE TABLE funcionarios (
    id          serial PRIMARY KEY,
    pessoa_id   int NOT NULL UNIQUE REFERENCES pessoas(id) ON DELETE RESTRICT,
    cargo       text,
    -- {"dias":[1,2,3,4,5],"inicio":"07:00","fim":"18:00"}
    escala      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE dispositivos (
    id          serial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    nome        text NOT NULL,
    fabricante  text NOT NULL DEFAULT 'controlid',
    modelo      text,
    serial      text,
    ip          text,
    porta       int NOT NULL DEFAULT 80,
    usuario_api text NOT NULL DEFAULT 'admin',
    -- senha da catraca: segredo, nunca aparece no painel
    senha_api   text,
    firmware    text,
    funcao      text NOT NULL DEFAULT 'catraca' CHECK (funcao IN ('catraca','leitora')),
    sentido     text NOT NULL DEFAULT 'bidirecional' CHECK (sentido IN ('entrada','saida','bidirecional')),
    modo        text NOT NULL DEFAULT 'standalone' CHECK (modo IN ('standalone','pro')),
    -- device_id que a própria Control iD informa (system_information / notificações)
    device_id_fabricante bigint,
    status      text NOT NULL DEFAULT 'desconhecido' CHECK (status IN ('online','offline','desconhecido')),
    ultimo_contato timestamptz,
    -- último access_logs.id coletado; coleta incremental parte daqui
    ultimo_log_id bigint NOT NULL DEFAULT 0,
    config      jsonb NOT NULL DEFAULT '{}'::jsonb,
    ativo       boolean NOT NULL DEFAULT true,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (unidade_id, nome)
);
CREATE INDEX dispositivos_unidade_idx ON dispositivos (unidade_id);
CREATE INDEX dispositivos_device_id_idx ON dispositivos (device_id_fabricante);

-- Como saber se um acesso foi entrada ou saída: no idBlock Next o evento online traz
-- component_id (0MS1 primário / 0MS2 secundário) e o access_log traz portal_id.
-- O mapeamento é por dispositivo e PRECISA ser validado no hardware.
CREATE TABLE dispositivo_leitores (
    id          serial PRIMARY KEY,
    dispositivo_id int NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    component_id text,
    portal_id   int,
    sentido     text NOT NULL CHECK (sentido IN ('entrada','saida')),
    descricao   text
);
CREATE INDEX dispositivo_leitores_idx ON dispositivo_leitores (dispositivo_id);

CREATE TABLE dispositivo_pessoa (
    dispositivo_id int NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    pessoa_id   int NOT NULL REFERENCES pessoas(id) ON DELETE CASCADE,
    user_id_no_dispositivo bigint,
    foto_sync_status   text NOT NULL DEFAULT 'pendente' CHECK (foto_sync_status IN ('pendente','sincronizado','erro','nao_aplicavel')),
    regras_sync_status text NOT NULL DEFAULT 'pendente' CHECK (regras_sync_status IN ('pendente','sincronizado','erro','nao_aplicavel')),
    erro        text,
    atualizado_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dispositivo_id, pessoa_id)
);
CREATE INDEX dispositivo_pessoa_user_idx ON dispositivo_pessoa (dispositivo_id, user_id_no_dispositivo);

CREATE TABLE fila_sync (
    id          bigserial PRIMARY KEY,
    dispositivo_id int NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    pessoa_id   int REFERENCES pessoas(id) ON DELETE CASCADE,
    tipo        text NOT NULL CHECK (tipo IN ('sync_user','set_photo','remove_user','set_rules','set_clock')),
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    tentativas  int NOT NULL DEFAULT 0,
    proximo_em  timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente','processando','concluido','erro')),
    erro        text,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    concluido_em timestamptz
);
CREATE INDEX fila_sync_pendente_idx ON fila_sync (status, proximo_em) WHERE status = 'pendente';

CREATE TABLE eventos (
    id          bigserial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    dispositivo_id int REFERENCES dispositivos(id) ON DELETE SET NULL,
    pessoa_id   int REFERENCES pessoas(id) ON DELETE SET NULL,
    tipo        text NOT NULL,
    sentido     text CHECK (sentido IN ('entrada','saida')),
    ocorrido_em timestamptz NOT NULL,
    origem      text NOT NULL DEFAULT 'dispositivo' CHECK (origem IN ('dispositivo','manual','sistema')),
    log_id_dispositivo bigint,
    -- código "event" bruto da Control iD (7 liberado, 6 negado, 13 desistência…)
    evento_dispositivo int,
    -- pessoa relacionada (ex.: responsável que retirou o aluno)
    correlacao_id int REFERENCES pessoas(id) ON DELETE SET NULL,
    autorizado_por int REFERENCES usuarios_admin(id) ON DELETE SET NULL,
    justificativa text,
    detalhes    jsonb NOT NULL DEFAULT '{}'::jsonb,
    criado_em   timestamptz NOT NULL DEFAULT now()
);
-- Dedupe: o mesmo access_log pode chegar pelo Monitor e depois pela coleta de pendentes.
CREATE UNIQUE INDEX eventos_dedupe_idx ON eventos (dispositivo_id, log_id_dispositivo) WHERE log_id_dispositivo IS NOT NULL;
CREATE INDEX eventos_unidade_tempo_idx ON eventos (unidade_id, ocorrido_em DESC);
CREATE INDEX eventos_pessoa_idx ON eventos (pessoa_id, ocorrido_em DESC);

CREATE TABLE responsavel_em_espera (
    id          bigserial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    responsavel_id int NOT NULL REFERENCES responsaveis(id) ON DELETE CASCADE,
    identificado_em timestamptz NOT NULL DEFAULT now(),
    expira_em   timestamptz NOT NULL,
    dispositivo_id int REFERENCES dispositivos(id) ON DELETE SET NULL,
    -- preenchido quando a espera foi consumida por uma saída
    consumido_em timestamptz,
    aluno_id    int REFERENCES alunos(id) ON DELETE SET NULL
);
CREATE INDEX espera_ativa_idx ON responsavel_em_espera (unidade_id, expira_em) WHERE consumido_em IS NULL;

-- Materializada por evento: quem está dentro agora.
CREATE TABLE presenca (
    pessoa_id   int PRIMARY KEY REFERENCES pessoas(id) ON DELETE CASCADE,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    dentro      boolean NOT NULL DEFAULT false,
    desde       timestamptz NOT NULL DEFAULT now(),
    ultimo_evento_id bigint REFERENCES eventos(id) ON DELETE SET NULL
);
CREATE INDEX presenca_unidade_idx ON presenca (unidade_id, dentro);

CREATE TABLE fila_notificacao (
    id          bigserial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    responsavel_id int NOT NULL REFERENCES responsaveis(id) ON DELETE CASCADE,
    evento_id   bigint REFERENCES eventos(id) ON DELETE SET NULL,
    texto       text NOT NULL,
    status      text NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente','enviado','erro','sem_chat')),
    tentativas  int NOT NULL DEFAULT 0,
    proximo_em  timestamptz NOT NULL DEFAULT now(),
    erro        text,
    enviado_em  timestamptz,
    criado_em   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fila_notificacao_pendente_idx ON fila_notificacao (status, proximo_em) WHERE status = 'pendente';

CREATE TABLE auditoria (
    id          bigserial PRIMARY KEY,
    unidade_id  int REFERENCES unidades(id) ON DELETE SET NULL,
    usuario_id  int REFERENCES usuarios_admin(id) ON DELETE SET NULL,
    acao        text NOT NULL,
    entidade    text NOT NULL,
    entidade_id text,
    antes       jsonb,
    depois      jsonb,
    criado_em   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auditoria_unidade_idx ON auditoria (unidade_id, criado_em DESC);

CREATE TABLE consentimentos_lgpd (
    id          serial PRIMARY KEY,
    pessoa_id   int NOT NULL REFERENCES pessoas(id) ON DELETE CASCADE,
    assinado_por text NOT NULL,
    assinado_em date NOT NULL,
    arquivo_nome text,
    arquivo     bytea,
    observacao  text,
    criado_em   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consentimentos_pessoa_idx ON consentimentos_lgpd (pessoa_id);
