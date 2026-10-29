<#
Configura o servidor da escola depois que o instalador copiou os arquivos para C:\nexosix.
O instalador (NexosixAgente.iss) chama uma etapa por vez, para mostrar o progresso na tela:

  preparar  permissões da pasta, runtime do Visual C++, relógio, energia, porta 80 livre
  ativar    troca o código de instalação pelo token da escola e cria o .env (só na 1ª vez)
  banco     PostgreSQL próprio (porta 5439, só localhost) como serviço NexosixBanco
  programa  confere o Python e migra o banco (configurar-local)
  servicos  serviços NexosixAgente e NexosixWorker (WinSW), firewall e backup diário
  conferir  espera o site responder e o cadastro chegar da nuvem

Toda etapa pode rodar de novo: rodar o instalador outra vez = atualizar.
Saída: 0 ok · 1 erro (mensagem em logs\instalacao-erro.txt) · 2 instalado com aviso.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('preparar', 'ativar', 'banco', 'programa', 'servicos', 'conferir')][string]$Etapa,
    [string]$Codigo = '',
    [string]$Nuvem = ''
)
. (Join-Path $PSScriptRoot 'comum.ps1')

switch ($Etapa) {

'preparar' { Etapa 'preparar' {
    $so = Get-CimInstance Win32_OperatingSystem
    Log "Windows: $($so.Caption) build $($so.BuildNumber) $($so.OSArchitecture); PC $env:COMPUTERNAME"
    $versao = Get-Content (Join-Path $RAIZ 'VERSAO.txt') -ErrorAction SilentlyContinue
    Log "instalador: $versao"

    # Instalação antiga (.cmd com Python/PostgreSQL do sistema e NSSM): sai do caminho.
    if (Test-Path (Join-Path $APP '.venv')) {
        Log 'instalação antiga encontrada (.venv): removendo serviços e tarefas dela'
        foreach ($n in $SVC_AGENTE, $SVC_WORKER) {
            if (ServicoExiste $n) { PararServico $n; Rodar 'sc.exe' @('delete', $n) -PodeFalhar | Out-Null; EsperarServico $n 'Ausente' 30 | Out-Null }
        }
        foreach ($t in 'NexosixAgente', 'NexosixWorker', 'NexosixVigia', 'NexosixBackup') { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }
        if (Test-Path $ENV_ARQ) { Move-Item $ENV_ARQ ("$ENV_ARQ.antigo-{0:yyyyMMddHHmm}" -f (Get-Date)) -Force }
        Remove-Item (Join-Path $APP '.venv'), (Join-Path $RAIZ 'nssm.exe') -Recurse -Force -ErrorAction SilentlyContinue
    }

    # .env (token da escola, senha do banco) e o banco (fotos = dado biométrico): só SYSTEM e
    # Administradores. Antes qualquer usuário do PC da recepção lia o .env e virava o "agente".
    Rodar 'icacls.exe' @($RAIZ, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
    foreach ($d in 'app', 'python', 'pgsql', 'servicos', 'instalador', 'logs', 'backups') {
        $p = Join-Path $RAIZ $d
        if (Test-Path $p) { Rodar 'icacls.exe' @($p, '/reset', '/T', '/C', '/Q') | Out-Null }
    }
    # Programas do banco: o PostgreSQL roda como NetworkService e o initdb com permissão reduzida;
    # os dois precisam ler os binários (que não têm nada de secreto)
    Rodar 'icacls.exe' @((Join-Path $RAIZ 'pgsql'), '/grant', '*S-1-5-32-545:(OI)(CI)RX') | Out-Null

    # Runtime do Visual C++ (o PostgreSQL precisa). Windows 11 novo pode não ter.
    $vc = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
    if (-not $vc -or $vc.Installed -ne 1 -or [int]$vc.Major -lt 14) {
        Log 'instalando o runtime do Visual C++'
        $c = Rodar (Join-Path $PSScriptRoot 'vc_redist.x64.exe') @('/install', '/quiet', '/norestart') -PodeFalhar
        if ($c -notin 0, 1638, 3010) { throw "o runtime do Visual C++ não instalou (código $c)" }
    } else { Log "runtime do Visual C++ ok ($($vc.Version))" }

    # Relógio certo importa: a catraca e os avisos usam a hora deste PC
    Set-Service -Name W32Time -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name W32Time -ErrorAction SilentlyContinue
    Rodar 'w32tm.exe' @('/resync', '/nowait') -PodeFalhar | Out-Null

    # PC da catraca não pode dormir
    Rodar 'powercfg.exe' @('/change', 'standby-timeout-ac', '0') -PodeFalhar | Out-Null
    Rodar 'powercfg.exe' @('/change', 'hibernate-timeout-ac', '0') -PodeFalhar | Out-Null
    # Placa de rede "economizando energia" cai e a catraca perde o servidor (padrão em muitos PCs,
    # principalmente no Wi-Fi). -NoRestart: reiniciar a placa agora derrubaria a ativação logo depois.
    foreach ($a in (Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' })) {
        try { Disable-NetAdapterPowerManagement -Name $a.Name -NoRestart -ErrorAction Stop; Log "economia de energia da placa desligada: $($a.Name)" }
        catch { Log "  placa $($a.Name): $($_.Exception.Message)" }
    }
    # Wi-Fi em desempenho máximo (vale se o PC ficar no Wi-Fi)
    Rodar 'powercfg.exe' @('/setacvalueindex', 'SCHEME_CURRENT', '19cbb8fa-5279-450e-9fac-8a3d5fedd0c1', '12bbebe6-58d6-4636-95bb-3217ef867c1a', '0') -PodeFalhar | Out-Null
    Rodar 'powercfg.exe' @('/setactive', 'SCHEME_CURRENT') -PodeFalhar | Out-Null

    # A catraca chama este PC na porta 80. Se outro programa já usa a porta, o serviço não sobe.
    $uso = Get-NetTCPConnection -LocalPort $PORTA -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uso) {
        $proc = Get-Process -Id $uso.OwningProcess -ErrorAction SilentlyContinue
        $nossos = @($PY.ToLower())
        if (-not ($proc -and $proc.Path -and $nossos -contains $proc.Path.ToLower())) {
            $quem = if ($uso.OwningProcess -eq 4) { 'o Windows (IIS / HTTP.sys — veja "netsh http show servicestate")' } elseif ($proc) { "$($proc.ProcessName) (PID $($proc.Id))" } else { "PID $($uso.OwningProcess)" }
            throw "A porta 80 deste PC já está em uso por $quem. A catraca precisa dela: desligue esse programa ou use outro PC."
        }
    }
    Log 'porta 80 livre'
} }

'ativar' { Etapa 'ativar' {
    if (Test-Path $ENV_ARQ) { Log 'já configurado (.env existe): atualização, sem pedir código'; return }
    $Codigo = ($Codigo -replace '[^A-Za-z0-9]', '').ToUpper()
    if ($Codigo.Length -ne 8) { throw 'Falta o código de instalação. No painel: Configuração › Escola › Servidor da escola › Gerar código de instalação.' }
    if (-not $Nuvem) { throw 'Endereço da nuvem não informado.' }
    $Nuvem = $Nuvem.TrimEnd('/')
    Log "ativando em $Nuvem"
    $corpo = @{ codigo = $Codigo; maquina = $env:COMPUTERNAME } | ConvertTo-Json -Compress
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Nuvem/api/espelho/ativar" -Body $corpo -ContentType 'application/json' -TimeoutSec 30 -UseBasicParsing
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($status -eq 404) { throw 'Código de instalação inválido ou vencido (vale 24 horas e uma instalação). Gere outro no painel.' }
        if ($status -eq 429) { throw 'Muitas tentativas com código errado. Espere alguns minutos e tente de novo.' }
        throw "Não consegui falar com a nuvem ($Nuvem): $($_.Exception.Message). Confira a internet deste PC."
    }
    if (-not $r.ok -or -not $r.token) { throw "A nuvem recusou a ativação: $($r.erro)" }
    Log "escola: $($r.unidade_nome) (id $($r.unidade_id))"

    $ip = (Get-NetIPConfiguration -ErrorAction SilentlyContinue | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1).IPv4Address.IPAddress
    if (-not $ip) { $ip = '127.0.0.1' }
    $linhas = @(
        'MODO=local'
        "UNIDADE_ID=$($r.unidade_id)"
        "NUVEM_URL=$Nuvem"
        "ESPELHO_TOKEN=$($r.token)"
        "DATABASE_URL=postgresql://postgres:$(Segredo 32)@127.0.0.1:$PORTA_PG/nexosix"
        "SECRET_KEY=$(Segredo 64)"
        "MONITOR_TOKEN=$(Segredo 32)"
        "BASE_URL=http://$ip"
        "PORT=$PORTA"
        'TZ_PADRAO=America/Sao_Paulo'
        'WORKER_INTERVALO_SEG=3'
    )
    # ASCII sem BOM: o python-dotenv leria o BOM como parte do primeiro nome
    Set-Content -Path $ENV_ARQ -Value $linhas -Encoding ASCII
    Set-Content -Path (Join-Path $LOGS 'escola.txt') -Value $r.unidade_nome -Encoding Default
    Log ".env criado (IP $ip)"
} }

'banco' { Etapa 'banco' {
    $senha = SenhaDoBanco
    $base = Split-Path -Parent $DADOS
    if (-not (Test-Path (Join-Path $DADOS 'PG_VERSION'))) {
        Log 'criando o banco local'
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        if (Test-Path $DADOS) { Remove-Item $DADOS -Recurse -Force }   # resto de uma tentativa que falhou
        # O initdb roda com permissão reduzida (sem o grupo Administradores): precisa de acesso
        # explícito. O serviço roda como NetworkService.
        $eu = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        Rodar 'icacls.exe' @($base, '/grant', "*${eu}:(OI)(CI)F", '*S-1-5-20:(OI)(CI)M') | Out-Null
        $pw = Join-Path $base 'pw.tmp'
        Set-Content -Path $pw -Value $senha -Encoding ASCII -NoNewline
        try {
            Rodar (Join-Path $PGBIN 'initdb.exe') @('-D', $DADOS, '-U', 'postgres', '-E', 'UTF8', '--locale=C', '-A', 'scram-sha-256', "--pwfile=$pw")
        } finally { Remove-Item $pw -Force -ErrorAction SilentlyContinue }
        $conf = @(
            '', '# --- Nexosix ---'
            "listen_addresses = '127.0.0.1'"
            "port = $PORTA_PG"
            'logging_collector = on'
            "log_directory = 'log'"
            "log_filename = 'postgresql-%a.log'"
            'log_truncate_on_rotation = on'
            'log_rotation_age = 1d'
        ) -join "`r`n"
        [IO.File]::AppendAllText((Join-Path $DADOS 'postgresql.conf'), $conf + "`r`n", (New-Object Text.UTF8Encoding $false))
    } else { Log 'banco local já existe' }
    Rodar 'icacls.exe' @($base, '/grant', '*S-1-5-20:(OI)(CI)M') | Out-Null

    if (-not (ServicoExiste $SVC_BANCO)) {
        Rodar (Join-Path $PGBIN 'pg_ctl.exe') @('register', '-N', $SVC_BANCO, '-D', $DADOS, '-S', 'auto', '-U', 'NT AUTHORITY\NetworkService') | Out-Null
        Rodar 'sc.exe' @('description', $SVC_BANCO, 'Nexosix - banco local do servidor da escola') -PodeFalhar | Out-Null
        Rodar 'sc.exe' @('failure', $SVC_BANCO, 'reset=', '3600', 'actions=', 'restart/5000/restart/15000/restart/60000') -PodeFalhar | Out-Null
        Log "serviço $SVC_BANCO registrado"
    }
    Start-Service -Name $SVC_BANCO
    if (-not (EsperarServico $SVC_BANCO 'Running' 60)) { throw "o banco ($SVC_BANCO) não iniciou: veja $DADOS\log" }
    $fim = (Get-Date).AddSeconds(60)
    while ((Rodar (Join-Path $PGBIN 'pg_isready.exe') @('-h', '127.0.0.1', '-p', "$PORTA_PG") -PodeFalhar) -ne 0) {
        if ((Get-Date) -gt $fim) { throw "o banco não respondeu na porta $PORTA_PG em 60 s: veja $DADOS\log" }
        Start-Sleep -Seconds 1
    }
    if ((Psql "SELECT 1 FROM pg_database WHERE datname = 'nexosix'") -ne '1') {
        Rodar (Join-Path $PGBIN 'psql.exe') @('-w', '-h', '127.0.0.1', '-p', "$PORTA_PG", '-U', 'postgres', '-d', 'postgres', '-c', 'CREATE DATABASE nexosix') -Ambiente @{ PGPASSWORD = $senha } | Out-Null
        Log 'banco nexosix criado'
    }
} }

'programa' { Etapa 'programa' {
    # Variáveis só do .env: uma DATABASE_URL ou MODO soltos no Windows (outro sistema no PC)
    # passariam na frente do .env (o python-dotenv não sobrescreve) e migrariam o banco errado
    $amb = LerEnv
    $amb['PYTHONIOENCODING'] = 'utf-8'; $amb['PYTHONUTF8'] = '1'
    Rodar $PY @('-c', "import flask, psycopg2, waitress, tzdata, cryptography, requests; from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo'); import app; print('python ok')") -Dir $APP -Ambiente $amb | Out-Null
    $uid = (LerEnv)['UNIDADE_ID']
    Rodar $PY @('-m', 'flask', '--app', 'wsgi', 'configurar-local', '--unidade-id', $uid) -Dir $APP -Ambiente $amb | Out-Null
} }

'servicos' { Etapa 'servicos' {
    $lista = @(
        @{ Nome = $SVC_AGENTE; Titulo = 'Nexosix - Servidor da escola'; Args = 'serve.py'
           Desc = 'Atende a catraca (Monitor e modo Pro) e guarda a cópia local do cadastro da escola.' },
        @{ Nome = $SVC_WORKER; Titulo = 'Nexosix - Sincronização'; Args = '-m worker.main'
           Desc = 'Sincroniza com a nuvem Nexosix e envia cadastro e fotos para a catraca.' }
    )
    foreach ($s in $lista) {
        $exe = Join-Path $SERVICOS "$($s.Nome).exe"
        if (ServicoExiste $s.Nome) {
            PararServico $s.Nome
            if (Test-Path $exe) { Rodar $exe @('uninstall') -PodeFalhar | Out-Null }
            if (-not (EsperarServico $s.Nome 'Ausente' 20)) { Rodar 'sc.exe' @('delete', $s.Nome) -PodeFalhar | Out-Null }
            if (-not (EsperarServico $s.Nome 'Ausente' 30)) { throw "não consegui remover o serviço antigo $($s.Nome) (feche o Gerenciador de Serviços e tente de novo)" }
        }
        Copy-Item (Join-Path $SERVICOS 'WinSW.exe') $exe -Force
        $xml = @"
<?xml version="1.0" encoding="UTF-8"?>
<service>
  <id>$($s.Nome)</id>
  <name>$($s.Titulo)</name>
  <description>$($s.Desc)</description>
  <executable>$PY</executable>
  <arguments>$($s.Args)</arguments>
  <workingdirectory>$APP</workingdirectory>
  <startmode>Automatic</startmode>
  <depend>$SVC_BANCO</depend>
  <onfailure action="restart" delay="5 sec"/>
  <onfailure action="restart" delay="15 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>20 sec</stoptimeout>
  <logpath>$LOGS</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <env name="PYTHONUTF8" value="1"/>
</service>
"@
        [IO.File]::WriteAllText((Join-Path $SERVICOS "$($s.Nome).xml"), $xml, (New-Object Text.UTF8Encoding $false))
        Rodar $exe @('install') | Out-Null
        Rodar $exe @('start') | Out-Null
        if (-not (EsperarServico $s.Nome 'Running' 30)) { throw "o serviço $($s.Nome) não iniciou: veja $LOGS\$($s.Nome).err.log" }
        Log "serviço $($s.Nome) rodando"
    }

    Get-NetFirewallRule -DisplayName $REGRA_FIREWALL -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $REGRA_FIREWALL -Description 'A catraca chama o servidor da escola nesta porta.' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $PORTA -Program $PY -Profile Any | Out-Null
    Log "firewall: porta $PORTA liberada para $PY"

    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $acao = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'backup.ps1')`""
    $quando = New-ScheduledTaskTrigger -Daily -At '23:30'
    $cfg = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $quem = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TAREFA_BACKUP -Action $acao -Trigger $quando -Settings $cfg -Principal $quem -Force | Out-Null
    Log 'backup diário agendado (23:30)'
} }

'conferir' { Etapa 'conferir' {
    $fim = (Get-Date).AddSeconds(90)
    $ok = $false
    while (-not $ok -and (Get-Date) -lt $fim) {
        try { $ok = (Invoke-WebRequest "http://127.0.0.1:$PORTA/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ok) { throw "o servidor não respondeu em http://127.0.0.1:$PORTA/health em 90 s: veja $LOGS\$SVC_AGENTE.err.log" }
    Log 'site do servidor respondendo'
    foreach ($n in $SVC_BANCO, $SVC_AGENTE, $SVC_WORKER) {
        if ("$((Get-Service $n).Status)" -ne 'Running') { throw "o serviço $n parou logo depois de iniciar: veja $LOGS" }
    }
    # Primeira carga: o worker baixa o cadastro da escola da nuvem
    $fim = (Get-Date).AddSeconds(150)
    $carga = ''
    while ($carga -ne 't' -and (Get-Date) -lt $fim) {
        $carga = Psql 'SELECT carga_em IS NOT NULL FROM espelho_estado WHERE id = 1' 'nexosix'
        if ($carga -ne 't') { Start-Sleep -Seconds 3 }
    }
    $escola = Psql "SELECT nome FROM unidades WHERE id = $((LerEnv)['UNIDADE_ID'])" 'nexosix'
    if ($escola) { Set-Content -Path (Join-Path $LOGS 'escola.txt') -Value $escola -Encoding Default }
    $avisos = @()
    if ($carga -ne 't') {
        $avisos += 'O cadastro da escola ainda não chegou da nuvem (internet lenta ou fora?); o servidor continua tentando sozinho, acompanhe em http://localhost/agente.'
    } else { Log "cadastro da escola $escola baixado da nuvem" }

    # Por onde este PC sai para a rede: a catraca precisa achá-lo sempre no mesmo endereço
    $rota = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
    $placa = if ($rota) { Get-NetAdapter -InterfaceIndex $rota.ifIndex -ErrorAction SilentlyContinue }
    $ip = if ($rota) { (Get-NetIPAddress -InterfaceIndex $rota.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress }
    if ($ip) { Set-Content -Path (Join-Path $LOGS 'ip.txt') -Value $ip -Encoding ASCII; Log "IP deste PC: $ip ($($placa.Name))" }
    if ($placa -and ("$($placa.PhysicalMediaType)" -match '802\.11|Wireless' -or $placa.InterfaceDescription -match 'Wi-?Fi|Wireless|WLAN')) {
        # Wi-Fi: cai, troca de IP e alguns roteadores isolam os aparelhos do Wi-Fi dos que estão no cabo
        $avisos += 'Este computador está no Wi-Fi. Ligue-o no cabo de rede, na mesma rede da catraca: no Wi-Fi a conexão cai e alguns roteadores não deixam a catraca alcançá-lo.'
    }
    if ($avisos) {
        $m = $avisos -join ' '
        Log "AVISO: $m"
        GravarErro $m
        exit 2
    }
} }
}
