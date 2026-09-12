(() => {
  "use strict";
  const dialog = document.querySelector("#photo-dialog");
  const full = document.querySelector("#full-photo");
  const dialogStatus = document.querySelector("#dialog-status");
  let selected = null;

  const expired = card => Date.now() >= Date.parse(card.dataset.expires);
  async function freshUrl(button) {
    const response = await fetch(button.dataset.photoUrl, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("Foto tidak tersedia. Muat ulang atau masuk kembali.");
    return (await response.json()).url;
  }

  async function preview(button) {
    const card = button.closest("[data-photo-card]");
    if (expired(card)) return;
    const image = button.querySelector("img");
    const label = button.querySelector("span");
    try {
      const url = await freshUrl(button);
      if (expired(card)) return;
      image.onload = () => { image.hidden = false; label.hidden = true; };
      image.onerror = () => { image.hidden = true; label.hidden = false; label.textContent = "Ketuk untuk mencoba lagi"; };
      image.src = url;
    } catch { label.textContent = "Ketuk untuk mencoba lagi"; }
  }

  function clearFull() { full.removeAttribute("src"); full.hidden = true; selected = null; }
  dialog.addEventListener("close", clearFull);
  document.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });

  function enforceExpiry(card) {
    if (expired(card)) {
      const button = card.querySelector("button");
      button.disabled = true;
      button.querySelector("img").removeAttribute("src");
      button.querySelector("img").hidden = true;
      const label = button.querySelector("span");
      label.hidden = false;
      label.textContent = "Foto sudah kedaluwarsa";
      card.querySelector(".danger-link")?.remove();
      if (selected === card) dialog.close();
    } else setTimeout(() => enforceExpiry(card), Math.min(2147483647, Math.max(1, Date.parse(card.dataset.expires) - Date.now())));
  }

  const observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) { preview(entry.target); observer.unobserve(entry.target); }
  }), {rootMargin: "180px"});
  document.querySelectorAll("[data-photo-url]").forEach(button => {
    const card = button.closest("[data-photo-card]");
    enforceExpiry(card);
    observer.observe(button);
    button.addEventListener("click", async () => {
      if (expired(card)) return;
      clearFull();
      selected = card;
      dialogStatus.textContent = "Memuat foto…";
      if (!dialog.open) dialog.showModal();
      try {
        const url = await freshUrl(button);
        if (!dialog.open || selected !== card || expired(card)) return;
        full.onload = () => { full.hidden = false; dialogStatus.textContent = ""; };
        full.onerror = () => { dialogStatus.textContent = "Foto gagal dimuat. Tutup lalu coba lagi."; };
        full.src = url;
        preview(button);
      } catch (error) { dialogStatus.textContent = error.message; }
    });
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) document.querySelectorAll("[data-photo-card]").forEach(enforceExpiry);
  });
})();
