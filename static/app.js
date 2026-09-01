/**
 * Enterprise AI Risk Console — frontend logic.
 *
 * Layout (v1.7):
 *   Left panel  → vendor intake → POST /api/v1/assess-vendor
 *   Right panel → pinned decision bar
 *              → audit trail grid + gate chips
 *              → tabbed report (Findings | Missing evidence | Jira)
 *              → structured assistant card + chat → POST /api/v1/chat
 *
 * UX rules:
 *   - Workspace column is the only right-side scroll container (no nested scrollbars).
 *   - Report rows use progressive disclosure: headline visible, rationale on expand.
 *   - Status chips always pair colour with a text label (Pending, missing, approved).
 *   - assessmentCard() is visual; formatAssessment() is plain text for clipboard + LLM history.
 *
 * Session: conversation history is client-side. Page reload restores THIS browser's
 * assessment via GET /api/v1/assessments/{id} using id + token in sessionStorage.
 * There is no server-side "latest assessment" shared across users.
 */

const messagesEl = document.getElementById("messages");
const assessForm = document.getElementById("assess-form");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const assessBtn = document.getElementById("assess-btn");
const chatBtn = document.getElementById("chat-btn");
const resetBtn = document.getElementById("reset-btn");
const copyBtn = document.getElementById("copy-btn");
const llmStatus = document.getElementById("llm-status");
const summary = document.getElementById("summary");
const report = document.getElementById("report");
const emptyState = document.getElementById("empty-state");
const suggestionsEl = document.getElementById("suggestions");
const workspace = document.getElementById("workspace");
const tabsEl = report.querySelector(".tabs");

/** @type {{ role: string, content: string }[]} */
let history = [];
/** @type {object | null} Current browser-scoped assessment, used by Copy report */
let currentAssessment = null;
/** @type {string | null} Correlates chat and webhooks with this browser's assessment */
let currentAssessmentId = null;
/** @type {string | null} Access token for the current assessment; persisted in sessionStorage across F5 */
let currentAssessmentToken = null;
/** @type {string | null} Optional process-wide API token when API_ACCESS_TOKEN is set */
let apiAccessToken = null;
/** @type {{ approver_domain?: string, api_auth_required?: boolean, frameworks?: object }} */
let consoleConfig = {};

const SESSION_ID_KEY = "ear.assessment_id";
const SESSION_TOKEN_KEY = "ear.assessment_token";
const API_TOKEN_KEY = "ear.api_token";

function persistSession() {
  try {
    if (currentAssessmentId && currentAssessmentToken) {
      sessionStorage.setItem(SESSION_ID_KEY, currentAssessmentId);
      sessionStorage.setItem(SESSION_TOKEN_KEY, currentAssessmentToken);
    }
  } catch {
    /* private mode / disabled storage */
  }
}

