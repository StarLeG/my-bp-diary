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
