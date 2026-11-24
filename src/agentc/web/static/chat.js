// The setup chat in the browser: keep the newest message in view, grow the message box,
// talk instead of typing, and hear the replies. Voice uses only the browser's own speech
// features, so nothing is installed. Browsers allow the mic on https pages only.
(() => {
  const main = document.querySelector("main");
  const chat = document.getElementById("chat");
  const form = document.querySelector("footer.say form");
  const box = form && form.querySelector("textarea");
  if (!main || !chat || !box) return;

  // Scrolling: open at the newest message, and follow new words as they arrive,
  // unless the owner has scrolled up to read something older.
  let follow = true;
  const toEnd = () => { main.scrollTop = main.scrollHeight; };
  main.addEventListener("scroll", () => {
    follow = main.scrollHeight - main.scrollTop - main.clientHeight < 80;
  }, { passive: true });
  new MutationObserver(() => { if (follow) toEnd(); })
    .observe(chat, { childList: true, subtree: true, characterData: true });
  toEnd();
  form.addEventListener("submit", () => { follow = true; });

  // The message box grows with the text, up to a few lines, and shrinks after sending.
  const fit = () => {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight + 2, 160) + "px";
  };
  box.addEventListener("input", fit);
  form.addEventListener("reset", () => setTimeout(fit));

  // The "more" menu closes on a tap anywhere else.
  const menu = document.querySelector("details.menu");
  document.addEventListener("click", (e) => {
    if (menu && menu.open && !menu.contains(e.target)) menu.open = false;
  });

  // Read aloud. iPhones speak only after a tap on the page, so the first tap
  // (the speaker button, or any tap later) starts the voice once.
  const speak = document.getElementById("speak");
  const voice = "speechSynthesis" in window ? window.speechSynthesis : null;
  let on = false;
  let woken = false;
  const wake = () => {
    if (!voice || woken) return;
    woken = true;
    voice.speak(new SpeechSynthesisUtterance(""));
  };
  const say = (text) => {
    voice.cancel();
    // short pieces: some browsers stop a long one part way
    for (const part of text.match(/[^.!?\n]+[.!?]*/g) || []) {
      if (part.trim()) voice.speak(new SpeechSynthesisUtterance(part.trim()));
    }
  };
  const show = () => { speak.setAttribute("aria-pressed", String(on)); };
  if (voice && speak) {
    speak.hidden = false;
    try { on = localStorage.getItem("agentc-speak") === "1"; } catch (_) {}
    show();
    document.addEventListener("pointerdown", wake, { once: true });
    speak.addEventListener("click", () => {
      on = !on;
      show();
      try { localStorage.setItem("agentc-speak", on ? "1" : "0"); } catch (_) {}
      if (on) { woken = true; say("Replies will be read aloud."); } else voice.cancel();
    });
    document.body.addEventListener("htmx:load", (e) => {  // a finished reply arrived
      const el = e.detail.elt;
      if (!on || el === document.body || !el.querySelector) return;
      const said = el.matches("[data-say]") ? el : el.querySelector("[data-say]");
      if (said) say(said.textContent);
    });
  }

  // Talk. Tap the mic, speak (pauses are fine), tap stop. The words land in the box
  // to check, then Send. Browsers end listening after a silence, so it starts again
  // until the owner taps stop.
  const Listen = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = document.getElementById("mic");
  const note = document.getElementById("heard");
  if (!Listen || !window.isSecureContext || !mic) return;
  mic.hidden = false;
  let ear = null;      // the recogniser while listening
  let before = "";     // the box's text before this round of listening
  let heard = "";      // what this round heard
  const join = (a, b) => [a.trim(), b.trim()].filter(Boolean).join(" ");
  const stop = () => {
    const e = ear;
    ear = null;
    mic.classList.remove("listening");
    mic.setAttribute("aria-label", "Talk");
    note.hidden = true;
    if (e) e.stop();
    fit();
  };
  const round = () => {
    const e = new Listen();
    e.lang = navigator.language || "en-GB";
    e.continuous = true;
    e.interimResults = true;  // the words appear as you speak
    heard = "";
    e.onresult = (r) => {
      heard = Array.from(r.results).map((x) => x[0].transcript).join(" ").replace(/\s+/g, " ");
      box.value = join(before, heard);
      fit();
    };
    e.onend = () => {
      if (ear !== e) return;  // stopped by the owner
      before = join(before, heard);  // a silence ended it: keep the words, listen again
      try { round(); } catch (_) { stop(); }
    };
    e.onerror = (err) => {
      if (err.error === "no-speech" || err.error === "aborted") return;
      box.placeholder = "Could not use the mic. Type instead.";
      stop();
    };
    ear = e;
    e.start();
  };
  mic.addEventListener("click", () => {
    wake();
    if (ear) { stop(); return; }
    before = box.value;
    mic.classList.add("listening");
    mic.setAttribute("aria-label", "Stop listening");
    note.hidden = false;
    try { round(); } catch (_) { stop(); }
  });
  form.addEventListener("submit", () => { if (ear) stop(); });
})();
