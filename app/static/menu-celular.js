/**
 * Menu lateral no celular.
 *
 * Em telas estreitas (até 980px) o menu lateral fica escondido; este
 * script cria o botão ☰ que abre o menu como uma "gaveta" deslizando da
 * esquerda. Funciona em qualquer tela que tenha o menu (.cmx-sidebar),
 * sem precisar mexer no HTML de cada uma. No computador não faz nada
 * visível (o botão só aparece pelo CSS de tela estreita).
 *
 * Fecha: tocando fora (no fundo escurecido), no ✕, escolhendo uma opção
 * ou com a tecla Esc.
 */
(function () {
  function iniciar() {
    const menu = document.querySelector(".cmx-sidebar");
    if (!menu || document.querySelector(".cmx-botao-menu")) return; // tela sem menu, ou já iniciado

    if (!menu.id) menu.id = "cmx-menu-lateral";

    const botao = document.createElement("button");
    botao.type = "button";
    botao.className = "cmx-botao-menu";
    botao.setAttribute("aria-label", "Abrir menu");
    botao.setAttribute("aria-controls", menu.id);
    botao.setAttribute("aria-expanded", "false");
    botao.textContent = "☰";

    const veu = document.createElement("div");
    veu.className = "cmx-veu-menu";

    const fechar = document.createElement("button");
    fechar.type = "button";
    fechar.className = "cmx-fechar-menu";
    fechar.setAttribute("aria-label", "Fechar menu");
    fechar.textContent = "✕";
    menu.appendChild(fechar);

    document.body.appendChild(veu);
    document.body.appendChild(botao);

    function abrir() {
      document.body.classList.add("cmx-menu-aberto");
      botao.setAttribute("aria-expanded", "true");
    }
    function fecharMenu() {
      document.body.classList.remove("cmx-menu-aberto");
      botao.setAttribute("aria-expanded", "false");
    }

    botao.addEventListener("click", abrir);
    fechar.addEventListener("click", fecharMenu);
    veu.addEventListener("click", fecharMenu);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") fecharMenu(); });
    // Escolheu uma opção: fecha (a página vai trocar, mas em links que abrem
    // em nova aba -- painéis de TV -- o menu não pode ficar aberto).
    menu.addEventListener("click", (e) => { if (e.target.closest("a")) fecharMenu(); });
    // Girou o celular / aumentou a janela para computador: garante fechado.
    window.addEventListener("resize", () => { if (window.innerWidth > 980) fecharMenu(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", iniciar);
  else iniciar();
})();
