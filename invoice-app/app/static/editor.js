/* Invoice editor: five steps, autosave, live PDF preview, in-app dialogs.
   No money is computed in the browser. Every total comes back from the server (Decimal). */
(function () {
  "use strict";
  const root = document.getElementById("editor");
  const dataEl = document.getElementById("editor-data");
  if (!dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  const id = data.invoice;
  const $ = sel => document.querySelector(sel);
  const $$ = sel => Array.from(document.querySelectorAll(sel));
  const M = window.Motion;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const state = {
    client_id: data.clientId,
    issue_date: $("#issue_date").value,
    due_date: $("#due_date").value,
    template: (document.querySelector("input[name=template]:checked") || {}).value || "minimal",
    payment_terms: $("#payment_terms").value,
    payment_instructions: $("#payment_instructions").value,
    notes: $("#notes").value,
    items: data.items.map(i => ({
      service_id: i.service_id, description: i.description, unit: i.unit, quantity: i.quantity,
      unit_price: i.unit_price, discount_pct: i.discount_pct, vat_rate: i.vat_rate,
    })),
  };
  const clientsById = {};
  data.clients.forEach(c => { clientsById[c.id] = c; });

  const stages = $$(".stage");
  const stepButtons = $$(".step");
  let current = 0;
  let saveTimer = null;
  let inFlight = null;
  let pending = false;
  let finalizing = false;
  let navigating = false;

  /* ---------- steps ---------- */
  function showStep(index) {
    current = Math.max(0, Math.min(stages.length - 1, index));
    stages.forEach((s, i) => { s.hidden = i !== current; });
    stepButtons.forEach((b, i) => {
      const on = i === current;
      b.classList.toggle("current", on);
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    $("#prev-step").disabled = current === 0;
    $("#next-step").hidden = current === stages.length - 1;
    if (!reduce && M && M.animate) {
      M.animate(stages[current], { opacity: [0, 1], transform: ["translateY(6px)", "none"] }, { duration: 0.22 });
    }
  }
  stepButtons.forEach((b, i) => b.addEventListener("click", () => showStep(i)));
  $("#prev-step").addEventListener("click", () => showStep(current - 1));
  $("#next-step").addEventListener("click", () => {
    if (current === 0 && !state.client_id) { showError("Choose a client to continue."); return; }
    showStep(current + 1);
  });

  /* ---------- status and errors ---------- */
  function setStatus(text, tone) {
    const el = $("#save-state");
    el.textContent = text;
    el.classList.toggle("text-destructive", tone === "error");
  }
  function showError(message, list) {
    const box = $("#form-error");
    box.hidden = false;
    box.textContent = "";
    const p = document.createElement("p");
    p.className = "font-medium";
    p.textContent = message;
    box.appendChild(p);
    if (list && list.length) {
      const ul = document.createElement("ul");
      ul.className = "mt-1 list-disc pl-5";
      list.forEach(t => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
      box.appendChild(ul);
    }
    box.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  }
  function clearError() { $("#form-error").hidden = true; }

  /* ---------- totals (server authoritative) ---------- */
  function applyTotals(payload) {
    if (!payload || !payload.totals) return;
    const t = payload.totals;
    $("#sum-subtotal").textContent = t.subtotal;
    $("#sum-vat").textContent = t.vat_total;
    $("#sum-total").textContent = t.total;
    $("#bar-total").textContent = `${t.total} ${data.currency}`;
    $("#mobile-total").textContent = `${t.total} ${data.currency}`;
    (payload.lines || []).forEach((l, idx) => {
      const cell = document.getElementById(`lt-${idx}`);
      if (cell) cell.textContent = l.line_total;
    });
  }

  /* ---------- clients ---------- */
  function selectClient(client) {
    state.client_id = client.id;
    $$(".pick").forEach(b => {
      const on = Number(b.dataset.id) === client.id;
      b.classList.toggle("selected", on);
      b.setAttribute("aria-pressed", String(on));
    });
    $("#client-card").hidden = false;
    $("#client-card-name").textContent = client.name;
    $("#client-card-email").textContent = client.email;
    $("#client-card-address").textContent = client.billing_address || "";
    $("#sum-client").textContent = client.name;
    clearError();
    scheduleSave(0);
  }
  function bindPick(btn) {
    btn.addEventListener("click", () => {
      const cached = clientsById[btn.dataset.id];
      selectClient(cached || { id: Number(btn.dataset.id), name: btn.dataset.name, email: btn.dataset.email, billing_address: btn.dataset.address });
    });
  }
  $$(".pick").forEach(bindPick);
  $("#client-search").addEventListener("input", e => {
    const q = e.target.value.trim().toLowerCase();
    $$("#client-list .pick").forEach(b => {
      const hit = !q || (b.dataset.name + " " + b.dataset.email).toLowerCase().includes(q);
      b.closest("li").hidden = !hit;
    });
  });

  const dialog = $("#client-dialog");
  $("#new-client-btn").addEventListener("click", () => {
    $("#client-form").reset();
    $("#client-error").hidden = true;
    dialog.showModal();
    dialog.querySelector("input[name=name]").focus();
  });
  dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
  $("#client-form").addEventListener("submit", async e => {
    e.preventDefault();
    const btn = $("#client-save");
    btn.disabled = true;
    const body = Object.fromEntries(new FormData(e.target).entries());
    try {
      const res = await fetch("/api/clients", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = $("#client-error");
        msg.hidden = false;
        msg.textContent = Object.values(json.errors || {}).join(" ") || json.error || "Could not save the client.";
        return;
      }
      const c = json.client;
      clientsById[c.id] = c;
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pick flex w-full flex-col items-start gap-0.5 px-4 py-3 text-left transition-colors hover:bg-muted";
      b.dataset.id = c.id; b.dataset.name = c.name; b.dataset.email = c.email; b.dataset.address = c.billing_address || "";
      b.innerHTML = '<span class="text-sm font-medium"></span><span class="text-xs text-muted-foreground"></span>';
      b.children[0].textContent = c.name;
      b.children[1].textContent = c.email;
      bindPick(b);
      li.appendChild(b);
      $("#client-list").prepend(li);
      selectClient(c);
      dialog.close();
    } finally {
      btn.disabled = false;
    }
  });

  /* ---------- lines ---------- */
  const INPUT = "field field-sm";
  const FIELDS = [
    ["description", "min-w-[9rem]", "text", "Description"],
    ["quantity", "w-14", "text", "Quantity", "decimal"],
    ["unit", "w-20", "text", "Unit"],
    ["unit_price", "w-24 text-right", "text", "Unit price", "decimal"],
    ["discount_pct", "w-14", "text", "Discount %", "decimal"],
    ["vat_rate", "w-14", "text", "VAT %", "decimal"],
  ];
  function renderLines() {
    const body = $("#line-body");
    body.textContent = "";
    $("#no-lines").hidden = state.items.length > 0;
    state.items.forEach((item, idx) => {
      const tr = document.createElement("tr");
      FIELDS.forEach(([field, cls, type, label, mode]) => {
        const td = document.createElement("td");
        td.className = "px-1.5 py-2 align-top";
        const input = document.createElement("input");
        input.type = type;
        input.className = `${INPUT} ${cls}`;
        input.value = item[field] ?? "";
        input.setAttribute("aria-label", `${label}, line ${idx + 1}`);
        if (mode) input.inputMode = mode;
        if (field === "description") input.maxLength = 500;
        if (field === "unit") input.maxLength = 40;
        input.addEventListener("input", () => { item[field] = input.value; scheduleSave(); });
        td.appendChild(input);
        tr.appendChild(td);
      });
      const lt = document.createElement("td");
      lt.className = "num px-3 py-2 pt-3.5 text-right whitespace-nowrap";
      lt.id = `lt-${idx}`;
      lt.textContent = "—";
      tr.appendChild(lt);
      const rmTd = document.createElement("td");
      rmTd.className = "px-1 py-2 pt-2.5";
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "grid h-8 w-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-destructive";
      rm.setAttribute("aria-label", `Remove line ${idx + 1}`);
      rm.innerHTML = '<svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
      rm.addEventListener("click", () => { state.items.splice(idx, 1); renderLines(); scheduleSave(0); });
      rmTd.appendChild(rm);
      tr.appendChild(rmTd);
      body.appendChild(tr);
    });
  }
  function addLine(line) {
    state.items.push({ service_id: null, description: "", unit: "", quantity: "1", unit_price: "0.00",
      discount_pct: "0", vat_rate: String(data.defaultVat), ...line });
    renderLines();
    const last = $("#line-body tr:last-child input");
    if (last && !line.description) last.focus();
    scheduleSave(0);
  }
  $$(".chip").forEach(chip => chip.addEventListener("click", () => {
    const s = JSON.parse(chip.dataset.service);
    addLine({ service_id: s.id, description: s.name + (s.description ? ` — ${s.description}` : ""),
      unit: s.unit, unit_price: s.unit_price, vat_rate: s.vat_rate });
  }));
  $("#add-custom").addEventListener("click", () => addLine({ description: "" }));

  /* ---------- details and template ---------- */
  ["issue_date", "due_date", "payment_terms", "payment_instructions", "notes"].forEach(name => {
    $("#" + name).addEventListener("input", e => { state[name] = e.target.value; scheduleSave(); });
  });
  $$("input[name=template]").forEach(r => r.addEventListener("change", e => {
    state.template = e.target.value;
    scheduleSave(0);
  }));

  /* ---------- autosave ---------- */
  function payload() {
    return {
      client_id: state.client_id,
      issue_date: state.issue_date,
      due_date: state.due_date,
      template: state.template,
      payment_terms: state.payment_terms,
      payment_instructions: state.payment_instructions,
      notes: state.notes,
      items: state.items.map(i => ({ ...i })),
    };
  }
  function scheduleSave(delay = 600) {
    clearTimeout(saveTimer);
    setStatus("Saving…");
    saveTimer = setTimeout(save, delay);
  }
  function save() {
    if (inFlight) { pending = true; return inFlight; }
    // A brand-new draft with nothing in it yet is not an error. Stay quiet until the user adds something.
    if (!state.client_id && !state.items.length) {
      setStatus("Not saved yet. Add a client and at least one line.");
      return Promise.resolve(false);
    }
    setStatus("Saving…");
    inFlight = fetch(`/api/invoices/${id}/draft`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: payload() }),
    }).then(async res => {
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setStatus("Not saved. Check the highlighted fields.", "error");
        showError(json.error || "Could not save this draft.", Object.values(json.errors || {}));
        return false;
      }
      clearError();
      applyTotals(json);
      setStatus(`Saved at ${json.saved_at}`);
      refreshPreview();
      return true;
    }).catch(() => {
      setStatus("Connection lost. Your changes are kept on this page. Press Save draft to retry.", "error");
      return false;
    }).finally(() => {
      inFlight = null;
      if (pending) { pending = false; scheduleSave(0); }
    });
    return inFlight;
  }
  function refreshPreview() {
    $("#preview").src = `/invoices/${id}/preview.pdf?t=${Date.now()}`;
    $("#preview-state").textContent = "Up to date";
  }
  $("#save-draft").addEventListener("click", () => { clearTimeout(saveTimer); save(); });
  $("#save-draft-2").addEventListener("click", () => { clearTimeout(saveTimer); save(); });

  /* ---------- finalize and send (in-app confirmation) ---------- */
  const finalizeDialog = $("#finalize-dialog");
  $("#finalize-send").addEventListener("click", () => {
    if (!state.client_id) { showStep(0); showError("Choose a client before finalizing."); return; }
    if (!state.items.length) { showStep(1); showError("Add at least one line before finalizing."); return; }
    $("#finalize-recipient").textContent = $("#client-card-email").textContent || "the client";
    finalizeDialog.showModal();
    $("#finalize-confirm").focus();
  });
  finalizeDialog.addEventListener("click", e => { if (e.target === finalizeDialog) finalizeDialog.close(); });
  finalizeDialog.querySelectorAll("[data-dialog-close]").forEach(b => b.addEventListener("click", () => finalizeDialog.close()));

  $("#finalize-confirm").addEventListener("click", async () => {
    if (finalizing) return;
    finalizing = true;
    const btn = $("#finalize-confirm");
    const trigger = $("#finalize-send");
    btn.disabled = true;
    trigger.disabled = true;
    btn.textContent = "Finalizing and sending…";
    clearTimeout(saveTimer);
    try {
      const res = await fetch(`/api/invoices/${id}/finalize-send`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: payload(), subject: $("#email_subject").value, body: $("#email_body").value }),
      });
      const json = await res.json().catch(() => ({ ok: false, error: "Unexpected response from the server." }));
      if (json.ok || json.status === "finalized" || json.status === "sent") {
        navigating = true;
        window.location.href = json.redirect;
        return;
      }
      finalizeDialog.close();
      showError(json.error || "Could not finalize.", Object.values(json.errors || {}));
    } catch (err) {
      finalizeDialog.close();
      showError("Network error. Nothing was sent. Check your connection and try again.");
    } finally {
      if (!navigating) {
        finalizing = false;
        btn.disabled = false;
        trigger.disabled = false;
        btn.textContent = "Finalize & send";
      }
    }
  });

  /* ---------- boot ---------- */
  renderLines();
  showStep(0);
  if (state.client_id) {
    const pick = $(`.pick[data-id="${state.client_id}"]`);
    if (pick) { pick.classList.add("selected"); pick.setAttribute("aria-pressed", "true"); }
  }
  save();
})();
