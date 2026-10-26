-- Regra "aviso" do vínculo aluno↔responsável: esse responsável só pode retirar quando a
-- secretaria registrou um aviso prévio pro dia. Também serve pra "hoje a tia busca".
CREATE TABLE avisos_retirada (
    id          serial PRIMARY KEY,
    unidade_id  int NOT NULL REFERENCES unidades(id) ON DELETE RESTRICT,
    aluno_id    int NOT NULL REFERENCES alunos(id) ON DELETE CASCADE,
    responsavel_id int NOT NULL REFERENCES responsaveis(id) ON DELETE CASCADE,
    data        date NOT NULL,
    observacao  text,
    criado_por  int REFERENCES usuarios_admin(id) ON DELETE SET NULL,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (aluno_id, responsavel_id, data)
);
CREATE INDEX avisos_retirada_dia_idx ON avisos_retirada (unidade_id, data);
