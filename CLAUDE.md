# Nexosix Acesso Escolar — contexto do projeto para o Claude Code

Este arquivo é o briefing do sistema de controle de acesso escolar da Nexosix. Leia-o inteiro antes de qualquer tarefa. Ele registra o que já foi decidido, o que é fato documentado pelo fabricante e o que ainda precisa ser validado em hardware real. Não invente endpoints, parâmetros ou comportamentos da catraca: tudo que diz respeito à Control iD deve vir da documentação oficial em https://www.controlid.com.br/docs/access-api-pt/ ou de teste no equipamento.

---

## 1. O negócio

- **Dono do projeto:** Bruno Bonfim, Nexosix (Rio de Janeiro). Desenvolve junto com o Claude Code; ele decide, você implementa e aponta riscos.
- **Cliente fechado:** Grupo Educacional Professor Pica Pau (GEPPP), 4 unidades. Começa pela **unidade Recreio** (Creche Escola Professor Pica Pau – Recreio, Rua Alberto Bianchi 46, Recreio dos Bandeirantes, RJ). Educação infantil até 5º ano. As outras 3 unidades entram depois, uma por semana, com o mesmo sistema.
- **Hardware fechado:** Control iD **idBlock Next** com 2 leitores faciais **iDFace** (um por sentido), 3 mil faces por leitor, braço articulado BQC. Comprada pelo colégio direto do fornecedor. Modelo a confirmar na entrega: Next padrão ou Next Mini (mais baixa, indicada para crianças). A API é a mesma.
- **Instalação Recreio:** área interna coberta. Piso emborrachado sobre contrapiso (recortar placas na base). A catraca vai na linha do gradil branco existente, substituindo o portãozinho central; o vão PCD é o portão que sobra. Uma catraca bidirecional (Cenário 1).
- **Produto:** plataforma própria (SaaS) da Nexosix. Este colégio é o piloto; o código deve nascer multi-tenant para virar produto para outras escolas.
- **Aviso aos pais:** **Telegram** (bot). WhatsApp fica como opcional futuro (custo por mensagem). O responsável ativa o bot uma vez na secretaria via QR Code.
- **Cadastro:** feito na secretaria, no PC deles, pela **webcam no navegador**. Ninguém leva aluno até a catraca. A foto vai para as catracas pela API.
- **Proposta comercial:** implantação R$ 3.800/colégio, mensalidade R$ 1.500/mês pela rede. Cronograma: piloto em ~60 dias, depois as outras 3.

## 2. O que o sistema faz (escopo do MVP, unidade Recreio)

1. **Entrada do aluno** por facial, dentro da janela do turno. Atraso além da tolerância vira evento `ENTRADA_ATRASADA`, visível para a coordenação.
2. **Aviso ao responsável** no Telegram a cada entrada e saída: "Maria (Pré 1) entrou às 07:12" / "Maria saiu às 17:05 com Ana (mãe)".
3. **Saída com responsável** (regra por turma): o aluno só sai se um responsável autorizado se identificou no leitor de fora nos últimos N minutos. Sem responsável → catraca nega, recepção é avisada.
4. **Saída autônoma** (turmas autorizadas pelos pais): sai sozinho na janela de fim de turno.
5. **Responsáveis autorizados:** até 5 por aluno, cada um com face e regra (sempre / dias específicos / só com aviso prévio).
6. **Exceção manual:** recepção libera saída antecipada com justificativa; fica no log com quem autorizou; pais recebem aviso.
7. **Cadastro na secretaria:** aluno, turma, turno, responsáveis, foto por webcam. Sincronização automática com as catracas, com status por dispositivo (sincronizado / pendente / erro).
8. **Presença em tempo real:** quem está dentro agora, por turma. Lista de chamada e de evacuação.
9. **Painel:** catracas online/offline, últimos acessos, negados, alertas, filtros por unidade/turma/pessoa.
10. **Relatórios:** frequência, atrasos, saídas antecipadas, histórico por pessoa.
11. **Funcionários:** acesso por escala. **Visitantes:** liberação manual registrada.
12. **Multi-unidade:** painel consolidado das 4 escolas (entra quando a 2ª unidade for instalada, mas o modelo de dados já nasce com `unidade_id`).

