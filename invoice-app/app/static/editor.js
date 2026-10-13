/* Invoice editor: five stages, autosave with debounce, live PDF preview.
   Money is never computed here. Every total comes back from the server, which uses Decimal. */
(function () {
  "use strict";
  const root = document.getElementById("editor");
  if (!root) return;
  const data = JSON.parse(document.getElementById("editor-data").textContent);
  const id = data.invoice;
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

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

  const steps = $$(".stage");
  const stepButtons = $$(".step");
  let current = 0;
  let saveTimer = null;
  let inFlight = null;
  let pending = false;
  let finalizing = false;
  let navigating = false;

  /* ---------- stage navigation ------------------------------------------ */
  function showStep(index) {
    current = Math.max(0, Math.min(steps.length - 1, index));
    steps.forEach((s, i) => { s.hidden = i !== current; });
    stepButtons.forEach((b, i) => {
      b.classList.toggle("current", i === current);
      b.setAttribute("aria-selected", i === current ? "true" : "false");
    });
    $("#prev-step").disabled = current === 0;
    const next = $("#next-step");
    next.hidden = current === steps.length - 1;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  stepButtons.forEach((b, i) => b.addEventListener("click", () => showStep(i)));
  $("#prev-step").addEventListener("click", () => showStep(current - 1));
  $("#next-step").addEventListener("click", () => {
    if (current === 0 && !state.client_id) { showError("Choose a client to continue."); return; }
    showStep(current + 1);
  });

  /* ---------- status + errors ------------------------------------------- */
  function setStatus(text, tone) {
    const el = $("#save-state");
    el.textContent = text;
    el.style.color = tone === "error" ? "var(--danger)" : "";
  }
  function showError(message, list) {
    const box = $("#form-error");
    box.hidden = false;
    box.textContent = "";
    const p = document.createElement("div");
    p.textContent = message;
    box.appendChild(p);
    if (list && list.length) {
      const ul = document.createElement("ul");
      ul.className = "error-list";
      list.forEach(t => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
      box.appendChild(ul);
    }
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function clearError() { $("#form-error").hidden = true; }

  /* ---------- totals (server authoritative) ----------------------------- */
  function applyTotals(payload) {
    if (!payload || !payload.totals) return;
    const t = payload.totals;
    $("#sum-subtotal").textContent = t.subtotal;
    $("#sum-vat").textContent = t.vat_total;
    $("#sum-total").textContent = t.total;
    $("#bar-total").textContent = `${t.total} ${data.currency}`;
    const mobile = $("#mobile-bar");
    mobile.hidden = false;
    mobile.innerHTML = "";
    const label = document.createElement("span");
    label.className = "muted small";
    label.textContent = "Total payable";
    const value = document.createElement("strong");
    value.textContent = t.total;
    mobile.append(label, value);
    if (payload.lines) {
      payload.lines.forEach((l, idx) => {
        const cell = document.getElementById(`lt-${idx}`);
        if (cell) cell.textContent = l.line_total;
      });
    }
  }

  /* ---------- clients ---------------------------------------------------- */
  function selectClient(client) {
    state.client_id = client.id;
    $$(".pick").forEach(b => b.classList.toggle("selected", Number(b.dataset.id) === client.id));
    $("#client-card").hidden = false;
    $("#client-card-name").textContent = client.name;
    $("#client-card-email").textContent = client.email;
    $("#client-card-address").textContent = client.billing_address || "";
    $("#sum-client").textContent = client.name;
    scheduleSave(0);
  }
  $$(".pick").forEach(btn => btn.addEventListener("click", () => {
    selectClient(clientsById[btn.dataset.id] || { id: Number(btn.dataset.id), name: btn.dataset.name,
      email: btn.dataset.email, billing_address: btn.dataset.address });
  }));
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
  });
  $("#client-cancel").addEventListener("click", () => dialog.close());
  $("#client-form").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.target;
    const btn = $("#client-save");
    btn.disabled = true;
    const body = Object.fromEntries(new FormData(form).entries());
    try {
      const res = await fetch("/api/clients", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const json = await res.json();
      if (!res.ok) {
        const msg = $("#client-error");
        msg.hidden = false;
        msg.textContent = Object.values(json.errors || {}).join(" ") || json.error || "Could not save the client.";
        return;
      }
      const c = json.client;
      clientsById[c.id] = c;
      const li = document.createElement("li");
      const btnEl = document.createElement("button");
      btnEl.type = "button"; btnEl.className = "pick selected"; btnEl.dataset.id = c.id;
      btnEl.dataset.name = c.name; btnEl.dataset.email = c.email; btnEl.dataset.address = c.billing_address;
      const strong = document.createElement("strong"); strong.textContent = c.name;
      const small = document.createElement("span"); small.className = "muted small"; small.textContent = c.email;
      btnEl.append(strong, small);
      btnEl.addEventListener("click", () => selectClient(clientsById[c.id]));
      li.appendChild(btnEl);
      $("#client-list").prepend(li);
      selectClient(c);
      dialog.close();
    } finally {
      btn.disabled = false;
    }
  });

  /* ---------- lines ------------------------------------------------------ */
  const FIELDS = [
    ["description", "l-desc", "text"], ["quantity", "qty", "text"], ["unit", "unit", "text"],
    ["unit_price", "price", "text"], ["discount_pct", "pct", "text"], ["vat_rate", "pct", "text"],
  ];
  function renderLines() {
    const body = $("#line-body");
    body.textContent = "";
    $("#no-lines").hidden = state.items.length > 0;
    state.items.forEach((item, idx) => {
      const tr = document.createElement("tr");
      FIELDS.forEach(([field, cls, type]) => {
        const td = document.createElement("td");
        td.className = cls === "l-desc" ? "l-desc" : cls;
        const input = document.createElement("input");
        input.type = type;
        input.value = item[field] ?? "";
        input.setAttribute("aria-label", `${field.replace("_", " ")}, line ${idx + 1}`);
        input.inputMode = ["quantity", "unit_price", "discount_pct", "vat_rate"].includes(field) ? "decimal" : "text";
        if (field === "description") input.maxLength = 500;
        if (field === "unit") input.maxLength = 40;
        input.addEventListener("input", () => { item[field] = input.value; scheduleSave(); });
        td.appendChild(input);
        tr.appendChild(td);
      });
      const lt = document.createElement("td");
      lt.className = "lt"; lt.id = `lt-${idx}`; lt.textContent = "—";
      tr.appendChild(lt);
      const rmTd = document.createElement("td");
      const rm = document.createElement("button");
      rm.type = "button"; rm.className = "rm"; rm.setAttribute("aria-label", `Remove line ${idx + 1}`);
      rm.textContent = "✕";
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
    scheduleSave(0);
  }
  $$(".chip").forEach(chip => chip.addEventListener("click", () => {
    const s = JSON.parse(chip.dataset.service);
    addLine({ service_id: s.id, description: s.name + (s.description ? ` — ${s.description}` : ""),
      unit: s.unit, unit_price: s.unit_price, vat_rate: s.vat_rate });
  }));
  $("#add-custom").addEventListener("click", () => addLine({ description: "" }));

  /* ---------- details / template ---------------------------------------- */
  ["issue_date", "due_date", "payment_terms", "payment_instructions", "notes"].forEach(name => {
    $("#" + name).addEventListener("input", e => { state[name] = e.target.value; scheduleSave(); });
  });
  $$("input[name=template]").forEach(r => r.addEventListener("change", e => {
    state.template = e.target.value;
    scheduleSave(0);
  }));

  /* ---------- autosave --------------------------------------------------- */
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
  async function save() {
    if (inFlight) { pending = true; return inFlight; }
    setStatus("Saving…");
    inFlight = fetch(`/api/invoices/${id}/draft`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: payload() }),
    }).then(async res => {
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setStatus("Not saved. Fix the highlighted issue.", "error");
        showError(json.error || "Could not save this draft.", Object.values(json.errors || {}));
        return false;
      }
      clearError();
      applyTotals(json);
      setStatus(`Saved at ${json.saved_at}`);
      refreshPreview();
      return true;
    }).catch(() => {
      setStatus("Connection lost. Changes are kept on this page, press Save draft to retry.", "error");
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

  /* ---------- finalize & send ------------------------------------------- */
  $("#finalize-send").addEventListener("click", async () => {
    if (finalizing) return;
    if (!state.client_id) { showStep(0); showError("Choose a client before finalizing."); return; }
    if (!state.items.length) { showStep(1); showError("Add at least one line before finalizing."); return; }
    if (!confirm("Finalize this invoice and email it to the client? The number is assigned now and the invoice can no longer be edited.")) return;
    finalizing = true;
    const btn = $("#finalize-send");
    btn.disabled = true;
    btn.textContent = "Finalizing and sending…";
    clearTimeout(saveTimer);
    try {
      const res = await fetch(`/api/invoices/${id}/finalize-send`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: payload(), subject: $("#email_subject").value, body: $("#email_body").value }),
      });
      const json = await res.json().catch(() => ({ ok: false, error: "Unexpected response from the server." }));
      if (json.ok || json.status === "finalized" || json.status === "sent") {
        // Finalized (and possibly not yet sent): the invoice page shows the outcome and offers Resend.
        navigating = true;
        window.location.href = json.redirect;
        return;
      }
      showError(json.error || "Could not finalize.", Object.values(json.errors || {}));
    } catch (err) {
      showError("Network error. Nothing was sent. Check your connection and try again.");
    } finally {
      if (!navigating) {
        finalizing = false;
        btn.disabled = false;
        btn.textContent = "Finalize & send";
      }
    }
  });

  /* ---------- boot ------------------------------------------------------- */
  renderLines();
  showStep(data.clientId ? 1 : 0);
  if (data.clientId) {
    const pick = $(`.pick[data-id="${data.clientId}"]`);
    if (pick) pick.classList.add("selected");
  }
  save();
})();
