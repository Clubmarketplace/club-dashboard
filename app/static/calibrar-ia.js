/*
 * Tela "Calibrar IA" (Administração). Lê os ajustes em /api/calibrar-ia,
 * deixa marcar/desmarcar e salva só quando clicar em "Salvar ajustes".
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const escapar = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let salvo = null;   // ajustes como estão no servidor
  let atual = null;   // ajustes na tela (com as mudanças ainda não salvas)
  let degraus = [];

  const NOMES = {
    ia_ativa: "Nossa IA ligada", janela_ml_min: "Tempo para a IA do ML", fonte_respostas_padrao: "Fonte: respostas padrão",
    fonte_dados_anuncio: "Fonte: ficha do anúncio", fonte_manuais: "Fonte: manuais", fonte_internet: "Fonte: pesquisa na internet",
    degraus: "Degraus da pesquisa", duas_fontes: "Exigir duas fontes", buscas_por_degrau: "Buscas por degrau",
    modelo_pesquisa: "Modelo da pesquisa", tom: "Tom", tamanho: "Tamanho",
  };
  const DESCRICOES = {
    fabricante: "Site oficial da marca.",
    pdf: "Manual, ficha ou boletim técnico em PDF do mesmo modelo, mesmo que esteja no site de uma loja.",
    lojas: "Ficha técnica na página do produto em lojas grandes (Leroy, Magalu, Casas Bahia, Amazon…).",
    videos: "Vídeos do canal oficial da marca (título, descrição, transcrição). A IA não vê a imagem.",
    tecnicos: "Catálogos de autopeças, distribuidores e portais técnicos. Pode confundir versões parecidas.",
    aberta: "Qualquer site confiável. É o que mais acha, e o que mais pode errar.",
  };

  function aviso(texto, tipo) {
    const el = $("ci-aviso");
    el.textContent = texto;
    el.style.display = texto ? "block" : "none";
    el.style.background = tipo === "erro" ? "#fdecea" : "#e8f6ef";
    el.style.color = tipo === "erro" ? "#8a1f11" : "#14532d";
  }

  function formatarValor(chave, v) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "sim" : "não";
    if (chave === "degraus") return degraus.filter((d) => v[d.chave]).map((d) => d.nome.split(" · ")[0]).join(", ") || "nenhum";
    if (chave === "janela_ml_min") return v + " min";
    if (chave === "modelo_pesquisa") return v === "preciso" ? "mais preciso" : "econômico";
    return String(v);
  }

  function mudancas() {
    // janela_ml_min é fixa (3 min): nunca vai como alteração
    return Object.keys(atual).filter((k) => k !== "janela_ml_min" && JSON.stringify(atual[k]) !== JSON.stringify(salvo[k]));
  }

  function pintar() {
    // Liga/pausa
    const sw = $("ci-ativa");
    sw.setAttribute("aria-checked", atual.ia_ativa ? "true" : "false");
    sw.style.background = atual.ia_ativa ? "#16a36a" : "#c3cad3";
    sw.querySelector("span").style.left = atual.ia_ativa ? "26px" : "4px";
    $("ci-ativa-txt").textContent = atual.ia_ativa ? "Ligada — respondendo sozinha" : "Pausada — tudo vai para a equipe";
    $("ci-janela-txt").textContent = atual.janela_ml_min + " minutos";
    document.querySelectorAll("input[data-chave]").forEach((c) => { c.checked = !!atual[c.dataset.chave]; });
    document.querySelectorAll("input[data-degrau]").forEach((c) => { c.checked = !!atual.degraus[c.dataset.degrau]; c.disabled = !atual.fonte_internet; });
    $("ci-duas").disabled = $("ci-buscas").disabled = !atual.fonte_internet;
    $("ci-buscas").value = String(atual.buscas_por_degrau);
    ["modelo", "tom", "tamanho"].forEach((nome) => {
      const chave = nome === "modelo" ? "modelo_pesquisa" : nome;
      document.querySelectorAll(`input[name="${nome}"]`).forEach((r) => { r.checked = r.value === atual[chave]; });
    });
    const m = mudancas();
    $("ci-mudancas").textContent = m.length ? `${m.length} alteração(ões) não salva(s): ${m.map((k) => NOMES[k] || k).join(", ")}` : "Nenhuma alteração.";
    $("ci-salvar").disabled = $("ci-desfazer").disabled = !m.length;
  }

  function montarCampos() {
    $("ci-buscas").innerHTML = [1, 2, 3, 4, 5].map((n) => `<option value="${n}">${n}</option>`).join("");
    $("ci-degraus").innerHTML = degraus.map((d, i) => {
      const [num, ...resto] = d.nome.split(" · ");
      return `<div class="ci-linha"><span class="ci-num">${escapar(num)}</span><input type="checkbox" id="dg-${d.chave}" data-degrau="${d.chave}" />
        <label for="dg-${d.chave}">${escapar(resto.join(" · ").replace(/^./, (c) => c.toUpperCase()))}<small>${escapar(DESCRICOES[d.chave] || "")}</small></label></div>`;
    }).join("");
  }

  function pintarHistorico(hist) {
    $("ci-hist").innerHTML = hist.length ? hist.map((h) => {
      const quando = h.em ? new Date(h.em).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
      return `<tr><td>${quando}</td><td>${escapar(h.por || "—")}</td><td>${escapar(NOMES[h.chave] || h.chave)}</td><td>${escapar(formatarValor(h.chave, h.antes))}</td><td>${escapar(formatarValor(h.chave, h.depois))}</td></tr>`;
    }).join("") : '<tr><td colspan="5">Nenhuma alteração ainda — a IA está com os ajustes padrão.</td></tr>';
    const ultimaPausa = hist.find((h) => h.chave === "ia_ativa");
    $("ci-ativa-info").textContent = ultimaPausa ? `${ultimaPausa.depois ? "Ligada" : "Pausada"} por ${ultimaPausa.por || "—"} em ${new Date(ultimaPausa.em).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo" })}` : "";
  }

  function aplicar(dados) {
    degraus = dados.degraus;
    salvo = JSON.parse(JSON.stringify(dados.config));
    atual = JSON.parse(JSON.stringify(dados.config));
    montarCampos();
    pintarHistorico(dados.historico || []);
    pintar();
  }

  async function carregar() {
    try {
      const r = await fetch("/api/calibrar-ia", { cache: "no-store" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "status " + r.status);
      aplicar(d);
    } catch (e) { aviso("Não consegui carregar os ajustes: " + e.message, "erro"); }
  }

  // Interações
  $("ci-ativa").addEventListener("click", () => {
    if (atual.ia_ativa && !confirm("Pausar a nossa IA? Enquanto pausada, tudo que a IA do ML não responder vai direto para a equipe.")) return;
    atual.ia_ativa = !atual.ia_ativa; pintar();
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (!atual) return;
    if (t.dataset.chave) atual[t.dataset.chave] = t.checked;
    else if (t.dataset.degrau) atual.degraus = { ...atual.degraus, [t.dataset.degrau]: t.checked };
    else if (t.id === "ci-buscas") atual.buscas_por_degrau = Number(t.value);
    else if (t.name === "modelo") atual.modelo_pesquisa = t.value;
    else if (t.name === "tom" || t.name === "tamanho") atual[t.name] = t.value;
    else return;
    pintar();
  });
  $("ci-desfazer").addEventListener("click", () => { atual = JSON.parse(JSON.stringify(salvo)); pintar(); });
  $("ci-salvar").addEventListener("click", async () => {
    const m = mudancas();
    if (!m.length) return;
    if (atual.fonte_internet && !Object.values(atual.degraus).some(Boolean)) {
      aviso("Ligue pelo menos um degrau da pesquisa, ou desligue a pesquisa na internet.", "erro"); return;
    }
    const corpo = {}; m.forEach((k) => { corpo[k] = atual[k]; });
    $("ci-salvar").disabled = true; $("ci-salvar").textContent = "Salvando…";
    try {
      const r = await fetch("/api/calibrar-ia", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "status " + r.status);
      aplicar(d);
      aviso("✓ Ajustes salvos. Valem para as próximas perguntas em até 30 segundos.", "ok");
    } catch (e) {
      aviso("Não foi possível salvar: " + e.message, "erro");
    } finally { $("ci-salvar").textContent = "Salvar ajustes"; pintar(); }
  });
  window.addEventListener("beforeunload", (e) => { if (atual && mudancas().length) { e.preventDefault(); e.returnValue = ""; } });

  carregar();
})();