function persistApiToken() {
  try {
    if (apiAccessToken) sessionStorage.setItem(API_TOKEN_KEY, apiAccessToken);
    else sessionStorage.removeItem(API_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

function ensureApiToken() {
  if (!consoleConfig.api_auth_required) return true;
  if (apiAccessToken) return true;
  const entered = window.prompt("This console requires an API token (API_ACCESS_TOKEN).");
  if (!entered) return false;
  apiAccessToken = entered.trim();
  persistApiToken();
  return Boolean(apiAccessToken);
}

function restoreSessionFromStorage() {
  try {
    currentAssessmentId = sessionStorage.getItem(SESSION_ID_KEY);
    currentAssessmentToken = sessionStorage.getItem(SESSION_TOKEN_KEY);
    apiAccessToken = sessionStorage.getItem(API_TOKEN_KEY);
  } catch {
    currentAssessmentId = null;
    currentAssessmentToken = null;
    apiAccessToken = null;
  }
}

function clearPersistedSession() {
  currentAssessmentId = null;
  currentAssessmentToken = null;
  try {
    sessionStorage.removeItem(SESSION_ID_KEY);
    sessionStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** Quick-ask chips shown after an assessment is loaded */
const SUGGESTIONS = [
  "Why is the risk high?",
  "What documents are missing to approve?",
  "Is there a DPA?",
  "Is a DPIA required?",
  "Which rules did the engine trigger?",
  "Who has approved in Legal and SecOps?",
  "What Jira tickets (Epic and department Tasks) were created?",
];

/** Human-readable labels for engine triage codes (not final human approval) */
const DECISION_LABELS = {
  APPROVE: "Approve (only after human Jira gates)",
  "APPROVE WITH CONDITIONS": "Approve with conditions (human)",
  "PENDING REVIEW": "Pending departmental review",
  "REQUIRES REMEDIATION": "Requires remediation",
  "ESCALATE TO AI GOVERNANCE / LEGAL / SECURITY": "Escalate to governance / legal / security",
  REJECT: "Reject",
};

/** Department gates rendered as chips in the audit trail */
const GATES = [
  { key: "legal", label: "Legal", approver: "legal_approver" },
  { key: "infosec", label: "SecOps", approver: "secops_approver" },
  { key: "aigov", label: "AI Governance", approver: "aigov_approver" },
];

/** gate_status codes from jira_workflow, phrased for a chip */
const GATE_STATUS_LABELS = {
  required_open: "Pending",
  required_closed: "Closed",
  missing_ticket: "No ticket",
};

initHealth();
initTabs();

assessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = formPayload(new FormData(assessForm));
  if (!ensureApiToken()) {
    addMessage("assistant", "Assessment not sent: an API token is required.", true);
    return;
  }
  assessBtn.disabled = true;
  showChat();
  addMessage("assistant", `Assessing ${payload.vendor_name}…`);
  try {
    const { data: assessment, headers } = await postJson("/api/v1/assess-vendor", payload);
    currentAssessmentToken = headers.get("X-Assessment-Token") || currentAssessmentToken;
    currentAssessmentId = assessment.assessment_metadata?.assessment_id || currentAssessmentId;
    persistSession();
    applyAssessment(assessment);
    seedChatFromAssessment(assessment);
    chatInput.focus();
  } catch (error) {
    addMessage("assistant", `Assessment could not be completed: ${error.message}`, true);
  } finally {
    assessBtn.disabled = false;
  }
});

resetBtn.addEventListener("click", () => {
  assessForm.reset();
  ["vendor_name", "service_description", "intended_use", "data_processed"].forEach((name) => {
    assessForm.elements[name].value = "";
  });
  assessForm.elements.vendor_name.focus();
});

copyBtn.addEventListener("click", async () => {
  if (!currentAssessment) return;
  const text = formatAssessment(currentAssessment);
  const copied = await copyTextToClipboard(text);
  copyBtn.textContent = copied ? "Report copied" : "Could not copy";
  setTimeout(() => {
    copyBtn.textContent = "Copy report";
  }, 2000);
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendChat(chatInput.value.trim());
});

suggestionsEl.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-q]");
  if (btn) sendChat(btn.dataset.q);
});

async function sendChat(message) {
  if (!message || chatInput.disabled) return;
  if (!ensureApiToken()) {
    addMessage("assistant", "Chat not sent: an API token is required.", true);
    return;
  }
  chatInput.value = "";
  addMessage("user", message);
  chatBtn.disabled = true;
  try {
    const { data } = await postJson("/api/v1/chat", {
      message,
      history,
      assessment_id: currentAssessmentId,
    });
    history.push({ role: "user", content: message });
    addMessage("assistant", data.reply);
    history.push({ role: "assistant", content: data.reply });
  } catch (error) {
    addMessage("assistant", `Could not answer: ${error.message}`, true);
  } finally {
    chatBtn.disabled = false;
    chatInput.focus();
  }
}

