/* GrowBot on the box: the setup page's scanner. The second QR on JBrain's GrowBot screen is a
   one-time /open?ticket= link on this same origin; it is the only thing this page will follow,
   so a code from anywhere else (another site, another box) opens nothing. Chrome on Android
   reads QR codes with its built-in BarcodeDetector, so no library ships. */
(function () {
  "use strict";
  var scan = document.getElementById("scan");
  var view = document.getElementById("view");
  var msg = document.getElementById("msg");
  var stream = null;
  var timer = 0;

  function say(text) {
    msg.textContent = text;
    msg.hidden = !text;
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = 0;
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    view.hidden = true;
  }

  // Only this box's own ticket link: same origin, the /open door, and a ticket in it.
  function ticketLink(raw) {
    var url;
    try {
      url = new URL(raw, location.href);
    } catch (e) {
      return null;
    }
    if (url.origin !== location.origin || url.pathname !== "/open") return null;
    if (!url.searchParams.get("ticket")) return null;
    return url.href;
  }

  scan.addEventListener("click", function () {
    say("");
    if (!("BarcodeDetector" in window)) {
      say("This browser can't read codes. Scan the code with the phone's camera app instead.");
      return;
    }
    var detector;
    try {
      detector = new window.BarcodeDetector({ formats: ["qr_code"] });
    } catch (e) {
      say("This browser can't read codes. Scan the code with the phone's camera app instead.");
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(function (s) {
        stream = s;
        view.srcObject = s;
        view.hidden = false;
        return view.play();
      })
      .then(function () {
        timer = setInterval(function () {
          detector.detect(view).then(function (codes) {
            for (var i = 0; i < codes.length; i++) {
              var link = ticketLink(codes[i].rawValue);
              if (link) {
                stop();
                say("Opening GrowBot…");
                location.replace(link);
                return;
              }
            }
            if (codes.length) say("That isn't a GrowBot code from this box.");
          }).catch(function () {});
        }, 300);
      })
      .catch(function () {
        stop();
        say("The camera didn't open. Allow it for this page and try again.");
      });
  });

  window.addEventListener("pagehide", stop);
})();
