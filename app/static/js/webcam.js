// Captura pela webcam do PC da secretaria (getUserMedia) → JPEG 640x800 → POST JSON.
// Nada de biblioteca externa: o navegador faz o recorte e a compressão.
function iniciarWebcam(urlSalvar) {
  const video = document.getElementById('camera');
  const tela = document.getElementById('tela');
  const previa = document.getElementById('previa');
  const msg = document.getElementById('msg');
  const bCapturar = document.getElementById('capturar');
  const bRepetir = document.getElementById('repetir');
  const bSalvar = document.getElementById('salvar');
  let dataUrl = null;

  const LARGURA = 480, ALTURA = 600; // retrato 4:5, rosto grande o bastante pra catraca (bounds_width ≥ 60)

  navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' }, audio: false })
    .then(stream => { video.srcObject = stream; msg.textContent = 'Enquadre o rosto na elipse e clique em Capturar.'; bCapturar.disabled = false; })
    .catch(err => { msg.textContent = 'Não foi possível abrir a câmera: ' + err.message + '. Verifique a permissão do navegador (precisa de HTTPS ou localhost).'; msg.className = 'mt-2 small text-danger'; });

  bCapturar.onclick = () => {
    // recorta o centro do vídeo em 4:5 (a elipse-guia está no centro)
    const vw = video.videoWidth, vh = video.videoHeight;
    const alvo = LARGURA / ALTURA;
    let sw = vh * alvo, sh = vh;
    if (sw > vw) { sw = vw; sh = vw / alvo; }
    const sx = (vw - sw) / 2, sy = (vh - sh) / 2;
    tela.width = LARGURA; tela.height = ALTURA;
    const ctx = tela.getContext('2d');
    ctx.drawImage(video, sx, sy, sw, sh, 0, 0, LARGURA, ALTURA);
    dataUrl = tela.toDataURL('image/jpeg', 0.9);
    previa.src = dataUrl; previa.style.display = '';
    bSalvar.disabled = false; bRepetir.disabled = false; bCapturar.disabled = true;
    msg.textContent = 'Confira a prévia. Se estiver boa, salve.';
  };

  bRepetir.onclick = () => { dataUrl = null; previa.style.display = 'none'; bSalvar.disabled = true; bRepetir.disabled = true; bCapturar.disabled = false; };

  bSalvar.onclick = async () => {
    if (!dataUrl) return;
    bSalvar.disabled = true; msg.textContent = 'Enviando…';
    try {
      const r = await fetch(urlSalvar, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ imagem: dataUrl }) });
      const d = await r.json();
      if (!r.ok || !d.ok) { msg.textContent = 'Erro: ' + (d.erro || r.status); msg.className = 'mt-2 small text-danger'; bSalvar.disabled = false; return; }
      msg.textContent = 'Foto salva. Sincronização com as catracas enfileirada.';
      window.location = d.redirect;
    } catch (e) { msg.textContent = 'Falha de rede: ' + e.message; bSalvar.disabled = false; }
  };
}
