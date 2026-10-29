# Nexosix Acesso Escolar

Controle de acesso escolar com catraca facial Control iD (idBlock Next + iDFace) e aviso aos
pais por Telegram. Multi-tenant (empresa → unidades). Piloto: GEPPP, unidade Recreio.

Briefing completo e decisões: [`CLAUDE.md`](CLAUDE.md). O que é documentado × suposição × testado
na catraca: [`integrations/controlid/README.md`](integrations/controlid/README.md).

## Arquitetura (decidida em 26/10/2026)

```
 NUVEM (Railway)  — o sistema                     ESCOLA — agente
 ┌────────────────────────────────────┐           ┌─────────────────────────────────────┐
 │ web MODO=nuvem                     │  HTTPS    │ mini PC, MODO=local                 │
 │  painel, cadastro, fotos (webcam), │ ◀──────▶  │  web: só API da catraca             │
 │  turmas, regras, relatórios,       │  só o     │  worker: fila → catraca, coleta,    │
 │  usuários, Telegram                │  agente   │          sincronização com a nuvem  │
 │ PostgreSQL (fonte da verdade)      │  liga     │  PostgreSQL (cópia da unidade)      │
 └────────────────────────────────────┘           │        ▲ LAN                        │
                                                  │  catraca idBlock Next (Monitor+Pro) │
                                                  └─────────────────────────────────────┘
```

- **A nuvem é o sistema.** Secretaria e direção usam só ela (HTTPS, webcam, Telegram).
- **O agente é a parte "básica" na escola**: mantém uma cópia do cadastro da unidade e fala
  com a catraca na rede local — decide entrada/saída em tempo real (modo Pro), recebe o
  Monitor, envia fotos/regras, coleta logs. Sobe eventos, presença e status para a nuvem.
- **Internet caiu** → o agente continua decidindo com a cópia local; eventos ficam na fila e
  sobem quando voltar (Telegram sai atrasado com a hora real).
- **Agente parou** → **redundância por DNS**: a catraca aponta para um nome
  (`catraca-recreio.nexosix.com.br`) que normalmente resolve para o IP do agente na LAN.
  O vigia da nuvem nota o agente mudo (2 min) e troca o registro para a própria nuvem
  (Cloudflare); como é a catraca quem inicia as conexões, ela passa a mandar Monitor e a
  perguntar o modo Pro para a nuvem, sem VPN. Quando o agente volta a dar sinal, o DNS
  volta e o agente puxa tudo o que a nuvem registrou. No intervalo de troca (TTL 60 s) a
  catraca fica em contingência: libera entrada e **nega a saída de quem precisa de
  responsável** (regra só no portal de entrada). Sem Cloudflare configurada, só a
  contingência vale (eventos ficam na catraca e são coletados quando o agente voltar).
- Sincronização por **outbox**: toda escrita vira uma linha em `espelho_outbox` (trigger),
  marcada com a escola **da própria linha** (pessoa → unidade, catraca → unidade…; empresa e
  usuário da rede vão pra todas as escolas). O agente puxa as dele (`GET /api/espelho/reverso`,
  cursor `(xid, id)` — só transações já terminadas, nada é pulado; cada item leva o estado
  atual da linha) e empurra as suas (`POST /api/espelho/receber`). Token sempre no cabeçalho
  `X-Espelho-Token` (nunca na URL) + `X-Agente-Id` (um agente ativo por escola). A nuvem só
  aceita do agente o que é dele (eventos, presença, esperas, status — `REGRAS_DO_AGENTE`);
  item que não aplica vai pra `espelho_quarentena` e é tentado de novo. Aplicar nunca gera
  eco. Ids não colidem: o agente usa a faixa `unidade_id × 10 milhões`.
- Tudo que o painel pede à catraca (testar, Monitor, modo, liberar, hora) vira **comando na
  fila**, que desce e é executado pelo agente em segundos.

## Nuvem (Railway)

Em produção desde 27/10/2026: projeto `nexosix-acesso` (região us-east4), painel em
`https://painel.nexosix.com.br` (o endereço antigo `https://web-production-199c9.up.railway.app` continua valendo). Empresa 1 = GEPPP, unidade 1 = Recreio.

Três serviços: **PostgreSQL**, **web** (`railway.json`: bootstrap + gunicorn) e **worker**
(`python -m worker.main` — Telegram, LGPD, limpeza). Variáveis: `MODO=nuvem`, `DATABASE_URL`,
`SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_SENHA`, `BASE_URL` (URL pública), `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`. `git push origin main` faz o deploy.
Depois do primeiro deploy: `flask --app wsgi telegram-webhook`.

