if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/pwa/sw.js").catch(function (err) {
      console.warn("Enregistrement du service worker impossible :", err);
    });
  });
}