Fora do MVP: WhatsApp, integração com Agenda ActiveSoft (app que a escola usa), app para pais, leitora facial extra na recepção.

## 3. Stack e convenções (não mudar sem conversar com o Bruno)

- **Backend:** Python 3.12, Flask com Blueprints, PostgreSQL via **psycopg2** (pool), Jinja2. Sem ORM pesado; SQL explícito. Gunicorn em produção.
- **Front:** HTML/CSS/JS + Bootstrap 5. Sem React no MVP. Webcam via `getUserMedia` no navegador.
- **Jobs:** worker Python separado para sincronização com catracas e envio de Telegram (fila em tabela PostgreSQL com `SELECT ... FOR UPDATE SKIP LOCKED`; Redis só se precisar depois).
- **Tempo real no painel:** polling a cada 5 s no MVP; WebSocket depois.
- **Deploy:** Railway (web + worker + PostgreSQL, `MODO=nuvem`) é o sistema; na escola um agente (`MODO=local`, ver 8b). Variáveis de ambiente, `git push origin main` faz deploy da nuvem. Fuso `America/Sao_Paulo`; salvar timestamps em UTC (`timestamptz`) e converter na exibição.
- **Ambiente do Bruno:** Windows, PowerShell, repo local em `C:\Users\Bruno Bonfim\`. Comandos de terminal devem ser PowerShell.
- **Padrão de código:** módulos pequenos, funções puras onde der, docstring curta em PT-BR, nomes em português para domínio (`aluno`, `responsavel`, `turma`) e inglês para infra (`db`, `queue`, `adapter`).
- **Segredos:** nunca no código. `.env` local, variáveis no Railway. Senha admin da catraca é segredo.
- **Testes:** `pytest`. Todo adaptador de hardware tem um **simulador** que reproduz apenas comportamento documentado; marcar claramente o que foi testado em hardware real.

## 4. Arquitetura

```
app/
  __init__.py            # create_app, blueprints, pool psycopg2
  db.py                  # pool, helper query/execute, migrations runner
  auth/                  # login, RBAC (superadmin, admin_unidade, recepcao, coordenacao, auditor)
  pessoas/               # alunos, responsaveis, funcionarios, vínculos, fotos
  dispositivos/          # cadastro de catracas/leitores, status, sincronização
  acesso/                # motor de regras + decisão online (modo Pro)
  eventos/               # ingestão de eventos, correlação, presença
  notificacoes/          # Telegram bot (ativação por QR, envio)
  painel/                # dashboard, relatórios
  api/                   # endpoints que a catraca chama (monitor/notificações)
integrations/
  base_adapter.py        # interface universal
  controlid/
    adapter.py           # implementação iDFace/idBlock Next
    simulator.py         # mock fiel à documentação
    README.md            # o que está documentado × testado
worker/
  sync.py                # fila: enviar usuários/fotos/regras às catracas
  collect.py             # coletar logs pendentes, dedupe
  notify.py              # fila de Telegram