Para cada escola: painel → Unidades → criar → Config da unidade → **Gerar token do agente**
(aparece uma vez; vai em `ESPELHO_TOKEN` no `.env` do agente, com `UNIDADE_ID` = id da unidade).

**Redundância por DNS** (opcional, recomendada): domínio na Cloudflare; na nuvem
`CLOUDFLARE_API_TOKEN` (Zone.DNS:Edit), `CLOUDFLARE_ZONE_ID`, `FAILOVER_NUVEM_ALVO` (host do
Railway) e `FAILOVER_DOMINIO` (ex.: `nexosix.com.br`); na Cloudflare, "Always Use HTTPS"
**desligado** para esses nomes e SSL "Full" (a catraca fala HTTP na porta 80; a Cloudflare fala
HTTPS com o Railway). O nome da catraca de cada escola é calculado (`catraca-u<id>.<domínio>`;
só o superadmin troca, e só dentro do domínio). O vigia aponta o nome pro IP do PC da escola
(acompanha quando o roteador troca o IP), passa pra nuvem se o agente ficar mudo **ou** o
site dele cair por `FAILOVER_APOS_SEG`, e só devolve depois de `FAILOVER_VOLTA_SEG` (120 s)
estável. IP que não é de rede local é ignorado. **No roteador da escola**, crie uma entrada de
DNS local com o nome → IP do PC: sem ela, se a internet cair, a catraca não resolve o nome e
fica sem servidor. **[HW]** validar na catraca: nome em `monitor.hostname`/`devices.ip`, HTTP
pela internet e cache de DNS.

## Monitoramento

- **Tela Monitor** (menu Nexosix › Monitor, só superadmin): site, worker, banco, Telegram,
  avisos aos pais, cópia do banco e, por escola, servidor da escola, catracas, fila e último
  acesso — atualiza a cada 5 s. Incidentes abertos no topo, com **Assumir** e **Encerrar**.
- **Incidentes** (`app/alertas.py`, vigia do worker a cada 30 s): escola sem sinal há 2 min,
  site do servidor da escola fora, catraca offline/contingência há 3 min, avisos aos pais
  parados há 5 min ou falhando, Telegram com erro, site da nuvem fora (2 falhas seguidas),
  deploy que falhou/caiu (webhook do Railway em `/api/railway/webhook?token=…`), cópia do
  banco atrasada, quarentena, servidor desatualizado, relógio errado. O crítico chega no
  grupo do Telegram (`ALERTA_TELEGRAM_CHAT`, no web e no worker) com o botão **Assumir** e
  é repetido a cada 15 min até alguém assumir; ao resolver, avisa a duração.
- **Vigia de fora** (a nuvem inteira fora não consegue se avisar): crie uma conta grátis no
  UptimeRobot e dois monitores HTTP de 5 min: `https://<site>/health` e
  `https://<site>/health/worker` (este responde 503 se o worker parou). Alerta por e-mail e
  pelo app do UptimeRobot no celular.

## Backup

- **Railway** (Postgres → aba Backups → Daily, Weekly, Monthly): cópia dentro do Railway;
  some se o volume for apagado.
- **Cópia semanal fora do Railway** (`app/backup.py`): o worker da nuvem gera o banco inteiro,
  criptografa (AES-256, senha `BACKUP_SENHA`) e manda pelo bot para `BACKUP_TELEGRAM_CHAT`
  (ou `ALERTA_TELEGRAM_CHAT`) em partes de até 45 MB. Atrasou ou falhou → alerta.
  Na hora: `flask --app wsgi backup-agora` (ou `--pasta C:\backup` para salvar local).
- **Restaurar**: baixe todas as partes do Telegram para uma pasta, aponte `DATABASE_URL` para
  um banco **vazio** e rode `flask --app wsgi restaurar-backup C:\backup\nexosix-backup-*.parte*`
  (pede a senha). Ele migra até a versão do backup, carrega os dados e aplica as migrations
  mais novas.

## Agente na escola (Windows) — NexosixAgente-Setup.exe

Um `.exe` só, igual para todas as escolas, com Python, PostgreSQL e o programa dentro (nada é
baixado na escola). Detalhes, problemas comuns e como é testado: `deploy/windows/agente/README.md`.

1. Painel da nuvem → Configuração › Escola › Servidor da escola → **Baixar NexosixAgente-Setup.exe**
   e **Gerar código de instalação** (uso único, 24 h).
2. No PC da escola (Windows 10/11 64 bits; pode ser por acesso remoto): abrir o `.exe`, digitar o
   código, conferir o nome da escola. 2–5 min. Silencioso: `/VERYSILENT /CODIGO=ABCD-EFGH`.
