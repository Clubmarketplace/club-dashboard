/**
 * Tela "Manuais por SKU": upload direto pelo painel, busca/checagem
 * por SKU, e lista de tudo já cadastrado.
 */

function formatarData(isoString) {
  if (!isoString) return "—";
  const data = new Date(isoString);
  return data.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function mostrarAviso(elementoId, mensagem, ehErro) {
  const el = document.getElementById(elementoId);
  el.textContent = mensagem;
  el.style.display = "block";
  el.style.color = ehErro ? "var(--cmx-rust)" : "var(--cmx-verde)";
}

async function carregarLista(busca) {
  const url = busca ? `/api/manuais/lista?busca=${encodeURIComponent(busca)}` : "/api/manuais/lista";
  const resposta = await fetch(url);
  if (!resposta.ok) throw new Error(`status ${resposta.status}`);
  return resposta.json();
}

function renderizarLista(manuais) {
  document.getElementById("manuais-contador").textContent = `(${manuais.length})`;
  const corpo = document.querySelector("#tabela-manuais tbody");
  corpo.innerHTML = "";

  if (manuais.length === 0) {
    corpo.innerHTML = `<tr><td colspan="4" style="text-align:center; color: var(--cmx-texto-suave);">Nenhum manual encontrado.</td></tr>`;
    return;
  }

  manuais.forEach((m) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${m.sku}</strong></td>
      <td>${m.titulo || "—"}</td>
      <td>${m.tamanho_caracteres.toLocaleString("pt-BR")} caracteres</td>
      <td>${formatarData(m.criado_em)}</td>
    `;
    corpo.appendChild(tr);
  });
}

async function inicializar() {
  async function recarregar(busca) {
    try {
      const manuais = await carregarLista(busca);
      renderizarLista(manuais);
    } catch (erro) {
      console.error(erro);
    }
  }

  await recarregar();

  document.getElementById("form-upload").addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const sku = document.getElementById("input-sku").value.trim();
    const arquivoInput = document.getElementById("input-arquivo");
    if (!sku || !arquivoInput.files.length) return;

    const dadosForm = new FormData();
    dadosForm.append("sku", sku);
    dadosForm.append("arquivo", arquivoInput.files[0]);

    mostrarAviso("upload-aviso", "Enviando e lendo o arquivo...", false);
    try {
      const resposta = await fetch("/api/manuais/upload", { method: "POST", body: dadosForm });
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.detail || `status ${resposta.status}`);

      mostrarAviso("upload-aviso", `Manual de "${dados.sku}" cadastrado! (${dados.tamanho_caracteres.toLocaleString("pt-BR")} caracteres extraídos)`, false);
      document.getElementById("input-sku").value = "";
      arquivoInput.value = "";
      await recarregar();
    } catch (erro) {
      mostrarAviso("upload-aviso", `Erro: ${erro.message}`, true);
    }
  });

  document.getElementById("form-busca").addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const sku = document.getElementById("input-busca").value.trim();
    if (!sku) return;

    try {
      const resposta = await fetch(`/api/manuais/existe/${encodeURIComponent(sku)}`);
      const dados = await resposta.json();
      const resultadoEl = document.getElementById("busca-resultado");
      resultadoEl.textContent = dados.tem_manual
        ? `✅ O SKU "${sku}" já tem manual cadastrado.`
        : `⚠️ O SKU "${sku}" ainda NÃO tem manual cadastrado.`;
      resultadoEl.style.color = dados.tem_manual ? "var(--cmx-verde)" : "var(--cmx-rust)";
    } catch (erro) {
      console.error(erro);
    }

    await recarregar(sku);
  });

  document.getElementById("btn-limpar-busca").addEventListener("click", async () => {
    document.getElementById("input-busca").value = "";
    document.getElementById("busca-resultado").textContent = "";
    await recarregar();
  });
}

document.addEventListener("DOMContentLoaded", inicializar);