/** Load health banner and restore session assessment on page load */
async function initHealth() {
  restoreSessionFromStorage();
  try {
    consoleConfig = await getJson("/api/v1/config");
    const health = consoleConfig;
    const enabled = (health.frameworks && health.frameworks.enabled) || [];
    llmStatus.textContent = health.llm_enabled
      ? "Full mode: answers are grounded in this session's report."
      : "Simulator mode: rule-based triage without a language model. Suitable for demos.";
    if (health.jira_outbound) {
      llmStatus.textContent += " Jira outbound is active.";
    } else {
      llmStatus.textContent += " Jira tickets are dry-run until credentials are configured.";
    }
    if (enabled.length) {
      llmStatus.textContent += ` Frameworks: ${enabled.join(", ")}.`;
    }
    if (health.api_auth_required) {
      llmStatus.textContent += " API token required.";
    }
  } catch {
    llmStatus.textContent = "Cannot connect to the console. Check that the service is running.";
    return;
  }
  try {
    if (!currentAssessmentId) return;
    const restored = await getJson(`/api/v1/assessments/${encodeURIComponent(currentAssessmentId)}`);
    if (restored.assessment) {
      applyAssessment(restored.assessment);
      persistSession();
      seedChatFromAssessment(restored.assessment);
    } else {
      clearPersistedSession();
    }
  } catch {
    clearPersistedSession();
  }
}

/** Tabs keep the report readable without three nested scroll areas */
function initTabs() {
  const tabs = Array.from(tabsEl.querySelectorAll('[role="tab"]'));
  tabsEl.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) selectTab(tab);
  });
  tabsEl.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const current = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    const next = tabs[(current + step + tabs.length) % tabs.length];
    selectTab(next);
    next.focus();
  });

  function selectTab(active) {
    tabs.forEach((tab) => {
      const selected = tab === active;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
    });
  }
}

function applyAssessment(assessment) {
  currentAssessment = assessment;
  currentAssessmentId = assessment.assessment_metadata?.assessment_id || null;
  renderSummary(assessment);
  renderReport(assessment);
  enableChat();
  renderSuggestions();
}

/**
 * Seed the visible chat from the restored report.
 * The card is a visual rendering; `history` keeps the plain-text version that
 * the model receives. Authorization for later questions is still
 * assessment_id + X-Assessment-Token on POST /api/v1/chat, not this intro.
 */
function seedChatFromAssessment(assessment) {
  history = [{ role: "assistant", content: formatAssessment(assessment) }];
  messagesEl.replaceChildren();
  showChat();
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant card";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = "Risk assistant";
  bubble.append(who, assessmentCard(assessment));
  messagesEl.append(bubble);
  workspace.scrollTop = 0;
}

/** Structured summary card — replaces the plain-text log in the assistant panel */
function assessmentCard(assessment) {
  const meta = assessment.assessment_metadata || {};
  const record = assessment.decision_record || {};
  const privacy = assessment.privacy_triage;
  const wrap = document.createElement("div");

  const title = document.createElement("p");
  title.className = "card-title";
  title.textContent = `${meta.vendor || "Vendor"} — assessed ${meta.assessment_date || ""}`.trim();
  wrap.append(title);

  const stats = [
    ["Decision", DECISION_LABELS[meta.decision] || meta.decision || "—", decisionClass(meta.decision)],
    ["Residual risk", riskLabel(meta.overall_residual_risk), riskClass(meta.overall_residual_risk)],
    ["Engine score", record.risk_score ? `${record.risk_score} / 5` : "—", ""],
    ["Workflow", humanize(record.workflow_status), ""],
    ["Engine", record.model_version || "—", ""],
    ["DPIA", privacy ? `${privacy.privacy_assessment_required ? "Indicated" : "Not indicated"} · ${privacy.dpia_status || "unknown"}` : "—", ""],
  ];
  wrap.append(statGrid(stats, "card-grid"));

  const chips = document.createElement("div");
  chips.className = "card-chips";
  chips.append(
    countChip("Findings", (assessment.critical_findings || []).length),
    countChip("Missing evidence", evidenceGapRows(assessment).length),
    countChip("Jira tickets", (assessment.jira_tickets || []).length)
  );
  GATES.forEach(({ label, approver }) => chips.append(gateChip(label, record[approver])));
  wrap.append(chips);

  const note = document.createElement("p");
  note.className = "card-note";
  note.textContent =
    "Engine triage only — the deterministic rules set score and decision; the assistant explains them. " +
    "Closing department gates in Jira is not a business approval. This is not legal advice.";
  wrap.append(note);
  return wrap;
}

