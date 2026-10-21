/* "Actualiser": starts a real refresh on the server in three steps (re-test disabled sources;
 * collect and process every live source; write the missing French briefs), shows where it
 * stands, then swaps the page content in place. The refresh goes on if you change page:
 * every page picks up its progress. Without JavaScript the button stays hidden. */
(function () {
  const $ = (sel) => document.querySelector(sel);
  const btn = $("[data-refresh]"), panel = $("[data-refresh-panel]"), stepEl = $("[data-refresh-step]");
  const text = $("[data-refresh-text]"), bar = $("[data-refresh-bar]"), stopBtn = $("[data-refresh-stop]");
  const status = $("[data-refresh-status]");
  if (!btn || !panel) return;
  btn.hidden = false;
  const label = btn.querySelector("span");
  let lastAnnounced = "";
  let canStop = false;              // only the machine hosting the site may stop a refresh

  const say = (msg) => { if (status && msg !== lastAnnounced) { status.textContent = msg; lastAnnounced = msg; } };
  const eta = (s) => s == null ? "" : s < 60 ? "moins d'une minute restante" : `environ ${Math.round(s / 60)} min restantes`;
  const show = (step, msg, pct, kind) => {
    panel.hidden = false;
    panel.querySelector(".refresh-panel__link")?.remove();
    panel.dataset.kind = kind || "run";
    stepEl.textContent = step || "";
    stepEl.hidden = !step;
    text.textContent = msg;
    bar.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
    stopBtn.hidden = !(kind === "run" && canStop);
  };
  const busy = (on) => {
    btn.disabled = on;
    if (on) btn.setAttribute("aria-busy", "true"); else btn.removeAttribute("aria-busy");
    label.textContent = on ? "Actualisation…" : "Actualiser";
  };

  function describe(s) {
    const step = s.step ? `Étape ${s.step} sur ${s.steps} · ${s.step_label}` : "";
    let msg = s.label || "";
    if (s.stage === "collect" && s.total) msg += ` — ${s.done} sur ${s.total}` + (s.detail ? ` · ${s.detail}` : "");
    else if (s.stage === "redact" && s.total) {
      msg += ` — ${s.done} sur ${s.total}` + (s.eta_s != null ? ` · ${eta(s.eta_s)}` : "");
      if (s.detail) msg += ` · « ${s.detail} »`;
    } else if (s.stage === "redact" && s.detail) msg += ` — ${s.detail}`;
    else if (s.stage === "redact" && s.total === 0 && s.done === 0 && s.percent === 0) msg += " — recherche des événements sans fiche";
    if (s.stopping) msg += " · arrêt demandé, fin de l'étape en cours";
    return [step, msg];
  }

  function addDetailLink() {
    if (panel.querySelector(".refresh-panel__link")) return;
    const a = document.createElement("a");
    a.href = "/collecte";
    a.className = "refresh-panel__link";
    a.textContent = "Voir l'état de la collecte";
    text.after(a);
  }

  async function swapContent() {
    const res = await fetch(location.href, { cache: "no-store" });
    if (!res.ok) return;
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    if (!!doc.querySelector(".notice--fixture") !== !!document.querySelector(".notice--fixture")) { location.reload(); return; }
    for (const part of ["main", "foot"]) {
      const now = $(`[data-refresh-part="${part}"]`), next = doc.querySelector(`[data-refresh-part="${part}"]`);
      if (now && next) now.replaceWith(next);
    }
  }

  async function poll() {
    try {
      const s = await (await fetch("/api/refresh", { cache: "no-store" })).json();
      if (s.state === "running") {
        const [step, msg] = describe(s);
        canStop = !!s.can_stop;
        show(step, msg + "…", s.percent, "run");
        stopBtn.disabled = !!s.stopping;
        say(step);                                   // announce step changes, not every tick
        setTimeout(poll, 1500);
        return;
      }
      busy(false);
      if (s.state === "done") {
        await swapContent();
        show("Actualisation terminée", s.message, 100, "done");
        say("Actualisation terminée. " + s.message);
        setTimeout(() => { if (panel.dataset.kind === "done") panel.hidden = true; }, 15000);
      } else if (s.state === "error") {
        show("Actualisation interrompue", s.message, s.percent, "error");
        addDetailLink();
        say(s.message);
      }
    } catch (err) {
      busy(false);
      show("", "Impossible de suivre l'actualisation. Rechargez la page.", 0, "error");
    }
  }

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    busy(true);
    try {
      const s = await (await fetch("/api/refresh", { method: "POST" })).json();
      if (s.state === "running") {
        show("Étape 1 sur 3 · Vérification des sources", "Démarrage… Vous pouvez continuer à naviguer pendant l'actualisation.", 0, "run");
        say("Actualisation lancée.");
        setTimeout(poll, 800);
      } else {
        busy(false);
        show("", s.message || "Actualisation impossible pour le moment.", 0, s.state === "cooldown" ? "info" : "error");
        say(s.message || "");
      }
    } catch (err) {
      busy(false);
      show("", "Le serveur ne répond pas.", 0, "error");
    }
  });

  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    try { await fetch("/api/refresh/stop", { method: "POST" }); } catch (err) { stopBtn.disabled = false; }
  });

  // A refresh started earlier (or from another page) keeps being followed.
  fetch("/api/refresh", { cache: "no-store" }).then((r) => r.json()).then((s) => {
    canStop = !!s.can_stop;
    if (s.allowed === false) { btn.hidden = true; return; }
    if (s.state !== "running" && s.cooldown_s > 0) {
      btn.title = `Actualisé récemment. Prochaine actualisation possible dans ${Math.floor(s.cooldown_s / 60) + 1} min.`;
    }
    if (s.state === "running") { busy(true); poll(); }
  }).catch(() => {});
})();
