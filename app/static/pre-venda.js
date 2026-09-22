/**
 * Tela "Pré-venda": mostra a fila de perguntas que caíram pra
 * atendimento humano, e a lista das últimas respondidas (automático
 * ou manual), pra acompanhamento.
 */

/**
 * O servidor grava as datas em UTC (datetime.utcnow) e envia sem fuso
 * (ex: "2026-09-22T19:50:00"). Sem o "Z", o navegador interpreta como
 * horário local e mostra 3h adiantado. Aqui marcamos como UTC quando não
 * houver fuso e exibimos sempre no horário de Brasília.
 */
function formatarData(isoString) {
  if (!isoString) return "—";
  const temFuso = /([zZ]|[+-]\d{2}:?\d{2})$/.test(isoString);
  const data = new Date(temFuso ? isoString : isoString + "Z");
  if (isNaN(data.getTime())) return "—"; // data inválida: não quebra a tela
  return data.toLocaleString("pt-BR", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "America/Sao_Paulo",
  });
}

function nomeDaCamada(camada) {
  const nomes = {
    resposta_validada: "Automática (histórico validado)",
    manual_sku_ia: "Automática (IA + manual)",
    politica_geral: "Automática (política geral)",
    manual: "Atendente",
  };
  return nomes[camada] || "—";
}

async function carregarFila() {
  const resposta = await fetch("/api/pre-venda/fila");
  if (!resposta.ok) throw new Error(`Falha ao carregar fila (status ${resposta.status})`);
  return resposta.json();
}

async function carregarResolvidas() {
  const resposta = await fetch("/api/pre-venda/resolvidas");
  if (!resposta.ok) throw new Error(`Falha ao carregar resolvidas (status ${resposta.status})`);
  return resposta.json();
}

function renderizarFila(perguntas) {
  const container = document.getElementById("lista-fila");
  container.innerHTML = "";

  if (perguntas.length === 0) {
    container.innerHTML = `<p class="cmx-grafico-nota">Nenhuma pergunta na fila no momento. 🎉</p>`;
    return;
  }

  for (const pergunta of perguntas) {
    const cartao = document.createElement("div");
    cartao.className = "cmx-cartao-pergunta";
    cartao.innerHTML = `
      <div class="cmx-cartao-pergunta-cabecalho">
        <span class="cmx-selo cmx-selo-aberto">${pergunta.conta}</span>
        <span class="cmx-cartao-pergunta-sku">${pergunta.sku ? `SKU: ${pergunta.sku}` : (pergunta.item_id || "sem item")}</span>
        <span class="cmx-cartao-pergunta-data">${formatarData(pergunta.recebida_em)}</span>
      </div>
      <p class="cmx-cartao-pergunta-texto">${pergunta.texto}</p>
      <form class="cmx-form-inline" data-form-responder="${pergunta.id}">
        <input type="text" placeholder="Digite a resposta..." required autocomplete="off" />
        <button type="submit" class="cmx-botao-primario">Enviar resposta</button>
      </form>
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
  const perguntaId = form.getAttribute("data-form-responder");
  const input = form.querySelector("input");
  const texto = input.value.trim();
  if (!texto) return;

  const botao = form.querySelector("button");
  botao.disabled = true;
  botao.textContent = "Enviando...";

  try {
    const resposta = await fetch(`/api/pre-venda/${perguntaId}/responder`, {
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
    // O servidor pode já ter resolvido o item por fora (ex.: pergunta respondida
    // por outro canal no Mercado Livre) mesmo quando a chamada retorna erro --
    // por isso sempre recarrega a fila aqui, senão o item some do banco mas
    // continua preso na tela.
    await atualizarTudo();
  }
}

function renderizarResolvidas(perguntas) {
  const corpo = document.querySelector("#tabela-resolvidas tbody");
  corpo.innerHTML = "";

  if (perguntas.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="6" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhuma pergunta respondida ainda.
      </td></tr>`;
    return;
  }

  for (const p of perguntas) {
    const linha = document.createElement("tr");
    const avisoAuditoria = p.precisa_auditoria
      ? ` <span class="cmx-selo cmx-selo-alerta" title="Resposta gerada pela IA -- revisar quando puder">revisar</span>`
      : "";
    linha.innerHTML = `
      <td>${p.conta}</td>
      <td>${p.sku || "—"}</td>
      <td>${p.texto}</td>
      <td>${p.resposta_enviada || "—"}${avisoAuditoria}</td>
      <td>${nomeDaCamada(p.camada_resolvida)}</td>
      <td>${formatarData(p.respondida_em)}</td>
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
    mostrarAviso("Não foi possível carregar a fila de pré-venda.");
    console.error(erro);
  }
}

document.addEventListener("DOMContentLoaded", atualizarTudo);
