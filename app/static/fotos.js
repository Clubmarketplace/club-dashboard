/*
 * Fotos (26/10) -- compressão única usada em todos os formulários que enviam foto.
 *
 * O celular tira a foto em alta resolução; AQUI ela é reduzida ANTES de enviar,
 * então a original nunca chega ao servidor (não pesa no 4G do galpão nem no banco).
 *   - lado maior: 1280 px (dá pra ler etiqueta/número de série e ver avaria);
 *   - formato: WebP quando o aparelho codifica WebP, senão JPEG;
 *   - meta de peso: ~80 KB. Começa em qualidade 0,7 e desce até 0,5; se ainda passar,
 *     reduz a imagem pra 1024 px e tenta de novo. O resultado final é o menor obtido.
 *
 * Uso:  const dataUrl = await window.comprimirFoto(arquivo);   // lança Error se não for imagem
 * O servidor também recusa foto acima de um teto (rede de segurança contra banco cheio).
 */
(function () {
  const ALVO_BYTES = 80 * 1024;
  const LADOS = [1280, 1024];
  const QUALIDADES = [0.7, 0.62, 0.55, 0.5];

  let webpOk = null;
  function suportaWebp() {
    if (webpOk === null) {
      const c = document.createElement("canvas");
      c.width = c.height = 1;
      webpOk = c.toDataURL("image/webp").indexOf("data:image/webp") === 0;   // Safari antigo devolve PNG
    }
    return webpOk;
  }

  const bytesDe = (url) => Math.floor(((url.length - url.indexOf(",") - 1) * 3) / 4);

  function abrirImagem(arquivo) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(arquivo);   // mais leve que FileReader pra foto de 12 MP
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Não consegui abrir essa imagem.")); };
      img.src = url;
    });
  }

  window.comprimirFoto = async function (arquivo) {
    if (!arquivo || !arquivo.type || arquivo.type.indexOf("image/") !== 0) {
      throw new Error("O arquivo precisa ser uma imagem.");
    }
    const img = await abrirImagem(arquivo);
    const formato = suportaWebp() ? "image/webp" : "image/jpeg";
    const maior = Math.max(img.width, img.height);
    let menor = null;
    for (const lado of LADOS) {
      const escala = maior > lado ? lado / maior : 1;
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(img.width * escala));
      canvas.height = Math.max(1, Math.round(img.height * escala));
      const ctx = canvas.getContext("2d");
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      for (const q of QUALIDADES) {
        const url = canvas.toDataURL(formato, q);
        if (menor === null || url.length < menor.length) menor = url;
        if (bytesDe(url) <= ALVO_BYTES) return url;
      }
    }
    return menor;
  };

  // Aparelho de toque (celular/tablet): só nele faz sentido o botão "Tirar foto agora".
  window.temCamera = function () {
    return "ontouchstart" in window || (navigator.maxTouchPoints || 0) > 0;
  };

  // Prévia ampliada (toque na miniatura): pra conferir se a foto ficou nítida e, se não,
  // removê-la. Cria o próprio overlay (não depende do HTML da página).
  //   window.abrirPreviaFoto(dataUrl, { rotulo: "Foto 2 de 4", onRemover: () => {...} })
  window.abrirPreviaFoto = function (dataUrl, opcoes) {
    opcoes = opcoes || {};
    const fundo = document.createElement("div");
    fundo.setAttribute("role", "dialog");
    fundo.setAttribute("aria-modal", "true");
    fundo.style.cssText = "position:fixed;inset:0;z-index:2000;background:rgba(10,14,18,.9);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;gap:12px;";
    const rotulo = document.createElement("div");
    rotulo.style.cssText = "color:#fff;font:600 14px system-ui,sans-serif;";
    rotulo.textContent = opcoes.rotulo || "Prévia da foto";
    const img = document.createElement("img");
    img.src = dataUrl;
    img.alt = "Prévia da foto";
    img.style.cssText = "max-width:100%;max-height:calc(100% - 120px);object-fit:contain;border-radius:10px;background:#000;";
    const barra = document.createElement("div");
    barra.style.cssText = "display:flex;gap:10px;flex-wrap:wrap;justify-content:center;";
    const estiloBotao = "border:none;border-radius:10px;padding:12px 20px;font:600 15px system-ui,sans-serif;cursor:pointer;";
    const fechar = () => { document.removeEventListener("keydown", aoTeclar); fundo.remove(); };
    const aoTeclar = (e) => { if (e.key === "Escape") fechar(); };
    if (typeof opcoes.onRemover === "function") {
      const rem = document.createElement("button");
      rem.type = "button"; rem.textContent = "🗑 Remover esta foto";
      rem.style.cssText = estiloBotao + "background:#c1502e;color:#fff;";
      rem.addEventListener("click", () => { fechar(); opcoes.onRemover(); });
      barra.appendChild(rem);
    }
    const ok = document.createElement("button");
    ok.type = "button"; ok.textContent = "Fechar";
    ok.style.cssText = estiloBotao + "background:#fff;color:#17202a;";
    ok.addEventListener("click", fechar);
    barra.appendChild(ok);
    fundo.addEventListener("click", (e) => { if (e.target === fundo) fechar(); });
    document.addEventListener("keydown", aoTeclar);
    fundo.append(rotulo, img, barra);
    document.body.appendChild(fundo);
  };
})();
