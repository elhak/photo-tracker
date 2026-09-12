/* Captures and previews live only in memory; no file inputs or browser persistence. */
(() => {
  "use strict";
  const form = document.querySelector("#capture-form");
  const video = document.querySelector("#camera");
  const order = document.querySelector("#order-id");
  const queue = document.querySelector("#photo-queue");
  const save = document.querySelector("#save-photos");
  const capture = document.querySelector("#take-photo");
  const open = document.querySelector("#open-camera");
  const switchButton = document.querySelector("#switch-camera");
  const status = document.querySelector("#camera-status");
  const uploadStatus = document.querySelector("#upload-status");
  const savedLink = document.querySelector("#saved-link");
  const csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;
  const items = [];
  let stream = null, facing = "environment", busy = false, opening = false, frameBusy = false;

  function cameraStatus(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  function stopCamera() {
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null;
    video.srcObject = null;
    video.hidden = true;
    document.querySelector("#camera-placeholder").hidden = false;
    document.querySelector("#camera-controls").hidden = true;
  }

  async function openCamera() {
    if (opening) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) {
      cameraStatus("Kamera membutuhkan HTTPS dan browser yang mendukung kamera. Buka di Safari atau Chrome terbaru.", true);
      return;
    }
    opening = true;
    open.disabled = true;
    switchButton.disabled = true;
    stopCamera();
    cameraStatus("Menghubungkan kamera…");
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: {facingMode: {ideal: facing}, width: {ideal: 1920}, height: {ideal: 1440}},
      });
      // A permission prompt may finish after the user switches to another app.
      if (document.hidden) { stopCamera(); return; }
      video.srcObject = stream;
      video.hidden = false;
      document.querySelector("#camera-placeholder").hidden = true;
      await video.play();
      document.querySelector("#camera-controls").hidden = false;
      const devices = await navigator.mediaDevices.enumerateDevices();
      switchButton.hidden = devices.filter(device => device.kind === "videoinput").length < 2;
      cameraStatus("Arahkan kamera ke order, lalu tekan Ambil foto.");
    } catch (error) {
      stopCamera();
      const errors = {
        NotAllowedError: "Izin kamera ditolak. Izinkan kamera di pengaturan situs, lalu tekan Buka kamera.",
        NotFoundError: "Kamera tidak ditemukan pada perangkat ini.",
        NotReadableError: "Kamera sedang digunakan aplikasi lain. Tutup aplikasi tersebut, lalu coba lagi.",
      };
      cameraStatus(errors[error.name] || "Kamera belum dapat dibuka. Coba lagi di Safari atau Chrome.", true);
    } finally {
      opening = false;
      open.disabled = false;
      switchButton.disabled = false;
    }
  }

  const toBlob = (canvas, quality) => new Promise((resolve, reject) => {
    canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("Foto tidak dapat diproses.")), "image/jpeg", quality);
  });

  async function compressFrame() {
    if (!video.videoWidth || !video.videoHeight) throw new Error("Tunggu sampai tampilan kamera siap.");
    const source = document.createElement("canvas");
    const scale = Math.min(1, 1920 / Math.max(video.videoWidth, video.videoHeight));
    source.width = Math.max(1, Math.round(video.videoWidth * scale));
    source.height = Math.max(1, Math.round(video.videoHeight * scale));
    source.getContext("2d", {alpha: false}).drawImage(video, 0, 0, source.width, source.height);
    const output = document.createElement("canvas");
    try {
      for (let ratio = 1; ratio >= 0.15; ratio *= 0.8) {
        output.width = Math.max(1, Math.round(source.width * ratio));
        output.height = Math.max(1, Math.round(source.height * ratio));
        output.getContext("2d", {alpha: false}).drawImage(source, 0, 0, output.width, output.height);
        for (const quality of [0.86, 0.72, 0.58, 0.44]) {
          const blob = await toBlob(output, quality);
          if (blob.type === "image/jpeg" && blob.size > 0 && blob.size <= 500000) return blob;
        }
      }
      throw new Error("Foto belum dapat dikompres sampai 500 KB. Silakan ambil kembali.");
    } finally {
      source.width = source.height = output.width = output.height = 0;
    }
  }

  function renderQueue() {
    queue.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "queue-empty";
      empty.textContent = "Foto yang Anda ambil akan muncul di sini.";
      queue.append(empty);
    }
    items.forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "queued-photo" + (item.saved ? " saved" : "");
      if (item.saved) {
        const mark = document.createElement("span");
        mark.className = "saved-mark";
        mark.textContent = "✓";
        row.append(mark);
      } else {
        const img = document.createElement("img");
        img.src = item.preview;
        img.alt = `Pratinjau foto ${index + 1}`;
        row.append(img);
      }
      const copy = document.createElement("div");
      copy.className = "queue-copy";
      const title = document.createElement("strong");
      title.textContent = `Foto ${index + 1}`;
      const subtitle = document.createElement("small");
      subtitle.textContent = item.saved ? "Tersimpan" : item.message || `${Math.ceil(item.blob.size / 1000)} KB · Siap disimpan`;
      copy.append(title, subtitle);
      row.append(copy);
      if (!item.saved) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", `Buang foto ${index + 1} untuk ambil ulang`);
        remove.disabled = busy;
        remove.addEventListener("click", () => {
          URL.revokeObjectURL(item.preview);
          items.splice(items.indexOf(item), 1);
          renderQueue();
        });
        row.append(remove);
      }
      queue.append(row);
    });
    document.querySelector("#photo-count").textContent = items.length;
    save.disabled = busy || frameBusy || !items.some(item => !item.saved);
    capture.disabled = busy || frameBusy;
    if (!busy) {
      const count = items.filter(item => !item.saved).length;
      save.textContent = items.some(item => item.message && !item.saved) ? "Coba simpan lagi" : "Simpan foto ↑";
      uploadStatus.textContent = count ? `${count} foto belum disimpan.` : items.length ? "Semua foto berhasil disimpan." : "Belum ada foto yang diambil.";
    }
  }

  async function api(url, body) {
    let response;
    try {
      response = await fetch(url, {method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
        body: JSON.stringify(body), signal: AbortSignal.timeout(90000)});
    } catch {
      throw new Error("Koneksi terputus. Foto masih di halaman ini; coba simpan lagi.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || (response.status === 403 ? "Sesi berubah. Muat ulang lalu masuk kembali." : "Permintaan gagal. Silakan coba lagi."));
    return data;
  }

  function uploadToS3(upload, blob, onProgress) {
    return new Promise((resolve, reject) => {
      let body = blob;
      const method = upload.method || "POST";
      if (method === "POST") {
        body = new FormData();
        Object.entries(upload.fields).forEach(([key, value]) => body.append(key, value));
        body.append("file", blob, "camera.jpg");
      }
      const xhr = new XMLHttpRequest();
      xhr.open(method, upload.url);
      Object.entries(upload.headers || {}).forEach(([key, value]) => xhr.setRequestHeader(key, value));
      xhr.timeout = 90000;
      xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100)); };
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`Penyimpanan menolak unggahan (HTTP ${xhr.status}). Periksa izin bucket dan konfigurasi penyimpanan.`));
      xhr.onerror = () => reject(new Error("Tidak dapat menghubungi penyimpanan. Periksa koneksi dan izin CORS untuk alamat situs ini."));
      xhr.ontimeout = () => reject(new Error("Unggahan terlalu lama. Silakan coba lagi."));
      xhr.send(body);
    });
  }

  capture.addEventListener("click", async () => {
    if (busy || frameBusy) return;
    frameBusy = true;
    renderQueue();
    try {
      const blob = await compressFrame();
      items.push({blob, preview: URL.createObjectURL(blob), requestId: crypto.randomUUID(), saved: false});
      cameraStatus("Foto ditambahkan. Ambil lagi atau simpan foto Anda.");
    } catch (error) { cameraStatus(error.message, true); }
    finally { frameBusy = false; renderQueue(); }
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || frameBusy || !form.reportValidity()) return;
    if (!order.value.trim()) { order.setCustomValidity("Isi ID order."); order.reportValidity(); return; }
    if (!items.some(item => !item.saved)) return;
    busy = true;
    order.readOnly = true; // An upload retry must keep the original order.
    stopCamera();
    renderQueue();
    let failed = 0;
    for (const [index, item] of items.entries()) {
      if (item.saved) continue;
      try {
        uploadStatus.textContent = `Menyimpan foto ${index + 1} dari ${items.length}…`;
        if (!item.checksum) {
          const hash = await crypto.subtle.digest("SHA-256", await item.blob.arrayBuffer());
          item.checksum = btoa(String.fromCharCode(...new Uint8Array(hash)));
        }
        let result;
        if (item.uploaded) {
          // A lost completion response is safe to retry without re-uploading bytes.
          result = await api(`/api/uploads/${item.intentId}/complete/`, {});
        } else {
          const intent = await api("/api/uploads/", {order_id: order.value.trim(), tracker_id: form.dataset.trackerId || null, request_id: item.requestId, checksum: item.checksum, byte_size: item.blob.size});
          item.intentId = intent.id;
          if (intent.completed) result = intent;
          else {
            await uploadToS3(intent.upload, item.blob, percent => {
              item.message = `Mengunggah ${percent}%`;
              renderQueue();
            });
            item.uploaded = true;
            result = await api(`/api/uploads/${intent.id}/complete/`, {});
          }
        }
        item.saved = true;
        URL.revokeObjectURL(item.preview);
        item.blob = null;
        item.preview = null;
        savedLink.href = result.tracker_url;
        savedLink.hidden = false;
      } catch (error) { failed++; item.message = error.message; }
      renderQueue();
    }
    busy = false;
    renderQueue();
    if (failed) uploadStatus.textContent = items.some(item => item.saved)
      ? `${failed} foto belum tersimpan. Foto yang berhasil sudah aman. Periksa pesan di atas, lalu coba lagi.`
      : `${failed} foto belum tersimpan. Periksa pesan pada foto, lalu coba lagi.`;
  });

  order.addEventListener("input", () => order.setCustomValidity(""));
  open.addEventListener("click", openCamera);
  document.querySelector("#stop-camera").addEventListener("click", stopCamera);
  switchButton.addEventListener("click", () => { facing = facing === "environment" ? "user" : "environment"; openCamera(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopCamera(); });
  window.addEventListener("beforeunload", event => {
    if (busy || items.some(item => !item.saved)) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", () => {
    stopCamera();
    items.forEach(item => { if (item.preview) URL.revokeObjectURL(item.preview); item.blob = null; });
    items.length = 0;
  });
  window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
})();
