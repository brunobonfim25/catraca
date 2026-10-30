# Instalador do servidor da escola (Windows)

Um arquivo só, **NexosixAgente-Setup.exe**, igual para todas as escolas. Python, PostgreSQL e o
programa vão dentro: nada é baixado na escola. A escola é escolhida pelo **código de instalação**
que o painel gera (Configuração › Escola › Servidor da escola › Gerar código de instalação).

## Como instalar (técnico na escola ou por acesso remoto)

1. No PC da escola (Windows 10 1809+ ou 11, 64 bits), baixe o `.exe` pelo painel e abra.
2. Ele pede administrador. Digite o código de instalação; ele mostra o nome da escola para conferir.
3. Espere 2 a 5 minutos. No fim: "Servidor da escola instalado" e o painel passa a mostrar "Conectado".

Silencioso (acesso remoto, várias máquinas):

```
NexosixAgente-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CODIGO=ABCD-EFGH /LOG=C:\instalacao.log
```

Código de saída: `0` ok · `10` a configuração falhou (motivo em `C:\nexosix\logs\instalacao-erro.txt`)
· outro valor: não começou (ex.: silencioso sem código).

**Atualizar:** rodar um `.exe` mais novo no mesmo PC. Não pede código, mantém dados e configuração,
e antes de parar sobe para a nuvem o que ainda não subiu.

**Desinstalar:** Configurações do Windows › Aplicativos › "Nexosix - Servidor da escola". Ele pergunta
se apaga o banco local (cadastro e fotos). Se o PC vai sair da escola, responda **Sim** (LGPD).
Silencioso: `C:\nexosix\unins000.exe /VERYSILENT /APAGARDADOS`.

**Trocar de PC:** instale no PC novo com um código novo. O token muda e o PC antigo para de
sincronizar na hora (não disputa a catraca com o novo). Depois desinstale o antigo apagando os dados.

## Antes de instalar: o PC da escola

Piloto: **Kamrui mini PC, AMD Ryzen 3 3250C, 8 GB, SSD 256 GB, Windows 11, ligado por cabo RJ45**. Folga de
sobra (o servidor usa ~300 MB de RAM e ~600 MB de disco + o banco). O que importa nele:

- [ ] **Ao chegar, antes de ir para a escola:** Windows ativado (Configurações › Sistema › Ativação);
      na BIOS (Del ou F7 ao ligar) ligar *Restore AC Power Loss* / *Auto Power On* — se a opção não
      existir, devolver; tirar da tomada ligado e ver se religa sozinho.

- [ ] **Cabo de rede**, na mesma rede da catraca. No Wi-Fi a conexão cai, o IP muda e alguns
      roteadores isolam o Wi-Fi do cabo (a catraca não acha o PC). O instalador avisa se estiver no Wi-Fi.
- [ ] **Wi-Fi desligado** nesse PC: com cabo e Wi-Fi juntos ele fica com dois IPs e confunde a
      configuração da catraca e do roteador.
- [ ] **IP reservado no roteador** (reserva DHCP) pelo MAC da **placa de cabo**. O instalador mostra o IP no fim.
- [ ] **Voltar a ligar sozinho depois de queda de energia**: na BIOS (F10 ao ligar) › Advanced ›
      Power-On Options › *After Power Loss* = **Power On**. Sem isso, faltou luz = catraca sem servidor
      até alguém apertar o botão.
- [ ] **No-break** pequeno no PC e no roteador, se der (a catraca tem a contingência, mas sem
      servidor a saída "só com responsável" passa pela recepção).
- [ ] Windows atualizado e com **data e hora certas** (o instalador liga a sincronização de hora).
- [ ] Comprar para as próximas escolas: processador **Intel de 8ª geração em diante** (i3/i5-8xxx,
      9xxx, 10xxx, 12xxx), **AMD Ryzen 3000 em diante** ou **Intel N100/N150**; **8 GB**; SSD; Windows 11
      ativado. Descartar: chip de notebook (final M, U, H), DDR3, "Core i5" sem número de geração,
      Windows "Trial". Usado corporativo (OptiPlex 3060+, EliteDesk 800 G4+, ThinkCentre M720q+)
      serve e costuma sair mais barato que mini PC novo.

