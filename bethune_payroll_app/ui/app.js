/* Bethune Nursing Home — Attendance & Payroll (front-end)
   Every call returns {ok, data|error}. Employees, salaries, waivers and policy
   are stored in SQLite by the Python side; punches are read from the eSSL .mdb. */

"use strict";

const State = {
  employees: [],
  payroll: null,
  config: null,
  filter: "all",
  month: new Date().getMonth() + 1,
  year: new Date().getFullYear(),
  cutoff: null,
  editingId: null,          // null = adding a new employee
};

/* ---------------- API: desktop (PyWebView) or browser (HTTP) ---------------- */
function apiReady() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve();
    const t = setTimeout(resolve, 400);
    window.addEventListener("pywebviewready", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}
async function call(method, ...args) {
  await apiReady();
  let res;
  if (window.pywebview && window.pywebview.api && window.pywebview.api[method]) {
    res = await window.pywebview.api[method](...args);
  } else {
    const resp = await fetch("/api/" + method, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args }),
    });
    res = await resp.json();
  }
  if (res && res.ok === false) throw new Error(res.error || "Unknown error");
  return res ? res.data : res;
}

/* ---------------- utils ---------------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const money = (n) => "₹" + (Number(n) || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];

let toastTimer;
function toast(msg, kind = "", ms = 2800) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

/* ---------------- tabs ---------------- */
$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((t) => t.classList.remove("active"));
  $$(".tab-panel").forEach((p) => p.classList.remove("active"));
  tab.classList.add("active");
  $("#tab-" + tab.dataset.tab).classList.add("active");
  if (tab.dataset.tab === "attendance" && !State.payroll) loadPayroll();
}));

/* ---------------- status badge ---------------- */
async function refreshStatus() {
  try {
    const s = await call("get_status");
    $("#syncBadge").className = "sync-badge " + (s.demo ? "sync-demo" : s.ok ? "sync-ok" : "sync-err");
    $("#syncText").textContent = s.demo ? "DEMO DATA — .mdb not found" : s.ok ? "eSSL DB Connected" : "eSSL DB Error";
  } catch (e) {
    $("#syncBadge").className = "sync-badge sync-err";
    $("#syncText").textContent = "Backend error";
  }
}

/* ===================================================================== */
/* TAB 1: Employees & Salaries (SQLite CRUD)                             */
/* ===================================================================== */
// unsaved inline edits, keyed by the employee's saved ID
State.edits = {};

async function loadEmployees() {
  State.employees = await call("list_employees");
  // keep unsaved edits for rows that still exist (e.g. after adding someone)
  const ids = new Set(State.employees.map((e) => String(e.employee_id)));
  State.edits = Object.fromEntries(Object.entries(State.edits).filter(([k]) => ids.has(k)));
  renderEmployees();
}

const current = (e) => State.edits[e.employee_id] || e;   // edited values win

function renderEmployees() {
  const q = ($("#empSearch").value || "").toLowerCase();
  const rows = State.employees.filter((e) => {
    const c = current(e);
    return !q || String(c.employee_id).toLowerCase().includes(q) || String(c.name).toLowerCase().includes(q);
  });
  const body = $("#empTableBody");
  if (!State.employees.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty">No employees yet. Click <b>⇩ Import from eSSL</b> to load everyone from the biometric database.</td></tr>`;
  } else if (!rows.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty">No match for “${esc(q)}”.</td></tr>`;
  } else {
    body.innerHTML = rows.map((e) => {
      const c = current(e), dirty = !!State.edits[e.employee_id];
      return `
      <tr data-id="${esc(e.employee_id)}" class="${dirty ? "row-dirty" : ""}">
        <td><input class="cell-edit js-id" value="${esc(c.employee_id)}" style="width:90px"></td>
        <td><input class="cell-edit js-name" value="${esc(c.name)}" style="width:100%;min-width:220px"></td>
        <td class="num"><input class="cell-edit js-sal num" type="number" min="0" step="100" value="${c.salary || 0}" style="width:120px"></td>
        <td class="num actions"><button class="btn btn-sm btn-danger js-del">Delete</button></td>
      </tr>`;
    }).join("");
    body.querySelectorAll("tr[data-id]").forEach((tr) => {
      const id = tr.dataset.id;
      tr.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => {
        const orig = State.employees.find((x) => String(x.employee_id) === id);
        const val = {
          employee_id: tr.querySelector(".js-id").value.trim(),
          name: tr.querySelector(".js-name").value,
          salary: tr.querySelector(".js-sal").value === "" ? "" : Number(tr.querySelector(".js-sal").value),
        };
        const changed = val.employee_id !== String(orig.employee_id) || val.name !== orig.name ||
                        Number(val.salary) !== Number(orig.salary);
        if (changed) State.edits[id] = val; else delete State.edits[id];
        tr.classList.toggle("row-dirty", changed);
        updateEmpFooter();
      }));
      tr.querySelector(".js-del").onclick = () => deleteEmployee(id);
    });
  }
  updateEmpFooter();
}