3. O painel passa a mostrar "Conectado". Status local: `http://<IP>/agente`.
4. Na nuvem: Catracas → cadastrar com o IP do iDFace primário → **Testar conexão** →
   **Configurar Monitor** → (depois) **Mudar para Pro**. O agente executa e o resultado aparece no painel.

Rodar um `.exe` mais novo no mesmo PC = atualizar (sem código, mantém dados). Código pela linha de
comando da nuvem: `flask --app wsgi codigo-instalacao --unidade-id <id>`. O `.exe` é compilado e
testado no Windows pelo GitHub Actions; tag `agente-vX.Y.Z` publica em Releases.

Linux (mini PC Ubuntu): `deploy/linux/` (systemd web + worker, `backup.sh`).

## Rodar em desenvolvimento (tudo numa máquina, sem catraca)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
Copy-Item .env.example .env        # MODO=unico, DATABASE_URL, SECRET_KEY, ADMIN_EMAIL, ADMIN_SENHA, MONITOR_TOKEN
flask --app wsgi bootstrap
flask --app wsgi run --debug       # http://localhost:5000 (localhost libera a webcam sem HTTPS)
python -m worker.main              # outro terminal
python -m integrations.controlid.simulator --porta 8085   # catraca simulada
```

Cadastre a catraca com IP `127.0.0.1`, porta `8085`, senha `admin` → **Testar conexão** →
**Configurar Monitor** (`BASE_URL=http://127.0.0.1:5000`). Para "passar alguém":

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8085/sim/identificar -ContentType 'application/json' -Body '{"user_id": 1, "component_id": "0MS1"}'
```

`user_id` é o id da pessoa **na catraca** (Catracas → detalhe); `0MS1` = leitor de fora
(entrada), `0MS2` = de dentro (saída).

## Testes

PostgreSQL de teste em `DATABASE_URL_TEST` (padrão `postgresql://postgres@127.0.0.1:5433/nexosix_test`;
o banco é recriado a cada rodada; o teste ponta a ponta cria também `nexosix_test_nuvem`).

```powershell
$env:DATABASE_URL_TEST = "postgresql://postgres:postgres@localhost:5432/nexosix_test"
pytest
```

77 testes: regras de saída, Monitor, modo Pro, contingência, fila de sync, coleta, Telegram,
outbox/snapshot, e um ponta a ponta com a **nuvem em outro processo** (cadastro na nuvem →
agente baixa → catraca simulada → evento sobe → liberação manual desce e gira a catraca).

## Primeiro dia com a catraca real

1. Na rede da escola: `python -m scripts.testar_catraca --ip <IP do iDFace primário> --senha <senha>`.
2. Nuvem → Catracas → cadastrar com o IP → **Testar conexão** (o agente preenche serial/firmware/device_id).
3. **Configurar Monitor** → a catraca passa a chamar `http://<nome DNS da escola>/api/notifications/<token da escola>/…`
   (sem domínio configurado: o `BASE_URL` do agente). O token é por escola (`unidades.config.monitor_token`);
   na nuvem o `MONITOR_TOKEN` global não vale.
   "Último contato" deve atualizar a cada 30 s.
4. Pessoa de teste com foto → Catracas → detalhe: foto `sincronizado`.
5. Passar em Standalone → evento com o sentido certo (senão, ajustar o mapeamento de leitores).
6. **Mudar para Pro** → repetir com um aluno de turma "só sai com responsável"; desligar o
   agente e conferir que a catraca entra em contingência e **nega** a saída dele.
7. Registrar em `integrations/controlid/README.md` o que foi validado (data, firmware).

## Estrutura

```
app/            Flask: auth, pessoas, dispositivos, acesso (regras), eventos (ingestão), notificacoes, painel, api, espelho
integrations/   base_adapter + controlid (adapter fiel à doc, simulador HTTP, README documentado×testado)
worker/         sync (fila → catraca), collect (logs), notify (Telegram), espelho (nuvem ↔ agente), lgpd, main
migrations/     SQL numerado (001 base, 002 avisos, 003 outbox/contingência, 004 outbox por unidade, 005 comandos)
deploy/         windows/agente (instalador .exe: Inno Setup + scripts) · linux (systemd, backup.sh)
tests/          pytest
```

## Perfis do painel

`superadmin` (Nexosix) · `admin_unidade` (direção/TI) · `recepcao` (espera, liberações manuais, visitantes) ·
`coordenacao` (presença, atrasos, relatórios) · `auditor` (só leitura).

## LGPD

Foto é dado biométrico: consentimento registrado por pessoa (com arquivo opcional), fotos no banco
(não em disco), desligamento remove da catraca na hora e apaga a foto em 30 dias (`worker/lgpd.py`,
a exclusão desce para o agente), exclusão imediata disponível no painel, auditoria de toda ação.