migrations/              # SQL numerado 001_..., aplicado por db.py
tests/
```

### Interface universal (`base_adapter.py`)

Métodos que todo adaptador implementa (só o que o equipamento suporta de verdade):
`connect() · status() · identify_device() · sync_user(pessoa) · remove_user(id) · set_user_photo(id, jpeg) · set_access_rules(pessoa, regras) · revoke(id) · release(direction) · fetch_logs(since_id) · set_clock() · health()`

## 5. Control iD — fatos documentados (usar como referência; confirmar em hardware)

Fonte: https://www.controlid.com.br/docs/access-api-pt/ (API REST, TCP/IP, JSON, exemplos em Python no GitHub `controlid/integracao`).

**Modos de operação** (`introduction-to-operating-modes`):
- **Standalone:** identificação e autorização no terminal (recomendado pela Control iD). Funciona sem internet.
- **Pro:** identificação no terminal, autorização no servidor (manda só o `user_id`). Cai em **Contingência** (decide local) se o servidor não responde.
- **Enterprise:** identificação e autorização no servidor. **Não usar.**

**Decisão de projeto:** leitor de **entrada em Standalone**; leitor de **saída em modo Pro** para as turmas com "saída só com responsável" (o servidor decide com base no responsável em espera). Contingência na saída = negar e avisar recepção (configurável por turma). Validar em hardware se o modo pode ser diferente por leitor dentro do mesmo conjunto idBlock Next (primário/secundário, config `catra_role` no módulo `sec_box`).

**Monitor:** o equipamento envia periodicamente requisições HTTP ao servidor buscando comandos e manda notificações de eventos para endpoints do servidor (ex.: `/api/notifications/...`). Funciona atrás de NAT — a catraca é quem chama. É o mecanismo para eventos em tempo real.

**Sessão/autenticação:** `POST /login.fcgi` com login e senha → `session` usada nas demais chamadas (`?session=...`). Confirmar na doc o nome exato e o tempo de vida.

**Usuários:** `POST /create_objects.fcgi` (object `users`), `load_objects.fcgi`, `modify_objects.fcgi`, `destroy_objects.fcgi`. Regras de horário: objetos `time_zones`, `time_spans`, `access_rules`, `user_access_rules` (confirmar nomes na doc "Objetos").

**Fotos faciais** (`reconhecimento-facial/cadastro-facial`, `fotos-e-logotipo/gerenciar-fotos-de-usuarios`):
- `POST /user_set_image.fcgi?user_id=ID` — Content-Type **application/octet-stream**, JPEG no corpo, < 2 MB. O terminal gera o template.
- `POST /user_set_image_list.fcgi` — JSON `{user_images:[{user_id, image(base64)}]}`, limite 1 MB por request; recomendado para carga em massa (lotes pequenos).
- `GET /user_get_image.fcgi?user_id=ID`, `POST /user_list_images.fcgi`, `POST /user_destroy_image.fcgi` (por id, lista ou `all`).
- `POST /remote_enroll.fcgi` tipo `face` — cadastro pela câmera da própria catraca; só como fallback.
- Seguir "Recomendações – fotos e instalação" (rosto frontal, fundo neutro, iluminação).

**Logs de acesso:** objeto `access_logs` via `load_objects.fcgi` com filtro por id incremental; guardar o último id por dispositivo e deduplicar por `(device_id, log_id)`.

**idBlock Next** (`particularidade-dos-produtos/idblock-next`): conjunto com iDFace primário/secundário; eventos de giro à direita/esquerda e desistência; nos eventos online do primário vem `component_id` identificando qual leitor. Liberação remota: ação `sec_box`.

**Não verificado ainda (marcar como tal no código):** nomes exatos dos endpoints do Monitor, formato do payload de notificação, se o modo Pro pode ser por leitor, latência de resposta exigida pela catraca no modo online, comportamento do BQC na queda de energia via API.

## 6. Modelo de dados (PostgreSQL) — esqueleto

Todas as tabelas de domínio têm `unidade_id` (e `empresa_id` via unidade). Índices em `unidade_id`, chaves estrangeiras com `ON DELETE RESTRICT` salvo onde indicado.

- `empresas` (GEPPP) · `unidades` (Recreio, …) · `usuarios_admin` (login, perfil, unidade opcional)
- `turmas` (unidade, nome, turno, `exige_responsavel_saida bool`, `tolerancia_entrada_min`, janelas de entrada/saída)
- `pessoas` (unidade, tipo: aluno|responsavel|funcionario|visitante, nome, foto_url, status, `device_user_id` por dispositivo em tabela própria)
- `alunos` (pessoa_id, turma_id, matrícula) · `responsaveis` (pessoa_id, telefone, telegram_chat_id, telegram_ativado_em)
- `aluno_responsavel` (aluno_id, responsavel_id, parentesco, regra: sempre|dias|aviso, dias_semana[])
- `dispositivos` (unidade, fabricante, modelo, serial, ip, porta, firmware, `funcao`: catraca|leitora, `sentido`: entrada|saida|bidirecional, modo: standalone|pro, status, ultimo_contato)
- `dispositivo_pessoa` (dispositivo_id, pessoa_id, user_id_no_dispositivo, foto_sync_status, regras_sync_status, atualizado_em)
- `fila_sync` (dispositivo_id, tipo, payload jsonb, tentativas, proximo_em, status, erro)
- `eventos` (unidade, dispositivo_id, pessoa_id nullable, tipo, sentido, ocorrido_em timestamptz, origem: dispositivo|manual, log_id_dispositivo, correlacao_id, autorizado_por, justificativa) — índice único `(dispositivo_id, log_id_dispositivo)`
- `responsavel_em_espera` (unidade, responsavel_id, identificado_em, expira_em, dispositivo_id)
- `presenca` (materializada por evento: aluno_id, dentro bool, desde)
- `fila_notificacao` (responsavel_id, texto, status, tentativas, enviado_em)
- `auditoria` (quem, o quê, quando, antes/depois jsonb)
- `consentimentos_lgpd` (pessoa_id, assinado_por, data, arquivo)

## 7. Fluxos críticos

**Cadastro de aluno (secretaria)** → salva pessoa + foto (validar rosto no front, tamanho < 1 MB) → enfileira `sync_user` + `set_photo` para cada dispositivo da unidade → worker envia → painel mostra status por catraca → erro de foto volta para a secretaria com "refazer foto".

**Entrada** → catraca decide (Standalone) → notifica servidor via Monitor → servidor grava evento, atualiza presença, correlaciona com entrada de responsável nos 60 s anteriores (mesma catraca) → enfileira Telegram.

**Saída com responsável** → responsável faz facial no leitor de fora → evento cria `responsavel_em_espera` (expira em N min, ex. 15) → painel da recepção mostra "Ana chegou para buscar Maria" → aluno faz facial no leitor de dentro (modo Pro) → servidor responde libera/nega conforme espera válida + vínculo + regra do dia → evento `SAIDA_ALUNO` com `retirado_por` → Telegram.

**Offline** → catraca segue Standalone → logs ficam nela → `collect.py` busca pendentes quando reconecta → dedupe → eventos com `ocorrido_em` real → Telegram atrasado com horário real → bloqueios feitos no painel chegam na reconexão.

**Ativação do Telegram** → link individual `t.me/<bot>?start=<token>` (QR, WhatsApp, e-mail) ou link da escola (`?start=u<id>`, casa pelo telefone) → bot mostra a **autorização LGPD** com 3 botões (foto + avisos / só avisos / não autorizo) → resposta gravada em `consentimentos_lgpd` (escopo, aceito, canal `telegram`, texto exato visto) por aluno e pelo responsável → só então grava `telegram_chat_id`. Decisão de 27/10: o termo dos pais saiu do contrato e é pedido aqui; quem não usa Telegram assina em papel e a secretaria registra na ficha. Basta um dos pais; a recusa mais nova do mesmo responsável vence. Sem autorização, Alunos mostra "Falta autorização dos pais" antes de "Falta foto".

## 8. Plano de trabalho (ordem)

1. Esqueleto Flask + pool + migrations + auth + CRUD de unidades/turmas/pessoas. Deploy no Railway.
2. `integrations/controlid/simulator.py` + `adapter.py` com login, usuários, fotos, logs, fiel à doc. Testes.
3. Cadastro na secretaria com webcam + fila de sync + worker. Testar com o simulador, depois **na catraca real assim que chegar** (primeira coisa a validar: sessão, `user_set_image`, `load_objects` de `access_logs`, Monitor).
4. Ingestão de eventos (Monitor) + coleta de pendentes + presença.
5. Telegram: bot, ativação, fila, mensagens.
6. Motor de regras + modo Pro na saída + responsável em espera + painel da recepção.
7. Painel/relatórios, exceções manuais, auditoria, LGPD (exclusão).
8. Operação assistida no Recreio; depois replicar nas outras 3 unidades (só cadastro de unidade + dispositivos).

## 8b. Decisão de arquitetura (26/10/2026) — nuvem é o sistema, escola tem um agente

Depois de ouvir um integrador Control iD (condomínio, colégio, clube), o Bruno decidiu:
**o sistema web fica na nuvem; na escola fica só o básico** — um agente que fala com a catraca.

- **Nuvem** (`MODO=nuvem`, Railway): painel, cadastro, foto por webcam (HTTPS), turmas,
  regras, relatórios, usuários, Telegram. Fonte da verdade. Worker da nuvem: Telegram, LGPD.
- **Agente** (`MODO=local`, mini PC na escola, NSSM/waitress ou systemd): cópia do cadastro da
  unidade + API da catraca (Monitor, modo Pro) + worker (fila → catraca, coleta, sync). Sem
  painel: qualquer rota que não seja API redireciona para a nuvem. Fica ligado antes da
  primeira entrada; escuta na porta 80; a catraca chama o nome DNS da escola.
- **Sync bidirecional por outbox** (`espelho_outbox`, triggers 003/004/009): cada linha é
  marcada com a escola **derivada da própria linha** (`espelho_unidades_da_linha`; GUC só de
  reserva). O agente puxa as da sua unidade (`/api/espelho/reverso`, cursor `(xid, id)` em
  `espelho_estado`, só transações terminadas, item = estado atual da linha) e empurra as suas
  (`/api/espelho/receber`, confirmadas por id). Carga inicial: `/snapshot` (REPEATABLE READ,
  sem fotos) + `/fotos` paginado; `carga_em` marca o fim. Token no cabeçalho `X-Espelho-Token`,
  `X-Agente-Id` (um agente ativo por escola, 409 pro segundo). Aplicar nunca gera eco; item com
  erro vai pra `espelho_quarentena` e é retentado. Donos: nuvem → cadastro, itens novos da
  fila, liberação manual; agente → eventos, presença (a mais nova ganha), esperas, status de
  sync e da catraca. **A nuvem só aceita do agente o que está em `REGRAS_DO_AGENTE`** (o resto é
  recusado: o token está no PC da escola). Hash de senha nunca desce pro agente. Outbox de
  escola com agente nunca é apagado em silêncio: passou de 30 dias → `espelho_recarga`.
- **Comandos à catraca** (testar, Monitor, modo, hora, liberar) são itens da `fila_sync`
  criados pelo painel e executados pelo agente — a nuvem nunca fala com a catraca.
- **Ids**: agente usa a faixa `unidade_id × 10M` (`flask configurar-local`); nuvem, ids pequenos.
- **Contingência** (agente parado): aluno "só com responsável" tem na catraca a regra
  `Nexosix - so entrada` (só portal de entrada) → offline a catraca nega a saída dele e a
  recepção libera pelo portão PCD. Status `contingencia` fica no painel até o `operation_mode`.
- **Internet fora**: agente segue decidindo com a cópia local; fila sobe depois.
- **Redundância** (27/10): a catraca sempre consulta o agente primeiro; a nuvem é reserva.
  A catraca aponta para um **nome DNS** (`catraca-u<id>.<FAILOVER_DOMINIO>`, gravado em
  `unidades.config.dns_nome`; só superadmin troca e só dentro do domínio); o vigia da nuvem
  (`app/espelho/failover.py`, worker da nuvem a cada 30 s) troca o registro na Cloudflare
  para a nuvem quando o agente fica mudo ou o site dele cai (`FAILOVER_APOS_SEG`) e devolve
  para o IP da LAN (só IP privado, vem no ping a cada 30 s numa thread própria) depois de
  `FAILOVER_VOLTA_SEG` estável. Token da catraca é **por escola** (`config.monitor_token`).
  Roteador da escola precisa de DNS local com o nome → IP do PC (senão, sem internet, a
  catraca não acha o agente). A catraca inicia as conexões, então funciona
  sem VPN; as escritas que a nuvem faz nesse período são marcadas com a unidade
  (`g.unidade_api_id` nas rotas da catraca) e o agente puxa ao voltar. Agente escuta na
  porta 80 (mesma da Cloudflare). [HW] nome/HTTP/cache DNS na catraca. VPN
  (`dispositivos.config.ip_vpn`) continua como alternativa se o DNS não servir.
- `MODO=unico` = tudo numa máquina (dev, testes, escola sem nuvem).

## 8c. Situação operacional (atualizar a cada entrega)

- **Nuvem no ar** (27/10/2026): Railway projeto `nexosix-acesso` (us-east4), web + worker +
  Postgres, `https://web-production-199c9.up.railway.app`. Empresa 1 = GEPPP; unidades
  Recreio (1) e Itaguaí (2). Superadmin = e-mail do Bruno.