function statGrid(entries, className) {
  const grid = document.createElement("dl");
  grid.className = className;
  entries.forEach(([label, value, valueClass]) => {
    const cell = document.createElement("div");
    cell.className = "audit-item";
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value || "—";
    if (valueClass) dd.className = valueClass;
    cell.append(dt, dd);
    grid.append(cell);
  });
  return grid;
}

function chip(text, variant) {
  const el = document.createElement("span");
  el.className = `chip ${variant}`;
  el.textContent = text;
  return el;
}

function countChip(label, count) {
  return chip(`${label}: ${count}`, count ? "chip-accent" : "chip-plain");
}

function gateChip(label, approver) {
  return approver
    ? chip(`${label}: approved`, "chip-ok")
    : chip(`${label}: pending`, "chip-pending");
}

function enableChat() {
  chatInput.disabled = false;
  chatBtn.disabled = false;
  chatInput.placeholder = "Ask about this assessment, e.g. Why is the risk high?";
}

function showChat() {
  emptyState.hidden = true;
  messagesEl.hidden = false;
}

function renderSuggestions() {
  suggestionsEl.hidden = false;
  suggestionsEl.replaceChildren();
  SUGGESTIONS.forEach((q) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.q = q;
    btn.textContent = q;
    suggestionsEl.append(btn);
  });
}

/** Map HTML form fields to VendorInput JSON for the API */
function formPayload(formData) {
  const value = (name) => (formData.get(name) || "").toString().trim();
  const triState = (name) => {
    const raw = value(name);
    if (raw === "true") return true;
    if (raw === "false") return false;
    return null;
  };
  return {
    vendor_name: value("vendor_name"),
    service_description: value("service_description"),
    intended_use: value("intended_use"),
    data_processed: value("data_processed"),
    has_dpa: formData.get("has_dpa") === "on",
    business_owner: value("business_owner") || null,
    geographic_scope: value("geographic_scope") || null,
    model_provider: value("model_provider") || null,
    integration_architecture: value("integration_architecture") || null,
    regulatory_context: value("regulatory_context") || null,
    privacy_assessment_required: triState("privacy_assessment_required"),
    dpia_status: value("dpia_status") || null,
    dpia_reference: value("dpia_reference") || null,
    processing_purposes: value("processing_purposes") || null,
    data_subjects: value("data_subjects") || null,
    special_category_data: formData.get("special_category_data") === "on",
    international_transfers: value("international_transfers") || null,
    retention_period: value("retention_period") || null,
    automated_decision_making: formData.get("automated_decision_making") === "on",
    privacy_risk_level: value("privacy_risk_level") || null,
  };
}

/** Plain-text report for chat seed and clipboard copy */
function formatAssessment(assessment) {
  const meta = assessment.assessment_metadata;
  const decisionLabel = DECISION_LABELS[meta.decision] || meta.decision;
  const findings = (assessment.critical_findings || []).map((item) => `• ${item}`).join("\n");
  const gaps = (assessment.evidence_gaps || []).map((item) => `• ${item}`).join("\n");
  const tickets = (assessment.jira_tickets || [])
    .map((ticket) => {
      const dept = ticket.fields.department || ticket.fields.issuetype?.name || "";
      return `• [${dept}] ${ticket.fields.summary} [${ticket.fields.priority.name}]`;
    })
    .join("\n");
  const record = assessment.decision_record;
  const privacy = assessment.privacy_triage;
  const engine = record
    ? `Engine ${record.model_version} · triage ${record.decision} · workflow ${record.workflow_status} · score ${record.risk_score}/5 · LLM does not decide`
    : "";
  const approvers = record
    ? `Approvers @${consoleConfig.approver_domain || "configured-domain"} — Legal: ${record.legal_approver || "pending"} · SecOps: ${record.secops_approver || "pending"} · AI Gov: ${record.aigov_approver || "pending"}`
    : "";
  const dpia = privacy
    ? `DPIA required: ${privacy.privacy_assessment_required} · status: ${privacy.dpia_status}`
    : "";
  return [
    `Assessment of ${meta.vendor} (${meta.assessment_date}).`,
    `Decision (triage): ${decisionLabel} (${meta.decision})`,
    `Residual risk: ${meta.overall_residual_risk}`,
    engine,
    approvers,
    dpia,
    "",
    "Critical findings:",
    findings || "• None recorded",
    "",
    "Missing evidence:",
    gaps || "• None recorded",
    "",
    "Proposed Jira work:",
    tickets || "• None",
    "",
    "Jira orchestration: parent Epic + Legal / SecOps / AI Governance departmental Tasks. Closing those gates is not a business approval.",
    "This text is not legal advice.",
  ].join("\n");
}

