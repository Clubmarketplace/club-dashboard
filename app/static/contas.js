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
    const botaoInativar = EH_ADMIN
      ? `<button class="cmx-botao-link-perigo" data-inativar="${conta.id}" data-apelido="${escaparHtml(conta.apelido)}" style="margin-left: 10px;">Inativar</button>`
      : "";
    linha.innerHTML = `
      <td>${conta.apelido}</td>
      <td>${montarSeloStatus(conta)}</td>
      <td>${conta.ml_user_id || "—"}</td>
      <td>${formatarData(conta.conectada_em)}</td>
      <td>${formatarData(conta.token_expira_em)}</td>
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
}

// --- Credenciais do app ClubMarketplace (promoções), tabela própria ------
// 08/10: cadastro de client_id/client_secret tem sua própria tabela nessa
// tela ("Credenciais ClubMarketplace (Promoções)"), separada da tabela
// "Contas cadastradas" (que já tem muita coluna) -- mesmo padrão visual
// da tabela "Progresso de autorização".
let CONTAS_POR_ID = {};

function renderizarCmxTabela(todas) {
  const contas = todas.filter((c) => !c.inativa);
  const corpo = document.querySelector("#tabela-cmx-credenciais tbody");
  if (!corpo) return;
  corpo.innerHTML = "";

  if (contas.length === 0) {
    corpo.innerHTML = `
      <tr><td colspan="4" style="text-align:center; color: var(--cmx-texto-suave);">
        Nenhuma conta cadastrada ainda.
      </td></tr>`;
    return;
  }

  for (const conta of contas) {
    CONTAS_POR_ID[conta.id] = conta;
    const linha = document.createElement("tr");
    linha.id = `cmx-linha-${conta.id}`;
    linha.innerHTML = montarLinhaCmxVisualizacao(conta);
    corpo.appendChild(linha);
  }

  corpo.querySelectorAll("[data-configurar-cmx]").forEach((botao) => {
    botao.addEventListener("click", () => abrirEdicaoCmx(botao.dataset.configurarCmx));
  });
}

function montarLinhaCmxVisualizacao(conta) {
  const idTexto = conta.cmx_configurado
    ? `<code style="font-size:12px;">${escaparHtml(conta.cmx_client_id)}</code>`
    : `<span style="color:var(--cmx-texto-suave);">—</span>`;
  const secretTexto = conta.cmx_configurado
    ? `<span style="font-family:monospace; letter-spacing:2px; color:var(--cmx-texto-suave);">••••••••</span>`
    : `<span style="color:var(--cmx-texto-suave);">—</span>`;
  const botao = EH_ADMIN
    ? `<button type="button" class="cmx-botao-link-perigo" style="color:var(--cmx-azul,#2b6cb0);" data-configurar-cmx="${conta.id}">${conta.cmx_configurado ? "Editar" : "Configurar"}</button>`
    : "—";
  return `
    <td>${escaparHtml(conta.apelido)}</td>
    <td>${idTexto}</td>
    <td>${secretTexto}</td>
    <td>${botao}</td>
  `;
}

function abrirEdicaoCmx(contaId) {
  const conta = CONTAS_POR_ID[contaId];
  const linha = document.getElementById(`cmx-linha-${contaId}`);
  if (!conta || !linha) return;
  linha.innerHTML = `
    <td>${escaparHtml(conta.apelido)}</td>
    <td colspan="3">
      <form autocomplete="off" style="display:flex; flex-wrap:wrap; align-items:flex-start; gap:10px;">
        <input type="text" style="display:none;" />
        <input type="password" style="display:none;" />
        <div style="display:flex; flex-direction:column; gap:2px;">
          <label style="font-size:11px; font-weight:600; color:var(--cmx-texto-suave);">client_id</label>
          <input type="text" id="cmx-edit-id-${contaId}" name="cmx_client_id_${contaId}" placeholder="client_id" value="${escaparHtml(conta.cmx_client_id || "")}" autocomplete="off" data-lpignore="true" data-1p-ignore style="padding:6px 8px; border-radius:6px; border:1px solid var(--cmx-borda); font-size:13px; min-width:200px;" />
        </div>
        <div style="display:flex; flex-direction:column; gap:2px;">
          <label style="font-size:11px; font-weight:600; color:var(--cmx-texto-suave);">client_secret</label>
          <div class="cmx-campo-senha">
            <input type="password" id="cmx-edit-secret-${contaId}" name="cmx_client_secret_${contaId}" placeholder="client_secret" autocomplete="new-password" data-lpignore="true" data-1p-ignore style="padding:6px 8px; border-radius:6px; border:1px solid var(--cmx-borda); font-size:13px; width:100%; min-width:200px;" />
            <button type="button" class="cmx-botao-olho" onclick="alternarSenhaCmx('cmx-edit-secret-${contaId}', this)" aria-label="Mostrar client_secret">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
          </div>
        </div>
        <div style="display:flex; gap:6px; align-self:flex-end;">
          <button type="button" class="cmx-botao-primario" style="padding:6px 10px; font-size:13px;" data-salvar-cmx="${contaId}">Salvar</button>
          <button type="button" class="cmx-botao-secundario" style="padding:6px 10px; font-size:13px;" data-cancelar-cmx="${contaId}">Cancelar</button>
        </div>
      </form>
      <p id="cmx-edit-aviso-${contaId}" style="display:none; font-size:12px; color:var(--cmx-rust); margin:6px 0 0;"></p>
    </td>
  `;
  // O navegador às vezes preenche sozinho um campo de texto vazio/parecido
  // com login com o último usuário salvo (ex: "Admin") -- limpa de novo
  // logo depois, só se não tiver sido um valor que a gente mesmo colocou
  // (client_id já cadastrado).
  setTimeout(() => {
    const campoId = document.getElementById(`cmx-edit-id-${contaId}`);
    if (campoId && campoId.value && campoId.value !== (conta.cmx_client_id || "") && !campoId.value.startsWith(String(conta.cmx_client_id || "\u0000"))) {
      campoId.value = conta.cmx_client_id || "";
    }
    const campoSecret = document.getElementById(`cmx-edit-secret-${contaId}`);
    if (campoSecret) campoSecret.value = "";
  }, 60);
  linha.querySelector(`[data-salvar-cmx="${contaId}"]`).addEventListener("click", () => salvarCmxLinha(contaId));
  linha.querySelector(`[data-cancelar-cmx="${contaId}"]`).addEventListener("click", () => {
    linha.innerHTML = montarLinhaCmxVisualizacao(conta);
    linha.querySelectorAll("[data-configurar-cmx]").forEach((botao) => {
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
    const linha = document.getElementById(`cmx-linha-${contaId}`);
    linha.innerHTML = montarLinhaCmxVisualizacao(conta);
    linha.querySelectorAll("[data-configurar-cmx]").forEach((botao) => {
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
  const contas = await carregarContas();
  renderizarTabela(contas);
  renderizarCmxTabela(contas);
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
    renderizarCmxTabela(contas);
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
    renderizarCmxTabela(contas);
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

// --- Credencial do app ClubMarketplace (promoções), por conta -----------
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

async function inicializar() {
  // Cadastro de credencial do app ClubMarketplace é ação administrativa
  // (o servidor também confere) -- esconde a seção pra quem não é admin.
  const secaoCmxCredenciais = document.getElementById("secao-cmx-credenciais");
  if (secaoCmxCredenciais && !EH_ADMIN) secaoCmxCredenciais.style.display = "none";

  try {
    const contas = await carregarContas();
    renderizarTabela(contas);
    renderizarCmxTabela(contas);
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
