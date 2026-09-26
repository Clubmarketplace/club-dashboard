/**
 * Tela "Respostas padrão": lista, filtros, liga/desliga, formulário
 * (criar/editar) com alcance "todas as contas" ou "só um produto", e o
 * botão Testar (pergunta à IA, no servidor, se usaria a resposta).
 * API: /api/respostas-padrao (ver app/routers/respostas_padrao.py).
 */
(function () {
  "use strict";

  const estado = { itens: [], filtro: "todas", busca: "", iaDisponivel: true, podeApagar: false, form: null };

  function esc(t) {
    return String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function aviso(texto, tipo) {
    const el = document.getElementById("rs-aviso");
    if (!texto) { el.style.display = "none"; return; }
    el.textContent = texto;
    el.style.display = "block";
    el.style.background = tipo === "erro" ? "#fbe9e3" : tipo === "ok" ? "#e3f5ea" : "#fdf3e3";
    el.style.color = tipo === "erro" ? "#96341c" : tipo === "ok" ? "#1a6641" : "#8a4200";
  }

  async function api(caminho, opcoes) {
    const r = await fetch("/api/respostas-padrao" + caminho, Object.assign({ headers: { "Content-Type": "application/json" } }, opcoes || {}));
    const dados = await r.json().catch(() => null);
    if (!r.ok) throw new Error((dados && dados.detail) || `status ${r.status}`);
    return dados;
  }

  async function carregar() {
    const d = await api("");
    estado.itens = d.itens;
    estado.iaDisponivel = d.ia_disponivel;
    estado.podeApagar = d.pode_apagar;
    if (!d.ia_disponivel && !estado.jaCarregou) aviso("A IA está sem chave configurada no servidor: as respostas ficam salvas, mas não serão usadas até a chave voltar.", "alerta");
    estado.jaCarregou = true;
    desenharLista();
  }

  // ---------------------------------------------------------------- lista
  function desenharLista() {
    const t = estado.busca.trim().toLowerCase();
    const lista = estado.itens.filter((r) => {
      if (estado.filtro === "geral" && r.alcance !== "geral") return false;
      if (estado.filtro === "produto" && r.alcance !== "produto") return false;
      if (estado.filtro === "desligadas" && r.ativa) return false;
      return !t || (r.tema + " " + r.resposta + " " + r.exemplos).toLowerCase().includes(t);
    });
    document.getElementById("rs-contador").textContent = `${lista.length} de ${estado.itens.length} respostas`;
    const alvo = document.getElementById("rs-lista");
    if (!lista.length) {
      alvo.innerHTML = `<div class="rs-vazio">${estado.itens.length ? "Nenhuma resposta com esse filtro." : "Nenhuma resposta cadastrada ainda. Clique em “+ Nova resposta”."}</div>`;
      return;
    }
    alvo.innerHTML = lista.map((r) => {
      const nEx = r.exemplos ? r.exemplos.split("\n").filter(Boolean).length : 0;
      const tag = r.alcance === "geral"
        ? `<span class="rs-tag" style="background:#e8f1fb;color:#174f80">Todas as contas</span>`
        : `<span class="rs-tag" style="background:#f3ecfc;color:#5b2f99" title="${esc(r.produto_nome || r.produto_chave)}">${esc(r.produto_nome || r.produto_chave)}</span>`;
      return `<div class="rs-linha ${r.ativa ? "" : "desligada"}">
        <div class="rs-tema"><b>${esc(r.tema)}</b><small>${nEx} exemplo(s) de pergunta${r.ativa ? "" : " · desligada"}</small></div>
        <div class="rs-resp" title="${esc(r.resposta)}">${esc(r.resposta)}</div>
        <div>${tag}</div>
        <div style="font-weight:600">${r.usada}×</div>
        <div><button class="rs-sw" type="button" data-alternar="${r.id}" role="switch" aria-checked="${r.ativa}" aria-label="${r.ativa ? "Desligar" : "Ligar"} ${esc(r.tema)}"
             style="background:${r.ativa ? "#1e7a47" : "#c3cad2"}"><span style="left:${r.ativa ? 21 : 3}px"></span></button></div>
        <div><button class="rs-btn" type="button" data-editar="${r.id}">Editar</button></div>
      </div>`;
    }).join("");
  }

  // ---------------------------------------------------------------- formulário
  function abrirForm(r) {
    estado.form = r
      ? { id: r.id, tema: r.tema, exemplos: r.exemplos, resposta: r.resposta, alcance: r.alcance, produto_chave: r.produto_chave || "", produto_nome: r.produto_nome || "", ativa: r.ativa }
      : { id: null, tema: "", exemplos: "", resposta: "", alcance: "geral", produto_chave: "", produto_nome: "", ativa: true };
    desenharForm();
    setTimeout(() => { const c = document.getElementById("rs-f-tema"); if (c) c.focus(); }, 30);
  }

  function fecharForm() { estado.form = null; document.getElementById("rs-form").innerHTML = ""; }

  function desenharForm() {
    const f = estado.form;
    const alvo = document.getElementById("rs-form");
    if (!f) { alvo.innerHTML = ""; return; }
    const produto = f.alcance === "produto";
    alvo.innerHTML = `<div class="rs-fundo" data-fechar></div>
      <aside class="rs-gaveta" role="dialog" aria-label="${f.id ? "Editar" : "Nova"} resposta padrão">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div style="font-size:20px;font-weight:700">${f.id ? "Editar resposta padrão" : "Nova resposta padrão"}</div>
          <button class="rs-btn" type="button" style="width:38px;padding:0" aria-label="Fechar" data-fechar>✕</button>
        </div>
        <div><label class="rs-lbl" for="rs-f-tema">Tema</label>
          <input id="rs-f-tema" class="rs-fld" maxlength="80" placeholder="Ex.: Nota fiscal" value="${esc(f.tema)}" data-campo="tema"></div>
        <div><label class="rs-lbl" for="rs-f-ex">Exemplos de pergunta <span style="font-weight:400;color:#5b6776">(um por linha)</span></label>
          <textarea id="rs-f-ex" class="rs-fld" style="height:110px;resize:vertical" placeholder="Possui nota?&#10;Emite NF?" data-campo="exemplos">${esc(f.exemplos)}</textarea>
          <div class="rs-dica">Quanto mais jeitos de perguntar, melhor a IA reconhece.</div></div>
        <div><label class="rs-lbl" for="rs-f-resp">Resposta <span style="font-weight:400;color:#5b6776" id="rs-f-cont"></span></label>
          <textarea id="rs-f-resp" class="rs-fld" style="height:120px;resize:vertical" maxlength="2000" placeholder="Olá! Sim, possui nota fiscal…" data-campo="resposta">${esc(f.resposta)}</textarea>
          <div class="rs-dica">Não use [PRAZO] ou outros campos entre colchetes numa resposta ligada: o texto vai exatamente assim para o comprador.</div></div>
        <div><span class="rs-lbl">Alcance</span>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button type="button" class="rs-opt ${produto ? "" : "ativo"}" data-alcance="geral"><span class="rs-radio"></span>Todas as contas</button>
            <button type="button" class="rs-opt ${produto ? "ativo" : ""}" data-alcance="produto"><span class="rs-radio"></span>Só um produto</button>
          </div>
          ${produto ? `<div style="margin-top:10px">
            ${f.produto_chave ? `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;background:#f3ecfc;border-radius:10px;padding:9px 12px;font-size:13.5px">
                <span><b>${esc(f.produto_chave)}</b>${f.produto_nome ? " · " + esc(f.produto_nome) : ""}</span>
                <button type="button" class="rs-btn" data-trocar-produto>Trocar</button></div>`
              : `<label class="rs-lbl" for="rs-f-prod" style="font-weight:400">Buscar por SKU, código do anúncio (MLB…) ou nome do produto</label>
                 <input id="rs-f-prod" class="rs-fld" autocomplete="off" placeholder="Ex.: 19-013">
                 <div id="rs-f-prod-lista"></div>`}
          </div>` : ""}
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-size:14px"><input type="checkbox" id="rs-f-ativa" ${f.ativa ? "checked" : ""}> Ligada (a IA já pode usar)</label>
        <div class="rs-teste">
          <label class="rs-lbl" for="rs-f-teste">Testar antes de salvar</label>
          <div style="display:flex;gap:8px">
            <input id="rs-f-teste" class="rs-fld" style="height:40px" placeholder="Digite uma pergunta de cliente…">
            <button class="rs-btn" type="button" id="rs-f-testar">Testar</button>
          </div>
          <div id="rs-f-resultado"></div>
        </div>
        <div style="display:flex;justify-content:space-between;gap:10px;margin-top:auto;padding-top:8px">
          <div>${f.id && estado.podeApagar ? `<button class="rs-btn rs-btn-perigo" type="button" id="rs-f-apagar">Apagar</button>` : ""}</div>
          <div style="display:flex;gap:10px">
            <button class="rs-btn" type="button" data-fechar>Cancelar</button>
            <button class="rs-btn rs-btn-escuro" type="button" id="rs-f-salvar">Salvar</button>
          </div>
        </div>
      </aside>`;
    atualizarContador();
  }

  function atualizarContador() {
    const el = document.getElementById("rs-f-cont");
    if (el && estado.form) el.textContent = `(${estado.form.resposta.length}/2000)`;
  }

  let buscaProdutoTimer = null;
  function buscarProduto(termo) {
    clearTimeout(buscaProdutoTimer);
    const alvo = document.getElementById("rs-f-prod-lista");
    if (!alvo) return;
    if (termo.trim().length < 2) { alvo.innerHTML = ""; return; }
    buscaProdutoTimer = setTimeout(async () => {
      try {
        const itens = await api("/produtos?busca=" + encodeURIComponent(termo.trim()));
        alvo.innerHTML = itens.length
          ? `<div class="rs-produtos">${itens.map((p) => `<button type="button" class="rs-produto" data-produto="${esc(p.chave)}" data-nome="${esc(p.titulo || "")}">
              <b>${esc(p.chave)}</b>${p.titulo ? " · " + esc(p.titulo) : ""} <span style="color:#5b6776">(${p.perguntas} pergunta(s))</span></button>`).join("")}</div>`
          : `<div class="rs-dica">Nenhum produto com perguntas encontrado. Confira o SKU ou o código do anúncio.</div>`;
      } catch (erro) {
        alvo.innerHTML = `<div class="rs-dica" style="color:#96341c">Não foi possível buscar: ${esc(erro.message)}</div>`;
      }
    }, 300);
  }

  async function testar() {
    const f = estado.form;
    const pergunta = (document.getElementById("rs-f-teste").value || "").trim();
    const alvo = document.getElementById("rs-f-resultado");
    if (!pergunta) { alvo.innerHTML = `<div class="rs-resultado" style="background:#fff4e6;color:#8a4200">Digite uma pergunta para testar.</div>`; return; }
    const botao = document.getElementById("rs-f-testar");
    botao.disabled = true; botao.textContent = "Testando…";
    try {
      const r = await api("/testar", { method: "POST", body: JSON.stringify({ pergunta, tema: f.tema, exemplos: f.exemplos, resposta: f.resposta }) });
      alvo.innerHTML = !r.ia_disponivel
        ? `<div class="rs-resultado" style="background:#fbe9e3;color:#96341c">A IA está sem chave configurada no servidor, então não dá para testar agora.</div>`
        : r.usaria
          ? `<div class="rs-resultado" style="background:#e3f5ea;color:#1a6641">✓ A IA usaria esta resposta para: “${esc(pergunta)}”</div>`
          : `<div class="rs-resultado" style="background:#fff4e6;color:#8a4200">⚠ A IA não usaria esta resposta para essa pergunta. Se deveria, acrescente um exemplo parecido.</div>`;
    } catch (erro) {
      alvo.innerHTML = `<div class="rs-resultado" style="background:#fbe9e3;color:#96341c">Não foi possível testar: ${esc(erro.message)}</div>`;
    } finally {
      botao.disabled = false; botao.textContent = "Testar";
    }
  }

  async function salvar() {
    const f = estado.form;
    f.ativa = document.getElementById("rs-f-ativa").checked;
    if (f.ativa && /\[[^\]]+\]/.test(f.resposta)) {
      if (!confirm("A resposta tem um campo entre colchetes (ex.: [PRAZO]). Ela vai para o comprador exatamente assim. Salvar ligada mesmo assim?")) return;
    }
    const botao = document.getElementById("rs-f-salvar");
    botao.disabled = true; botao.textContent = "Salvando…";
    try {
      const corpo = JSON.stringify({ tema: f.tema, exemplos: f.exemplos, resposta: f.resposta, alcance: f.alcance, produto_chave: f.produto_chave, produto_nome: f.produto_nome, ativa: f.ativa });
      if (f.id) await api("/" + f.id, { method: "PUT", body: corpo });
      else await api("", { method: "POST", body: corpo });
      fecharForm();
      aviso(f.ativa ? "Resposta salva e ligada: a nossa IA já passa a usar." : "Resposta salva (desligada).", "ok");
      await carregar();
    } catch (erro) {
      alert("Não foi possível salvar: " + erro.message);
      botao.disabled = false; botao.textContent = "Salvar";
    }
  }

  async function apagar() {
    const f = estado.form;
    if (!confirm(`Apagar a resposta “${f.tema}”? Não dá para desfazer.`)) return;
    try {
      await api("/" + f.id, { method: "DELETE" });
      fecharForm();
      aviso("Resposta apagada.", "ok");
      await carregar();
    } catch (erro) { alert("Não foi possível apagar: " + erro.message); }
  }

  async function alternar(id) {
    const r = estado.itens.find((x) => x.id === id);
    if (!r) return;
    if (!r.ativa && /\[[^\]]+\]/.test(r.resposta)) {
      aviso(`“${r.tema}” tem um campo entre colchetes (ex.: [PRAZO]). Clique em Editar, complete o texto e ligue por lá.`, "alerta");
      return;
    }
    try {
      const novo = await api(`/${id}/ativa`, { method: "POST", body: JSON.stringify({ ativa: !r.ativa }) });
      Object.assign(r, novo);
      desenharLista();
    } catch (erro) { aviso("Não foi possível alterar: " + erro.message, "erro"); }
  }

  // ---------------------------------------------------------------- eventos
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-filtro],[data-alternar],[data-editar],[data-fechar],[data-alcance],[data-produto],[data-trocar-produto],#rs-nova,#rs-f-testar,#rs-f-salvar,#rs-f-apagar");
    if (!el) return;
    if (el.id === "rs-nova") return abrirForm(null);
    if (el.id === "rs-f-testar") return testar();
    if (el.id === "rs-f-salvar") return salvar();
    if (el.id === "rs-f-apagar") return apagar();
    if (el.hasAttribute("data-fechar")) return fecharForm();
    if (el.dataset.filtro) {
      estado.filtro = el.dataset.filtro;
      document.querySelectorAll("[data-filtro]").forEach((b) => b.classList.toggle("ativo", b === el));
      return desenharLista();
    }
    if (el.dataset.alternar) return alternar(Number(el.dataset.alternar));
    if (el.dataset.editar) return abrirForm(estado.itens.find((x) => x.id === Number(el.dataset.editar)));
    if (el.dataset.alcance && estado.form) { estado.form.alcance = el.dataset.alcance; return desenharForm(); }
    if (el.dataset.produto && estado.form) { estado.form.produto_chave = el.dataset.produto; estado.form.produto_nome = el.dataset.nome; return desenharForm(); }
    if (el.hasAttribute("data-trocar-produto") && estado.form) { estado.form.produto_chave = ""; estado.form.produto_nome = ""; return desenharForm(); }
  });

  document.addEventListener("input", (e) => {
    if (e.target.id === "rs-busca") { estado.busca = e.target.value; return desenharLista(); }
    if (e.target.id === "rs-f-prod") return buscarProduto(e.target.value);
    const campo = e.target.dataset && e.target.dataset.campo;
    if (campo && estado.form) {
      estado.form[campo] = e.target.value;
      if (campo === "resposta") atualizarContador();
    }
  });

  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && estado.form) fecharForm(); });

  document.addEventListener("DOMContentLoaded", () => {
    carregar().catch((erro) => {
      document.getElementById("rs-lista").innerHTML = `<div class="rs-vazio">Não foi possível carregar as respostas (${esc(erro.message)}).</div>`;
    });
  });
})();