function updateEmpFooter() {
  const n = Object.keys(State.edits).length;
  $("#saveEmpBtn").disabled = n === 0;
  $("#saveEmpBtn").textContent = n ? `Save Changes (${n})` : "Save Changes";
  const total = State.employees.reduce((s, e) => s + (Number(current(e).salary) || 0), 0);
  const noSalary = State.employees.filter((e) => !Number(current(e).salary)).length;
  $("#empHint").textContent = `${State.employees.length} employees · total monthly salary ${money(total)}` +
    (noSalary ? ` · ${noSalary} without a salary yet` : "") +
    (n ? ` · ${n} unsaved change${n > 1 ? "s" : ""}` : "");
}
$("#empSearch").addEventListener("input", renderEmployees);

$("#saveEmpBtn").onclick = async () => {
  const changes = Object.entries(State.edits).map(([orig, v]) => ({ original_id: orig, ...v }));
  if (!changes.length) return;
  try {
    const r = await call("save_employees", changes);
    const failed = r.errors.map((e) => e.original_id);
    if (r.errors.length) {
      alert("Some rows were not saved:\n\n" + r.errors.map((e) => `ID ${e.original_id}: ${e.error}`).join("\n"));
    }
    toast(`Saved ${r.saved} employee${r.saved === 1 ? "" : "s"}`, r.errors.length ? "err" : "ok");
    const keep = Object.fromEntries(Object.entries(State.edits).filter(([k]) => failed.includes(k)));
    State.employees = await call("list_employees");
    State.edits = keep;                       // failed rows stay highlighted for fixing
    State.payroll = null;
    renderEmployees();
  } catch (e) { toast("Save failed: " + e.message, "err"); }
};

$("#esslImportBtn").onclick = async () => {
  try {
    toast("Reading employees from eSSL…");
    const r = await call("import_from_essl");
    toast(r.added
      ? `Added ${r.added} new employee${r.added > 1 ? "s" : ""} from eSSL (${r.existing} already here). Now fill in salaries and Save.`
      : `All ${r.in_essl} eSSL employees are already here — nothing to add.`, "ok", 6000);
    State.payroll = null;
    await loadEmployees();
  } catch (e) { toast("eSSL import failed: " + e.message, "err", 6000); }
};

window.addEventListener("beforeunload", (ev) => {
  if (Object.keys(State.edits).length) { ev.preventDefault(); ev.returnValue = ""; }
});

/* add / edit modal */
function openEmpModal(id = null) {
  State.editingId = id;
  const e = id ? State.employees.find((x) => String(x.employee_id) === String(id)) : null;
  $("#empModalTitle").textContent = e ? `Edit Employee ${e.employee_id}` : "Add Employee";
  $("#fId").value = e ? e.employee_id : "";
  $("#fName").value = e ? e.name : "";
  $("#fSalary").value = e ? e.salary : "";
  $("#empFormError").textContent = "";
  $("#empModal").classList.remove("hidden");
  setTimeout(() => $(e ? "#fName" : "#fId").focus(), 30);
}
function closeEmpModal() { $("#empModal").classList.add("hidden"); }
$("#addEmpBtn").onclick = () => openEmpModal(null);
$("#empModalClose").onclick = closeEmpModal;
$("#empCancelBtn").onclick = closeEmpModal;