O instalador já faz sozinho: tira a suspensão e a hibernação na tomada, desliga a economia de
energia das placas de rede e deixa o Wi-Fi em desempenho máximo.

## O que fica no PC

| Onde | O quê |
|---|---|
| `C:\nexosix\app` | o programa e o `.env` (token da escola, senha do banco) |
| `C:\nexosix\python` | Python embutido (não mexe no Python do Windows, se houver) |
| `C:\nexosix\pgsql`, `dados\pg` | PostgreSQL próprio, porta **5439**, só `127.0.0.1` |
| `C:\nexosix\logs` | `instalacao.log`, logs dos serviços, `backup.log` |
| `C:\nexosix\backups` | cópia diária do banco (23:30), 14 dias |
| Serviços | `NexosixBanco`, `NexosixAgente` (porta 80, a catraca chama), `NexosixWorker` — automáticos, religam sozinhos se caírem |
| Firewall | entrada TCP 80 só para `C:\nexosix\python\python.exe` |

A pasta `C:\nexosix` só é acessível a SYSTEM e Administradores (o `.env` e as fotos não ficam
expostos a quem usa o PC da recepção).

## Problemas comuns

| Mensagem | O que fazer |
|---|---|
| Código inválido ou vencido | Gere outro no painel (vale 24 h e uma instalação) |
| Não consegui falar com a nuvem | Internet do PC; proxy/firewall da escola liberando HTTPS |
| A porta 80 já está em uso (quem usa: …) | Desative o programa citado. IIS: Painel de Controle › Programas › Ativar ou desativar recursos do Windows › desmarcar "Serviços de Informações da Internet". Ou use outro PC |
| Instalado, mas o cadastro ainda não chegou | Internet lenta: ele continua tentando; acompanhe em `http://localhost/agente` |
| Este computador está no Wi-Fi | Ligue no cabo de rede da mesma rede da catraca e rode o instalador de novo (só confere) |

## Como é montado e testado (Nexosix)

`.github/workflows/agente-windows.yml`, a cada mudança no programa ou aqui:

1. `montar.ps1` baixa versões fixas (Python embutível, PostgreSQL binários, WinSW, runtime do
   Visual C++), instala as dependências do `requirements.txt` e confere que tudo importa.
2. Inno Setup compila `NexosixAgente.iss` → `dist\NexosixAgente-Setup.exe`.
3. `testar.ps1` sobe uma nuvem de teste no próprio Windows do GitHub e testa: código errado
   (sai com 10, nada fica rodando), instalação (serviços, porta 80, catraca autenticada, cadastro
   baixado, nuvem vê o agente, `.env` trancado, backup como SYSTEM, serviço que cai e volta),
   atualização (mesmo token e dados), desinstalar mantendo dados + reinstalar sem código, e
   desinstalar apagando tudo.
4. **Publicar uma versão:** GitHub › Actions › "Instalador Windows (servidor da escola)" ›
   **Run workflow** › versão (ex.: `1.0.1`). Ele monta, testa tudo de novo e só então cria a versão
   em Releases (uma tag `agente-vX.Y.Z` enviada pelo git faz o mesmo). O painel baixa a mais nova, de
   `releases/latest/download/NexosixAgente-Setup.exe` (ou de `AGENTE_EXE_URL`, se definida na nuvem
   — necessário se o repositório virar privado).

Arquivos: `comum.ps1` (caminhos e funções), `configurar.ps1` (etapas), `remover.ps1`
(parar/desinstalar), `backup.ps1`, `ci_nuvem.py` (só o teste). Os `.ps1` e o `.iss` ficam em
UTF-8 com BOM e CRLF (`.gitattributes`): o PowerShell 5.1 do Windows lê sem BOM como ANSI.

**Testado no Windows Server 2025 do GitHub (mesma base do Windows 11 24H2): todas as checagens acima passam (instalação em ~27 s).** Ainda não testado:
Windows 11 Home de fábrica com antivírus de terceiros, PC atrás de proxy, e a catraca real
chamando o PC pela rede (ver `integrations/controlid/README.md`).

**Assinatura digital:** o `.exe` ainda não é assinado. O Windows mostra "O Windows protegeu o
computador" → **Mais informações › Executar assim mesmo**. Para sumir com o aviso é preciso um
certificado de assinatura de código em nome da Nexosix (ver CLAUDE.md, pendentes).
