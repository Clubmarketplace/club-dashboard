// Tela "Custos" do painel (seller) -- gerar planilha com os custos já
// cadastrados e importar planilha em lote. Busca/edição de um SKU
// específico (e o estoque ao vivo) ficou em "Produtos > Lista"
// (produtos_lista.js). Tudo aqui fala com /api/painel/custos (cookie de
// sessão do painel), a mesma tabela que a extensão lê em /api/cmx/custos.
(function () {
  "use strict";

  const API_BASE = "/api/painel/custos";

  const btnEscolherArquivo = document.getElementById("btn-escolher-arquivo");
  const inputPlanilha = document.getElementById("input-planilha");
  const btnGerarPlanilha = document.getElementById("btn-gerar-planilha");
  const resultadoImportacao = document.getElementById("resultado-importacao");

  function mostrarResultadoImportacao(texto, classe) {
    resultadoImportacao.classList.remove("oculto");
    resultadoImportacao.textContent = texto;
    resultadoImportacao.className = "msg " + (classe || "");
  }

  // --- Gerar planilha (exporta os custos já cadastrados) -------------

  btnGerarPlanilha.addEventListener("click", function () {
    btnGerarPlanilha.disabled = true;
    fetch(API_BASE)
      .then(function (r) {
        if (!r.ok) throw new Error("Não consegui buscar os custos.");
        return r.json();
      })
      .then(function (dados) {
        const itens = dados.itens || [];
        const linhas = [["SKU", "Custo", "Produto"]];
        itens.forEach(function (item) {
          linhas.push([item.sku, item.custo, item.nome_produto || ""]);
        });
        if (itens.length === 0) {
          linhas.push(["EXEMPLO-001", 25.9, "Nome do produto (opcional)"]);
        }
        const planilha = XLSX.utils.aoa_to_sheet(linhas);
        planilha["!cols"] = [{ wch: 20 }, { wch: 14 }, { wch: 32 }];
        const workbook = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(workbook, planilha, "Custos");
        XLSX.writeFile(workbook, "planilha_custos_sku.xlsx");
      })
      .catch(function (erro) { mostrarResultadoImportacao(erro.message, "erro"); })
      .finally(function () { btnGerarPlanilha.disabled = false; });
  });

  // --- Importar planilha (.xlsx ou .csv) -----------------------------
  // Mesmo formato de 2 colunas (SKU, Custo) que a extensão já usa em
  // popup.js -- sem diferenciar cabeçalho: a primeira linha em que a
  // segunda coluna não é um número é tratada como cabeçalho e pulada.

  btnEscolherArquivo.addEventListener("click", function () { inputPlanilha.click(); });

  function normalizarNumero(valor) {
    if (typeof valor === "number") return valor;
    const texto = String(valor || "").trim().replace(",", ".");
    const numero = parseFloat(texto);
    return isNaN(numero) ? null : numero;
  }

  function linhasParaItens(linhas) {
    // Aceita tanto o modelo de 2 colunas (SKU, Custo) quanto o de 3
    // colunas que a extensão gera na varredura (SKU, Custo, Produto) --
    // a 3ª coluna é sempre opcional, então uma planilha antiga com só
    // 2 colunas continua funcionando normalmente.
    const itens = [];
    let ignoradas = 0;
    linhas.forEach(function (linha, indice) {
      const sku = String(linha[0] || "").trim();
      const custo = normalizarNumero(linha[1]);
      const nomeProduto = linha[2] !== undefined ? String(linha[2] || "").trim() : "";
      if (!sku) return; // linha em branco, ignora sem contar como erro
      if (indice === 0 && custo === null) return; // cabeçalho (ex: "SKU","Custo","Produto")
      if (custo === null || custo <= 0) { ignoradas++; return; }
      const item = { sku: sku, custo: custo };
      if (nomeProduto) item.nome_produto = nomeProduto;
      itens.push(item);
    });
    return { itens: itens, ignoradas: ignoradas };
  }

  function enviarImportacao(itens, ignoradas) {
    if (itens.length === 0) {
      mostrarResultadoImportacao("Não encontrei nenhum SKU válido nesse arquivo.", "erro");
      return;
    }
    mostrarResultadoImportacao("Importando " + itens.length + " SKU(s)...", "");
    fetch(API_BASE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ itens: itens }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (corpo) { throw new Error(corpo.detail || "Não consegui importar a planilha."); });
        return r.json();
      })
      .then(function (dados) {
        let texto = "Importado! " + dados.total_processados + " SKU(s) atualizados.";
        if (ignoradas > 0) texto += " (" + ignoradas + " linha(s) ignoradas -- custo inválido ou em branco.)";
        mostrarResultadoImportacao(texto, "ok");
      })
      .catch(function (erro) { mostrarResultadoImportacao(erro.message, "erro"); });
  }

  inputPlanilha.addEventListener("change", function () {
    const arquivo = inputPlanilha.files[0];
    if (!arquivo) return;
    mostrarResultadoImportacao("Lendo arquivo...", "");

    const leitor = new FileReader();
    const ehCsv = /\.csv$/i.test(arquivo.name);

    leitor.onload = function () {
      try {
        let linhas;
        if (ehCsv) {
          linhas = String(leitor.result)
            .split(/\r?\n/)
            .filter(function (l) { return l.trim() !== ""; })
            .map(function (l) { return l.split(","); });
        } else {
          const workbook = XLSX.read(leitor.result, { type: "array" });
          const planilha = workbook.Sheets[workbook.SheetNames[0]];
          linhas = XLSX.utils.sheet_to_json(planilha, { header: 1, defval: "" });
        }
        const { itens, ignoradas } = linhasParaItens(linhas);
        enviarImportacao(itens, ignoradas);
      } catch (erro) {
        mostrarResultadoImportacao("Não consegui ler esse arquivo. Confira o formato (.xlsx ou .csv).", "erro");
      } finally {
        inputPlanilha.value = "";
      }
    };

    if (ehCsv) leitor.readAsText(arquivo, "utf-8");
    else leitor.readAsArrayBuffer(arquivo);
  });
})();
