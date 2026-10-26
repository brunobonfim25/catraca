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
- **Agente parou** → a catraca entra em contingência: libera entrada, **nega a saída de quem
  precisa de responsável** (regra só no portal de entrada); a recepção libera pelo portão PCD
  e registra no painel. Eventos ficam na catraca e são coletados quando o agente voltar.
  Fase 2: com a catraca na VPN (client da Control iD), a nuvem pode assumir a catraca.
- Sincronização por **outbox**: toda escrita vira uma linha em `espelho_outbox` (trigger); a
  nuvem marca de qual unidade é cada linha; o agente puxa as dele (`/api/espelho/<token>/reverso`)
  e empurra as suas (`POST /api/espelho/<token>`). Aplicar nunca gera eco. Ids não colidem: o
  agente usa a faixa `unidade_id × 10 milhões`.
- Tudo que o painel pede à catraca (testar, Monitor, modo, liberar, hora) vira **comando na
  fila**, que desce e é executado pelo agente em segundos.

## Nuvem (Railway)

Três serviços: **PostgreSQL**, **web** (`railway.json`: bootstrap + gunicorn) e **worker**
(`python -m worker.main` — Telegram, LGPD, limpeza). Variáveis: `MODO=nuvem`, `DATABASE_URL`,
`SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_SENHA`, `BASE_URL` (URL pública), `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`. `git push origin main` faz o deploy.
Depois do primeiro deploy: `flask --app wsgi telegram-webhook`.

Para cada escola: painel → Unidades → criar; shell do Railway:
`flask --app wsgi token-espelho --unidade-id <id>` → `ESPELHO_TOKEN` para o agente.

## Agente na escola (Windows, PowerShell como administrador)

1. Instale Python 3.12, PostgreSQL 16 (banco `nexosix`) e [NSSM](https://nssm.cc) em `C:\nexosix\nssm.exe`.
2. Dê **IP fixo** ao PC (a catraca liga nele).
3. ```powershell
   git clone https://github.com/brunobonfim25/catraca C:\nexosix\app
   cd C:\nexosix\app
   .\deploy\windows\instalar.ps1 -UnidadeId 1
   ```
   O script cria o `.env` (edite `DATABASE_URL`, `NUVEM_URL`, `ESPELHO_TOKEN`, `BASE_URL=http://<IP>:8000`),
   migra, reserva a faixa de ids, registra os serviços **NexosixAgente** e **NexosixWorker**
   (sobem com o Windows, antes do login), abre a porta 8000 e agenda o backup diário.
4. O worker baixa o cadastro da unidade na primeira rodada. Status: `http://localhost:8000/agente`.
5. Na nuvem: Catracas → cadastrar com o IP do iDFace primário → **Testar conexão** →
   **Configurar Monitor** → (depois) **Mudar para Pro**. O agente executa e o resultado aparece no painel.
6. O PC fica ligado (ou liga antes da primeira entrada).

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
3. **Configurar Monitor** → a catraca passa a chamar `http://<IP do agente>:8000/api/notifications/<MONITOR_TOKEN>/…`.
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
deploy/         windows (instalar.ps1, backup.ps1) · linux (systemd, backup.sh)
tests/          pytest
```

## Perfis do painel

`superadmin` (Nexosix) · `admin_unidade` (direção/TI) · `recepcao` (espera, liberações manuais, visitantes) ·
`coordenacao` (presença, atrasos, relatórios) · `auditor` (só leitura).

## LGPD

Foto é dado biométrico: consentimento registrado por pessoa (com arquivo opcional), fotos no banco
(não em disco), desligamento remove da catraca na hora e apaga a foto em 30 dias (`worker/lgpd.py`,
a exclusão desce para o agente), exclusão imediata disponível no painel, auditoria de toda ação.
