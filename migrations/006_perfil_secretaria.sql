-- Perfil "secretaria": cadastra alunos/responsáveis/turmas, vê presença; não libera catraca
-- nem mexe em configuração. Antes a secretaria tinha que ser admin_unidade (vê tudo).
ALTER TABLE usuarios_admin DROP CONSTRAINT usuarios_admin_perfil_check;
ALTER TABLE usuarios_admin ADD CONSTRAINT usuarios_admin_perfil_check
    CHECK (perfil IN ('superadmin','admin_unidade','secretaria','recepcao','coordenacao','auditor'));