async function saveEmployee() {
  const emp = { employee_id: $("#fId").value.trim(), name: $("#fName").value.trim(), salary: $("#fSalary").value };
  try {
    if (State.editingId === null) {
      await call("add_employee", emp);
      toast(`Added ${emp.name}`, "ok");
    } else {
      await call("update_employee", State.editingId, emp);
      toast(`Updated ${emp.name}`, "ok");
    }
    closeEmpModal();
    State.payroll = null;
    await loadEmployees();
  } catch (e) {
    $("#empFormError").textContent = e.message;
  }
}
$("#empSaveBtn").onclick = saveEmployee;
["#fId", "#fName", "#fSalary"].forEach((s) => $(s).addEventListener("keydown", (ev) => { if (ev.key === "Enter") saveEmployee(); }));

async function deleteEmployee(id) {
  const e = State.employees.find((x) => String(x.employee_id) === String(id));
  if (!confirm(`Delete employee ${id} — ${e ? e.name : ""}?\nThis removes them from payroll.`)) return;
  try {
    await call("delete_employee", id);
    toast(`Deleted employee ${id}`, "ok");
    State.payroll = null;
    await loadEmployees();
  } catch (err) { toast("Delete failed: " + err.message, "err"); }
}

/* import from Excel / CSV */
$("#importBtn").onclick = () => $("#importFile").click();
$("#importFile").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    toast("Importing " + file.name + "…");
    const b64 = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1]);
      r.onerror = () => reject(new Error("Could not read the file"));
      r.readAsDataURL(file);
    });
    const res = await call("import_employees_file", file.name, b64);
    let msg = `Imported from “${res.sheet}”: ${res.added} added, ${res.updated} updated`;
    if (res.skipped.length) {
      msg += `, ${res.skipped.length} skipped`;
      alert("Some rows were skipped:\n\n" + res.skipped.map((s) => `Row ${s.row}: ${s.reason}`).join("\n"));
    }
    toast(msg, "ok", 5000);
    State.payroll = null;
    await loadEmployees();
  } catch (e) { toast("Import failed: " + e.message, "err", 6000); }
});

/* ===================================================================== */
/* TAB 2: Attendance & Deductions                                        */
/* ===================================================================== */
function initPickers() {
  $("#monthPicker").innerHTML = MONTHS.map((m, i) => `<option value="${i + 1}" ${i + 1 === State.month ? "selected" : ""}>${m}</option>`).join("");
  let y = "";
  for (let yr = State.year - 3; yr <= State.year + 1; yr++) y += `<option value="${yr}" ${yr === State.year ? "selected" : ""}>${yr}</option>`;
  $("#yearPicker").innerHTML = y;
  $("#monthPicker").onchange = $("#yearPicker").onchange = () => {
    State.month = +$("#monthPicker").value; State.year = +$("#yearPicker").value;
    State.cutoff = null;                    // new month -> default cutoff
    loadPayroll();
  };
  $("#cutoffPicker").onchange = () => { State.cutoff = +$("#cutoffPicker").value || null; loadPayroll(); };
}

async function loadPayroll() {
  $("#attTableBody").innerHTML = `<tr><td colspan="11" class="empty">Calculating…</td></tr>`;
  try {
    const d = await call("get_payroll", State.month, State.year, State.cutoff);
    State.payroll = d;
    $("#cutoffPicker").value = d.cutoff_day;
    $("#cutoffPicker").max = d.days_in_month;
    const k = d.kpis;
    $("#kpiStaff").textContent = k.total_staff;
    $("#kpiSalary").textContent = money(k.total_salary);
    $("#kpiDeduct").textContent = money(k.total_deductions);
    $("#kpiNet").textContent = money(k.net_total_payable);
    $("#attMeta").textContent = `${MONTHS[d.month - 1]} ${d.year} · days 1–${d.cutoff_day} of ${d.days_in_month} · click a row for daily punches`;
    renderPayroll();
  } catch (e) {
    $("#attTableBody").innerHTML = `<tr><td colspan="11" class="empty">Error: ${esc(e.message)}</td></tr>`;
  }
}