- **Telegram**: bot **@NexosixAcessoBot** criado em 27/10 na conta pessoal do Bruno (depois
  transferir para um número da Nexosix — BotFather permite). Token no Railway (web e worker),
  webhook registrado em `/api/telegram/webhook`. Ativação dos responsáveis já funciona
  (ficha do responsável → Ativar Telegram → QR).
- **PC do agente**: HP EliteDesk 705 G1 DM (A8, 8 GB, SSD 120, Win 10 Pro, RJ45) comprado
  para teste. Instalação pelo `instalar-agente.cmd` do painel (Unidades → Config).
- **Reorganização do painel** (27/10, PDF em `docs/` da análise de usabilidade): fase 1 no ar —
  menu lateral por papel, tela inicial por papel (`HOME_POR_PERFIL`), perfil `secretaria`,
  Agora ≠ Recepção, Alunos/Responsáveis/Funcionários/Pendências, vocabulário único
  (escola / cliente / catraca / servidor da escola). **Fase 2 no ar** (sugestão do integrador,
  áudio 27/10): importar planilha (Cadastro › Importar planilha; modelo em
  `docs/modelo-cadastro-alunos.xlsx`; aceita a planilha da própria escola e adivinha as
  colunas; prévia antes de gravar; tudo numa transação `db.lote()`; reimportar não duplica),
  convites em massa (Cadastro › Convites: WhatsApp um clique por família, e-mail em lote pela
  fila do worker da nuvem, link da escola) e coluna "Pronto para a catraca?" em Alunos.
  E-mail precisa de conta Brevo (grátis, 300/dia) + `BREVO_API_KEY` e `EMAIL_REMETENTE` no
  web e no worker — Railway bloqueia SMTP, por isso é API. Faltam: fase 3 (cadastro guiado,
  checklist de implantação, Configuração passo a passo com Avançado), fase 4 (fila de fotos).
- **Pendentes**: planilha de alunos da escola (mandar o modelo); conta Brevo para o convite
  por e-mail; bot do Telegram; PC chegar e aparecer como "agente visto"; domínio +
  Cloudflare (redundância por DNS); catraca chegar (roteiro em `integrations/controlid/README.md`);
  módulo de mensalidade/cobrança (decidido deixar para depois do piloto).

## 9. Como trabalhar com o Bruno

- Respostas diretas e práticas. Código pronto para rodar, compatível com Flask + psycopg2. Não propor reescrever o que já existe.
- Antes de codar algo que depende da catraca, dizer o que é **documentado**, o que é **suposição** e o que precisa de **teste no equipamento**.
- Cada entrega: o que foi feito, como testar, próximo passo. Sem teoria desnecessária.
- Segurança e LGPD não são opcionais: foto é dado biométrico; consentimento antes do cadastro; exclusão em até 30 dias após desligamento; acesso restrito; auditoria.
- Nunca criar mecanismo que contorne autenticação da catraca ou a saída de emergência (BQC).
