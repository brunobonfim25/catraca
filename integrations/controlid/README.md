# Control iD — o que está documentado × o que foi testado

Fonte: https://www.controlid.com.br/docs/access-api-pt/ (lida em 26/10/2026).
Coluna **Hardware** fica `pendente` até alguém rodar na catraca real e anotar aqui data e firmware.

| Função | Endpoint | Doc | Hardware |
|---|---|---|---|
| Login | `POST /login.fcgi` `{login,password}` → `{session}` | ✅ | pendente |
| Sessão válida | `POST /session_is_valid.fcgi?session=` → `{session_is_valid}` | ✅ | pendente |
| Logout | `POST /logout.fcgi` | ✅ | pendente |
| Criar objetos | `POST /create_objects.fcgi` `{object, values[]}` → `{ids[]}` | ✅ | pendente |
| Carregar | `POST /load_objects.fcgi` `{object, where, order, limit, offset, fields}` | ✅ | pendente |
| Modificar | `POST /modify_objects.fcgi` `{object, values{}, where}` → `{changes}` | ✅ | pendente |
| Destruir | `POST /destroy_objects.fcgi` `{object, where}` → `{changes}` | ✅ | pendente |
| Foto do usuário | `POST /user_set_image.fcgi?user_id&timestamp&match` octet-stream <2 MB → `{success, errors[], scores{}}` | ✅ | pendente |
| Testar foto | `POST /user_test_image.fcgi` octet-stream → mesmo retorno, não grava | ✅ | pendente |
| Apagar foto | `POST /user_destroy_image.fcgi` `{user_id | user_ids | all}` | ✅ | pendente |
| Listar fotos | `/user_list_images.fcgi` → `{user_ids[]}` | ✅ (doc diverge GET/POST) | pendente |
| Liberar catraca | `POST /execute_actions.fcgi` `{"actions":[{"action":"catra","parameters":"allow=clockwise"}]}` | ✅ | pendente |
| Hora | `POST /set_system_time.fcgi` `{day,month,year,hour,minute,second}` | ✅ | pendente |
| Info do sistema | `POST /system_information.fcgi` → serial, version, device_id, online… | ✅ | pendente |
| Configuração | `POST /set_configuration.fcgi` `{"modulo":{"chave":"valor"}}` (valores string) | ✅ | pendente |
| Monitor | módulo `monitor`: hostname, port, path, request_timeout, alive_interval | ✅ | pendente |
| Modo Pro | devices + `online_client.server_id` + `general.online=1,local_identification=1` | ✅ | pendente |
| Evento online | catraca → servidor `POST /new_user_identified.fcgi` (form-urlencoded) ← `{"result":{event,user_id,user_name,actions[],message}}` | ✅ | pendente |
| Logs de acesso | `load_objects access_logs` com `where id > N` | ✅ | pendente |

## Fatos documentados que o código usa

- **Eventos** (`access_logs.event`): 3 não identificado · 6 negado · 7 liberado · 10 liberado pela API · 11 botoeira · 13 desistência (iDBlock: liberou e não girou) · 14 sem resposta.
- **Monitor** (catraca → servidor, POST JSON em `http://hostname:port/path/<endpoint>`): `dao` (access_logs inseridos), `catra_event` (TURN_LEFT/TURN_RIGHT/GIVE_UP), `device_is_alive`, `operation_mode`, `door`, `secbox`. Funciona atrás de NAT porque quem chama é a catraca.
- **Modo Pro**: identificação local, autorização no servidor. Após 3 falhas seguidas cai em **contingência** (decide local com as regras carregadas) e manda `device_is_alive` a cada minuto até voltar.
- **idBlock Next**: integração só pelo iDFace **primário** (`sec_box.catra_role=1`); o secundário não fala com servidor. Nos eventos online do primário vem `component_id`: `0MS1` = iDFace primário, `0MS2` = secundário. Mudar `catra_role` **reinicia e apaga o banco** do leitor.
- **Fotos**: < 2 MB, JPG/PNG, rosto frontal. Erros 2 (sem rosto), 3 (rosto já existe, `match=1`), 4 (descentrado), 5/6 (longe/perto), 7 (inclinado), 8 (desfocada), 9 (perto da borda).
- **Usuário**: `registration` recebe o id da pessoa no Nexosix (reconcilia se o `user_id` se perder). `end_time` expirado **apaga as credenciais** no equipamento — por isso `revoke()` só tira regras, não mexe em `end_time`.

## Suposições a confirmar na catraca (marcadas `[SUP]`/`[HW]` no código)

1. Sessão inválida responde **HTTP 401** (o adaptador reloga uma vez). A doc não informa o código.
2. **Portais** de fábrica do idBlock Next são `1 = entrada` e `2 = saída`. Conferir com `load_objects portals` e ajustar `dispositivo_leitores` no painel.
3. `0MS1` (primário) é o leitor **de fora** (entrada) e `0MS2` o **de dentro** (saída). Depende de qual iDFace foi configurado como primário na instalação.
4. Giro à direita = entrada quando `catra_side_to_enter = "0"`. Validar com um giro real e o `catra_event` recebido.
5. **Modo por leitor**: o briefing quer entrada Standalone e saída Pro. A doc só configura `general.online` por equipamento (= o conjunto). Se não houver modo por leitor, o conjunto roda em **Pro** e o servidor libera a entrada incondicionalmente para aluno/funcionário ativo (contingência cobre a queda do servidor). O motor de regras já está pronto pros dois casos.
6. Resposta `{"result":{"event":7}}` **sem** `actions` mantém a catraca travada (usado quando o responsável se identifica do lado de fora só pra "chegar"). Se na prática liberar, trocar por `event: 6` com `message` amigável — configurável em `unidades.config.responsavel_resposta`.
7. Tempo máximo de resposta do servidor no modo online. A doc não dá o número. O endpoint `/api/catraca/<token>/new_user_identified.fcgi` responde em uma consulta SQL; medir em campo.
8. `destroy_objects users` já apaga a foto. Chamamos `user_destroy_image` antes por garantia.
9. Comportamento do braço BQC na queda de energia **não** é exposto pela API — é mecânico. Nada no código mexe nisso.

## Primeiro dia com a catraca (roteiro)

1. `python -m scripts.testar_catraca --ip 192.168.x.x --senha ...` (login, system_information, portals, set_system_time).
2. Cadastrar uma pessoa de teste no painel → ver `dispositivo_pessoa.foto_sync_status = sincronizado`.
3. Configurar o Monitor pelo painel (botão "Configurar Monitor") e passar na catraca: conferir `device_is_alive` chegando em `/api/notifications/<token>/device_is_alive`.
4. Passar na catraca em Standalone: conferir `dao` → evento no painel, sentido correto (ajustar `dispositivo_leitores`).
5. Ligar o modo Pro pelo painel e repetir com um aluno de turma "só sai com responsável".
6. Anotar nesta tabela: data, firmware, o que funcionou.
