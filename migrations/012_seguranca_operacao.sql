-- Revisão de 27/10/2026, onda 5: segurança restante e operação.

-- S14: tentativas de login erradas por e-mail e por IP (bloqueio progressivo)
CREATE TABLE login_falhas (
    chave         text PRIMARY KEY,          -- "email:<e-mail>" ou "ip:<endereço>"
    falhas        int NOT NULL DEFAULT 0,
    bloqueado_ate timestamptz,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

-- O2: sinal de vida dos processos e alertas já enviados (para não repetir)
CREATE TABLE sistema_status (
    chave         text PRIMARY KEY,          -- "worker:nuvem", "alerta:agente:1"…
    valor         jsonb NOT NULL DEFAULT '{}'::jsonb,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);
