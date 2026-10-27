-- Revisão de 27/10/2026, onda 3: catraca que reseta, perde notificação ou repete pedido.

-- C4: comando que refaz o vínculo pessoa ↔ número na catraca a partir do que está nela
ALTER TABLE fila_sync DROP CONSTRAINT fila_sync_tipo_check;
ALTER TABLE fila_sync ADD CONSTRAINT fila_sync_tipo_check CHECK (tipo IN (
    'sync_user','set_photo','remove_user','set_rules','set_clock',
    'liberar','testar','configurar_monitor','modo','reconciliar'
));

-- C5: reset da catraca recomeça a numeração dos registros (access_logs) do 1. O dedupe era
-- (catraca, número): depois do reset todo acesso real era descartado como "repetido". Cada
-- recomeço abre uma época nova; o dedupe passa a ser (catraca, época, número).
ALTER TABLE dispositivos ADD COLUMN log_epoca int NOT NULL DEFAULT 0;
ALTER TABLE eventos ADD COLUMN log_epoca int NOT NULL DEFAULT 0;
DROP INDEX eventos_dedupe_idx;
CREATE UNIQUE INDEX eventos_dedupe_idx ON eventos (dispositivo_id, log_epoca, log_id_dispositivo) WHERE log_id_dispositivo IS NOT NULL;

-- C8: a coleta tinha o mesmo cursor que o Monitor. O Monitor entregava o 105 com 101–104
-- perdidos (PC reiniciando) e a coleta começava do 105: os quatro sumiam. Agora a coleta tem
-- cursor próprio e varre tudo em sequência (o dedupe descarta o que o Monitor já trouxe).
ALTER TABLE dispositivos ADD COLUMN coleta_log_id bigint NOT NULL DEFAULT 0;
UPDATE dispositivos SET coleta_log_id = ultimo_log_id;

-- C9: a catraca repete o pedido do modo Pro (mesmo uuid) quando a resposta demora
CREATE INDEX eventos_uuid_idx ON eventos (dispositivo_id, (detalhes->>'uuid')) WHERE detalhes ? 'uuid';
