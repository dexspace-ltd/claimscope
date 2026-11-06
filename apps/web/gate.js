// ClaimScope is desktop-only: the tour walk needs tab sharing, which phones and tablets don't support.
// Runs before first paint (loaded in <head>) and sets a class the stylesheet uses to show the notice.
(function () {
  var ua = navigator.userAgent || "";
  var touchMac = /Macintosh/.test(ua) && navigator.maxTouchPoints > 1; // iPadOS reports as a Mac
  var mobile = /Android|iPhone|iPad|iPod|Mobile|Silk|Kindle|BlackBerry|Opera Mini/i.test(ua) || touchMac;
  var MIN_WIDTH = 900;

  function check() {
    var narrow = window.innerWidth < MIN_WIDTH;
    var root = document.documentElement;
    root.classList.toggle("gate-mobile", mobile);
    root.classList.toggle("gate-narrow", !mobile && narrow);
  }
  check();
  window.addEventListener("resize", check);

  document.addEventListener("DOMContentLoaded", function () {
    var gate = document.createElement("div");
    gate.id = "device-gate";
    gate.setAttribute("role", "alert");
    gate.innerHTML =
      '<div class="gate-card">' +
      '<p class="gate-title">Please open ClaimScope on a computer</p>' +
      '<p class="gate-mobile-text">ClaimScope works on a laptop or desktop in Chrome or Edge. ' +
      "Walking the 3D tour uses screen sharing, which phones and tablets don't support.</p>" +
      '<p class="gate-narrow-text">This window is too narrow. Make it wider, or open ClaimScope full screen on a laptop or desktop.</p>' +
      '<button type="button" id="gate-copy">Copy link</button>' +
      '<p class="gate-hint" id="gate-done"></p>' +
      "</div>";
    document.body.appendChild(gate);
    document.getElementById("gate-copy").addEventListener("click", function () {
      var done = document.getElementById("gate-done");
      var url = location.href;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () { done.textContent = "Link copied. Send it to yourself and open it on your computer."; },
          function () { done.textContent = url; });
      } else {
        done.textContent = url;
      }
    });
  });
})();
