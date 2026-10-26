# Nexosix Acesso Escolar

Controle de acesso escolar com catraca facial Control iD (idBlock Next + iDFace) e aviso aos
pais por Telegram. Multi-tenant (empresa → unidades). Piloto: GEPPP, unidade Recreio.

Briefing completo e decisões: [`CLAUDE.md`](CLAUDE.md). O que é documentado × suposição × testado
na catraca: [`integrations/controlid/README.md`](integrations/controlid/README.md).

## Arquitetura (Modelo 3 — decidido em 26/10/2026)

```
 ESCOLA (rede local)                                   NUVEM (Railway)
 ┌─────────────────────────────────────────┐           ┌──────────────────────────────┐
 │ servidor local (mini PC, MODO=local)    │  HTTPS    │ web MODO=nuvem + PostgreSQL  │
 │  web (painel da secretaria/recepção)    │ ───────▶  │  recebe o espelho (outbox)   │
 │  worker (catraca, Telegram, espelho)    │  só sai   │  painel da rede (4 unidades) │
 │  PostgreSQL local                       │           │  somente leitura · backup    │
 │        ▲ LAN                            │           └──────────────────────────────┘
 │  catraca idBlock Next (Monitor + Pro)   │
 └─────────────────────────────────────────┘
```

- A **catraca decide com o servidor local** (modo Pro na saída, Monitor para eventos). Internet
  fora do ar não afeta a catraca; só atrasa o Telegram.
- O servidor local **espelha** cada linha alterada para a nuvem (`espelho_outbox`, triggers da
  migration 003). A nuvem é réplica: backup vivo + painel consolidado da rede. Idempotente;
  nada entra na rede da escola (a conexão sempre sai de dentro).
- Cada escola usa uma **faixa de ids** própria (`unidade_id × 10 milhões`), então os dados das 4
  unidades convivem numa só base na nuvem sem colidir.
- Se o servidor local morrer, os dados estão na nuvem; restaura-se outro PC com o backup do dia
  (ou `pg_dump` da nuvem filtrando a unidade).

## Instalar o servidor local (Windows, PowerShell como administrador)

1. Instale Python 3.12, PostgreSQL 16 (crie o banco `nexosix`), [NSSM](https://nssm.cc) em
   `C:\nexosix\nssm.exe` e [Caddy](https://caddyserver.com) em `C:\nexosix\caddy.exe`.
2. Na nuvem, crie a empresa e a unidade (painel → Unidades) e gere o token:
   `flask --app wsgi token-espelho --unidade-id 1` (shell do Railway). Anote `ESPELHO_TOKEN`.
3. No PC da escola:
   ```powershell
   git clone https://github.com/brunobonfim25/catraca C:\nexosix\app
   cd C:\nexosix\app
   .\deploy\windows\instalar.ps1 -EmpresaId 1 -UnidadeId 1 -Empresa "GEPPP" -Unidade "Recreio"
   ```
   O script cria o `.env` (edite `DATABASE_URL`, `ADMIN_EMAIL/SENHA`, `BASE_URL`, `NUVEM_URL`,
   `ESPELHO_TOKEN`), migra, reserva a faixa de ids e registra os serviços **NexosixWeb**,
   **NexosixWorker** e **NexosixCaddy** (iniciam com o Windows, antes do login) e o backup
   diário (`C:\nexosix\backups`, 14 dias).
4. Painel: `https://nexosix.local` (HTTPS é obrigatório para a webcam; nos PCs da secretaria
   adicione `<IP do servidor> nexosix.local` no `hosts`). A catraca usa `http://<IP>:8000`.
5. **O PC servidor fica ligado** (ou liga antes da primeira entrada). Qualquer PC da secretaria
   acessa o painel pelo navegador.

Linux (mini PC Ubuntu): units em `deploy/linux/` (`nexosix-web.service`, `nexosix-worker.service`,
`Caddyfile`, `backup.sh`).

## Nuvem (Railway)

Dois serviços: **PostgreSQL** e **web** (`railway.json`). Variáveis do web: `MODO=nuvem`,
`DATABASE_URL`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_SENHA`, `BASE_URL`. Sem worker na nuvem
(catracas e Telegram são do servidor local). `git push origin main` faz o deploy.

Na nuvem o painel é **somente leitura** (edita-se na escola). Usuários da escola entram na
nuvem com a mesma senha (a tabela `usuarios_admin` é espelhada); `/painel/rede` mostra as
unidades, presença, negados, catracas e a hora do último espelho.

## Rodar em desenvolvimento (sem catraca)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
Copy-Item .env.example .env        # DATABASE_URL, SECRET_KEY, ADMIN_EMAIL, ADMIN_SENHA, MONITOR_TOKEN
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
o banco é recriado a cada rodada).

```powershell
$env:DATABASE_URL_TEST = "postgresql://postgres:postgres@localhost:5432/nexosix_test"
pytest
```

A suíte sobe a catraca simulada e o servidor Flask em HTTP real: regras de saída, Monitor,
modo Pro, contingência (aluno que precisa de responsável não sai sem servidor), fila de sync,
coleta de logs, Telegram, espelho local→nuvem, nuvem somente leitura, permissões e telas.

## Primeiro dia com a catraca real

1. Na rede da escola: `python -m scripts.testar_catraca --ip <IP do iDFace primário> --senha <senha>`.
2. Painel → Catracas → cadastrar com o IP → **Testar conexão** (preenche serial/firmware/device_id).
3. **Configurar Monitor** → a catraca passa a chamar `http://<IP do servidor>:8000/api/notifications/<MONITOR_TOKEN>/…`.
   "Último contato" deve atualizar a cada 30 s.
4. Pessoa de teste com foto → Catracas → detalhe: foto `sincronizado`.
5. Passar em Standalone → evento com o sentido certo (senão, ajustar o mapeamento de leitores).
6. **Mudar para Pro** → repetir com um aluno de turma "só sai com responsável"; desligar o
   servidor e conferir que a catraca entra em contingência e **nega** a saída dele.
7. Registrar em `integrations/controlid/README.md` o que foi validado (data, firmware).

## Estrutura

```
app/            Flask: auth, pessoas, dispositivos, acesso (regras), eventos (ingestão), notificacoes, painel, api, espelho
integrations/   base_adapter + controlid (adapter fiel à doc, simulador HTTP, README documentado×testado)
worker/         sync (fila → catraca), collect (logs pendentes), notify (Telegram), espelho (→ nuvem), lgpd, main
migrations/     SQL numerado (001 base, 002 avisos, 003 espelho/contingência)
deploy/         windows (instalar.ps1, backup.ps1, Caddyfile) · linux (systemd, Caddyfile, backup.sh)
tests/          pytest (73 testes)
```

## Perfis do painel

`superadmin` (Nexosix) · `admin_unidade` (direção/TI) · `recepcao` (espera, liberações manuais, visitantes) ·
`coordenacao` (presença, atrasos, relatórios) · `auditor` (só leitura).

## LGPD

Foto é dado biométrico: consentimento registrado por pessoa (com arquivo opcional), fotos no banco
(não em disco), desligamento remove da catraca na hora e apaga a foto em 30 dias (`worker/lgpd.py`),
exclusão imediata disponível no painel, auditoria de toda ação. O espelho leva a foto para a nuvem
(backup do mesmo controlador); a purga de 30 dias também é espelhada.
