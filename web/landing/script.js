/* Минимальный JS лендинга: плавная прокрутка по якорям.
 * При переходе к цели внутри аккордеонов руководства — раскрывает их. */
document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="#"]');
  if (!link) {
    return;
  }
  const hash = link.getAttribute("href");
  if (!hash || hash === "#") {
    return; /* пустой якорь (логотип) — не мешаем браузеру */
  }
  const target = document.querySelector(hash);
  if (!target) {
    return;
  }
  event.preventDefault();
  /* Раскрываем все <details> от цели вверх (аккордеоны руководства). */
  let node = target;
  while (node) {
    if (node instanceof HTMLDetailsElement) {
      node.open = true;
    }
    node = node.parentElement;
  }
  target.scrollIntoView({ behavior: "smooth", block: "start" });
  if (history.pushState) {
    history.pushState(null, "", hash);
  }
});

/* «Как это выглядит»: вкладки (стрелки ←/→ переключают) и лайтбокс на <dialog>. */
(() => {
  const tabs = [...document.querySelectorAll("#screens .screens-tab")];
  const select = (tab) => {
    for (const other of tabs) {
      const active = other === tab;
      other.setAttribute("aria-selected", String(active));
      other.tabIndex = active ? 0 : -1;
      const panel = document.getElementById(other.getAttribute("aria-controls"));
      panel.hidden = !active;
      panel.classList.toggle("hidden", !active);
    }
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) {
        return;
      }
      const next = tabs[(index + step + tabs.length) % tabs.length];
      select(next);
      next.focus();
    });
  });

  const box = document.getElementById("screen-lightbox");
  if (!box || typeof box.showModal !== "function") {
    return;
  }
  const image = document.getElementById("screen-lightbox-img");
  document.addEventListener("click", (event) => {
    const opener = event.target.closest(".screen-open");
    if (opener) {
      image.src = opener.dataset.full;
      image.alt = opener.dataset.title;
      document.getElementById("screen-lightbox-title").textContent = opener.dataset.title;
      document.getElementById("screen-lightbox-text").textContent = opener.dataset.text;
      box.showModal();
      box.scrollTop = 0;
      return;
    }
    /* Клик по кнопке «✕» или по затемнению вокруг окна закрывает лайтбокс. */
    if (event.target.closest(".screen-close") || event.target === box) {
      box.close();
    }
  });
})();
