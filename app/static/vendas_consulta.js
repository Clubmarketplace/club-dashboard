(function () {
  "use strict";

  const API_LISTA = "/api/painel/vendas/lista";
  const API_SINCRONIZAR = "/api/painel/vendas/sincronizar";
  const API_TAXAS = "/api/painel/taxas";

  const INTERVALO_ATUALIZACAO_MS = 25000; // mantém a lista e as margens "ao vivo"

  const filtroInicio = document.getElementById("filtro-inicio");
  const filtroFim = document.getElementById("filtro-fim");
  const btnAplicarPeriodo = document.getElementById("btn-aplicar-periodo");
  const btnAtualizarVendas = document.getElementById("btn-atualizar-vendas");
  const botoesAtalho = document.querySelectorAll(".btn-atalho");
  const avisoVendas = document.getElementById("aviso-vendas");
  const campoBusca = document.getElementById("campo-busca-vendas");

  const listaVendasConsulta = document.getElementById("lista-vendas-consulta");
  const mensagemVazia = document.getElementById("mensagem-vazia");

  // --- Chips de taxas (dentro da faixa única de filtro) ---
  const btnAbrirModalTaxaVendas = document.getElementById("btn-abrir-modal-taxa-vendas");
  const modalTaxaVendas = document.getElementById("modal-taxa-vendas");
  const campoNomeTaxaVendas = document.getElementById("campo-nome-taxa-vendas");
  const campoPercentualTaxaVendas = document.getElementById("campo-percentual-taxa-vendas");
  const pillsBaseTaxaVendas = document.querySelectorAll("#pills-base-taxa-vendas .pill-base");
  const msgModalTaxaVendas = document.getElementById("msg-modal-taxa-vendas");
  const btnCancelarModalTaxaVendas = document.getElementById("btn-cancelar-modal-taxa-vendas");
  const btnSalvarModalTaxaVendas = document.getElementById("btn-salvar-modal-taxa-vendas");
  const chipsTaxasFiltro = document.getElementById("chips-taxas-filtro");

  let baseSelecionadaTaxaVendas = "venda_bruta";

  // --- Modal "Escolher data" (abas Por Dia / Por Mês / Por Ano) ---
  const btnAbrirModalData = document.getElementById("btn-abrir-modal-data");
  const modalEscolherData = document.getElementById("modal-escolher-data");
  const btnFecharModalData = document.getElementById("btn-fechar-modal-data");
  const btnCancelarModalData = document.getElementById("btn-cancelar-modal-data");
  const btnPesquisarModalData = document.getElementById("btn-pesquisar-modal-data");
  const abasEscolherData = document.querySelectorAll("#abas-escolher-data .aba-escolher-data");
  const painelDia = document.getElementById("painel-dia");
  const painelMes = document.getElementById("painel-mes");
  const painelAno = document.getElementById("painel-ano");
  const modalFiltroInicio = document.getElementById("modal-filtro-inicio");
  const modalFiltroFim = document.getElementById("modal-filtro-fim");
  const rotuloAnoMes = document.getElementById("rotulo-ano-mes");
  const btnAnoMesAnterior = document.getElementById("btn-ano-mes-anterior");
  const btnAnoMesProximo = document.getElementById("btn-ano-mes-proximo");
  const gradeMeses = document.getElementById("grade-meses");
  const gradeAnos = document.getElementById("grade-anos");
  const msgModalData = document.getElementById("msg-modal-data");

  const NOMES_MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];
  const RÓTULO_BASE_TAXA = { venda_bruta: "s/ Venda Bruta", repasse: "s/ Repasse", lucro: "s/ Lucro" };

  let abaAtivaData = "dia";
  let anoExibidoMes = new Date().getFullYear();

  // Vendas cruas da última busca + quais cartões estão abertos no momento
  // (guardado por id de venda, não por posição -- assim o "▾" não fecha
  // sozinho quando a lista é re-renderizada a cada atualização automática).
  let itensAtuais = [];
  let idsExpandidos = new Set();
  let intervaloAtualizacao = null;

  function dataISO(data) {
    return data.getFullYear() + "-" + String(data.getMonth() + 1).padStart(2, "0") + "-" + String(data.getDate()).padStart(2, "0");
  }

  function ultimoDiaDoMes(ano, mesIndice0) {
    return new Date(ano, mesIndice0 + 1, 0);
  }

  function dataParaInput(data) {
    return data.toISOString().slice(0, 10);
  }

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
    if (!isoString) return { dia: "-", hora: "" };
    const data = new Date(isoString.endsWith("Z") || isoString.includes("+") ? isoString : isoString + "Z");
    return {
      dia: data.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" }),
      hora: data.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }),
    };
  }

  function iniciais(titulo, sku) {
    const base = (titulo || sku || "??").trim();
    const partes = base.split(/\s+/).filter(Boolean);
    if (!partes.length) return "??";
    if (partes.length === 1) return partes[0].slice(0, 2).toUpperCase();
    return (partes[0][0] + partes[1][0]).toUpperCase();
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
    carregarLista();
  }

  botoesAtalho.forEach((botao) => {
    botao.addEventListener("click", function () {
      aplicarAtalho(botao.dataset.atalho);
    });
  });

  // --- Modal "Escolher data" ---
  function selecionarAbaData(aba) {
    abaAtivaData = aba;
    abasEscolherData.forEach((b) => b.classList.toggle("ativa", b.dataset.aba === aba));
    painelDia.style.display = aba === "dia" ? "block" : "none";
    painelMes.style.display = aba === "mes" ? "block" : "none";
    painelAno.style.display = aba === "ano" ? "block" : "none";
    btnPesquisarModalData.style.display = aba === "dia" ? "inline-block" : "none";
    msgModalData.textContent = "";
    if (aba === "mes") renderizarGradeMeses();
    if (aba === "ano") renderizarGradeAnos();
  }

  function renderizarGradeMeses() {
    const hoje = new Date();
    const anoAtual = hoje.getFullYear();
    const mesAtual = hoje.getMonth();
    rotuloAnoMes.textContent = String(anoExibidoMes);
    btnAnoMesProximo.disabled = anoExibidoMes >= anoAtual;
    gradeMeses.innerHTML = "";
    NOMES_MESES.forEach((nome, indice) => {
      const futuro = anoExibidoMes > anoAtual || (anoExibidoMes === anoAtual && indice > mesAtual);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "celula-mes";
      btn.textContent = nome;
      btn.disabled = futuro;
      if (futuro) {
        gradeMeses.appendChild(btn);
        return;
      }
      btn.addEventListener("click", function () {
        const inicio = new Date(anoExibidoMes, indice, 1);
        const fimMes = ultimoDiaDoMes(anoExibidoMes, indice);
        const fim = fimMes > hoje ? hoje : fimMes;
        aplicarPeriodoEscolhido(dataISO(inicio), dataISO(fim));
      });
      gradeMeses.appendChild(btn);
    });
  }

  function renderizarGradeAnos() {
    const anoAtual = new Date().getFullYear();
    gradeAnos.innerHTML = "";
    for (let ano = anoAtual; ano >= anoAtual - 5; ano--) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "celula-ano";
      btn.textContent = String(ano);
      btn.addEventListener("click", function () {
        const hoje = new Date();
        const inicio = new Date(ano, 0, 1);
        const fimAno = new Date(ano, 11, 31);
        const fim = fimAno > hoje ? hoje : fimAno;
        aplicarPeriodoEscolhido(dataISO(inicio), dataISO(fim));
      });
      gradeAnos.appendChild(btn);
    }
  }

  function aplicarPeriodoEscolhido(inicioISO, fimISO) {
    filtroInicio.value = inicioISO;
    filtroFim.value = fimISO;
    marcarAtalhoAtivo(null);
    fecharModalData();
    carregarLista();
  }

  function abrirModalData() {
    selecionarAbaData("dia");
    modalFiltroInicio.value = filtroInicio.value;
    modalFiltroFim.value = filtroFim.value;
    anoExibidoMes = new Date().getFullYear();
    modalEscolherData.classList.add("aberto");
  }

  function fecharModalData() {
    modalEscolherData.classList.remove("aberto");
  }

  btnAbrirModalData.addEventListener("click", abrirModalData);
  btnFecharModalData.addEventListener("click", fecharModalData);
  btnCancelarModalData.addEventListener("click", fecharModalData);
  modalEscolherData.addEventListener("click", function (e) {
    if (e.target === modalEscolherData) fecharModalData();
  });
  abasEscolherData.forEach((botao) => {
    botao.addEventListener("click", function () {
      selecionarAbaData(botao.dataset.aba);
    });
  });
  btnAnoMesAnterior.addEventListener("click", function () {
    anoExibidoMes -= 1;
    renderizarGradeMeses();
  });
  btnAnoMesProximo.addEventListener("click", function () {
    if (anoExibidoMes >= new Date().getFullYear()) return;
    anoExibidoMes += 1;
    renderizarGradeMeses();
  });
  btnPesquisarModalData.addEventListener("click", function () {
    const inicio = modalFiltroInicio.value;
    const fim = modalFiltroFim.value;
    if (!inicio || !fim) {
      msgModalData.textContent = "Preencha as duas datas.";
      msgModalData.className = "msg erro";
      return;
    }
    if (inicio > fim) {
      msgModalData.textContent = "A data \"De\" não pode ser depois da data \"Até\".";
      msgModalData.className = "msg erro";
      return;
    }
    aplicarPeriodoEscolhido(inicio, fim);
  });

  btnAplicarPeriodo.addEventListener("click", function () {
    marcarAtalhoAtivo(null);
    carregarLista();
  });

  // --- Chips de taxas ---
  function renderizarChipsTaxasFiltro(itens) {
    if (!chipsTaxasFiltro) return;
    if (!itens.length) {
      chipsTaxasFiltro.innerHTML = '<span class="vazio-chips">Nenhuma taxa cadastrada ainda.</span>';
      return;
    }
    chipsTaxasFiltro.innerHTML = itens
      .map(
        (item) =>
          '<span class="chip-taxa-filtro">' +
          item.nome + " · " + formatarPercentual(item.percentual) +
          '<button type="button" class="chip-taxa-filtro-remover" data-nome="' + item.nome + '" aria-label="Remover taxa ' + item.nome + '">✕</button>' +
          "</span>"
      )
      .join("");
    chipsTaxasFiltro.querySelectorAll(".chip-taxa-filtro-remover").forEach((botao) => {
      botao.addEventListener("click", function () {
        if (!window.confirm('Remover a taxa "' + botao.dataset.nome + '"?')) return;
        fetch(API_TAXAS + "/" + encodeURIComponent(botao.dataset.nome), { method: "DELETE" }).then(function () {
          carregarTaxasCadastradas();
          carregarLista(); // taxa mudou -> lucro líquido/markup/margem de cada venda precisam recalcular
        });
      });
    });
  }

  function carregarTaxasCadastradas() {
    fetch(API_TAXAS)
      .then((r) => r.json())
      .then((dados) => renderizarChipsTaxasFiltro(dados.itens || []))
      .catch(() => {
        if (chipsTaxasFiltro) chipsTaxasFiltro.innerHTML = '<span class="vazio-chips">⚠ Não consegui carregar as taxas agora.</span>';
      });
  }

  function selecionarBaseTaxaVendas(base) {
    baseSelecionadaTaxaVendas = base;
    pillsBaseTaxaVendas.forEach((p) => p.classList.toggle("ativa", p.dataset.base === base));
  }

  function abrirModalTaxaVendas() {
    campoNomeTaxaVendas.value = "";
    campoPercentualTaxaVendas.value = "";
    msgModalTaxaVendas.textContent = "";
    msgModalTaxaVendas.className = "msg";
    selecionarBaseTaxaVendas("venda_bruta");
    modalTaxaVendas.classList.add("aberto");
    campoNomeTaxaVendas.focus();
  }

  function fecharModalTaxaVendas() {
    modalTaxaVendas.classList.remove("aberto");
  }

  pillsBaseTaxaVendas.forEach((p) => {
    p.addEventListener("click", function () {
      selecionarBaseTaxaVendas(p.dataset.base);
    });
  });

  btnAbrirModalTaxaVendas.addEventListener("click", abrirModalTaxaVendas);
  btnCancelarModalTaxaVendas.addEventListener("click", fecharModalTaxaVendas);
  modalTaxaVendas.addEventListener("click", function (e) {
    if (e.target === modalTaxaVendas) fecharModalTaxaVendas();
  });

  btnSalvarModalTaxaVendas.addEventListener("click", function () {
    const nome = campoNomeTaxaVendas.value.trim();
    const percentual = campoPercentualTaxaVendas.value.trim().replace(",", ".");
    msgModalTaxaVendas.textContent = "";
    if (!nome || percentual === "") {
      msgModalTaxaVendas.textContent = "Preencha nome e porcentagem.";
      msgModalTaxaVendas.className = "msg erro";
      return;
    }
    if (isNaN(Number(percentual)) || Number(percentual) < 0) {
      msgModalTaxaVendas.textContent = "Porcentagem inválida.";
      msgModalTaxaVendas.className = "msg erro";
      return;
    }
    fetch(API_TAXAS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nome: nome, percentual: Number(percentual), base_calculo: baseSelecionadaTaxaVendas }),
    })
      .then((r) => {
        if (!r.ok) return r.json().then((d) => Promise.reject(d));
        fecharModalTaxaVendas();
        carregarTaxasCadastradas();
        carregarLista();
      })
      .catch((erro) => {
        msgModalTaxaVendas.textContent = (erro && erro.detail) || "Não consegui salvar agora.";
        msgModalTaxaVendas.className = "msg erro";
      });
  });

  // --- Lista venda a venda, em cartões ---

  function markupPercentual(item) {
    // lucro líquido / custo total -- só dá pra calcular com custo cadastrado.
    if (item.custo_total === null || item.custo_total === undefined || !item.custo_total) return null;
    if (item.lucro_liquido === null || item.lucro_liquido === undefined) return null;
    return (item.lucro_liquido / item.custo_total) * 100;
  }

  function seloClasse(valor) {
    // Mesmo critério visual em Lucro, Markup e Margem: negativo é sempre
    // vermelho (como no print de referência do Washington), positivo verde.
    if (valor === null || valor === undefined) return "neutro";
    return valor < 0 ? "ruim" : "boa";
  }

  function itemCorresponde(item, termo) {
    if (!termo) return true;
    const alvo = ((item.sku || "") + " " + (item.titulo || "")).toLowerCase();
    return alvo.includes(termo);
  }

  function renderizarDetalheTaxas(item) {
    const taxasAplicadas = item.taxas_aplicadas || [];
    if (!taxasAplicadas.length) return "Nenhuma taxa aplicada";
    const totalTaxas = taxasAplicadas.reduce((soma, t) => soma + (t.valor || 0), 0);
    const detalheNomes = taxasAplicadas
      .map((t) => t.nome + " " + formatarPercentual(t.percentual))
      .join(" + ");
    return detalheNomes + " = " + formatarMoeda(totalTaxas);
  }

  function construirCartao(item) {
    const aberto = idsExpandidos.has(item.id);
    const data = formatarData(item.data_venda);
    const markup = markupPercentual(item);
    const semCusto = item.custo_total === null || item.custo_total === undefined;

    const cartao = document.createElement("div");
    cartao.className = "cartao-venda" + (aberto ? " aberto" : "");
    cartao.dataset.id = item.id;

    const linha = document.createElement("div");
    linha.className = "cartao-venda-linha";

    const avatar = document.createElement("div");
    avatar.className = "cartao-venda-avatar";
    avatar.textContent = iniciais(item.titulo, item.sku);
    linha.appendChild(avatar);

    const info = document.createElement("div");
    info.className = "cartao-venda-info";
    const titulo = document.createElement("div");
    titulo.className = "cartao-venda-titulo";
    titulo.textContent = item.titulo || item.sku || "(sem título)";
    const sub = document.createElement("div");
    sub.className = "cartao-venda-sub";
    sub.innerHTML =
      "SKU: " + (item.sku || "(sem SKU)") + " · " + item.quantidade + " un." +
      (semCusto ? '<span class="tag-sem-custo">sem custo</span>' : "");
    info.appendChild(titulo);
    info.appendChild(sub);
    linha.appendChild(info);

    const badges = document.createElement("div");
    badges.className = "cartao-venda-badges";

    const seloLucro = document.createElement("span");
    seloLucro.className = "selo-venda " + seloClasse(item.lucro_liquido);
    seloLucro.textContent = "Lucro: " + formatarMoeda(item.lucro_liquido);
    badges.appendChild(seloLucro);

    const seloMarkup = document.createElement("span");
    seloMarkup.className = "selo-venda " + seloClasse(markup);
    seloMarkup.textContent = "Markup: " + formatarPercentual(markup);
    badges.appendChild(seloMarkup);

    const seloMargem = document.createElement("span");
    seloMargem.className = "selo-venda " + seloClasse(item.margem_percentual);
    seloMargem.textContent = "Margem: " + formatarPercentual(item.margem_percentual);
    badges.appendChild(seloMargem);

    linha.appendChild(badges);

    const dataEl = document.createElement("div");
    dataEl.className = "cartao-venda-data";
    dataEl.innerHTML = data.dia + "<br><span>" + data.hora + "</span>";
    linha.appendChild(dataEl);

    const seta = document.createElement("span");
    seta.className = "cartao-venda-seta";
    seta.textContent = "▾";
    linha.appendChild(seta);

    linha.addEventListener("click", function () {
      if (idsExpandidos.has(item.id)) {
        idsExpandidos.delete(item.id);
      } else {
        idsExpandidos.add(item.id);
      }
      renderizarLista(itensAtuais);
    });

    cartao.appendChild(linha);

    if (aberto) {
      const detalhe = document.createElement("div");
      detalhe.className = "cartao-venda-detalhe";
      detalhe.innerHTML =
        '<div class="item-detalhe"><span>Venda Bruta</span><strong>' + formatarMoeda(item.venda_bruta) + "</strong></div>" +
        '<div class="item-detalhe"><span>Repasse</span><strong>' + formatarMoeda(item.repasse) + "</strong></div>" +
        '<div class="item-detalhe"><span>Custo (CMV)</span><strong>' + formatarMoeda(item.custo_total) + "</strong></div>" +
        '<div class="item-detalhe"><span>Taxas aplicadas</span><strong>' + renderizarDetalheTaxas(item) + "</strong></div>" +
        '<div class="item-detalhe"><span>Pedido</span><strong>#' + (item.ml_order_id || "-") + "</strong></div>";
      cartao.appendChild(detalhe);
    }

    return cartao;
  }

  function renderizarLista(itens) {
    const termo = (campoBusca.value || "").trim().toLowerCase();
    const filtrados = itens.filter((item) => itemCorresponde(item, termo));

    if (!filtrados.length) {
      listaVendasConsulta.innerHTML = "";
      mensagemVazia.style.display = "block";
      mensagemVazia.textContent = termo
        ? "Nenhuma venda encontrada pra esse termo nesse período."
        : "Nenhuma venda encontrada nesse período.";
      return;
    }
    mensagemVazia.style.display = "none";

    listaVendasConsulta.innerHTML = "";
    filtrados.forEach((item) => {
      listaVendasConsulta.appendChild(construirCartao(item));
    });
  }

  function carregarLista() {
    avisoVendas.classList.remove("visivel");
    fetch(API_LISTA + queryPeriodo())
      .then((r) => r.json())
      .then((dados) => {
        itensAtuais = dados.itens || [];
        if (dados.aviso) {
          avisoVendas.textContent = "⚠️ " + dados.aviso;
          avisoVendas.classList.add("visivel");
        }
        renderizarLista(itensAtuais);
      })
      .catch(() => {
        listaVendasConsulta.innerHTML = "";
        mensagemVazia.style.display = "block";
        mensagemVazia.textContent = "Não consegui carregar a lista de vendas agora.";
      });
  }

  campoBusca.addEventListener("input", function () {
    renderizarLista(itensAtuais);
  });

  btnAtualizarVendas.addEventListener("click", function () {
    btnAtualizarVendas.disabled = true;
    btnAtualizarVendas.textContent = "⏳";
    fetch(API_SINCRONIZAR, { method: "POST" })
      .then(() => carregarLista())
      .finally(() => {
        btnAtualizarVendas.disabled = false;
        btnAtualizarVendas.textContent = "↻";
      });
  });

  function iniciarAtualizacaoAutomatica() {
    if (intervaloAtualizacao) clearInterval(intervaloAtualizacao);
    // Só re-busca a lista (sem re-sincronizar com o Mercado Livre a cada
    // tick) -- assim a margem/lucro de cada venda já cadastrada vai se
    // atualizando em tempo real (ex: taxa nova aplicada) sem martelar a API externa.
    intervaloAtualizacao = setInterval(carregarLista, INTERVALO_ATUALIZACAO_MS);
  }

  carregarTaxasCadastradas();
  aplicarAtalho("hoje"); // já chama carregarLista() dentro de aplicarAtalho()
  iniciarAtualizacaoAutomatica();
})();
