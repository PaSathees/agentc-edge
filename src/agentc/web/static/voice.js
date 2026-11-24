// Voice for the setup chat: talk instead of typing, and hear the replies.
// Uses only the browser's own speech features, so nothing is installed. Browsers allow
// the microphone on https pages only, so on plain http the mic button stays hidden.
(() => {
  const Listen = window.SpeechRecognition || window.webkitSpeechRecognition;
  const form = document.querySelector("form.say");
  const box = form && form.querySelector("textarea");
  const mic = document.getElementById("mic");
  const speak = document.getElementById("speak");

  if (Listen && mic && box) {
    mic.hidden = false;
    let heard = null;
    mic.addEventListener("click", () => {
      if (heard) { heard.stop(); return; }  // a second tap stops listening
      heard = new Listen();
      heard.lang = navigator.language || "en-GB";
      heard.interimResults = true;  // the words appear in the box as you speak
      heard.onresult = (e) => {
        box.value = Array.from(e.results).map((r) => r[0].transcript).join("");
      };
      heard.onend = () => {
        heard = null;
        mic.classList.remove("listening");
        mic.textContent = "Mic";
        if (box.value.trim()) form.requestSubmit();  // sends what you said
      };
      heard.onerror = () => { box.placeholder = "Could not hear that. Type instead."; };
      mic.classList.add("listening");
      mic.textContent = "Stop";
      heard.start();
    });
  }

  if ("speechSynthesis" in window && speak) {
    speak.closest(".voice-only").hidden = false;
    try { speak.checked = localStorage.getItem("agentc-speak") === "1"; } catch (_) {}
    speak.addEventListener("change", () => {
      try { localStorage.setItem("agentc-speak", speak.checked ? "1" : "0"); } catch (_) {}
    });
    document.body.addEventListener("htmx:load", (e) => {  // a new answer arrived
      const el = e.detail.elt;
      if (!speak.checked || el === document.body) return;
      const said = el.matches && el.matches("[data-say]") ? el : el.querySelector && el.querySelector("[data-say]");
      if (said) speechSynthesis.speak(new SpeechSynthesisUtterance(said.textContent));
    });
  }
})();