function renderSummary(assessment) {
  const meta = assessment.assessment_metadata || {};
  const record = assessment.decision_record || {};
  summary.hidden = false;
  const decisionEl = document.getElementById("decision");
  const gatesComplete = record.workflow_status === "DEPARTMENT_GATES_COMPLETED";
  const humanApproved = record.workflow_status === "HUMAN_APPROVED_WITH_CONDITIONS";
  let shown;
  let bannerClass;
  if (humanApproved) {
    shown = "Approved with conditions";
    bannerClass = "decision-conditions";
  } else if (gatesComplete) {
    shown = "Department gates completed — awaiting final approval";
    bannerClass = "decision-conditions";
  } else {
    shown = DECISION_LABELS[meta.decision] || meta.decision || "—";
    bannerClass = decisionClass(meta.decision);
  }
  decisionEl.textContent = shown;
  decisionEl.className = "decision " + bannerClass;
  const codeBits = [];
  if (meta.decision) codeBits.push(`Triage: ${meta.decision}`);
  if (record.workflow_status) codeBits.push(`Workflow: ${record.workflow_status}`);
  if (record.human_decision) codeBits.push(`Human: ${record.human_decision}`);
  document.getElementById("decision-code").textContent = codeBits.join(" · ");
  const residual = document.getElementById("residual");
  residual.textContent = riskLabel(meta.overall_residual_risk);
  residual.className = "residual " + riskClass(meta.overall_residual_risk);
  document.getElementById("vendor-chip").textContent = meta.vendor || "—";
}

function renderReport(assessment) {
  report.hidden = false;
  renderAudit(assessment.decision_record);
  const findings = assessment.critical_findings || [];
  const gaps = evidenceGapRows(assessment);
  const tickets = assessment.jira_tickets || [];
  renderFindings(findings);
  renderGaps(gaps);
  renderTickets(tickets);
  document.getElementById("count-findings").textContent = findings.length;
  document.getElementById("count-gaps").textContent = gaps.length;
  document.getElementById("count-tickets").textContent = tickets.length;
}

/** Audit trail as labelled data + gate chips instead of a single log line */
function renderAudit(record) {
  const audit = document.getElementById("audit");
  if (!record) {
    audit.hidden = true;
    return;
  }
  audit.hidden = false;
  const grid = statGrid(
    [
      ["Triage", record.decision || "—", decisionClass(record.decision)],
      ["Workflow", humanize(record.workflow_status), ""],
      ["Engine score", record.risk_score ? `${record.risk_score} / 5` : "—", ""],
      ["Residual risk", riskLabel(record.residual_risk), riskClass(record.residual_risk)],
      ["Engine version", record.model_version || "—", ""],
      ["Scoring", record.llm_used_for_decision ? "LLM (unexpected)" : "Deterministic rules", ""],
    ],
    "audit-grid"
  );
  grid.id = "audit-grid";
  document.getElementById("audit-grid").replaceWith(grid);

  const gatesRow = document.getElementById("gates-row");
  gatesRow.replaceChildren();
  GATES.forEach(({ key, label, approver }) => {
    const item = document.createElement("div");
    item.className = "gate";
    const name = document.createElement("span");
    name.className = "gate-name";
    name.textContent = label;
    const status = record.gate_status?.[key];
    const approved = Boolean(record[approver]);
    const pendingLabel = GATE_STATUS_LABELS[status] || "Pending";
    item.append(
      name,
      approved
        ? chip(record[approver], "chip-ok")
        : chip(pendingLabel, status === "missing_ticket" ? "chip-danger" : "chip-pending")
    );
    gatesRow.append(item);
  });
}

