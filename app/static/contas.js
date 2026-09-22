/**
 * Tela "Contas conectadas": lista as contas cadastradas com o status
 * da conexão OAuth, e permite iniciar a autorização de uma conta nova.
 */

async function carregarContas() {
  const resposta = await fetch("/auth/contas");
  if (!resposta.ok) {
    throw new Error(`Falha ao carregar contas (status ${resposta.status})`);
  }
  return resposta.json();
}

async function carregarProgresso() {
  const resposta = await fetch("/auth/progresso");
  if (!resposta.ok) {
    throw new Error(`Falha ao carregar progresso (status ${resposta.status})`);
  }
  return resposta.json();
}

function renderizarProgresso(dados) {
  document.getElementById("progresso-contador").textContent = `(${dados.autorizadas} de ${dados.total})`;
  const corpo = document.querySelector("#tabela-progresso tbody");
  corpo.innerHTML = "";

  if (dados.empresas.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="3" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhuma empresa na lista ainda -- edite dados/empresas.txt e rode o importador.
      </td></tr>`;
    return;
  }

  dados.empresas.forEach((empresa) => {
    const tr = document.createElement("tr");
    const selo = empresa.autorizada
      ? `<span class="cmx-selo cmx-selo-aberto">Autorizada</span>`
      : `<span class="cmx-selo cmx-selo-encerrado">Pendente</span>`;
    tr.innerHTML = `
      <td>${empresa.nome}</td>
      <td>${selo}</td>
      <td>${formatarData(empresa.conectada_em)}</td>
    `;
    corpo.appendChild(tr);
  });
}

function formatarData(isoString) {
  if (!isoString) return "—";
  const data = new Date(isoString);
  return data.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function montarSeloStatus(conta) {
  if (!conta.conectada) {
    return `<span class="cmx-selo cmx-selo-aberto">Não conectada</span>`;
  }
  if (conta.token_expirado) {
    return `<span class="cmx-selo cmx-selo-alerta">Token expirado</span>`;
  }
  return `<span class="cmx-selo cmx-selo-encerrado">Conectada</span>`;
}

function renderizarTabela(contas) {
  const corpo = document.querySelector("#tabela-contas tbody");
  corpo.innerHTML = "";

  if (contas.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="6" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhuma conta cadastrada ainda. Use o formulário acima pra conectar a primeira.
      </td></tr>`;
    return;
  }

  for (const conta of contas) {
    const linha = document.createElement("tr");
    const botaoDesconectar = conta.conectada
      ? `<button class="cmx-botao-link-perigo" data-desconectar="${conta.id}" data-apelido="${conta.apelido}">Desconectar</button>`
      : "";
    const botaoExcluir = `<button class="cmx-botao-link-perigo" data-excluir="${conta.id}" data-apelido="${conta.apelido}" style="margin-left: 10px;">Excluir</button>`;
    linha.innerHTML = `
      <td>${conta.apelido}</td>
      <td>${montarSeloStatus(conta)}</td>
      <td>${conta.ml_user_id || "—"}</td>
      <td>${formatarData(conta.conectada_em)}</td>
      <td>${formatarData(conta.token_expira_em)}</td>
      <td>${botaoDesconectar}${botaoExcluir}</td>
    `;
    corpo.appendChild(linha);
  }

  corpo.querySelectorAll("[data-desconectar]").forEach((botao) => {
    botao.addEventListener("click", () => desconectarConta(botao));
  });
  corpo.querySelectorAll("[data-excluir]").forEach((botao) => {
    botao.addEventListener("click", () => excluirConta(botao));
  });
}

