/**
 * Tela "Pós-venda": mensagens e reclamações/devoluções aguardando um
 * atendente. A IA deixa uma sugestão pronta (resposta_sugerida) já no
 * campo de texto -- o atendente edita à vontade antes de enviar, mas
 * nada sai sozinho.
 */

function formatarData(isoString) {
  if (!isoString) return "—";
  const data = new Date(isoString);
  return data.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function nomeDoTipo(tipo) {
  return tipo === "reclamacao" ? "Reclamação/Devolução" : "Mensagem";
}

async function carregarFila() {
  const resposta = await fetch("/api/pos-venda/fila");
  if (!resposta.ok) throw new Error(`Falha ao carregar fila (status ${resposta.status})`);
  return resposta.json();
}

async function carregarResolvidas() {
  const resposta = await fetch("/api/pos-venda/resolvidas");
  if (!resposta.ok) throw new Error(`Falha ao carregar resolvidas (status ${resposta.status})`);
  return resposta.json();
}

function renderizarFila(itens) {
  const container = document.getElementById("lista-fila");
  container.innerHTML = "";

  if (itens.length === 0) {
    container.innerHTML = `<p class="cmx-grafico-nota">Nenhuma mensagem na fila no momento. 🎉</p>`;
    return;
  }

  for (const item of itens) {
    const cartao = document.createElement("div");
    cartao.className = "cmx-cartao-pergunta";
    const selo = item.tipo === "reclamacao" ? "cmx-selo-alerta" : "cmx-selo-aberto";
    cartao.innerHTML = `
      <div class="cmx-cartao-pergunta-cabecalho">
        <span class="cmx-selo ${selo}">${item.conta}</span>
        <span class="cmx-cartao-pergunta-sku">${nomeDoTipo(item.tipo)}${item.sku ? ` · SKU: ${item.sku}` : ""}</span>
        <span class="cmx-cartao-pergunta-data">${formatarData(item.recebida_em)}</span>
      </div>
      <p class="cmx-cartao-pergunta-texto">${item.texto}</p>
      <form class="cmx-form-inline" data-form-responder="${item.id}">
        <input type="text" placeholder="Digite a resposta..." required autocomplete="off" value="${item.resposta_sugerida ? item.resposta_sugerida.replace(/"/g, "&quot;") : ""}" />
        <button type="submit" class="cmx-botao-primario">Enviar resposta</button>
      </form>
      ${item.resposta_sugerida ? `<p class="cmx-grafico-nota">💡 Sugestão da IA já preenchida no campo -- edite antes de enviar, se quiser.</p>` : ""}
    `;
    container.appendChild(cartao);
  }

  container.querySelectorAll("[data-form-responder]").forEach((form) => {
    form.addEventListener("submit", (evento) => {
      evento.preventDefault();
      enviarResposta(form);
    });
  });
}

async function enviarResposta(form) {
  const itemId = form.getAttribute("data-form-responder");
  const input = form.querySelector("input");
  const texto = input.value.trim();
  if (!texto) return;

  const botao = form.querySelector("button");
  botao.disabled = true;
  botao.textContent = "Enviando...";

  try {
    const resposta = await fetch(`/api/pos-venda/${itemId}/responder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto }),
    });
    if (!resposta.ok) {
      const erro = await resposta.json().catch(() => ({}));
      throw new Error(erro.detail || `status ${resposta.status}`);
    }
    await atualizarTudo();
  } catch (erro) {
    mostrarAviso(`Não foi possível enviar essa resposta: ${erro.message}`);
    console.error(erro);
    botao.disabled = false;
    botao.textContent = "Enviar resposta";
  }
}

function renderizarResolvidas(itens) {
  const corpo = document.querySelector("#tabela-resolvidas tbody");
  corpo.innerHTML = "";

  if (itens.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="6" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhuma mensagem respondida ainda.
      </td></tr>`;
    return;
  }

  for (const i of itens) {
    const linha = document.createElement("tr");
    linha.innerHTML = `
      <td>${i.conta}</td>
      <td>${nomeDoTipo(i.tipo)}</td>
      <td>${i.sku || "—"}</td>
      <td>${i.texto}</td>
      <td>${i.resposta_enviada || "—"}</td>
      <td>${formatarData(i.respondida_em)}</td>
    `;
    corpo.appendChild(linha);
  }
}

function mostrarAviso(mensagem) {
  const aviso = document.getElementById("fila-aviso");
  aviso.textContent = mensagem;
  aviso.classList.add("cmx-aviso-erro");
  aviso.style.display = "block";
}

async function atualizarTudo() {
  try {
    const [fila, resolvidas] = await Promise.all([carregarFila(), carregarResolvidas()]);
    renderizarFila(fila);
    renderizarResolvidas(resolvidas);
  } catch (erro) {
    mostrarAviso("Não foi possível carregar a fila de pós-venda.");
    console.error(erro);
  }
}

document.addEventListener("DOMContentLoaded", atualizarTudo);
