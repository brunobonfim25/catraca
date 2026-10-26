-- A nuvem não alcança a catraca: tudo que o painel pede à catraca vira item da fila_sync,
-- que desce pro agente da escola e é executado lá.
ALTER TABLE fila_sync DROP CONSTRAINT fila_sync_tipo_check;
ALTER TABLE fila_sync ADD CONSTRAINT fila_sync_tipo_check CHECK (tipo IN (
    'sync_user','set_photo','remove_user','set_rules','set_clock',
    'liberar','testar','configurar_monitor','modo'
));
-- fila_sync agora viaja nuvem → agente (item novo) e agente → nuvem (status)
CREATE TRIGGER espelho_fila_sync AFTER INSERT OR UPDATE OR DELETE ON fila_sync FOR EACH ROW EXECUTE FUNCTION espelho_registrar('id');
