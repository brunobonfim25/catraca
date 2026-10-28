-- Monitoramento em tempo real (28/10/2026): cada problema vira um incidente com início, fim,
-- quem assumiu e quantas vezes foi avisado. Antes era só "começou/acabou" no Telegram, sem
-- saber se alguém viu, e sem histórico para medir o tempo de resposta.
CREATE TABLE incidentes (
    id              bigserial PRIMARY KEY,
    chave           text NOT NULL,                 -- "agente:1", "catraca:3", "deploy:worker"…
    gravidade       text NOT NULL DEFAULT 'critico' CHECK (gravidade IN ('critico','atencao')),
    texto           text NOT NULL,
    unidade_id      int REFERENCES unidades(id) ON DELETE SET NULL,
    inicio          timestamptz NOT NULL DEFAULT now(),
    fim             timestamptz,
    reconhecido_por text,
    reconhecido_em  timestamptz,
    ultimo_aviso    timestamptz,
    avisos          int NOT NULL DEFAULT 0
);
-- um incidente aberto por problema
CREATE UNIQUE INDEX incidentes_aberto_idx ON incidentes (chave) WHERE fim IS NULL;
CREATE INDEX incidentes_inicio_idx ON incidentes (inicio DESC);

-- o mecanismo anterior (sistema_status 'alerta:*') é substituído pelos incidentes
DELETE FROM sistema_status WHERE chave LIKE 'alerta:%';
