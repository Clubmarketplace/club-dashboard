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

let PLATAFORMAS_OPCOES = {};

function escaparHtml(t) {
  return String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderizarProgresso(dados) {
  PLATAFORMAS_OPCOES = dados.plataformas_opcoes || {};
  document.getElementById("progresso-contador").textContent = `(${dados.autorizadas} de ${dados.total})`;
  const corpo = document.querySelector("#tabela-progresso tbody");
  corpo.innerHTML = "";

  if (dados.empresas.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="4" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhuma empresa na lista ainda -- clique em "+ Incluir empresa".
      </td></tr>`;
    return;
  }

  // Pendentes primeiro (é o que falta fazer); dentro de cada grupo, por nome.
  const ordenadas = [...dados.empresas].sort((a, b) => (a.autorizada - b.autorizada) || a.nome.localeCompare(b.nome, "pt-BR"));
  ordenadas.forEach((empresa) => {
    const tr = document.createElement("tr");
    // Verde = autorizada (em dia); laranja = pendente (falta fazer).
    const selo = empresa.autorizada
      ? `<span class="cmx-selo cmx-selo-encerrado">Autorizada</span>`
      : `<span class="cmx-selo cmx-selo-alerta">Pendente</span>`;
    const nome = escaparHtml(empresa.nome);
    const obs = empresa.observacao ? `<div style="font-size:12px; color:var(--cmx-texto-suave);">${escaparHtml(empresa.observacao)}</div>` : "";
    tr.innerHTML = `
      <td>${nome}${obs}</td>
      <td>${selo}</td>
      <td>${formatarData(empresa.conectada_em)}</td>
      <td style="white-space:nowrap;">
        ${empresa.autorizada ? "" : `<button type="button" data-gerar-link="${nome}" style="background:none; border:none; padding:0; font:inherit; font-size:13px; font-weight:600; color:var(--cmx-verde); text-decoration:underline; cursor:pointer;">Gerar link</button>`}
        <button type="button" class="cmx-botao-link-perigo" data-remover-empresa="${empresa.id}" data-nome="${nome}" style="margin-left:10px;">Remover da lista</button>
      </td>
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

// Inativar/Reativar é só do admin (o servidor também confere).
const EH_ADMIN = window.CMX_PAPEL === "admin";

function escaparHtml(texto) {
  return String(texto ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderizarTabela(todas) {
  // Separa: ativas na tabela principal, inativas (saíram do Club) na seção de baixo.
  const contas = todas.filter((c) => !c.inativa);
  renderizarInativas(todas.filter((c) => c.inativa));
  const corpo = document.querySelector("#tabela-contas tbody");
  corpo.innerHTML = "";

  if (contas.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="7" style="text-align:center; color: var(--cmx-texto-suave);">
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
    const botaoInativar = EH_ADMIN
      ? `<button class="cmx-botao-link-perigo" data-inativar="${conta.id}" data-apelido="${escaparHtml(conta.apelido)}" style="margin-left: 10px;">Inativar</button>`
      : "";
    linha.innerHTML = `
      <td>${conta.apelido}</td>
      <td>${montarSeloStatus(conta)}</td>
      <td>${conta.ml_user_id || "—"}</td>
      <td>${formatarData(conta.conectada_em)}</td>
      <td>${formatarData(conta.token_expira_em)}</td>
      <td id="cmx-celula-${conta.id}">${montarVisaoCmx(conta)}</td>
      <td>${botaoDesconectar}${botaoInativar}${botaoExcluir}</td>
    `;
    corpo.appendChild(linha);
  }

  corpo.querySelectorAll("[data-desconectar]").forEach((botao) => {
    botao.addEventListener("click", () => desconectarConta(botao));
  });
  corpo.querySelectorAll("[data-excluir]").forEach((botao) => {
    botao.addEventListener("click", () => excluirConta(botao));
  });
  corpo.querySelectorAll("[data-inativar]").forEach((botao) => {
    botao.addEventListener("click", () => inativarConta(botao));
  });
  corpo.querySelectorAll("[data-configurar-cmx]").forEach((botao) => {
    botao.addEventListener("click", () => abrirEdicaoCmx(botao.dataset.configurarCmx));
  });
}

// --- Credencial do app ClubMarketplaceX (promoções), direto na linha -----
// 08/10: cadastro de client_id/client_secret movido pra dentro da própria
// linha da conta (um só lugar, junto com o status) em vez de um formulário
// solto em outra parte da tela -- menos lugares pra procurar.
let CONTAS_POR_ID = {};

function montarVisaoCmx(conta) {
  CONTAS_POR_ID[conta.id] = conta;
  const selo = conta.cmx_configurado
    ? `<span class="cmx-selo cmx-selo-encerrado">Configurado</span><div style="font-size:11px; color:var(--cmx-texto-suave); margin-top:3px; font-family:monospace;">ID: ${escaparHtml(conta.cmx_client_id)}</div>`
    : `<span class="cmx-selo cmx-selo-aberto">Não configurado</span>`;
  const botao = EH_ADMIN
    ? `<button type="button" class="cmx-botao-link-perigo" style="color:var(--cmx-azul,#2b6cb0); margin-top:4px;" data-configurar-cmx="${conta.id}">${conta.cmx_configurado ? "Editar" : "Configurar"}</button>`
    : "";
  return `${selo}${botao ? `<div>${botao}</div>` : ""}`;
}

function abrirEdicaoCmx(contaId) {
  const conta = CONTAS_POR_ID[contaId];
  const celula = document.getElementById(`cmx-celula-${contaId}`);
  if (!conta || !celula) return;
  celula.innerHTML = `
    <div style="display:flex; flex-direction:column; gap:6px; min-width:200px;">
      <input type="text" id="cmx-edit-id-${contaId}" placeholder="client_id" value="${escaparHtml(conta.cmx_client_id || "")}" autocomplete="off" style="padding:6px 8px; border-radius:6px; border:1px solid var(--cmx-borda); font-size:13px;" />
      <div class="cmx-campo-senha">
        <input type="password" id="cmx-edit-secret-${contaId}" placeholder="client_secret" autocomplete="off" style="padding:6px 8px; border-radius:6px; border:1px solid var(--cmx-borda); font-size:13px; width:100%;" />
        <button type="button" class="cmx-botao-olho" onclick="alternarSenhaCmx('cmx-edit-secret-${contaId}', this)" aria-label="Mostrar client_secret">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
      </div>
      <p id="cmx-edit-aviso-${contaId}" style="display:none; font-size:12px; color:var(--cmx-rust); margin:0;"></p>
      <div style="display:flex; gap:6px;">
        <button type="button" class="cmx-botao-primario" style="padding:6px 10px; font-size:13px;" data-salvar-cmx="${contaId}">Salvar</button>
        <button type="button" class="cmx-botao-secundario" style="padding:6px 10px; font-size:13px;" data-cancelar-cmx="${contaId}">Cancelar</button>
      </div>
    </div>
  `;
  celula.querySelector(`[data-salvar-cmx="${contaId}"]`).addEventListener("click", () => salvarCmxLinha(contaId));
  celula.querySelector(`[data-cancelar-cmx="${contaId}"]`).addEventListener("click", () => {
    celula.innerHTML = montarVisaoCmx(conta);
    celula.querySelectorAll("[data-configurar-cmx]").forEach((botao) => {
      botao.addEventListener("click", () => abrirEdicaoCmx(botao.dataset.configurarCmx));
    });
  });
}

async function salvarCmxLinha(contaId) {
  const conta = CONTAS_POR_ID[contaId];
  const clientId = document.getElementById(`cmx-edit-id-${contaId}`).value.trim();
  const clientSecret = document.getElementById(`cmx-edit-secret-${contaId}`).value.trim();
  const aviso = document.getElementById(`cmx-edit-aviso-${contaId}`);
  const botaoSalvar = document.querySelector(`[data-salvar-cmx="${contaId}"]`);

  if (!clientId || !clientSecret) {
    aviso.textContent = "Preencha client_id e client_secret.";
    aviso.style.display = "block";
    return;
  }

  botaoSalvar.disabled = true;
  botaoSalvar.textContent = "Salvando...";
  try {
    const resposta = await fetch(`/api/cmx/admin/app-promocoes/${encodeURIComponent(conta.apelido)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    });
    const dados = await resposta.json().catch(() => ({}));
    if (!resposta.ok) throw new Error(dados.detail || `status ${resposta.status}`);

    conta.cmx_configurado = true;
    conta.cmx_client_id = dados.cmx_client_id;
    const celula = document.getElementById(`cmx-celula-${contaId}`);
    celula.innerHTML = montarVisaoCmx(conta);
    celula.querySelectorAll("[data-configurar-cmx]").forEach((botao) => {
      botao.addEventListener("click", () => abrirEdicaoCmx(botao.dataset.configurarCmx));
    });
  } catch (erro) {
    aviso.textContent = "Não foi possível salvar: " + erro.message;
    aviso.style.display = "block";
    botaoSalvar.disabled = false;
    botaoSalvar.textContent = "Salvar";
  }
}

function renderizarInativas(inativas) {
  const secao = document.getElementById("secao-inativas");
  const corpo = document.querySelector("#tabela-inativas tbody");
  if (!secao || !corpo) return;
  secao.style.display = inativas.length ? "block" : "none";
  document.getElementById("inativas-contador").textContent = `(${inativas.length})`;
  corpo.innerHTML = "";
  for (const conta of inativas) {
    const linha = document.createElement("tr");
    const botaoReativar = EH_ADMIN
      ? `<button class="cmx-botao-link-perigo" style="color:#2f6f4f;" data-reativar="${conta.id}" data-apelido="${escaparHtml(conta.apelido)}">Reativar</button>`
      : "—";
    linha.innerHTML = `
      <td>${escaparHtml(conta.apelido)}</td>
      <td>${escaparHtml(conta.ml_user_id || "—")}</td>
      <td>${formatarData(conta.inativa_em)}</td>
      <td>${escaparHtml(conta.motivo_inativacao || "—")}</td>
      <td>${botaoReativar}</td>
    `;
    corpo.appendChild(linha);
  }
  corpo.querySelectorAll("[data-reativar]").forEach((botao) => {
    botao.addEventListener("click", () => reativarConta(botao));
  });
}

async function recarregarContas() {
  renderizarTabela(await carregarContas());
}

async function inativarConta(botao) {
  const contaId = botao.getAttribute("data-inativar");
  const apelido = botao.getAttribute("data-apelido");
  const motivo = window.prompt(
    `Inativar a conta "${apelido}"?\n\n` +
    `Ela sai das listas e filtros e é desconectada do Mercado Livre. ` +
    `O histórico NÃO é apagado e dá pra reativar depois.\n\nMotivo (opcional):`,
    "Saiu do Club"
  );
  if (motivo === null) return; // cancelou

  botao.disabled = true;
  botao.textContent = "Inativando...";
  try {
    const resposta = await fetch(`/auth/contas/${contaId}/inativar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ motivo }),
    });
    if (!resposta.ok) {
      const dados = await resposta.json().catch(() => null);
      throw new Error(dados && dados.detail ? dados.detail : `status ${resposta.status}`);
    }
    await recarregarContas();
  } catch (erro) {
    mostrarAviso(`Não foi possível inativar: ${erro.message}`);
    console.error(erro);
    botao.disabled = false;
    botao.textContent = "Inativar";
  }
}

async function reativarConta(botao) {
  const contaId = botao.getAttribute("data-reativar");
  const apelido = botao.getAttribute("data-apelido");
  if (!window.confirm(`Reativar a conta "${apelido}"?\n\nEla volta para as listas e filtros.`)) return;

  botao.disabled = true;
  botao.textContent = "Reativando...";
  try {
    const resposta = await fetch(`/auth/contas/${contaId}/reativar`, { method: "POST" });
    const dados = await resposta.json().catch(() => null);
    if (!resposta.ok) throw new Error(dados && dados.detail ? dados.detail : `status ${resposta.status}`);
    await recarregarContas();
    if (dados && dados.precisa_reconectar) {
      mostrarAviso(`"${apelido}" reativada. Ela está desconectada: gere o link de autorização acima e envie ao seller.`);
      document.getElementById("conectar-aviso").classList.remove("cmx-aviso-erro");
    }
  } catch (erro) {
    mostrarAviso(`Não foi possível reativar: ${erro.message}`);
    console.error(erro);
    botao.disabled = false;
    botao.textContent = "Reativar";
  }
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

// --- Credencial do app ClubMarketplaceX (promoções), por conta -----------
// 08/10: cada conta tem seu próprio client_id/client_secret, cadastrados
// aqui (tela visual) em vez de precisar de curl/terminal. Chama a mesma
// rota admin que já existia (app/routers/cmx.py), autenticada pelo cookie
// de sessão do painel (mesma sessão usada pro resto da tela).
// Mostrar/ocultar o client_secret digitado (mesmo padrão do olhinho da
// tela de login) -- só afeta o que o admin está digitando agora; o
// servidor nunca devolve um secret já salvo pra tela mostrar.
function alternarSenhaCmx(idCampo, botao) {
  const campo = document.getElementById(idCampo);
  const mostrando = campo.type === "text";
  campo.type = mostrando ? "password" : "text";
  botao.innerHTML = mostrando
    ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
    : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  botao.setAttribute("aria-label", mostrando ? "Mostrar client_secret" : "Ocultar client_secret");
}

let CMX_CLIENT_ID_POR_APELIDO = {};

function popularSelectContasCmx(contas) {
  const select = document.getElementById("cmx-select-conta");
  if (!select) return;
  const valorAtual = select.value;
  select.innerHTML = '<option value="">Selecione a conta...</option>';
  CMX_CLIENT_ID_POR_APELIDO = {};
  for (const conta of contas.filter((c) => !c.inativa)) {
    const opcao = document.createElement("option");
    opcao.value = conta.apelido;
    opcao.textContent = conta.cmx_configurado ? `${conta.apelido} (já configurada)` : conta.apelido;
    select.appendChild(opcao);
    if (conta.cmx_configurado) CMX_CLIENT_ID_POR_APELIDO[conta.apelido] = conta.cmx_client_id;
  }
  if (valorAtual) select.value = valorAtual;
}

function avisarCmxCredenciais(texto, erro) {
  const aviso = document.getElementById("cmx-credenciais-aviso");
  if (!aviso) return;
  aviso.textContent = texto;
  aviso.style.display = texto ? "block" : "none";
  aviso.style.color = erro ? "var(--cmx-rust)" : "var(--cmx-verde)";
}

async function inicializar() {
  // Cadastro de credencial do app ClubMarketplaceX é ação administrativa
  // (o servidor também confere) -- esconde a seção pra quem não é admin.
  const secaoCmxCredenciais = document.getElementById("secao-cmx-credenciais");
  if (secaoCmxCredenciais && !EH_ADMIN) secaoCmxCredenciais.style.display = "none";

  try {
    const contas = await carregarContas();
    renderizarTabela(contas);
    popularSelectContasCmx(contas);
  } catch (erro) {
    mostrarAviso("Não foi possível carregar as contas. Verifique se o backend está rodando.");
    console.error(erro);
  }

  const selectCmxConta = document.getElementById("cmx-select-conta");
  if (selectCmxConta) {
    // Ao escolher uma conta já configurada, mostra o client_id atual dela
    // pra conferência (o client_secret nunca é mostrado -- fica só no
    // servidor). Se o admin não mexer no campo, reenviar o formulário
    // troca o client_id pelo mesmo valor; só o secret precisa ser
    // preenchido de novo (ou reaproveitado se for o mesmo já cadastrado).
    selectCmxConta.addEventListener("change", () => {
      const apelido = selectCmxConta.value;
      document.getElementById("cmx-input-client-id").value = CMX_CLIENT_ID_POR_APELIDO[apelido] || "";
      document.getElementById("cmx-input-client-secret").value = "";
      avisarCmxCredenciais("");
    });
  }

  const formCmxCredenciais = document.getElementById("form-cmx-credenciais");
  if (formCmxCredenciais) {
    formCmxCredenciais.addEventListener("submit", async (evento) => {
      evento.preventDefault();
      const apelido = document.getElementById("cmx-select-conta").value;
      const clientId = document.getElementById("cmx-input-client-id").value.trim();
      const clientSecret = document.getElementById("cmx-input-client-secret").value.trim();
      if (!apelido) { avisarCmxCredenciais("Selecione a conta.", true); return; }
      if (!clientId || !clientSecret) { avisarCmxCredenciais("Preencha client_id e client_secret.", true); return; }

      const botao = document.getElementById("btn-salvar-cmx-credenciais");
      botao.disabled = true;
      botao.textContent = "Salvando...";
      try {
        const resposta = await fetch(`/api/cmx/admin/app-promocoes/${encodeURIComponent(apelido)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
        });
        const dados = await resposta.json().catch(() => ({}));
        if (!resposta.ok) throw new Error(dados.detail || `status ${resposta.status}`);

        avisarCmxCredenciais(`Credencial de "${dados.apelido}" salva com sucesso.`, false);
        document.getElementById("cmx-input-client-id").value = "";
        document.getElementById("cmx-input-client-secret").value = "";
        const contasAtualizadas = await carregarContas();
        renderizarTabela(contasAtualizadas);
        popularSelectContasCmx(contasAtualizadas);
        document.getElementById("cmx-select-conta").value = apelido;
      } catch (erro) {
        avisarCmxCredenciais("Não foi possível salvar: " + erro.message, true);
      } finally {
        botao.disabled = false;
        botao.textContent = "Salvar credencial";
      }
    });
  }

  try {
    const progresso = await carregarProgresso();
    renderizarProgresso(progresso);
  } catch (erro) {
    console.error(erro);
  }

  // ---------- Incluir / remover empresa da lista ----------
  const boxEmpresa = document.getElementById("form-empresa-box");
  const avisoEmpresa = document.getElementById("empresa-aviso");
  function avisarEmpresa(texto, erro) {
    avisoEmpresa.textContent = texto;
    avisoEmpresa.style.display = texto ? "block" : "none";
    avisoEmpresa.style.color = erro ? "var(--cmx-rust)" : "var(--cmx-verde)";
  }
  document.getElementById("btn-incluir-empresa").addEventListener("click", () => {
    avisarEmpresa("");
    boxEmpresa.style.display = "block";
    document.getElementById("empresa-nome").focus();
  });
  document.getElementById("btn-cancelar-empresa").addEventListener("click", () => {
    boxEmpresa.style.display = "none";
    document.getElementById("form-empresa").reset();
  });
  document.getElementById("form-empresa").addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const nome = document.getElementById("empresa-nome").value.trim();
    if (!nome) { avisarEmpresa("Informe o nome da empresa.", true); return; }
    const plataformas = ["mercado_livre"];  // por enquanto só Mercado Livre, igual às empresas que já estão na lista
    const botao = document.getElementById("btn-salvar-empresa");
    botao.disabled = true;
    try {
      const r = await fetch("/auth/empresas", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nome, plataformas, observacao: document.getElementById("empresa-obs").value.trim() }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `status ${r.status}`);
      boxEmpresa.style.display = "none";
      document.getElementById("form-empresa").reset();
      renderizarProgresso(await carregarProgresso());
      // Já deixa o link de autorização pronto pra copiar e mandar.
      document.getElementById("input-apelido").value = d.nome;
      await gerarLink();
      mostrarAviso(`"${d.nome}" ${d.reativada ? "voltou para" : "incluída na"} lista. O link de autorização está pronto logo acima — copie e envie.`);
      document.getElementById("conectar-aviso").classList.remove("cmx-aviso-erro");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (erro) {
      avisarEmpresa("Não foi possível incluir: " + erro.message, true);
    } finally {
      botao.disabled = false;
    }
  });
  document.getElementById("tabela-progresso").addEventListener("click", async (evento) => {
    const gerar = evento.target.closest("[data-gerar-link]");
    if (gerar) {
      document.getElementById("input-apelido").value = gerar.dataset.gerarLink;
      await gerarLink();
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    const remover = evento.target.closest("[data-remover-empresa]");
    if (!remover) return;
    if (!confirm(`Remover "${remover.dataset.nome}" da lista do Club?\n\nEla some da lista e dos formulários. As contas conectadas e o histórico NÃO são apagados.`)) return;
    try {
      const r = await fetch(`/auth/empresas/${remover.dataset.removerEmpresa}/remover`, { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `status ${r.status}`);
      renderizarProgresso(await carregarProgresso());
    } catch (erro) {
      mostrarAviso("Não foi possível remover: " + erro.message);
    }
  });

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