function renderPayroll() {
  const all = State.payroll.rows;
  const rows = all.filter((r) => {
    if (State.filter === "deducted") return r.total_deduction > 0;
    if (State.filter === "late") return r.late_penalty > 0;
    if (State.filter === "clean") return r.total_deduction === 0;
    return true;
  });
  const body = $("#attTableBody");
  if (!all.length) {
    body.innerHTML = `<tr><td colspan="11" class="empty">No employees yet — add them in the Employees &amp; Salaries tab.</td></tr>`;
    return;
  }
  if (!rows.length) { body.innerHTML = `<tr><td colspan="11" class="empty">No staff match this filter.</td></tr>`; return; }
  body.innerHTML = rows.map((r, i) => `
    <tr class="clickable ${r.present_days === 0 ? "row-zero" : ""}" data-id="${esc(r.employee_id)}">
      <td>${i + 1}</td>
      <td class="strong">${esc(r.employee_id)}</td>
      <td>${esc(r.name)}</td>
      <td class="num">${money(r.salary)}</td>
      <td class="num"><span class="pill-cell ${r.present_days ? "p-green" : "p-red"}">${r.present_days}</span></td>
      <td class="num"><span class="pill-cell ${r.leaves ? "p-red" : "p-gray"}">${r.leaves}</span></td>
      <td class="num"><span class="pill-cell ${r.late_penalty ? "p-red" : r.late_count ? "p-amber" : "p-gray"}">${r.late_count}</span></td>
      <td class="num ${r.leave_deduction ? "neg" : ""}" title="${r.leaves} × ${money(r.leave_rate)}">${money(r.leave_deduction)}</td>
      <td class="num ${r.late_penalty ? "neg" : ""}">${money(r.late_penalty)}</td>
      <td class="num strong">${money(r.total_deduction)}</td>
      <td class="num net-cell">${money(r.net_salary)}</td>
    </tr>`).join("");
  body.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => openDrawer(tr.dataset.id));
}

$$("#statusPills .pill").forEach((p) => p.addEventListener("click", () => {
  $$("#statusPills .pill").forEach((x) => x.classList.remove("active"));
  p.classList.add("active");
  State.filter = p.dataset.filter;
  if (State.payroll) renderPayroll();
}));

$("#exportXlsxBtn").onclick = async () => {
  try {
    toast("Generating salary sheet…");
    const r = await call("export_payout_sheet", State.month, State.year, State.cutoff);
    if (r.cancelled) return;
    if (r.url) {
      const a = document.createElement("a");
      a.href = r.url; a.download = r.name || ""; document.body.appendChild(a); a.click(); a.remove();
      toast("Downloaded " + (r.name || "salary sheet"), "ok");
    } else {
      toast("Saved: " + r.path, "ok", 5000);
    }
  } catch (e) { toast("Export failed: " + e.message, "err"); }
};

/* daily drawer + waivers */
async function openDrawer(id) {
  try {
    const d = await call("get_employee_daily", id, State.month, State.year, State.cutoff);
    $("#drawerName").textContent = `${d.name || ""} (${id})`;
    $("#drawerSub").textContent = `${MONTHS[State.month - 1]} ${State.year} · waive a day to excuse a late or a leave`;
    const body = $("#drawerBody");
    body.innerHTML = d.rows.length ? d.rows.map((r) => `
      <tr>
        <td>${esc(r.date)}</td><td>${esc(r.shift)}</td>
        <td>${esc(r.check_in) || "—"}</td><td>${esc(r.check_out) || "—"}</td>
        <td class="num">${r.late_minutes || 0}</td>
        <td><span class="pill-cell ${r.status === "Late" ? "p-red" : r.status === "Excused" ? "p-amber" : "p-green"}">${esc(r.status)}</span></td>
        <td><label class="toggle"><input type="checkbox" data-date="${esc(r.date)}" ${r.waived ? "checked" : ""}><span class="slider"></span></label></td>
      </tr>`).join("")
      : `<tr><td colspan="7" class="empty">No punches for this employee in this period.</td></tr>`;
    body.querySelectorAll("input[data-date]").forEach((cb) => cb.onchange = async () => {
      try {
        await call("set_waiver", id, cb.dataset.date, cb.checked);
        toast("Waiver saved — recalculating", "ok");
        await loadPayroll();
      } catch (e) { toast("Waiver failed: " + e.message, "err"); }
    });
    $("#drawer").classList.remove("hidden");
    $("#drawerOverlay").classList.remove("hidden");
  } catch (e) { toast("Could not load daily punches: " + e.message, "err"); }
}
function closeDrawer() { $("#drawer").classList.add("hidden"); $("#drawerOverlay").classList.add("hidden"); }
$("#drawerClose").onclick = closeDrawer;
$("#drawerOverlay").onclick = closeDrawer;

