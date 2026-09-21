/**
 * Tela "Cancelamentos": todo pedido de cancelamento identificado
 * (dentro de mensagens ou reclamações) cai aqui, com uma sugestão de
 * resposta da IA. 100% manual por enquanto -- só registra o que o
 * atendente decidiu/fez (aprovar, negar, orientar), sem cancelar nem
 * enviar nada pela API sozinho.
 */

function formatarData(isoString) {
  if (!isoString) return "—";
  const data = new Date(isoString);
  return data.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

async function carregarPendentes() {
  const resposta = await fetch("/api/cancelamentos/lista?status=pendente");
  if (!resposta.ok) throw new Error(`Falha ao carregar pendentes (status ${resposta.status})`);
  return resposta.json();
}

async function carregarTratados() {
  const resposta = await fetch("/api/cancelamentos/lista?status=tratado");
  if (!resposta.ok) throw new Error(`Falha ao carregar tratados (status ${resposta.status})`);
  return resposta.json();
}

function renderizarPendentes(pedidos) {
  const container = document.getElementById("lista-pendentes");
  container.innerHTML = "";

  if (pedidos.length === 0) {
    container.innerHTML = `<p class="cmx-grafico-nota">Nenhum pedido de cancelamento pendente. 🎉</p>`;
    return;
  }

  for (const pedido of pedidos) {
    const cartao = document.createElement("div");
    cartao.className = "cmx-cartao-pergunta";
    cartao.innerHTML = `
      <div class="cmx-cartao-pergunta-cabecalho">
        <span class="cmx-selo cmx-selo-alerta">${pedido.conta}</span>
        <span class="cmx-cartao-pergunta-sku">${pedido.sku ? `SKU: ${pedido.sku}` : (pedido.order_id || "sem pedido")}</span>
        <span class="cmx-cartao-pergunta-data">${formatarData(pedido.criado_em)}</span>
      </div>
      <p class="cmx-cartao-pergunta-texto"><strong>Pedido do cliente:</strong> ${pedido.texto_pedido}</p>
      ${pedido.resposta_sugerida ? `<p class="cmx-grafico-nota">💡 Sugestão da IA (só orientação, não confirma cancelamento): "${pedido.resposta_sugerida}"</p>` : ""}
      <form class="cmx-form-inline" data-form-tratar="${pedido.id}">
        <input type="text" placeholder="O que foi decidido/feito? (ex: cancelado manualmente pelo Mercado Livre)" required autocomplete="off" />
        <button type="submit" class="cmx-botao-primario">Marcar como tratado</button>
      </form>
    `;
    container.appendChild(cartao);
  }

  container.querySelectorAll("[data-form-tratar]").forEach((form) => {
    form.addEventListener("submit", (evento) => {
      evento.preventDefault();
      tratarPedido(form);
    });
  });
}

async function tratarPedido(form) {
  const pedidoId = form.getAttribute("data-form-tratar");
  const input = form.querySelector("input");
  const observacao = input.value.trim();
  if (!observacao) return;

  const botao = form.querySelector("button");
  botao.disabled = true;
  botao.textContent = "Salvando...";

  try {
    const resposta = await fetch(`/api/cancelamentos/${pedidoId}/tratar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ observacao }),
    });
    if (!resposta.ok) {
      const erro = await resposta.json().catch(() => ({}));
      throw new Error(erro.detail || `status ${resposta.status}`);
    }
    await atualizarTudo();
  } catch (erro) {
    mostrarAviso(`Não foi possível registrar: ${erro.message}`);
    console.error(erro);
    botao.disabled = false;
    botao.textContent = "Marcar como tratado";
  }
}

function renderizarTratados(pedidos) {
  const corpo = document.querySelector("#tabela-tratados tbody");
  corpo.innerHTML = "";

  if (pedidos.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="5" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhum cancelamento tratado ainda.
      </td></tr>`;
    return;
  }

  for (const p of pedidos) {
    const linha = document.createElement("tr");
    linha.innerHTML = `
      <td>${p.conta}</td>
      <td>${p.sku || "—"}</td>
      <td>${p.texto_pedido}</td>
      <td>${p.observacao_humano || "—"}</td>
      <td>${formatarData(p.tratado_em)}</td>
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
    const [pendentes, tratados] = await Promise.all([carregarPendentes(), carregarTratados()]);
    renderizarPendentes(pendentes);
    renderizarTratados(tratados);
  } catch (erro) {
    mostrarAviso("Não foi possível carregar os cancelamentos.");
    console.error(erro);
  }
}

document.addEventListener("DOMContentLoaded", atualizarTudo);