async function desconectarConta(botao) {
  const contaId = botao.getAttribute("data-desconectar");
  const apelido = botao.getAttribute("data-apelido");

  const confirmou = window.confirm(
    `Desconectar a conta "${apelido}"?\n\nOs tokens salvos serão apagados. ` +
    `Pra voltar a usar essa conta, será preciso autorizar de novo pelo Mercado Livre.`
  );
  if (!confirmou) return;

  botao.disabled = true;
  botao.textContent = "Desconectando...";

  try {
    const resposta = await fetch(`/auth/desconectar/${contaId}`, { method: "POST" });
    if (!resposta.ok) {
      throw new Error(`Falha ao desconectar (status ${resposta.status})`);
    }
    const contas = await carregarContas();
    renderizarTabela(contas);
  } catch (erro) {
    mostrarAviso("Não foi possível desconectar essa conta. Tente novamente.");
    console.error(erro);
    botao.disabled = false;
    botao.textContent = "Desconectar";
  }
}

async function excluirConta(botao) {
  const contaId = botao.getAttribute("data-excluir");
  const apelido = botao.getAttribute("data-apelido");

  const confirmou = window.confirm(
    `Excluir a conta "${apelido}" por completo?\n\n` +
    `Diferente de "Desconectar", isso apaga o registro inteiro -- não dá pra desfazer. ` +
    `Só funciona se essa conta nunca teve devolução ou ação registrada.`
  );
  if (!confirmou) return;

  botao.disabled = true;
  botao.textContent = "Excluindo...";

  try {
    const resposta = await fetch(`/auth/contas/${contaId}`, { method: "DELETE" });
    if (!resposta.ok) {
      const dados = await resposta.json().catch(() => null);
      throw new Error(dados && dados.detail ? dados.detail : `Falha ao excluir (status ${resposta.status})`);
    }
    const contas = await carregarContas();
    renderizarTabela(contas);
  } catch (erro) {
    mostrarAviso(erro.message || "Não foi possível excluir essa conta. Tente novamente.");
    console.error(erro);
    botao.disabled = false;
    botao.textContent = "Excluir";
  }
}

function mostrarAviso(mensagem) {
  const aviso = document.getElementById("conectar-aviso");
  aviso.textContent = mensagem;
  aviso.classList.add("cmx-aviso-erro");
  aviso.style.display = "block";
}

async function inicializar() {
  try {
    const contas = await carregarContas();
    renderizarTabela(contas);
  } catch (erro) {
    mostrarAviso("Não foi possível carregar as contas. Verifique se o backend está rodando.");
    console.error(erro);
  }

  try {
    const progresso = await carregarProgresso();
    renderizarProgresso(progresso);
  } catch (erro) {
    console.error(erro);
  }

  async function gerarLink() {
    const apelido = document.getElementById("input-apelido").value.trim();
    if (!apelido) {
      mostrarAviso("Digite o apelido da conta antes de conectar.");
      return;
    }
    try {
      const resposta = await fetch(`/auth/link/${encodeURIComponent(apelido)}`);
      if (!resposta.ok) throw new Error(`status ${resposta.status}`);
      const dados = await resposta.json();

      document.getElementById("link-gerado-empresa").textContent = apelido;
      document.getElementById("link-gerado-valor").value = dados.link;
      document.getElementById("link-gerado-box").style.display = "block";
    } catch (erro) {
      mostrarAviso("Não foi possível gerar o link. Verifique se o backend está rodando.");
      console.error(erro);
    }
  }

  // Form sem botão de submit visível, mas mantém o Enter no campo de
  // texto funcionando -- previne o recarregamento padrão e reusa a
  // mesma função do botão único.
  document.getElementById("form-conectar").addEventListener("submit", (evento) => {
    evento.preventDefault();
    gerarLink();
  });

  document.getElementById("btn-gerar-link").addEventListener("click", gerarLink);

  document.getElementById("btn-copiar-link-gerado").addEventListener("click", async () => {
    const campo = document.getElementById("link-gerado-valor");
    campo.select();
    try {
      await navigator.clipboard.writeText(campo.value);
      mostrarAviso(`Link de "${document.getElementById("link-gerado-empresa").textContent}" copiado!`);
      document.getElementById("conectar-aviso").classList.remove("cmx-aviso-erro");
    } catch (erro) {
      console.error(erro);
    }
  });
}

document.addEventListener("DOMContentLoaded", inicializar);