/* ===================================================================== */
/* TAB 3: Policy Configuration (SQLite)                                  */
/* ===================================================================== */
const POLICY_FIELDS = {
  morningEntry: ["morning_entry", "time"], eveningEntry: ["evening_entry", "time"],
  graceMinutes: ["grace_minutes", "num"],
  t1Max: ["leave_tier1_max", "num"], t1Rate: ["leave_tier1_rate", "num"],
  t2Max: ["leave_tier2_max", "num"], t2Rate: ["leave_tier2_rate", "num"],
  t3Div: ["leave_tier3_divisor", "num"], weeklyOffs: ["paid_weekly_offs", "num"],
  lateBlock: ["late_block_days", "num"], latePenalty: ["late_block_penalty", "num"],
  dbPath: ["db_path", "text"], dbPassword: ["db_password", "text"],
};

function policyLine(c) {
  return `Policy: leaves ≤${c.leave_tier1_max} → ₹${c.leave_tier1_rate}/day · ≤${c.leave_tier2_max} → ₹${c.leave_tier2_rate}/day · ` +
         `more → salary ÷ ${c.leave_tier3_divisor}/day · every ${c.late_block_days} late days → ₹${c.late_block_penalty} · ` +
         `morning ${c.morning_entry}, evening ${c.evening_entry}`;
}

async function loadConfig() {
  const c = await call("get_config");
  State.config = c;
  for (const [id, [key]] of Object.entries(POLICY_FIELDS)) $("#" + id).value = c[key] ?? "";
  $("#policyLine").textContent = policyLine(c);
}

$("#savePolicyBtn").onclick = async () => {
  const cfg = {};
  for (const [id, [key, kind]] of Object.entries(POLICY_FIELDS)) {
    const v = $("#" + id).value;
    cfg[key] = kind === "num" ? (v === "" ? null : Number(v)) : v.trim();
  }
  if (cfg.leave_tier2_max !== null && cfg.leave_tier1_max !== null && cfg.leave_tier2_max < cfg.leave_tier1_max) {
    toast("The second leave slab must be ≥ the first", "err"); return;
  }
  try {
    State.config = await call("save_config", cfg);
    $("#policyLine").textContent = policyLine(State.config);
    $("#policySaved").textContent = "Saved ✓ " + new Date().toLocaleTimeString();
    State.payroll = null;
    await refreshStatus();
    toast("Policy saved", "ok");
  } catch (e) { toast("Save failed: " + e.message, "err"); }
};

$("#testConnBtn").onclick = async () => {
  const el = $("#connResult");
  el.className = "conn-result show"; el.textContent = "Testing…";
  try {
    const r = await call("test_connection", $("#dbPath").value.trim(), $("#dbPassword").value);
    el.className = "conn-result show " + (r.ok ? "conn-ok" : "conn-err");
    el.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
  } catch (e) { el.className = "conn-result show conn-err"; el.textContent = "✗ " + e.message; }
};

$("#pickMdbBtn").onclick = async () => {
  try {
    const r = await call("pick_mdb");
    if (r.path) $("#dbPath").value = r.path;
    else toast("Type or paste the full .mdb path into the box", "", 4000);
  } catch (e) { toast(e.message, "err"); }
};

/* ---------------- refresh + boot ---------------- */
$("#refreshBtn").onclick = async () => {
  State.payroll = null;
  await refreshStatus();
  await loadEmployees();
  if ($("#tab-attendance").classList.contains("active")) await loadPayroll();
  toast("Refreshed", "ok");
};

async function boot() {
  initPickers();
  await Promise.all([refreshStatus(), loadConfig(), loadEmployees()]);
}
apiReady().then(boot);
