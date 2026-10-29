; Instalador único (.exe) do servidor da escola — Nexosix Acesso Escolar.
; Compilado pelo GitHub Actions (.github/workflows/agente-windows.yml) a partir da pasta montada
; por montar.ps1: Python, PostgreSQL e o programa já vão dentro, nada é baixado na escola.
;
; Uso: dois cliques (pede administrador). Silencioso, para acesso remoto ou teste:
;   NexosixAgente-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CODIGO=ABCD-EFGH [/NUVEM=https://...]
; Rodar de novo no mesmo PC = atualizar (não pede código, mantém os dados).
; Código de saída: 0 ok · 1 não iniciou (ex.: silencioso sem código) · 10 a configuração falhou.

#ifndef Staging
  #define Staging "..\..\..\build\agente"
#endif
#ifndef AppVersao
  #define AppVersao "0.0.0-dev"
#endif
#ifndef VersaoNumerica
  #define VersaoNumerica "0.0.0.0"
#endif
#ifndef NuvemPadrao
  #define NuvemPadrao "https://web-production-199c9.up.railway.app"
#endif

[Setup]
; AppId fixo: é ele que faz o instalador novo reconhecer o antigo e atualizar por cima
AppId={{6F2B8C1E-3A4D-4E7B-9C21-5D8E0A1F7B34}
AppName=Nexosix - Servidor da escola
AppVersion={#AppVersao}
AppVerName=Nexosix - Servidor da escola {#AppVersao}
AppPublisher=Nexosix
VersionInfoVersion={#VersaoNumerica}
VersionInfoCompany=Nexosix
VersionInfoDescription=Instalador do servidor da escola (Nexosix Acesso Escolar)
VersionInfoProductName=Nexosix Acesso Escolar
DefaultDirName={sd}\nexosix
DisableDirPage=yes
UsePreviousAppDir=no
DirExistsWarning=no
DisableProgramGroupPage=yes
UninstallDisplayName=Nexosix - Servidor da escola
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 ou mais novo (o alvo é o Windows 11)
MinVersion=10.0.17763
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
OutputDir=..\..\..\dist
OutputBaseFilename=NexosixAgente-Setup
SetupLogging=yes
CloseApplications=no
ShowLanguageDialog=no

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Messages]
ptbr.WelcomeLabel2=Este assistente instala o servidor da escola: o programa que fica neste computador e conversa com a catraca, mesmo quando a internet cai.%n%nVocê vai precisar do código de instalação gerado no painel Nexosix.

[Files]
Source: "{#Staging}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Staging}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Staging}\pgsql\*"; DestDir: "{app}\pgsql"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Staging}\servicos\*"; DestDir: "{app}\servicos"; Flags: ignoreversion
Source: "{#Staging}\instalador\*"; DestDir: "{app}\instalador"; Flags: ignoreversion
Source: "{#Staging}\VERSAO.txt"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Atualização: tira o código antigo antes de copiar o novo (arquivo removido numa versão nova
; não pode ficar para trás). O .env e o banco (dados\) ficam.
Type: filesandordirs; Name: "{app}\app\app"
Type: filesandordirs; Name: "{app}\app\worker"
Type: filesandordirs; Name: "{app}\app\integrations"
Type: filesandordirs; Name: "{app}\app\migrations"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\pgsql"
Type: filesandordirs; Name: "{app}\instalador"

[UninstallDelete]
; o que o programa cria rodando (cache do Python, serviços gerados); dados\ só com "apagar dados"
Type: filesandordirs; Name: "{app}\app\app"
Type: filesandordirs; Name: "{app}\app\worker"
Type: filesandordirs; Name: "{app}\app\integrations"
Type: filesandordirs; Name: "{app}\app\migrations"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\pgsql"
Type: filesandordirs; Name: "{app}\servicos"
Type: filesandordirs; Name: "{app}\instalador"

[Run]
Filename: "http://localhost/agente"; Description: "Abrir a página de status do servidor"; Flags: postinstall shellexec nowait skipifsilent; Check: InstalouSemErro

[Code]
var
  PaginaCodigo: TInputQueryWizardPage;
  Codigo, Nuvem, Escola, MensagemFinal: String;
  Atualizacao, Falhou, Aviso, ApagarDados: Boolean;

function Raiz: String;
begin
  Result := ExpandConstant('{sd}\nexosix');
end;

function PowerShell: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

{ Já instalado neste PC (instalação nova, não a antiga do .cmd com .venv) = atualização }
function JaConfigurado: Boolean;
begin
  Result := FileExists(Raiz + '\app\.env') and not DirExists(Raiz + '\app\.venv');
end;

function TemParametro(const Nome: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Nome) = 0 then
      Result := True;
end;

function SoCodigo(S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  S := Uppercase(S);
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if ((C >= 'A') and (C <= 'Z')) or ((C >= '0') and (C <= '9')) then
      Result := Result + C;
  end;
end;

function CodigoFormatado(const C: String): String;
begin
  if Length(C) = 8 then
    Result := Copy(C, 1, 4) + '-' + Copy(C, 5, 4)
  else
    Result := C;
end;

{ Valor de texto de um campo JSON simples. A nuvem responde em UTF-8 sem \u (ensure_ascii=False). }
function CampoJson(const Json, Nome: String): String;
var
  P, I: Integer;
  Chave: String;
begin
  Result := '';
  Chave := '"' + Nome + '"';
  P := Pos(Chave, Json);
  if P = 0 then
    Exit;
  I := P + Length(Chave);
  while (I <= Length(Json)) and ((Json[I] = ' ') or (Json[I] = ':')) do
    I := I + 1;
  if (I > Length(Json)) or (Json[I] <> '"') then
    Exit;
  I := I + 1;
  while (I <= Length(Json)) and (Json[I] <> '"') do
  begin
    if (Json[I] = '\') and (I < Length(Json)) then
      I := I + 1;
    Result := Result + Json[I];
    I := I + 1;
  end;
end;

function LerMensagem(const Arquivo: String): String;
var
  Linhas: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if LoadStringsFromFile(Arquivo, Linhas) then
    for I := 0 to GetArrayLength(Linhas) - 1 do
      Result := Trim(Result + ' ' + Linhas[I]);
end;

{ POST JSON na nuvem. Status 0 = sem conexão (Resposta traz o motivo). }
function PostarNuvem(const Caminho, Corpo: String; var Resposta: String): Integer;
var
  Req: Variant;
begin
  Result := 0;
  try
    Req := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Req.Open('POST', Nuvem + Caminho, False);
    Req.SetTimeouts(15000, 15000, 20000, 20000);
    Req.SetRequestHeader('Content-Type', 'application/json');
    Req.Send(Corpo);
    Result := Req.Status;
    Resposta := Req.ResponseText;
  except
    Resposta := GetExceptionMessage;
  end;
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  Nuvem := ExpandConstant('{param:NUVEM|{#NuvemPadrao}}');
  while (Length(Nuvem) > 0) and (Nuvem[Length(Nuvem)] = '/') do
    Delete(Nuvem, Length(Nuvem), 1);
  Codigo := SoCodigo(ExpandConstant('{param:CODIGO|}'));
  Atualizacao := JaConfigurado;
  if WizardSilent and not Atualizacao and (Length(Codigo) <> 8) then
  begin
    Log('Instalação silenciosa nova sem /CODIGO=XXXX-XXXX: nada a fazer.');
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  PaginaCodigo := CreateInputQueryPage(wpWelcome,
    'Código de instalação', 'De qual escola é este computador?',
    'No painel Nexosix, abra Configuração › Escola › Servidor da escola e clique em ' +
    '"Gerar código de instalação". Digite o código aqui (vale 24 horas e uma instalação).');
  PaginaCodigo.Add('Código:', False);
  PaginaCodigo.Values[0] := CodigoFormatado(Codigo);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = PaginaCodigo.ID) and Atualizacao;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Resposta: String;
  Status: Integer;
begin
  Result := True;
  if CurPageID <> PaginaCodigo.ID then
    Exit;
  { Silencioso: o Inno também "clica Avançar" aqui. A conferência fica com a etapa ativar, que
    tem log e código de saída — um diálogo aqui prenderia a instalação remota para sempre. }
  if WizardSilent then
    Exit;
  Codigo := SoCodigo(PaginaCodigo.Values[0]);
  if Length(Codigo) <> 8 then
  begin
    SuppressibleMsgBox('O código tem 8 letras e números, como ABCD-EFGH.', mbError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  PaginaCodigo.Values[0] := CodigoFormatado(Codigo);
  WizardForm.NextButton.Enabled := False;
  try
    Status := PostarNuvem('/api/espelho/ativar', '{"codigo": "' + Codigo + '", "so_verificar": true}', Resposta);
  finally
    WizardForm.NextButton.Enabled := True;
  end;
  if Status = 200 then
  begin
    Escola := CampoJson(Resposta, 'unidade_nome');
    Result := SuppressibleMsgBox('Instalar o servidor da escola "' + Escola + '" neste computador?', mbConfirmation, MB_YESNO, IDYES) = IDYES;
  end
  else if Status = 0 then
  begin
    SuppressibleMsgBox('Não consegui falar com a nuvem Nexosix (' + Nuvem + ').' + #13#10#13#10 +
      'Confira a internet deste computador e tente de novo.' + #13#10#13#10 + 'Detalhe: ' + Resposta, mbError, MB_OK, IDOK);
    Result := False;
  end
  else
  begin
    if CampoJson(Resposta, 'erro') <> '' then
      SuppressibleMsgBox(CampoJson(Resposta, 'erro'), mbError, MB_OK, IDOK)
    else
      SuppressibleMsgBox('A nuvem respondeu com erro ' + IntToStr(Status) + '. Tente de novo em instantes.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  if Atualizacao then
    Result := 'Atualizar o servidor da escola já instalado neste computador.' + NewLine +
      'Os dados e a configuração são mantidos; o que ainda não subiu para a nuvem é enviado antes.'
  else
    Result := 'Escola: ' + Escola + NewLine + 'Código: ' + CodigoFormatado(Codigo);
  Result := Result + NewLine + NewLine + 'Pasta: ' + Raiz + NewLine +
    'Serviços: NexosixBanco, NexosixAgente, NexosixWorker (iniciam com o Windows)' + NewLine +
    'Porta liberada no firewall: 80 (a catraca chama este computador)';
end;

{ Atualização: sobe o que falta para a nuvem e para os serviços antes de trocar os arquivos }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  R: Integer;
  Script: String;
begin
  Result := '';
  Script := Raiz + '\instalador\remover.ps1';
  if FileExists(Script) then
  begin
    Log('Parando o servidor instalado antes de atualizar');
    if not Exec(PowerShell, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" -SoParar',
      Raiz + '\instalador', SW_HIDE, ewWaitUntilTerminated, R) then
      Log('Não consegui rodar remover.ps1: ' + SysErrorMessage(R))
    else
      Log('remover.ps1 -SoParar terminou com código ' + IntToStr(R));
  end;
end;

function NomeEtapa(I: Integer): String;
begin
  case I of
    0: Result := 'preparar';
    1: Result := 'ativar';
    2: Result := 'banco';
    3: Result := 'programa';
    4: Result := 'servicos';
  else
    Result := 'conferir';
  end;
end;

function TextoEtapa(I: Integer): String;
begin
  case I of
    0: Result := 'Preparando o computador (permissões, relógio, energia)...';
    1: Result := 'Ativando com o código de instalação...';
    2: Result := 'Configurando o banco de dados local...';
    3: Result := 'Preparando o programa...';
    4: Result := 'Registrando os serviços do Windows e o firewall...';
  else
    Result := 'Conferindo o funcionamento e baixando o cadastro da escola (até 3 min)...';
  end;
end;

procedure ConfigurarServidor;
var
  I, R: Integer;
  Parametros: String;
begin
  Falhou := False;
  Aviso := False;
  WizardForm.ProgressGauge.Style := npbstMarquee;
  try
    for I := 0 to 5 do
    begin
      WizardForm.StatusLabel.Caption := TextoEtapa(I);
      WizardForm.FilenameLabel.Caption := '';
      Parametros := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Raiz + '\instalador\configurar.ps1" -Etapa ' + NomeEtapa(I);
      { Atualização não tem código: "-Codigo" sem valor faria o PowerShell recusar o comando inteiro }
      if (I = 1) and (Codigo <> '') then
        Parametros := Parametros + ' -Codigo "' + Codigo + '"';
      if I = 1 then
        Parametros := Parametros + ' -Nuvem "' + Nuvem + '"';
      Log('Etapa ' + NomeEtapa(I));
      if not Exec(PowerShell, Parametros, Raiz + '\instalador', SW_HIDE, ewWaitUntilTerminated, R) then
      begin
        MensagemFinal := 'Não consegui iniciar o PowerShell: ' + SysErrorMessage(R);
        Falhou := True;
        Break;
      end;
      Log('Etapa ' + NomeEtapa(I) + ' terminou com código ' + IntToStr(R));
      if R = 2 then
      begin
        Aviso := True;
        MensagemFinal := LerMensagem(Raiz + '\logs\instalacao-erro.txt');
      end
      else if R <> 0 then
      begin
        Falhou := True;
        MensagemFinal := LerMensagem(Raiz + '\logs\instalacao-erro.txt');
        if MensagemFinal = '' then
          MensagemFinal := 'A etapa "' + NomeEtapa(I) + '" falhou (código ' + IntToStr(R) + ').';
        Break;
      end;
    end;
  finally
    WizardForm.ProgressGauge.Style := npbstNormal;
  end;
  if Escola = '' then
    Escola := LerMensagem(Raiz + '\logs\escola.txt');
  if Falhou then
  begin
    Log('FALHOU: ' + MensagemFinal);
    SuppressibleMsgBox('A instalação não terminou:' + #13#10#13#10 + MensagemFinal + #13#10#13#10 +
      'Detalhes em ' + Raiz + '\logs\instalacao.log' + #13#10 +
      'Depois de resolver, rode este instalador de novo: ele continua de onde parou.', mbError, MB_OK, IDOK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    ConfigurarServidor;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then
    Exit;
  if Falhou then
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'A instalação não terminou';
    WizardForm.FinishedLabel.Caption := MensagemFinal + #13#10#13#10 + 'Detalhes em ' + Raiz + '\logs\instalacao.log. ' +
      'Rode o instalador de novo depois de resolver.';
  end
  else
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'Servidor da escola instalado';
    WizardForm.FinishedLabel.Caption := 'Escola: ' + Escola + #13#10#13#10 +
      'Os serviços iniciam sozinhos com o Windows. Em instantes o painel mostra "Conectado".' + #13#10#13#10 +
      'Reserve no roteador o IP deste computador (' + LerMensagem(Raiz + '\logs\ip.txt') + ') para ele não mudar.' + #13#10#13#10 +
      'Próximo passo, no painel: Configuração › Catraca › cadastrar a catraca com o IP dela › Testar conexão › Configurar Monitor.';
    if Aviso then
      WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + 'Atenção: ' + MensagemFinal;
  end;
end;

function InstalouSemErro: Boolean;
begin
  Result := not Falhou;
end;

function GetCustomSetupExitCode: Integer;
begin
  if Falhou then
    Result := 10
  else
    Result := 0;
end;

{ ---------------------------------------------------------------- desinstalação }

function InitializeUninstall: Boolean;
begin
  Result := True;
  ApagarDados := TemParametro('/APAGARDADOS');
  if not UninstallSilent then
    ApagarDados := MsgBox('Apagar também o banco local deste computador (cadastro e fotos dos alunos)?' + #13#10#13#10 +
      'Sim: recomendado se o computador vai sair da escola (LGPD). O que ainda não subiu para a nuvem é enviado antes.' + #13#10#13#10 +
      'Não: mantém os dados; instalar de novo neste computador continua de onde parou, sem pedir código.',
      mbConfirmation, MB_YESNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  R: Integer;
  Parametros: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Parametros := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Raiz + '\instalador\remover.ps1"';
    if ApagarDados then
      Parametros := Parametros + ' -ApagarDados';
    if not Exec(PowerShell, Parametros, Raiz + '\instalador', SW_HIDE, ewWaitUntilTerminated, R) or (R <> 0) then
      SuppressibleMsgBox('Não consegui remover tudo (código ' + IntToStr(R) + '). Veja ' + Raiz + '\logs\instalacao.log.',
        mbError, MB_OK, IDOK);
  end;
  if (CurUninstallStep = usPostUninstall) and ApagarDados then
    DelTree(Raiz, True, True, True);
end;
