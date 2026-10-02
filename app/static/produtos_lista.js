// Tela "Produtos > Lista" do painel (seller) -- junta o custo (nosso
// banco) com o estoque ao vivo (lido do Mercado Livre pelo servidor em
// /api/painel/produtos/lista) e permite criar/editar/remover o custo de
// um SKU (reaproveitando /api/painel/custos, igual à tela "Custos").
(function () {
  "use strict";

  const API_LISTA = "/api/painel/produtos/lista";
  const API_ATUALIZAR_ESTOQUE = "/api/painel/produtos/lista/atualizar-estoque";
  const API_CUSTOS = "/api/painel/custos";

  const campoBusca = document.getElementById("busca-sku");
  const btnBuscar = document.getElementById("btn-buscar");
  const btnAtualizarEstoque = document.getElementById("btn-atualizar-estoque");
  const btnNovo = document.getElementById("btn-novo");
  const estoqueAtualizadoEm = document.getElementById("estoque-atualizado-em");
  const tabela = document.getElementById("tabela-resultados");
  const corpoTabela = document.getElementById("corpo-resultados");
  const mensagemVazia = document.getElementById("mensagem-vazia");
  const avisoEstoque = document.getElementById("aviso-estoque");

  const cardEstoqueTotal = document.getElementById("card-estoque-total");
  const cardValorTotal = document.getElementById("card-valor-total");
  const cardSemEstoque = document.getElementById("card-sem-estoque");
  const cardSemCusto = document.getElementById("card-sem-custo");
  const cardTotalSkus = document.getElementById("card-total-skus");
  const cardSemCustoClicavel = document.getElementById("card-sem-custo-clicavel");
  const filtroAtivoAviso = document.getElementById("filtro-ativo-aviso");
  const linkLimparFiltro = document.getElementById("link-limpar-filtro");

  const formEdicao = document.getElementById("form-edicao");
  const tituloForm = document.getElementById("titulo-form");
  const campoSku = document.getElementById("campo-sku-valor");
  const campoCusto = document.getElementById("campo-custo-valor");
  const campoNome = document.getElementById("campo-nome-valor");
  const btnSalvar = document.getElementById("btn-salvar-custo");
  const btnCancelar = document.getElementById("btn-cancelar-edicao");
  const msgSalvar = document.getElementById("msg-salvar");

  let editandoSkuOriginal = null; // null = criando novo
  let ultimosItens = []; // última lista recebida do servidor, pra aplicar o filtro "sem custo" sem precisar buscar de novo
  let filtroSemCusto = false;

  function formatarMoeda(valor) {
    if (valor === null || valor === undefined) return "-";
    return "R$ " + Number(valor).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatarQuantidade(valor) {
    if (valor === null || valor === undefined) return "-";
    return Number(valor).toLocaleString("pt-BR");
  }

  function abrirFormulario(item) {
    if (item) {
      editandoSkuOriginal = item.sku;
      tituloForm.textContent = "Editar custo";
      campoSku.value = item.sku;
      campoSku.readOnly = true; // SKU não muda numa edição -- criaria uma linha nova em vez de atualizar
      campoCusto.value = item.custo !== null ? item.custo : "";
      campoNome.value = item.nome_produto || "";
    } else {
      editandoSkuOriginal = null;
      tituloForm.textContent = "Novo produto";
      campoSku.value = "";
      campoSku.readOnly = false;
      campoCusto.value = "";
      campoNome.value = "";
    }
    msgSalvar.textContent = "";
    msgSalvar.className = "msg";
    formEdicao.style.display = "block";
    formEdicao.scrollIntoView({ behavior: "smooth", block: "center" });
    (item ? campoCusto : campoSku).focus();
  }

  function fecharFormulario() {
    formEdicao.style.display = "none";
    editandoSkuOriginal = null;
  }

  function renderizarResumo(resumo, estoqueIndisponivel) {
    cardEstoqueTotal.textContent = estoqueIndisponivel ? "-" : formatarQuantidade(resumo.estoque_total);
    cardValorTotal.textContent = formatarMoeda(resumo.valor_total_estoque);
    cardSemEstoque.textContent = estoqueIndisponivel ? "-" : formatarQuantidade(resumo.sem_estoque);
    cardSemCusto.textContent = formatarQuantidade(resumo.sem_custo);
    cardTotalSkus.textContent = formatarQuantidade(resumo.total_skus);
  }

  function renderizarResultados(itens) {
    corpoTabela.innerHTML = "";
    if (itens.length === 0) {
      tabela.style.display = "none";
      mensagemVazia.style.display = "block";
      return;
    }
    mensagemVazia.style.display = "none";
    tabela.style.display = "table";

    itens.forEach(function (item) {
      const tr = document.createElement("tr");

      const tdSku = document.createElement("td");
      tdSku.textContent = item.sku;
      if (item.custo === null) {
        const tag = document.createElement("span");
        tag.className = "tag-sem-custo";
        tag.textContent = "sem custo";
        tdSku.appendChild(tag);
      }
      if (item.quantidade === 0) {
        const tag = document.createElement("span");
        tag.className = "tag-sem-estoque";
        tag.textContent = "sem estoque";
        tdSku.appendChild(tag);
      }
      tr.appendChild(tdSku);

      const tdNome = document.createElement("td");
      tdNome.textContent = item.nome_produto || "-";
      tr.appendChild(tdNome);

      const tdCusto = document.createElement("td");
      tdCusto.className = "col-numero";
      tdCusto.textContent = formatarMoeda(item.custo);
      tr.appendChild(tdCusto);

      const tdQuantidade = document.createElement("td");
      tdQuantidade.className = "col-numero";
      tdQuantidade.textContent = formatarQuantidade(item.quantidade);
      tr.appendChild(tdQuantidade);

      const tdValor = document.createElement("td");
      tdValor.className = "col-numero";
      tdValor.textContent = formatarMoeda(item.valor_total);
      tr.appendChild(tdValor);

      const tdAcoes = document.createElement("td");
      tdAcoes.className = "col-acoes";

      const btnEditar = document.createElement("button");
      btnEditar.className = "btn btn-secundario";
      btnEditar.textContent = item.custo === null ? "Definir custo" : "Editar";
      btnEditar.onclick = function () { abrirFormulario(item); };
      tdAcoes.appendChild(btnEditar);

      tr.appendChild(tdAcoes);
      corpoTabela.appendChild(tr);
    });
  }

  function formatarAtualizadoEm(isoString) {
    if (!isoString) return "";
    // O backend manda em UTC "cru" (sem fuso) -- igual ao resto do painel.
    const data = new Date(isoString.endsWith("Z") ? isoString : isoString + "Z");
    if (isNaN(data.getTime())) return "";
    const diffMinutos = Math.round((Date.now() - data.getTime()) / 60000);
    let quando;
    if (diffMinutos < 1) quando = "agora mesmo";
    else if (diffMinutos < 60) quando = "há " + diffMinutos + " min";
    else quando = "às " + data.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    return "Estoque atualizado " + quando + ".";
  }

  // Reaplica o filtro "só sem custo" (se estiver ativo) em cima da
  // última lista recebida, sem precisar buscar no servidor de novo.
  function aplicarFiltroEExibir() {
    filtroAtivoAviso.style.display = filtroSemCusto ? "block" : "none";
    const itensExibidos = filtroSemCusto ? ultimosItens.filter(function (i) { return i.custo === null; }) : ultimosItens;
    renderizarResultados(itensExibidos);
  }

  function renderizarDados(dados) {
    avisoEstoque.classList.toggle("visivel", !!dados.aviso_estoque);
    if (dados.aviso_estoque) avisoEstoque.textContent = "⚠️ " + dados.aviso_estoque;
    renderizarResumo(dados.resumo || {}, dados.estoque_indisponivel);
    ultimosItens = dados.itens || [];
    aplicarFiltroEExibir();
    estoqueAtualizadoEm.textContent = formatarAtualizadoEm(dados.estoque_atualizado_em);
  }

  function buscar() {
    filtroSemCusto = false; // pesquisar por SKU sempre sai do modo "só sem custo"
    const termo = campoBusca.value.trim();
    const url = termo ? API_LISTA + "?sku=" + encodeURIComponent(termo) : API_LISTA;
    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error("Não consegui buscar os produtos.");
        return r.json();
      })
      .then(renderizarDados)
      .catch(function (erro) {
        corpoTabela.innerHTML = "";
        tabela.style.display = "none";
        mensagemVazia.style.display = "block";
        mensagemVazia.textContent = erro.message || "Não consegui buscar os produtos.";
      });
  }

  function mostrarApenasSemCusto() {
    filtroSemCusto = true;
    campoBusca.value = "";
    aplicarFiltroEExibir();
  }

  function atualizarEstoqueAgora() {
    btnAtualizarEstoque.disabled = true;
    btnAtualizarEstoque.textContent = "Atualizando...";
    fetch(API_ATUALIZAR_ESTOQUE, { method: "POST" })
      .then(function (r) {
        if (!r.ok) throw new Error("Não consegui atualizar o estoque agora.");
        return r.json();
      })
      .then(renderizarDados)
      .catch(function (erro) { window.alert(erro.message); })
      .finally(function () {
        btnAtualizarEstoque.disabled = false;
        btnAtualizarEstoque.textContent = "↻ Atualizar estoque agora";
      });
  }

  function removerCusto(sku) {
    if (!window.confirm('Remover o custo do SKU "' + sku + '"?')) return;
    fetch(API_CUSTOS + "/" + encodeURIComponent(sku), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) throw new Error("Não consegui remover esse SKU.");
        buscar();
      })
      .catch(function (erro) { window.alert(erro.message); });
  }

  function salvarCustoAtual() {
    const sku = campoSku.value.trim();
    const custo = parseFloat(String(campoCusto.value).replace(",", "."));
    const nome = campoNome.value.trim();

    if (!sku) {
      msgSalvar.textContent = "Informe o SKU.";
      msgSalvar.className = "msg erro";
      return;
    }
    if (!custo || custo <= 0) {
      msgSalvar.textContent = "Informe um custo maior que zero.";
      msgSalvar.className = "msg erro";
      return;
    }

    btnSalvar.disabled = true;
    const item = { sku: sku, custo: custo };
    if (nome) item.nome_produto = nome;

    fetch(API_CUSTOS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ itens: [item] }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (corpo) { throw new Error(corpo.detail || "Não consegui salvar."); });
        return r.json();
      })
      .then(function () {
        msgSalvar.textContent = "Salvo!";
        msgSalvar.className = "msg ok";
        campoBusca.value = editandoSkuOriginal || sku;
        buscar();
        setTimeout(fecharFormulario, 700);
      })
      .catch(function (erro) {
        msgSalvar.textContent = erro.message;
        msgSalvar.className = "msg erro";
      })
      .finally(function () { btnSalvar.disabled = false; });
  }

  btnBuscar.addEventListener("click", buscar);
  campoBusca.addEventListener("keydown", function (e) { if (e.key === "Enter") buscar(); });
  btnAtualizarEstoque.addEventListener("click", atualizarEstoqueAgora);
  btnNovo.addEventListener("click", function () { abrirFormulario(null); });
  cardSemCustoClicavel.addEventListener("click", mostrarApenasSemCusto);
  linkLimparFiltro.addEventListener("click", function (e) {
    e.preventDefault();
    filtroSemCusto = false;
    aplicarFiltroEExibir();
  });
  btnCancelar.addEventListener("click", fecharFormulario);
  btnSalvar.addEventListener("click", salvarCustoAtual);

  // Mostra a lista completa assim que a tela abre.
  buscar();
})();