/** Findings: bold headline visible, long rationale behind a disclosure row */
function renderFindings(findings) {
  const list = document.getElementById("findings-list");
  list.replaceChildren();
  if (!findings.length) {
    list.append(emptyRow("No critical findings recorded."));
    return;
  }
  findings.forEach((text) => {
    const { headline, detail } = splitHeadline(text);
    list.append(disclosureRow({ title: headline, detail }));
  });
}

/** Missing evidence: control id + status chip visible, source excerpt on expand */
function renderGaps(rows) {
  const list = document.getElementById("gaps-list");
  list.replaceChildren();
  if (!rows.length) {
    list.append(emptyRow("No evidence gaps marked."));
    return;
  }
  rows.forEach((row) => {
    list.append(
      disclosureRow({
        title: row.title,
        chips: row.status ? [chip(row.status, statusVariant(row.status))] : [],
        detail: row.detail,
        meta: row.meta,
      })
    );
  });
}

/** Jira tickets: summary + department/priority chips, description on expand */
function renderTickets(tickets) {
  const list = document.getElementById("tickets-list");
  list.replaceChildren();
  if (!tickets.length) {
    list.append(emptyRow("No tickets proposed."));
    return;
  }
  tickets.forEach((ticket) => {
    const fields = ticket.fields || {};
    const dept = fields.department || fields.issuetype?.name || "";
    const priority = fields.priority?.name || "";
    const chips = [];
    if (dept) chips.push(chip(dept, "chip-plain"));
    if (priority) chips.push(chip(priority, priorityVariant(priority)));
    list.append(
      disclosureRow({
        title: fields.summary || "Untitled ticket",
        chips,
        detail: fields.description,
        meta: fields.issue_key ? `Issue ${fields.issue_key}` : "",
      })
    );
  });
}

/** Normalize evidence gaps into rows with control id, status, and source */
function evidenceGapRows(assessment) {
  const items = (assessment.evidence_items || []).filter(
    (item) => item.evidence_status === "missing" || item.evidence_status === "insufficient"
  );
  if (items.length) {
    return items.map((item) => ({
      title: item.control_id,
      status: item.evidence_status,
      detail: item.excerpt,
      meta: [item.document && `Document: ${item.document}`, item.source && `Source: ${item.source}`]
        .filter(Boolean)
        .join(" · "),
    }));
  }
  return (assessment.evidence_gaps || []).map((text) => {
    const { headline, detail } = splitHeadline(text);
    return { title: headline, detail };
  });
}

/**
 * Build a row that shows only the headline until the user expands it.
 * Rows without a detail render as a plain line (no useless toggle).
 */
function disclosureRow({ title, chips = [], detail, meta }) {
  const li = document.createElement("li");
  const hasDetail = Boolean((detail || "").trim() || (meta || "").trim());

  const label = document.createElement("span");
  label.className = "row-title";
  label.textContent = title;

  if (!hasDetail) {
    const row = document.createElement("div");
    row.className = "row-static";
    row.append(label, ...chips);
    li.append(row);
    return li;
  }

  const icon = document.createElement("span");
  icon.className = "row-icon";
  icon.textContent = "i";
  icon.setAttribute("aria-hidden", "true");

  const head = document.createElement("summary");
  head.title = "Show the full rationale";
  head.append(label, ...chips, icon);

  const body = document.createElement("p");
  body.className = "row-body";
  body.textContent = (detail || "").trim();
  if (meta) {
    const metaEl = document.createElement("span");
    metaEl.className = "row-meta";
    metaEl.textContent = meta;
    body.append(metaEl);
  }

  const details = document.createElement("details");
  details.className = "disclosure";
  details.append(head, body);
  li.append(details);
  return li;
}

function emptyRow(text) {
  const li = document.createElement("li");
  const div = document.createElement("div");
  div.className = "row-static";
  const span = document.createElement("span");
  span.className = "row-title";
  span.textContent = text;
  div.append(span);
  li.append(div);
  return li;
}

