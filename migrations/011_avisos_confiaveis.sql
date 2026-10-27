-- Revisão de 27/10/2026, onda 4: ativação do Telegram e avisos aos pais.

-- A5: link de ativação com validade (antes valia pra sempre, encaminhado ou não)
ALTER TABLE responsaveis ADD COLUMN telegram_token_em timestamptz;
UPDATE responsaveis SET telegram_token_em = now() WHERE telegram_token IS NOT NULL;

-- A8: quem bloqueou o bot continua "ativo" no painel; agora fica marcado
ALTER TABLE responsaveis ADD COLUMN telegram_bloqueado_em timestamptz;

-- A9/A18: a fila é lida por hora; linhas antigas são limpas
CREATE INDEX IF NOT EXISTS fila_notificacao_evento_idx ON fila_notificacao (evento_id);
CREATE INDEX IF NOT EXISTS fila_sync_concluido_idx ON fila_sync (concluido_em) WHERE status = 'concluido';
