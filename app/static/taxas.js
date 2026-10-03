(function () {
  "use strict";

  const API_TAXAS = "/api/painel/taxas";

  const RÓTULO_BASE = {
    venda_bruta: "Venda Bruta",
    repasse: "Repasse",
    lucro: "Lucro",
  };

  const listaTaxas = document.getElementById("lista-taxas");
  const btnAbrirModal = document.getElementById("btn-abrir-modal");
  const btnCancelarModal = document.getElementById("btn-cancelar-modal");
  const btnSalvarModal = document.getElementById("btn-salvar-modal");
  const modalTaxa = document.getElementById("modal-taxa");
  const campoNome = document.getElementById("campo-nome");
  const campoPercentual = document.getElementById("campo-percentual");
  const msgModal = document.getElementById("msg-modal");
  const pillsBase = document.querySelectorAll(".pill-base");

  let baseSelecionada = "venda_bruta";

  function formatarPercentual(valor) {
    if (valor === null || valor === undefined) return "-";
    return Number(valor).toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 2 }) + "%";
  }

  function abrirModal() {
    campoNome.value = "";
    campoPercentual.value = "";
    msgModal.textContent = "";
    msgModal.className = "msg";
    selecionarBase("venda_bruta");
    modalTaxa.classList.add("aberto");
    campoNome.focus();
  }

  function fecharModal() {
    modalTaxa.classList.remove("aberto");
  }

  function selecionarBase(base) {
    baseSelecionada = base;
    pillsBase.forEach((p) => p.classList.toggle("ativa", p.dataset.base === base));
  }

  pillsBase.forEach((p) => {
    p.addEventListener("click", function () {
      selecionarBase(p.dataset.base);
    });
  });

  btnAbrirModal.addEventListener("click", abrirModal);
  btnCancelarModal.addEventListener("click", fecharModal);
  modalTaxa.addEventListener("click", function (e) {
    if (e.target === modalTaxa) fecharModal();
  });

  function carregarTaxas() {
    fetch(API_TAXAS)
      .then((r) => r.json())
      .then((dados) => renderizarTaxas(dados.itens || []))
      .catch(() => {
        listaTaxas.innerHTML = '<p class="vazio-taxas">⚠ Não consegui carregar suas taxas agora.</p>';
      });
  }

  function renderizarTaxas(itens) {
    listaTaxas.innerHTML = "";
    if (!itens.length) {
      listaTaxas.innerHTML = '<p class="vazio-taxas">Nenhuma taxa cadastrada ainda. Clique em "+ Adicionar variável" pra começar.</p>';
      return;
    }
    itens.forEach((item) => {
      const cartao = document.createElement("div");
      cartao.className = "cartao-taxa";
      const base = item.base_calculo || "venda_bruta";
      cartao.innerHTML =
        '<div><span class="nome">' + item.nome + "</span>" +
        '<span class="base-selo">' + (RÓTULO_BASE[base] || base) + "</span></div>" +
        '<div class="acoes"><span class="percentual">' + formatarPercentual(item.percentual) + "</span>" +
        '<button class="remover" data-nome="' + item.nome + '">remover</button></div>';
      listaTaxas.appendChild(cartao);
    });
    listaTaxas.querySelectorAll(".remover").forEach((botao) => {
      botao.addEventListener("click", function () {
        if (!window.confirm('Remover a taxa "' + botao.dataset.nome + '"?')) return;
        fetch(API_TAXAS + "/" + encodeURIComponent(botao.dataset.nome), { method: "DELETE" }).then(carregarTaxas);
      });
    });
  }

  btnSalvarModal.addEventListener("click", function () {
    const nome = campoNome.value.trim();
    const percentual = campoPercentual.value.trim().replace(",", ".");
    msgModal.textContent = "";
    if (!nome || percentual === "") {
      msgModal.textContent = "Preencha nome e porcentagem.";
      msgModal.className = "msg erro";
      return;
    }
    if (isNaN(Number(percentual)) || Number(percentual) < 0) {
      msgModal.textContent = "Porcentagem inválida.";
      msgModal.className = "msg erro";
      return;
    }
    fetch(API_TAXAS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nome: nome, percentual: Number(percentual), base_calculo: baseSelecionada }),
    })
      .then((r) => {
        if (!r.ok) return r.json().then((d) => Promise.reject(d));
        fecharModal();
        carregarTaxas();
      })
      .catch((erro) => {
        msgModal.textContent = (erro && erro.detail) || "Não consegui salvar agora.";
        msgModal.className = "msg erro";
      });
  });

  carregarTaxas();
})();
