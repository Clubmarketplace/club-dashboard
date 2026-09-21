const seletorConta = document.getElementById("seletor-conta");
const seletorDias = document.getElementById("seletor-dias");

let graficoTendencia = null;
let graficoMotivos = null;
let graficoCondicao = null;

const formatarMoeda = (valor) =>
  (valor || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

const ROTULOS_STATUS = {
  opened: "Aberta",
  shipped: "Enviada",
  delivered: "Entregue",
  not_delivered: "Não entregue",
  closed: "Encerrada",
  cancelled: "Cancelada",
  failed: "Falhou",
  expired: "Expirada",
};

const ROTULOS_CONDICAO = {
  saleable: "Pode revender",
  unsaleable: "Não revendável",
  discard: "Descartado",
};

const CORES_CONDICAO = {
  saleable: "#2f6f4f",
  unsaleable: "#d98e2f",
  discard: "#c1502e",
};

Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.color = "#6b7684";
Chart.defaults.borderColor = "#e4e7eb";

async function buscarJson(caminho) {
  const resposta = await fetch(caminho);
  return resposta.json();
}

async function carregarContas() {
  const contas = await buscarJson("/api/devolucoes/contas");
  seletorConta.innerHTML = '<option value="">Todas as contas</option>';
  contas.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.apelido;
    seletorConta.appendChild(opt);
  });
}

function montarQueryString() {
  const params = new URLSearchParams();
  params.set("dias", seletorDias.value);
  if (seletorConta.value) params.set("conta_id", seletorConta.value);
  return params.toString();
}

let ultimoResumo = null;

async function carregarResumo() {
  const dados = await buscarJson(`/api/devolucoes/resumo?${montarQueryString()}`);
  ultimoResumo = dados;
  document.getElementById("kpi-total").textContent = dados.total_devolucoes;
  document.getElementById("kpi-abertas").textContent = dados.devolucoes_abertas;
  document.getElementById("kpi-valor").textContent = formatarMoeda(dados.valor_envolvido);
  document.getElementById("kpi-frete").textContent = formatarMoeda(dados.custo_frete_retorno);
  document.getElementById("kpi-impacto").textContent = formatarMoeda(dados.impacto_total);
  document.getElementById("kpi-motivo").textContent = dados.motivo_mais_comum
    ? `${dados.motivo_mais_comum.motivo} (${dados.motivo_mais_comum.quantidade})`
    : "Sem dados no período";

  desenharGraficoCondicao(dados.condicao_produto || {});
}

function desenharGraficoCondicao(condicoes) {
  const chaves = Object.keys(condicoes);
  const ctx = document.getElementById("grafico-condicao").getContext("2d");

  if (graficoCondicao) graficoCondicao.destroy();

  if (chaves.length === 0) {
    // sem dado nenhum (nenhuma devolução passou pelo depósito) — evita
    // desenhar um gráfico vazio sem sentido
    return;
  }

  graficoCondicao = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: chaves.map((k) => ROTULOS_CONDICAO[k] || k),
      datasets: [
        {
          data: chaves.map((k) => condicoes[k]),
          backgroundColor: chaves.map((k) => CORES_CONDICAO[k] || "#6b7684"),
          borderWidth: 0,
        },
      ],
    },
    options: {
      cutout: "68%",
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, padding: 14, font: { size: 12 } } },
      },
    },
  });
}

async function carregarTendencia() {
  const params = new URLSearchParams();
  if (seletorConta.value) params.set("conta_id", seletorConta.value);
  const dados = await buscarJson(`/api/devolucoes/tendencia-mensal?${params.toString()}`);

  const ctx = document.getElementById("grafico-tendencia").getContext("2d");
  const rotulos = dados.map((d) => d.mes);
  const valores = dados.map((d) => d.quantidade);

  const gradiente = ctx.createLinearGradient(0, 0, 0, 220);
  gradiente.addColorStop(0, "rgba(217,142,47,0.22)");
  gradiente.addColorStop(1, "rgba(217,142,47,0)");

  if (graficoTendencia) graficoTendencia.destroy();
  graficoTendencia = new Chart(ctx, {
    type: "line",
    data: {
      labels: rotulos,
      datasets: [
        {
          label: "Devoluções",
          data: valores,
          borderColor: "#d98e2f",
          backgroundColor: gradiente,
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          pointBackgroundColor: "#d98e2f",
          pointBorderColor: "#ffffff",
          pointBorderWidth: 1.5,
          borderWidth: 2,
        },
      ],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#eef0f2" } },
        x: { grid: { display: false } },
      },
    },
  });
}

async function carregarMotivos() {
  const dados = await buscarJson(`/api/devolucoes/por-motivo?${montarQueryString()}`);
  const ctx = document.getElementById("grafico-motivos").getContext("2d");

  if (graficoMotivos) graficoMotivos.destroy();
  graficoMotivos = new Chart(ctx, {
    type: "bar",
    data: {
      labels: dados.map((d) => d.motivo),
      datasets: [
        {
          data: dados.map((d) => d.quantidade),
          backgroundColor: "#12181f",
          borderRadius: 5,
          maxBarThickness: 26,
        },
      ],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#eef0f2" } },
        y: { grid: { display: false } },
      },
    },
  });
}

async function carregarTabela() {
  const dados = await buscarJson(`/api/devolucoes/lista?${montarQueryString()}`);
  const corpo = document.querySelector("#tabela-devolucoes tbody");
  corpo.innerHTML = "";

  dados.forEach((d) => {
    const tr = document.createElement("tr");
    const dataFormatada = d.data_criacao ? new Date(d.data_criacao).toLocaleDateString("pt-BR") : "—";
    const statusRotulo = ROTULOS_STATUS[d.status] || d.status;
    const eAberta = ["opened", "shipped", "delivered", "not_delivered"].includes(d.status);
    const condicaoRotulo = ROTULOS_CONDICAO[d.product_condition] || "—";

    tr.innerHTML = `
      <td>${dataFormatada}</td>
      <td>${d.nome_produto || "—"}</td>
      <td>${d.sku || "—"}</td>
      <td>${d.motivo || "—"}</td>
      <td><span class="cmx-selo ${eAberta ? "cmx-selo-aberto" : "cmx-selo-encerrado"}">${statusRotulo}</span></td>
      <td>${condicaoRotulo}</td>
      <td>${formatarMoeda(d.valor)}</td>
    `;
    corpo.appendChild(tr);
  });
}

async function carregarTudo() {
  await Promise.all([carregarResumo(), carregarTendencia(), carregarMotivos(), carregarTabela()]);
}

seletorConta.addEventListener("change", carregarTudo);
seletorDias.addEventListener("change", carregarTudo);

carregarContas().then(carregarTudo);
