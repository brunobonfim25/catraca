-- Consentimento dos pais pedido pelo próprio bot, na ativação do Telegram (27/10/2026).
-- O Bruno tirou o termo em papel do contrato: quem toca no link de ativação responde ali
-- se autoriza a foto (biometria) e os avisos, e a resposta fica guardada como prova.

-- escopo NULL = registro antigo (termo em papel ou declarado na importação) = biometria.
-- Uma recusa é uma linha com aceito = false: a mais nova do mesmo responsável vale.
ALTER TABLE consentimentos_lgpd
    ADD COLUMN escopo text CHECK (escopo IN ('biometria','avisos')),
    ADD COLUMN aceito boolean NOT NULL DEFAULT true,
    ADD COLUMN canal text,                -- papel | telegram | importacao
    ADD COLUMN responsavel_id int REFERENCES responsaveis(id) ON DELETE SET NULL,
    ADD COLUMN texto text;                -- o texto exato que a pessoa viu ao responder

-- Chat que abriu o link e ainda não respondeu à autorização. A resposta (botão) só vale
-- para o responsável marcado aqui — o chat vem do Telegram, não de dado que o usuário digita.
ALTER TABLE responsaveis ADD COLUMN telegram_chat_pendente bigint;
CREATE INDEX responsaveis_chat_pendente_idx ON responsaveis (telegram_chat_pendente) WHERE telegram_chat_pendente IS NOT NULL;