/**
 * Split a long finding into a bold headline and the rationale behind it.
 * Prefers a sentence break, then a colon, so "No executed DPA. GDPR is not…"
 * shows as "No executed DPA" with the rest hidden.
 */
function splitHeadline(text) {
  const value = String(text || "").trim();
  if (value.length <= 90) return { headline: value, detail: "" };
  const candidates = [". ", ": ", "; "]
    .map((token) => ({ token, index: value.indexOf(token) }))
    .filter(({ index }) => index >= 12 && index <= 120)
    .sort((a, b) => a.index - b.index);
  if (!candidates.length) return { headline: value.slice(0, 88).trim() + "…", detail: value };
  const { token, index } = candidates[0];
  return {
    headline: value.slice(0, index).trim(),
    detail: value.slice(index + token.length).trim(),
  };
}

function statusVariant(status) {
  return status === "missing" ? "chip-danger" : "chip-pending";
}

function priorityVariant(priority) {
  const text = (priority || "").toLowerCase();
  if (text.includes("highest") || text.includes("high")) return "chip-danger";
  if (text.includes("medium")) return "chip-pending";
  return "chip-plain";
}

/** Turn DEPARTMENT_GATES_COMPLETED into "Department gates completed" */
function humanize(value) {
  if (!value) return "—";
  const text = String(value).replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** CSS class for decision banner coloring */
function decisionClass(value) {
  const text = (value || "").toUpperCase();
  if (text.includes("REJECT")) return "decision-reject";
  if (text.includes("ESCALATE")) return "decision-escalate";
  if (text.includes("REMEDIATION")) return "decision-remediation";
  if (text.includes("PENDING")) return "decision-conditions";
  if (text.includes("CONDITIONS")) return "decision-conditions";
  if (text.includes("APPROVE")) return "decision-approve";
  return "";
}

function riskClass(value) {
  const text = (value || "").toLowerCase();
  if (text.includes("critical")) return "risk-critical";
  if (text.includes("high")) return "risk-high";
  if (text.includes("moderate")) return "risk-moderate";
  if (text.includes("low") || text.includes("very")) return "risk-low";
  return "";
}

function riskLabel(value) {
  const map = {
    "Very Low": "Very Low",
    Low: "Low",
    Moderate: "Moderate",
    High: "High",
    Critical: "Critical",
  };
  return map[value] || value || "—";
}

function addMessage(role, content, isError = false) {
  showChat();
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}${isError ? " error" : ""}`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role === "user" ? "You" : "Risk assistant";
  const body = document.createElement("div");
  body.innerHTML = renderLite(content);
  bubble.append(who, body);
  messagesEl.append(bubble);
  workspace.scrollTop = workspace.scrollHeight;
}

/** Minimal markdown: bold and line breaks only */
function renderLite(text) {
  return escapeHtml(text)
    .replaceAll("**", "")
    .replaceAll("\n", "<br>");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * Clipboard API only works in a secure context (HTTPS or localhost).
 * Fall back to execCommand when opened over plain HTTP or permission denied.
 */
async function copyTextToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through */
    }
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.left = "-9999px";
  document.body.appendChild(field);
  field.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(field);
  return ok;
}

async function postJson(url, body) {
  const headers = { "Content-Type": "application/json" };
  if (apiAccessToken) headers["X-API-Token"] = apiAccessToken;
  if (currentAssessmentToken) headers["X-Assessment-Token"] = currentAssessmentToken;
  if (currentAssessmentId) headers["X-Assessment-Id"] = currentAssessmentId;
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && consoleConfig.api_auth_required) {
      apiAccessToken = null;
      persistApiToken();
    }
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail) || response.statusText);
  }
  return { data, headers: response.headers };
}

async function getJson(url) {
  const headers = {};
  if (apiAccessToken) headers["X-API-Token"] = apiAccessToken;
  if (currentAssessmentToken) headers["X-Assessment-Token"] = currentAssessmentToken;
  if (currentAssessmentId) headers["X-Assessment-Id"] = currentAssessmentId;
  const response = await fetch(url, { headers });
  if (!response.ok) throw new Error(response.statusText);
  return response.json();
}
