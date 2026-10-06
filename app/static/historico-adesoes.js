// Tela "Produtos > Histórico de adesões" do painel (seller) -- lê
// GET /api/painel/historico-adesoes (app/routers/historico_adesoes_painel.py),
// que por sua vez consulta a tabela EventoAdesaoCmx (cópia no servidor de
// toda adesão que a extensão ClubMarketplaceX registrou, enviada por ela
// em segundo plano). Sempre paginado e com janela de data (padrão: 10
// dias) -- nunca pede a tabela inteira de uma vez, pro servidor continuar
// rápido com muitas contas.
(function () {
  "use strict";

  const API_HISTORICO = "/api/painel/historico-adesoes";

  const campoDesde = document.getElementById("filtro-desde");
  const campoAte = document.getElementById("filtro-ate");
  const campoBusca = document.getElementById("filtro-busca");
  const btnFiltrar = document.getElementById("btn-filtrar");
  const btnLimpar = document.getElementById("btn-limpar-filtro");

  const carregando = document.getElementById("carregando");
  const tabela = document.getElementById("tabela-resultados");
  const corpoTabela = document.getElementById("corpo-resultados");
  const mensagemVazia = document.getElementById("mensagem-vazia");
  const rodapePaginacao = document.getElementById("rodape-paginacao");
  const textoPaginacao = document.getElementById("texto-paginacao");
  const btnPaginaAnterior = document.getElementById("btn-pagina-anterior");
  const btnPaginaProxima = document.getElementById("btn-pagina-proxima");

  let paginaAtual = 1;
  let totalPaginas = 1;

  function formatarMoeda(valor) {
    if (valor === null || valor === undefined) return "-";
    return "R$ " + Number(valor).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatarDataHora(isoString) {
    if (!isoString) return "-";
    const data = new Date(isoString);
    if (isNaN(data.getTime())) return "-";
    return data.toLocaleString("pt-BR");
  }

  function escaparHtml(texto) {
    const div = document.createElement("div");
    div.textContent = texto === null || texto === undefined ? "" : String(texto);
    return div.innerHTML;
  }

  function montarUrl() {
    const params = new URLSearchParams();
    if (campoDesde.value) params.set("desde", campoDesde.value + "T00:00:00");
    if (campoAte.value) params.set("ate", campoAte.value + "T23:59:59");
    if (campoBusca.value.trim()) params.set("busca", campoBusca.value.trim());
    params.set("pagina", String(paginaAtual));
    return API_HISTORICO + "?" + params.toString();
  }

  function renderizarLinhas(itens) {
    corpoTabela.innerHTML = itens
      .map((item) => {
        const tagResultado = item.sucesso
          ? '<span class="tag-ok">✓ aderiu</span>'
          : '<span class="tag-falhou" title="' + escaparHtml(item.motivo || "") + '">✕ não aderiu</span>';
        return (
          "<tr>" +
          "<td>" + escaparHtml(formatarDataHora(item.data_adesao)) + "</td>" +
          "<td>" + escaparHtml(item.nome || "-") + "</td>" +
          "<td>" + escaparHtml(item.sku || "-") + " · " + escaparHtml(item.item_id || "-") + "</td>" +
          "<td>" + escaparHtml(item.tipo_promocao || "-") + "</td>" +
          "<td class=\"col-numero\">" + escaparHtml(formatarMoeda(item.preco_final)) + "</td>" +
          "<td class=\"col-numero\">" + escaparHtml(formatarMoeda(item.lucro_liquido)) + "</td>" +
          "<td>" + tagResultado + "</td>" +
          "<td class=\"quem\">" + escaparHtml(item.usuario || "-") + (item.maquina ? " (" + escaparHtml(item.maquina) + ")" : "") + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function carregar() {
    carregando.style.display = "block";
    tabela.style.display = "none";
    mensagemVazia.style.display = "none";
    rodapePaginacao.style.display = "none";

    fetch(montarUrl())
      .then((resposta) => {
        if (!resposta.ok) throw new Error("Servidor respondeu " + resposta.status);
        return resposta.json();
      })
      .then((dados) => {
        carregando.style.display = "none";
        totalPaginas = dados.total_paginas || 0;
        const itens = dados.itens || [];

        if (!itens.length) {
          mensagemVazia.style.display = "block";
          return;
        }

        renderizarLinhas(itens);
        tabela.style.display = "table";

        if (totalPaginas > 1) {
          rodapePaginacao.style.display = "flex";
          textoPaginacao.textContent = "Página " + dados.pagina + " de " + totalPaginas + " (" + dados.total + " adesões no período)";
          btnPaginaAnterior.disabled = dados.pagina <= 1;
          btnPaginaProxima.disabled = dados.pagina >= totalPaginas;
        }
      })
      .catch((erro) => {
        carregando.style.display = "none";
        mensagemVazia.textContent = "Não consegui carregar o histórico agora (" + erro.message + "). Tente de novo em alguns segundos.";
        mensagemVazia.style.display = "block";
      });
  }

  btnFiltrar.addEventListener("click", () => {
    paginaAtual = 1;
    carregar();
  });

  btnLimpar.addEventListener("click", () => {
    campoDesde.value = "";
    campoAte.value = "";
    campoBusca.value = "";
    paginaAtual = 1;
    carregar();
  });

  campoBusca.addEventListener("keydown", (evento) => {
    if (evento.key === "Enter") {
      paginaAtual = 1;
      carregar();
    }
  });

  btnPaginaAnterior.addEventListener("click", () => {
    if (paginaAtual > 1) {
      paginaAtual--;
      carregar();
    }
  });

  btnPaginaProxima.addEventListener("click", () => {
    if (paginaAtual < totalPaginas) {
      paginaAtual++;
      carregar();
    }
  });

  carregar();
})();
