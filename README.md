# Nexosix Acesso Escolar

Controle de acesso escolar com catraca facial Control iD (idBlock Next + iDFace) e aviso aos
pais por Telegram. Multi-tenant (empresa → unidades). Piloto: GEPPP, unidade Recreio.

Briefing completo e decisões: [`CLAUDE.md`](CLAUDE.md). O que é documentado × suposição × testado
na catraca: [`integrations/controlid/README.md`](integrations/controlid/README.md).

## Rodar local (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env        # edite DATABASE_URL, SECRET_KEY, ADMIN_EMAIL, ADMIN_SENHA, MONITOR_TOKEN
flask --app wsgi bootstrap         # migra o banco e cria o superadmin
flask --app wsgi run --debug       # http://localhost:5000
```

Em outro terminal, o worker (sincroniza catracas, coleta logs, envia Telegram):

```powershell
python -m worker.main
```

E, sem catraca na mão, uma catraca simulada:

```powershell
python -m integrations.controlid.simulator --porta 8085
```

Cadastre a catraca no painel com IP `127.0.0.1`, porta `8085`, senha `admin`. Clique em
**Testar conexão** e **Configurar Monitor** (com `BASE_URL=http://127.0.0.1:5000`). Para
"passar alguém na catraca":

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8085/sim/identificar -ContentType 'application/json' -Body '{"user_id": 1, "component_id": "0MS1"}'
```

`user_id` é o id da pessoa **na catraca** (coluna *user_id* em Catracas → detalhe); `0MS1` é o
leitor de fora (entrada), `0MS2` o de dentro (saída).

## Testes

Precisa de um PostgreSQL de teste (padrão `postgresql://postgres@127.0.0.1:5433/nexosix_test`,
troque com `DATABASE_URL_TEST`). O banco é recriado a cada rodada.

```powershell
$env:DATABASE_URL_TEST = "postgresql://postgres:postgres@localhost:5432/nexosix_test"
pytest
```

A suíte sobe a catraca simulada e o servidor Flask em HTTP real e cobre: regras de saída,
Monitor (`dao`, `catra_event`, contingência), modo Pro (`new_user_identified`), fila de sync,
coleta de logs, Telegram, permissões e telas.

## Deploy no Railway

Três serviços no mesmo projeto:

| Serviço | Start | Variáveis |
|---|---|---|
| **PostgreSQL** | plugin | — |
| **web** | `railway.json` (bootstrap + gunicorn) | `DATABASE_URL`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_SENHA`, `MONITOR_TOKEN`, `BASE_URL` (URL pública), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` |
| **worker** | `python -m worker.main` | `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `WORKER_INTERVALO_SEG=5` |

`git push origin main` faz o deploy. Depois do primeiro deploy: `flask --app wsgi telegram-webhook`
(ou chame pelo shell do Railway) para registrar o webhook do bot.

## Primeiro dia com a catraca real

1. Na rede da escola: `python -m scripts.testar_catraca --ip <IP do iDFace primário> --senha <senha>`.
2. Painel → Catracas → cadastrar com o IP → **Testar conexão** (preenche serial/firmware/device_id).
3. **Configurar Monitor** → a catraca passa a chamar `BASE_URL/api/notifications/<MONITOR_TOKEN>/…`.
   Confira no painel se "último contato" atualiza (device_is_alive a cada 30 s).
4. Cadastre uma pessoa de teste com foto → em Catracas → detalhe, foto deve ficar `sincronizado`.
5. Passe na catraca em Standalone → evento no painel com o sentido certo. Se vier invertido,
   ajuste o mapeamento de leitores no detalhe da catraca.
6. **Mudar para Pro** → repita com um aluno de turma "só sai com responsável".
7. Registre em `integrations/controlid/README.md` o que foi validado (data, firmware).

## Estrutura

```
app/            Flask: auth, pessoas, dispositivos, acesso (regras), eventos (ingestão), notificacoes, painel, api
integrations/   base_adapter + controlid (adapter fiel à doc, simulador HTTP, README documentado×testado)
worker/         sync (fila → catraca), collect (logs pendentes), notify (Telegram), lgpd (purga 30 dias), main (loop)
migrations/     SQL numerado, aplicado por `flask --app wsgi migrar` (ou bootstrap)
tests/          pytest
```

## Perfis do painel

`superadmin` (Nexosix) · `admin_unidade` (direção/TI) · `recepcao` (espera, liberações manuais, visitantes) ·
`coordenacao` (presença, atrasos, relatórios) · `auditor` (só leitura).

## LGPD

Foto é dado biométrico: consentimento registrado por pessoa (com arquivo opcional), fotos no banco
(não em disco), desligamento remove da catraca na hora e apaga a foto em 30 dias (`worker/lgpd.py`),
exclusão imediata disponível no painel, auditoria de toda ação.
