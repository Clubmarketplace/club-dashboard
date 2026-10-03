(function () {
  "use strict";

  const API_RESUMO = "/api/painel/vendas/resumo";
  const API_LISTA = "/api/painel/vendas/lista";
  const API_SINCRONIZAR = "/api/painel/vendas/sincronizar";
  const API_MARGEM = "/api/painel/vendas/margem";
  const API_VARIAVEIS = "/api/painel/vendas/variaveis";
  const API_DESPESAS_FIXAS = "/api/painel/vendas/despesas-fixas";

  const filtroInicio = document.getElementById("filtro-inicio");
  const filtroFim = document.getElementById("filtro-fim");
  const btnAplicarPeriodo = document.getElementById("btn-aplicar-periodo");
  const btnAtualizarVendas = document.getElementById("btn-atualizar-vendas");
  const botoesAtalho = document.querySelectorAll(".btn-atalho");
  const avisoVendas = document.getElementById("aviso-vendas");

  const cardVendas = document.getElementById("card-vendas");
  const cardUnidades = document.getElementById("card-unidades");
  const cardVendaBruta = document.getElementById("card-venda-bruta");
  const cardRepasse = document.getElementById("card-repasse");
  const cardTaxasFrete = document.getElementById("card-taxas-frete");
  const cardCmv = document.getElementById("card-cmv");
  const cardTicketMedio = document.getElementById("card-ticket-medio");
  const cardLucroBruto = document.getElementById("card-lucro-bruto");
  const cardLucroLiquido = document.getElementById("card-lucro-liquido");
  const cardMargemMarkup = document.getElementById("card-margem-markup");
  const cardMargemContribuicao = document.getElementById("card-margem-contribuicao");
  const cardSemCusto = document.getElementById("card-sem-custo");
  const cardSemCustoClicavel = document.getElementById("card-sem-custo-clicavel");

  const tabela = document.getElementById("tabela-vendas");
  const corpoVendas = document.getElementById("corpo-vendas");
  const mensagemVazia = document.getElementById("mensagem-vazia");
  const filtroAtivoAviso = document.getElementById("filtro-ativo-aviso");
  const linkLimparFiltro = document.getElementById("link-limpar-filtro");

  const campoMargemMinima = document.getElementById("campo-margem-minima");
  const campoMargemMaxima = document.getElementById("campo-margem-maxima");
  const btnSalvarMargem = document.getElementById("btn-salvar-margem");
  const msgMargem = document.getElementById("msg-margem");

  const listaVariaveis = document.getElementById("lista-variaveis");
  const campoVariavelNome = document.getElementById("campo-variavel-nome");
  const campoVariavelPercentual = document.getElementById("campo-variavel-percentual");
  const btnAddVariavel = document.getElementById("btn-add-variavel");
  const msgVariavel = document.getElementById("msg-variavel");

  const listaDespesasFixas = document.getElementById("lista-despesas-fixas");
  const campoDespesaNome = document.getElementById("campo-despesa-nome");
  const campoDespesaValor = document.getElementById("campo-despesa-valor");
  const btnAddDespesa = document.getElementById("btn-add-despesa");
  const msgDespesa = document.getElementById("msg-despesa");

  let filtroSemCustoOuRuim = false;

  function formatarMoeda(valor) {
    if (valor === null || valor === undefined) return "-";
    const negativo = valor < 0;
    const texto = "R$ " + Math.abs(Number(valor)).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return negativo ? "-" + texto : texto;
  }

  function formatarPercentual(valor) {
    if (valor === null || valor === undefined) return "-";
    return Number(valor).toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "%";
  }

  function formatarData(isoString) {
    if (!isoString) return "-";
    const data = new Date(isoString.endsWith("Z") || isoString.includes("+") ? isoString : isoString + "Z");
    return data.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function dataParaInput(data) {
    return data.toISOString().slice(0, 10);
  }

  function periodoAtual() {
    return { inicio: filtroInicio.value || "", fim: filtroFim.value || "" };
  }

  function queryPeriodo() {
    const p = periodoAtual();
    const partes = [];
    if (p.inicio) partes.push("inicio=" + encodeURIComponent(p.inicio));
    if (p.fim) partes.push("fim=" + encodeURIComponent(p.fim));
    return partes.length ? "?" + partes.join("&") : "";
  }

  function marcarAtalhoAtivo(nome) {
    botoesAtalho.forEach((botao) => {
      botao.classList.toggle("ativo", botao.dataset.atalho === nome);
    });
  }

  // Dia/mês/ano é sempre em cima da data de HOJE (não da última data
  // filtrada) -- clicar em "Este mês" depois de já estar vendo outro mês
  // sempre volta pro mês atual, igual o "Hoje" sempre volta pro dia atual.
  function aplicarAtalho(nome) {
    const hoje = new Date();
    let inicio;
    if (nome === "hoje") {
      inicio = new Date(hoje.getFullYear(), hoje.getMonth(), hoje.getDate());
    } else if (nome === "mes") {
      inicio = new Date(hoje.getFullYear(), hoje.getMonth(), 1);
    } else {
      inicio = new Date(hoje.getFullYear(), 0, 1);
    }
    filtroInicio.value = dataParaInput(inicio);
    filtroFim.value = dataParaInput(hoje);
    marcarAtalhoAtivo(nome);
    carregarTudo();
  }

  botoesAtalho.forEach((botao) => {
    botao.addEventListener("click", function () {
      aplicarAtalho(botao.dataset.atalho);
    });
  });

  function definirPeriodoPadrao() {
    // Tela mais usada -> abre direto no "Hoje", pra bater com o que o
    // seller mais quer ver de cara (venda do dia).
    aplicarAtalho("hoje");
  }

  function carregarResumo() {
    avisoVendas.classList.remove("visivel");
    fetch(API_RESUMO + queryPeriodo())
      .then((r) => r.json())
      .then((resumo) => {
        if (resumo.aviso) {
          avisoVendas.textContent = "⚠️ " + resumo.aviso;
          avisoVendas.classList.add("visivel");
        }
        cardVendas.textContent = resumo.vendas;
        cardUnidades.textContent = resumo.unidades + " unidade(s)";
        cardVendaBruta.textContent = formatarMoeda(resumo.venda_bruta);
        cardRepasse.textContent = formatarMoeda(resumo.repasse);
        cardTaxasFrete.textContent = formatarMoeda(resumo.taxas_e_frete);
        cardCmv.textContent = formatarMoeda(resumo.cmv);
        cardTicketMedio.textContent = formatarMoeda(resumo.ticket_medio);
        cardLucroBruto.textContent = formatarMoeda(resumo.lucro_bruto);
        cardLucroLiquido.textContent = formatarMoeda(resumo.lucro_liquido);
        cardMargemMarkup.textContent = formatarPercentual(resumo.margem_markup);
        cardMargemContribuicao.textContent = formatarPercentual(resumo.margem_contribuicao);
        cardSemCusto.textContent = resumo.qtd_sem_custo;

        campoMargemMinima.value = resumo.margem_minima === null || resumo.margem_minima === undefined ? "" : resumo.margem_minima;
        campoMargemMaxima.value = resumo.margem_maxima === null || resumo.margem_maxima === undefined ? "" : resumo.margem_maxima;

        renderizarVariaveis(resumo.despesas_percentuais || []);
        renderizarDespesasFixas(resumo.despesas_fixas || []);
      })
      .catch(() => {
        avisoVendas.textContent = "⚠️ Não consegui carregar o resumo de vendas agora.";
        avisoVendas.classList.add("visivel");
      });
  }

  function renderizarVendas(itens) {
    corpoVendas.innerHTML = "";
    const filtrados = filtroSemCustoOuRuim
      ? itens.filter((i) => i.custo_total === null || i.margem_classificacao === "ruim")
      : itens;

    if (!filtrados.length) {
      tabela.style.display = "none";
      mensagemVazia.style.display = "block";
      return;
    }
    tabela.style.display = "table";
    mensagemVazia.style.display = "none";

    filtrados.forEach((item) => {
      const tr = document.createElement("tr");

      const tdData = document.createElement("td");
      tdData.textContent = formatarData(item.data_venda);
      tr.appendChild(tdData);

      const tdProduto = document.createElement("td");
      const skuTexto = item.sku || "(sem SKU)";
      tdProduto.innerHTML =
        "<strong>" + skuTexto + "</strong>" +
        (item.custo_total === null ? '<span class="tag-sem-custo">sem custo</span>' : "") +
        (item.titulo ? "<br><span style=\"color:var(--cmx-texto-suave); font-size:12.5px;\">" + item.titulo + "</span>" : "");
      tr.appendChild(tdProduto);

      const tdQtd = document.createElement("td");
      tdQtd.className = "col-numero";
      tdQtd.textContent = item.quantidade;
      tr.appendChild(tdQtd);

      const tdVenda = document.createElement("td");
      tdVenda.className = "col-numero";
      tdVenda.textContent = formatarMoeda(item.venda_bruta);
      tr.appendChild(tdVenda);

      const tdRepasse = document.createElement("td");
      tdRepasse.className = "col-numero";
      tdRepasse.textContent = formatarMoeda(item.repasse);
      tr.appendChild(tdRepasse);

      const tdCusto = document.createElement("td");
      tdCusto.className = "col-numero";
      tdCusto.textContent = formatarMoeda(item.custo_total);
      tr.appendChild(tdCusto);

      const tdLucro = document.createElement("td");
      tdLucro.className = "col-numero";
      tdLucro.textContent = formatarMoeda(item.lucro);
      tr.appendChild(tdLucro);

      const tdMargem = document.createElement("td");
      tdMargem.className = "col-numero";
      if (item.margem_classificacao) {
        const selo = document.createElement("span");
        selo.className = "selo-margem " + item.margem_classificacao;
        selo.textContent = formatarPercentual(item.margem_percentual);
        tdMargem.appendChild(selo);
      } else {
        tdMargem.textContent = formatarPercentual(item.margem_percentual);
      }
      tr.appendChild(tdMargem);

      corpoVendas.appendChild(tr);
    });
  }

  function carregarLista() {
    fetch(API_LISTA + queryPeriodo())
      .then((r) => r.json())
      .then((dados) => renderizarVendas(dados.itens || []))
      .catch(() => {
        tabela.style.display = "none";
        mensagemVazia.style.display = "block";
        mensagemVazia.textContent = "Não consegui carregar a lista de vendas agora.";
      });
  }

  function carregarTudo() {
    carregarResumo();
    carregarLista();
  }

  btnAplicarPeriodo.addEventListener("click", function () {
    marcarAtalhoAtivo(null); // período digitado à mão -- nenhum atalho corresponde mais
    carregarTudo();
  });

  btnAtualizarVendas.addEventListener("click", function () {
    btnAtualizarVendas.disabled = true;
    btnAtualizarVendas.textContent = "Atualizando...";
    fetch(API_SINCRONIZAR, { method: "POST" })
      .then(() => carregarTudo())
      .finally(() => {
        btnAtualizarVendas.disabled = false;
        btnAtualizarVendas.textContent = "↻ Atualizar agora";
      });
  });

  cardSemCustoClicavel.addEventListener("click", function () {
    filtroSemCustoOuRuim = true;
    filtroAtivoAviso.style.display = "block";
    fetch(API_LISTA + queryPeriodo())
      .then((r) => r.json())
      .then((dados) => renderizarVendas(dados.itens || []));
  });

  linkLimparFiltro.addEventListener("click", function (e) {
    e.preventDefault();
    filtroSemCustoOuRuim = false;
    filtroAtivoAviso.style.display = "none";
    carregarLista();
  });

  // --- Faixa de margem ---
  btnSalvarMargem.addEventListener("click", function () {
    const minima = campoMargemMinima.value.trim();
    const maxima = campoMargemMaxima.value.trim();
    msgMargem.textContent = "";
    fetch(API_MARGEM, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        margem_minima: minima === "" ? null : Number(minima.replace(",", ".")),
        margem_maxima: maxima === "" ? null : Number(maxima.replace(",", ".")),
      }),
    })
      .then((r) => {
        if (!r.ok) throw new Error();
        msgMargem.textContent = "Salvo!";
        msgMargem.className = "msg ok";
        carregarTudo();
      })
      .catch(() => {
        msgMargem.textContent = "Não consegui salvar agora.";
        msgMargem.className = "msg erro";
      });
  });

  // --- Despesas percentuais (VariavelConta) ---
  function renderizarVariaveis(itens) {
    listaVariaveis.innerHTML = "";
    if (!itens.length) {
      listaVariaveis.innerHTML = '<p class="sub" style="margin:0;">Nenhuma cadastrada ainda.</p>';
      return;
    }
    itens.forEach((item) => {
      const linha = document.createElement("div");
      linha.className = "linha-item";
      const valorTexto = item.valor !== undefined ? " (" + formatarMoeda(item.valor) + " no período)" : "";
      linha.innerHTML =
        "<span>" + item.nome + " — " + formatarPercentual(item.percentual) + valorTexto + "</span>" +
        '<button class="remover" data-nome="' + item.nome + '">remover</button>';
      listaVariaveis.appendChild(linha);
    });
    listaVariaveis.querySelectorAll(".remover").forEach((botao) => {
      botao.addEventListener("click", function () {
        fetch(API_VARIAVEIS + "/" + encodeURIComponent(botao.dataset.nome), { method: "DELETE" }).then(carregarTudo);
      });
    });
  }

  btnAddVariavel.addEventListener("click", function () {
    const nome = campoVariavelNome.value.trim();
    const percentual = campoVariavelPercentual.value.trim().replace(",", ".");
    msgVariavel.textContent = "";
    if (!nome || percentual === "") {
      msgVariavel.textContent = "Preencha nome e percentual.";
      msgVariavel.className = "msg erro";
      return;
    }
    fetch(API_VARIAVEIS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nome: nome, percentual: Number(percentual) }),
    })
      .then((r) => {
        if (!r.ok) return r.json().then((d) => Promise.reject(d));
        campoVariavelNome.value = "";
        campoVariavelPercentual.value = "";
        msgVariavel.textContent = "Adicionado!";
        msgVariavel.className = "msg ok";
        carregarTudo();
      })
      .catch((erro) => {
        msgVariavel.textContent = (erro && erro.detail) || "Não consegui adicionar agora.";
        msgVariavel.className = "msg erro";
      });
  });

  // --- Despesas fixas mensais ---
  function renderizarDespesasFixas(itens) {
    listaDespesasFixas.innerHTML = "";
    if (!itens.length) {
      listaDespesasFixas.innerHTML = '<p class="sub" style="margin:0;">Nenhuma cadastrada ainda.</p>';
      return;
    }
    itens.forEach((item) => {
      const linha = document.createElement("div");
      linha.className = "linha-item";
      const valorPeriodoTexto = item.valor_periodo !== undefined ? " (" + formatarMoeda(item.valor_periodo) + " no período)" : "";
      linha.innerHTML =
        "<span>" + item.nome + " — " + formatarMoeda(item.valor_mensal) + "/mês" + valorPeriodoTexto + "</span>" +
        '<button class="remover" data-nome="' + item.nome + '">remover</button>';
      listaDespesasFixas.appendChild(linha);
    });
    listaDespesasFixas.querySelectorAll(".remover").forEach((botao) => {
      botao.addEventListener("click", function () {
        fetch(API_DESPESAS_FIXAS + "/" + encodeURIComponent(botao.dataset.nome), { method: "DELETE" }).then(carregarTudo);
      });
    });
  }

  btnAddDespesa.addEventListener("click", function () {
    const nome = campoDespesaNome.value.trim();
    const valor = campoDespesaValor.value.trim().replace(",", ".");
    msgDespesa.textContent = "";
    if (!nome || valor === "") {
      msgDespesa.textContent = "Preencha nome e valor mensal.";
      msgDespesa.className = "msg erro";
      return;
    }
    fetch(API_DESPESAS_FIXAS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nome: nome, valor_mensal: Number(valor) }),
    })
      .then((r) => {
        if (!r.ok) return r.json().then((d) => Promise.reject(d));
        campoDespesaNome.value = "";
        campoDespesaValor.value = "";
        msgDespesa.textContent = "Adicionado!";
        msgDespesa.className = "msg ok";
        carregarTudo();
      })
      .catch((erro) => {
        msgDespesa.textContent = (erro && erro.detail) || "Não consegui adicionar agora.";
        msgDespesa.className = "msg erro";
      });
  });

  definirPeriodoPadrao(); // já chama carregarTudo() dentro de aplicarAtalho()
})();
