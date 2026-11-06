import { autoWalk, VIEWS } from "./autowalk.js";
import { TabCapture, tourEmbedUrl } from "./capture.js";

const POLL_MS = 4000;
const KEY_STORAGE = "claimscope.matterportSdkKey";
// Share links, Discover pages, model pages, or a bare model ID. The server parses the ID.
const MATTERPORT_LINK = /^(?:https:\/\/my\.matterport\.com\/show\/?\?(?:.*&)?m=[A-Za-z0-9]{11}(?:&.*)?|https:\/\/(?:discover|my)\.matterport\.com\/(?:space|models)\/[A-Za-z0-9]{11}(?:[/?#].*)?|[A-Za-z0-9]{11})$/;
const TARGET_CAPTURE_PX = 1280; // captured view width; the dock is sized so the tab image has this many pixels
const FLAG_LABELS = {
  from_brief: "From brief",
  inspection_needed: "Inspect on site",
  hidden_damage_possible: "Hidden damage possible",
  manual_quantity: "Quantity estimated",
  low_confidence: "Low confidence",
};

const $ = (sel) => document.querySelector(sel);
const gbp = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" });
const gbp0 = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", maximumFractionDigits: 0 });
const num = (v) => Number(v).toFixed(2);
const qty = (v) => (Number(v) % 1 === 0 ? String(Number(v)) : Number(v).toFixed(2));

let assessment = null;
let schedule = null;
let pollTimer = null;
let sdkKey = "";
let selectedRoom = null;
let walking = false;
let walkAbort = null;
let planSkipped = false;
let mountedModel = null;
const capture = new TabCapture($("#tour"));

// --- helpers -----------------------------------------------------------------

async function api(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
  return node;
}

function stage(message, isError = false) {
  const p = $("#stage-msg");
  p.textContent = message || "";
  p.classList.toggle("error", isError);
}

function storageGet(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}

async function loadConfig() {
  try {
    const cfg = await fetch("/config.json", { cache: "no-store" }).then((r) => r.json());
    sdkKey = cfg.matterportSdkKey || "";
  } catch { /* fall through to a stored key */ }
  sdkKey ||= storageGet(KEY_STORAGE);
}

async function uploadEvidence(kind, roomId, blob, captureMeta) {
  const contentType = blob.type || "image/jpeg";
  const res = await api("POST", `/assessments/${assessment.assessment_id}/evidence/presign`, {
    kind, room_id: roomId, content_type: contentType, capture: captureMeta,
  });
  const put = await fetch(res.upload_url, { method: "PUT", headers: res.headers, body: blob });
  if (!put.ok) throw new Error(`Upload failed (${put.status})`);
  return res.evidence_id;
}

function viewStats() {
  const views = assessment.evidence.filter((e) => e.kind === "scan_view" || e.kind === "photo");
  return {
    total: views.length,
    done: views.filter((e) => e.analysis === "done").length,
    failed: views.filter((e) => e.analysis === "failed").length,
  };
}

// --- loading -------------------------------------------------------------------

async function load(id) {
  assessment = await api("GET", `/assessments/${id}`);
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
  render();
  const s = viewStats();
  const analysing = s.total > s.done + s.failed;
  if (["queued", "processing"].includes(assessment.status) || assessment.plan_status === "reading" || analysing) poll();
  if (assessment.status === "draft_ready" && !schedule) await loadSchedule();
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      await load(assessment.assessment_id);
    } catch (err) {
      stage(err.message, true);
      poll();
    }
  }, POLL_MS);
}

// --- rendering: header, flow, tour -----------------------------------------------

function render() {
  const a = assessment;
  $("#step-create").hidden = true;
  $("#step-assessment").hidden = false;
  $("#new-assessment").hidden = false;
  $("#asmt-title").textContent = a.claim_ref || `Assessment ${a.assessment_id}`;
  $("#asmt-summary").textContent = [a.peril.replaceAll("_", " "), a.matterport_model ? `scan ${a.matterport_model}` : null,
    a.claim_ref ? a.assessment_id : null].filter(Boolean).join(" · ");
  const statusText = { created: "Not drafted", queued: "Drafting", processing: "Drafting", draft_ready: "Draft ready", failed: "Needs attention" };
  $("#asmt-status").textContent = statusText[a.status] || a.status;
  $("#asmt-status").className = `pill ${a.status}`;

  $("#form-link").hidden = Boolean(a.matterport_model);
  $("#flow").hidden = !a.matterport_model;
  mountTour();
  renderVideo();
  renderBrief();
  renderFlow();
  renderProgress();
  renderRooms();
  renderChips();
  $("#sdk-row").hidden = !sdkKey || !a.matterport_model;
}

function renderBrief() {
  const brief = assessment.incident || {};
  const filled = Object.values(brief).some(Boolean);
  $("#brief-status").textContent = filled ? "Added" : "Recommended";
  $("#brief-status").className = `pill ${filled ? "draft_ready" : "queued"}`;
  const form = $("#form-brief");
  if (form.contains(document.activeElement)) return; // don't overwrite typing
  for (const name of ["date_of_incident", "date_noticed", "detail", "cause", "further_info"]) {
    form.elements[name].value = brief[name] || "";
  }
}

function mountTour() {
  const model = assessment.matterport_model;
  $("#tour-wrap").hidden = !model;
  if (!model || mountedModel === model) return;
  $("#tour").replaceChildren(el("iframe", {
    src: tourEmbedUrl(model), allow: "fullscreen; xr-spatial-tracking", allowfullscreen: true, title: "Matterport tour",
  }));
  mountedModel = model;
}

function setStep(active, done = []) {
  for (const li of $("#steps").children) {
    li.classList.toggle("active", li.dataset.step === active);
    li.classList.toggle("done", done.includes(li.dataset.step));
  }
}

function setNext(title, hint, primary, secondary) {
  $("#next-title").textContent = title;
  $("#next-hint").textContent = hint;
  for (const [btn, spec] of [[$("#btn-primary"), primary], [$("#btn-secondary"), secondary]]) {
    btn.hidden = !spec;
    if (!spec) continue;
    btn.textContent = spec.label;
    btn.disabled = Boolean(spec.disabled);
    btn.onclick = spec.action ? withBusy(btn, spec.action) : null;
  }
}

// --- walkthrough video --------------------------------------------------------------

const MAX_VIDEO_MB = 1024;
let videoUploading = null; // { loaded, total }
const autoDrafted = new Set();

function putWithProgress(url, headers, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    for (const [k, v] of Object.entries(headers || {})) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded, e.total);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(`Upload failed (${xhr.status})`)));
    xhr.onerror = () => reject(new Error("Upload failed: network error"));
    xhr.send(file);
  });
}

async function uploadVideo(file) {
  if (file.size > MAX_VIDEO_MB * 1024 * 1024) throw new Error(`Videos up to ${MAX_VIDEO_MB} MB, please. Trim it or record at a lower resolution.`);
  const contentType = file.type || "video/mp4";
  const res = await api("POST", `/assessments/${assessment.assessment_id}/evidence/presign`, {
    kind: "video", content_type: contentType,
  });
  videoUploading = { loaded: 0, total: file.size };
  renderVideo();
  try {
    await putWithProgress(res.upload_url, res.headers, file, (loaded, total) => {
      videoUploading = { loaded, total };
      renderVideo();
    });
  } finally {
    videoUploading = null;
  }
  await load(assessment.assessment_id);
}

function videoFrameStats() {
  const frames = assessment.evidence.filter((e) => (e.capture?.sweep_id || "").startsWith("video:"));
  return { total: frames.length, done: frames.filter((e) => e.analysis === "done" || e.analysis === "failed").length };
}

function renderVideo() {
  const a = assessment;
  $("#video-card").hidden = false; // a video can be added to any assessment, with or without a tour
  const bar = $("#video-progress");
  const text = $("#video-text");
  const frames = videoFrameStats();
  const running = ["queued", "processing"].includes(a.status);
  $("#btn-video-draft").hidden = true;
  bar.hidden = true;

  if (videoUploading) {
    bar.hidden = false;
    bar.max = videoUploading.total;
    bar.value = videoUploading.loaded;
    text.textContent = `Uploading… ${Math.round((100 * videoUploading.loaded) / videoUploading.total)}%`;
  } else if (a.video_status === "uploading" || a.video_status === "processing") {
    bar.hidden = false;
    bar.removeAttribute("value");
    text.textContent = `${a.video_message || "Processing"}… A 5-minute video takes about 3 to 5 minutes. You can leave this tab.`;
  } else if (a.video_status === "failed") {
    text.textContent = `The video couldn't be processed: ${a.video_message || "unknown error"}. Try uploading it again.`;
  } else if (a.video_status === "ready") {
    const pending = frames.total - frames.done;
    text.textContent = `${a.video_message}.` + (pending > 0 ? ` Analysing frames: ${frames.done} of ${frames.total}.` : "")
      + (a.incident_source === "video commentary" ? " The incident brief was filled in from your commentary; check it below." : "");
    $("#btn-video-draft").hidden = running || a.status === "draft_ready";
    // Draft once automatically when a video finishes, like after a tour walk.
    const key = a.evidence.find((e) => e.kind === "video")?.evidence_id;
    if (key && !autoDrafted.has(key) && a.status === "created" && !running) {
      autoDrafted.add(key);
      analyse().catch((err) => stage(err.message, true));
    }
  }
  if (a.video_status === "processing" || a.video_status === "uploading" || (a.video_status === "ready" && frames.done < frames.total)) poll();
}

// --- drafting progress --------------------------------------------------------------

let dockDismissed = false;
let draftStartedAt = null;
let lastStatus = null;

function draftSteps() {
  const a = assessment;
  const s = viewStats();
  const p = a.progress || "";
  const roomsMatch = /Drafting rooms: (\d+) of (\d+)/.exec(p);
  const phase = a.status === "draft_ready" ? 4 : /Pricing/.test(p) ? 3 : roomsMatch ? 2 : /Matching/.test(p) ? 1.5 : 1;
  const viewsDone = s.done + s.failed;
  return [
    { label: "Views analysed", detail: s.total ? `${viewsDone} of ${s.total}` : "", state: phase > 1 || (s.total && viewsDone >= s.total) ? "done" : "active" },
    { label: "Rooms drafted", detail: roomsMatch ? `${roomsMatch[1]} of ${roomsMatch[2]}` : "", state: phase > 2 ? "done" : phase >= 1.5 ? "active" : "todo" },
    { label: "Pricing and checks", detail: "", state: phase > 3 ? "done" : phase === 3 ? "active" : "todo" },
  ];
}

function renderProgress() {
  const a = assessment;
  const running = ["queued", "processing"].includes(a.status);
  if (running && !draftStartedAt) draftStartedAt = Date.now();
  if (!running && a.status !== "draft_ready") draftStartedAt = null;

  // placeholder where the schedule will appear
  $("#drafting-card").hidden = !running;
  if (running) $("#drafting-text").textContent = `${a.progress && a.progress !== "Queued" ? a.progress + ". " : ""}This usually takes 1 to 3 minutes. The schedule appears here when it's ready.`;

  // browser tab title, visible from other tabs
  const base = a.claim_ref || "ClaimScope";
  document.title = running ? `Drafting… · ${base}` : a.status === "draft_ready" && lastStatus && lastStatus !== "draft_ready" ? `✓ Draft ready · ${base}` : base;

  // docked tracker after a walk (or any draft) until dismissed
  const showDock = !walking && !dockDismissed && (running || (a.status === "draft_ready" && lastStatus && lastStatus !== "draft_ready") || $("#dock").dataset.mode === "draft");
  if (!walking) {
    $("#dock").hidden = !showDock;
    $("#dock").dataset.mode = showDock ? "draft" : "";
    if (showDock) {
      $(".dock-walk").hidden = true;
      $(".dock-draft").hidden = false;
      $("#btn-stop-walk").hidden = true;
      $("#btn-dock-close").hidden = false;
      const ready = a.status === "draft_ready";
      const failed = a.status === "failed";
      $("#dock-title").textContent = ready ? "Draft ready" : failed ? "Drafting stopped" : "Drafting your schedule";
      $("#draft-steps").replaceChildren(...draftSteps().map((step) =>
        el("li", { class: ready ? "done" : step.state }, el("span", { class: "dot" }), step.label,
          step.detail ? el("span", { class: "muted small" }, ` · ${step.detail}`) : null)));
      const secs = draftStartedAt ? Math.round((Date.now() - draftStartedAt) / 1000) : 0;
      $("#draft-eta").textContent = ready ? "Your schedule and PDF report are ready."
        : failed ? (a.error || "Something went wrong.")
        : `Running for ${secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`} · usually 1 to 3 minutes. You can leave this tab.`;
      $("#btn-view-schedule").hidden = !ready;
    }
  }
  lastStatus = a.status;
}

function renderFlow() {
  const a = assessment;
  if (!a.matterport_model) return;
  const s = viewStats();
  const running = ["queued", "processing"].includes(a.status);
  const planDone = a.plan_status === "done" || a.rooms.length > 0 || planSkipped;
  const analysis = s.total ? `${s.done} of ${s.total} views analysed${s.failed ? ` · ${s.failed} failed` : ""}` : "";

  if (walking) {
    setStep("walk", ["share", "plan"]);
    setNext("Walking the tour", "Views are analysed as they come in. You can scroll and read while it runs.",
      { label: "Stop walk", action: async () => walkAbort?.abort() });
  } else if (running) {
    setStep("draft", ["share", "plan", "walk"]);
    setNext("Drafting the schedule", a.progress || "Working…", null);
  } else if (a.status === "draft_ready") {
    setStep(null, ["share", "plan", "walk", "draft"]);
    setNext("Draft ready", `${analysis}. Re-drafting reuses the analysed views.`,
      { label: "Re-draft", action: analyse },
      { label: "Walk again", action: startWalk });
  } else if (s.total > 0) {
    setStep("draft", ["share", "plan", "walk"]);
    const briefed = Object.values(a.incident || {}).some(Boolean);
    setNext("Draft the schedule",
      briefed ? (analysis || "Views captured.") : `${analysis}. Add the incident brief first for a complete schedule.`,
      { label: "Draft schedule", action: analyse },
      { label: "Walk again", action: startWalk });
  } else if (!capture.active) {
    setStep("share", []);
    setNext("Allow this tab to be captured",
      "Your browser asks which tab to share: choose this one. Only the tour area is used.",
      { label: "Allow capture", action: startCapture });
  } else if (a.plan_status === "reading") {
    setStep("plan", ["share"]);
    setNext("Reading the floor plan…", a.plan_message || "", null, { label: "Skip", action: async () => { planSkipped = true; } });
  } else if (!planDone) {
    setStep("plan", ["share"]);
    setNext("Read rooms from the floor plan",
      "Optional, but gives real room names and sizes. In the tour, press the floor plan button, wait a moment, then press Read.",
      { label: "Read floor plan", action: readFloorPlan },
      { label: "Skip", action: async () => { planSkipped = true; } });
  } else {
    setStep("walk", ["share", "plan"]);
    setNext("Walk the tour",
      `Visits every scan point and keeps ${VIEWS.length} views at each, about 5 seconds per view. A small panel shows progress.`,
      { label: "Start walk", action: startWalk });
  }
  if (a.status === "failed" && !walking && !running) stage(a.error || "Something went wrong.", true);
  // The tour preview is only needed for allowing capture and reading the floor plan.
  $("#tour-wrap").hidden = !a.matterport_model || (s.total > 0 && !walking);
}

// --- rendering: rooms --------------------------------------------------------------

function viewCounts() {
  const counts = {};
  for (const e of assessment.evidence) if (e.kind !== "floor_plan") counts[e.room_id] = (counts[e.room_id] || 0) + 1;
  return counts;
}

function renderRooms() {
  const rooms = assessment.rooms;
  $("#rooms-summary").textContent = rooms.length ? `Rooms (${rooms.length})` : "Rooms";
  const tbody = $("#rooms tbody");
  if (tbody.contains(document.activeElement)) return; // don't wipe an edit in progress
  const counts = viewCounts();
  tbody.replaceChildren(...rooms.map((r) => {
    const input = (name, value, attrs = {}) => el("input", { name, value: String(value), ...attrs });
    const d = r.dimensions;
    const source = r.dimensions_source || d.source;
    const row = el("tr", { title: r.plan_notes || "" },
      el("td", {}, input("name", r.name, { maxlength: "80" }),
        source && source !== "assessor" ? el("span", { class: "tag" }, source) : null),
      el("td", {}, input("length_m", num(d.length_m), { type: "number", step: "0.01", min: "0.1" })),
      el("td", {}, input("breadth_m", num(d.breadth_m), { type: "number", step: "0.01", min: "0.1" })),
      el("td", {}, input("height_m", num(d.height_m), { type: "number", step: "0.01", min: "0.1" })),
      el("td", { class: "num" }, num(r.derived.floor_area)),
      el("td", { class: "num" }, String(counts[r.room_id] || 0)),
      el("td", { class: "row-actions" },
        el("button", { class: "secondary small-btn", onclick: () => saveRoom(r, row) }, "Save"),
        el("button", { class: "secondary small-btn danger", title: "Remove room", onclick: () => deleteRoom(r) }, "×")),
    );
    return row;
  }));
  $("#form-upload [name=room_id]").replaceChildren(...rooms.map((r) => el("option", { value: r.room_id }, r.name)));
}

function renderChips() {
  const counts = viewCounts();
  if (selectedRoom && !assessment.rooms.some((r) => r.room_id === selectedRoom)) selectedRoom = null;
  $("#room-chips").replaceChildren(...(assessment.rooms.length
    ? assessment.rooms.map((r) => el("button", {
      class: `chip${r.room_id === selectedRoom ? " on" : ""}`, type: "button",
      onclick: () => selectRoom(r.room_id),
    }, `${r.name} · ${counts[r.room_id] || 0}`))
    : [el("span", { class: "muted small" }, "add rooms first")]));
  $("#btn-shot").disabled = !capture.active || !selectedRoom;
}

// --- rendering: schedule ------------------------------------------------------------

function flagDots(flags = []) {
  if (!flags.length) return null;
  const label = flags.map((f) => FLAG_LABELS[f] || f.replaceAll("_", " ")).join(" · ");
  return el("span", { class: "flag", title: label }, label);
}

function lineRow(line) {
  const evidence = (line.evidence_refs || []).filter((id) => /^[0-9a-f]{12}$/.test(id)).slice(0, 4);
  const why = el("details", { class: "why" },
    el("summary", {}, "Why"),
    el("p", {}, line.rationale || "No rationale recorded."),
    el("p", { class: "muted small" }, `Materials ${gbp.format(line.material_cost)} · Labour ${gbp.format(line.labour_cost)} · ${line.item_code}`),
    evidence.length ? el("div", { class: "thumbs" }, evidence.map((id, i) => {
      const src = `/api/assessments/${assessment.assessment_id}/evidence/${id}/image`;
      const urls = evidence.map((e) => `/api/assessments/${assessment.assessment_id}/evidence/${e}/image`);
      return el("button", { class: "thumb", type: "button", title: "View larger", onclick: () => openLightbox(urls, i) },
        el("img", { src, loading: "lazy", alt: "Evidence view" }));
    })) : null,
  );
  return el("li", { class: "line" },
    el("div", { class: "line-main" },
      el("span", { class: "desc" }, line.description),
      flagDots(line.flags)),
    el("span", { class: "qty" }, `${qty(line.quantity)} ${line.unit.toLowerCase()}`),
    el("span", { class: "amount" }, gbp.format(line.total)),
    why,
  );
}

let lightbox = { urls: [], index: 0 };

function showLightboxImage() {
  const { urls, index } = lightbox;
  $("#lightbox-img").src = urls[index];
  $("#lightbox-caption").textContent = urls.length > 1
    ? `${index + 1} of ${urls.length} · ← → to browse · click anywhere to close`
    : "Click anywhere to close";
}

function openLightbox(urls, index) {
  lightbox = { urls, index };
  showLightboxImage();
  $("#lightbox").showModal();
}

$("#lightbox").addEventListener("click", () => $("#lightbox").close());
document.addEventListener("keydown", (e) => {
  if (!$("#lightbox").open || lightbox.urls.length < 2) return;
  const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
  if (!step) return;
  lightbox.index = (lightbox.index + step + lightbox.urls.length) % lightbox.urls.length;
  showLightboxImage();
});

function roomCard(group, open) {
  const lines = group.lines || [];
  const checks = group.site_checks?.length
    ? group.site_checks
    : (group.special_notes ? group.special_notes.split(/(?<=\.)\s+(?=[A-Z0-9])/).slice(0, 5) : []);
  const toCheck = lines.filter((l) => (l.flags || []).length).length;
  const d = group.dimensions;
  const meta = [
    `${lines.length} line${lines.length === 1 ? "" : "s"}`,
    toCheck ? `${toCheck} to check` : null,
    d ? `${num(d.length_m)} × ${num(d.breadth_m)} m${d.source && d.source !== "assessor" ? ` (${d.source})` : ""}` : null,
  ].filter(Boolean).join(" · ");
  return el("details", { class: "room card", open },
    el("summary", {},
      el("div", {}, el("span", { class: "room-name" }, group.name), el("span", { class: "muted small room-meta" }, meta)),
      el("span", { class: "amount" }, gbp.format(group.group_total))),
    checks.length ? el("div", { class: "checks" },
      el("p", { class: "label" }, "Check on site"),
      el("ul", {}, checks.map((c) => el("li", {}, c)))) : null,
    lines.length ? el("ul", { class: "lines" }, lines.map(lineRow)) : el("p", { class: "muted small" }, "No damage scheduled."),
  );
}

async function loadSchedule() {
  ({ schedule } = await api("GET", `/assessments/${assessment.assessment_id}/schedule`));
  const t = schedule.totals;
  const groups = schedule.rooms;
  const allLines = [...schedule.general_items.lines, ...groups.flatMap((g) => g.lines)];
  const toCheck = allLines.filter((l) => (l.flags || []).length).length;

  $("#sum-gross").textContent = gbp0.format(t.gross);
  $("#sum-split").textContent = `Net ${gbp.format(t.net)} · VAT ${gbp.format(t.vat)}`;
  $("#sum-stats").replaceChildren(
    ...[[groups.length, "rooms"], [allLines.length, "lines"], [toCheck, "to check"]].map(([n, label]) =>
      el("div", { class: "stat" }, el("span", { class: "stat-n" }, String(n)), el("span", { class: "muted small" }, label))),
  );

  const qa = schedule.qa_findings || [];
  $("#qa").replaceChildren(...(qa.length ? [el("details", { class: "card quiet qa" },
    el("summary", {}, `${qa.length} automatic check${qa.length === 1 ? "" : "s"} raised`),
    el("ul", {}, qa.map((f) => el("li", { class: f.severity }, f.message))))] : []));

  const ordered = [...groups].sort((a, b) => Number(b.group_total) - Number(a.group_total));
  $("#schedule").replaceChildren(
    ...ordered.map((g, i) => roomCard(g, i === 0)),
    roomCard({ ...schedule.general_items, site_checks: [] }, false),
  );
  $("#btn-report").href = `report.html#${assessment.assessment_id}`;
  $("#schedule-section").hidden = false;
}

// --- actions ---------------------------------------------------------------------

function readRoomInputs(scope) {
  const v = (n) => scope.querySelector(`[name=${n}]`).value;
  return {
    name: v("name").trim(),
    dimensions: { length_m: Number(v("length_m")), breadth_m: Number(v("breadth_m")), height_m: Number(v("height_m")), openings: [] },
  };
}

async function saveRoom(room, row) {
  try {
    const body = readRoomInputs(row);
    const source = room.dimensions.source === "assessor" ? "assessor" : "scan_corrected";
    await api("POST", `/assessments/${assessment.assessment_id}/rooms`, {
      ...body, room_id: room.room_id, dimensions: { ...body.dimensions, source },
    });
    document.activeElement?.blur();
    await load(assessment.assessment_id);
    stage(`Saved ${body.name}.`);
  } catch (err) {
    stage(err.message, true);
  }
}

async function deleteRoom(room) {
  if (!confirm(`Remove ${room.name}?`)) return;
  await api("DELETE", `/assessments/${assessment.assessment_id}/rooms/${room.room_id}`);
  await load(assessment.assessment_id);
}

async function startCapture() {
  await capture.start();
  stage("");
}

async function readFloorPlan() {
  const blob = await capture.snapshot();
  const evidenceId = await uploadEvidence("floor_plan", null, blob);
  await api("POST", `/assessments/${assessment.assessment_id}/rooms-from-plan`, { evidence_id: evidenceId });
  await load(assessment.assessment_id);
}

function sizeDock() {
  const dpr = window.devicePixelRatio || 1;
  const width = Math.round(Math.min(Math.max(TARGET_CAPTURE_PX / dpr, 420), window.innerWidth * 0.55));
  const lane = $("#dock-lane");
  lane.style.width = `${width}px`;
  lane.style.height = `${Math.round(width * 0.62)}px`;
}

async function startWalk() {
  if (!capture.active) await capture.start();
  capture.stopAuto();
  await api("DELETE", `/assessments/${assessment.assessment_id}/views`); // a new walk replaces the old one
  schedule = null;
  $("#schedule-section").hidden = true;
  walking = true;
  walkAbort = new AbortController();
  sizeDock();
  dockDismissed = false;
  $("#dock").dataset.mode = "walk";
  $("#dock-title").textContent = "Walking the tour";
  $(".dock-walk").hidden = false;
  $(".dock-draft").hidden = true;
  $("#btn-stop-walk").hidden = false;
  $("#btn-dock-close").hidden = true;
  $("#dock").hidden = false;
  await load(assessment.assessment_id);
  let stopped = false;
  try {
    const bar = $("#dock-progress");
    const result = await autoWalk({
      host: $("#dock-lane"),
      modelId: assessment.matterport_model,
      capture,
      signal: walkAbort.signal,
      onView: (blob, meta) => uploadEvidence("scan_view", null, blob, meta),
      onProgress: ({ scanPoint, views, viewInPoint, viewsPerPoint }) => {
        $("#dock-status").textContent = scanPoint
          ? `Scan point ${scanPoint} · view ${viewInPoint} of ${viewsPerPoint} · ${views} kept`
          : "Loading the tour…";
        bar.removeAttribute("value"); // indeterminate: the number of scan points is unknown until the end
      },
    });
    stage(`Walk finished: ${result.views} views from ${result.scanPoints} scan points.`);
  } catch (err) {
    if (err.name !== "AbortError") throw err;
    stopped = true;
    stage("Walk stopped. The views kept so far will be used.");
  } finally {
    walking = false;
    walkAbort = null;
    $("#dock").dataset.mode = stopped ? "" : "draft"; // after a full walk the dock becomes the drafting tracker
    $("#dock").hidden = stopped;
    $("#dock-lane").replaceChildren();
    await load(assessment.assessment_id);
  }
  if (!stopped && viewStats().total) await analyse(); // draft automatically once the walk completes
}

async function analyse() {
  capture.stopAuto();
  dockDismissed = false;
  draftStartedAt = Date.now();
  $("#dock").dataset.mode = "draft";
  schedule = null;
  $("#schedule-section").hidden = true;
  await api("POST", `/assessments/${assessment.assessment_id}/analyse`);
  await load(assessment.assessment_id);
}

function selectRoom(roomId) {
  selectedRoom = roomId;
  renderChips();
  if ($("#auto").checked && capture.active) {
    capture.startAuto((blob) => keepView(blob).catch((err) => stage(err.message, true)));
  }
}

async function keepView(blob) {
  if (!selectedRoom) return;
  await uploadEvidence("scan_view", selectedRoom, blob, { sweep_id: "manual" });
  assessment.evidence.push({ room_id: selectedRoom, kind: "scan_view", analysis: "pending" });
  renderChips();
  renderFlow();
}

async function sdkWalk() {
  // Only works where the SDK key is valid: localhost for sandbox keys, licensed domains otherwise.
  const { captureSweep, connect, planCapture } = await import("./scan.js");
  walking = true;
  mountedModel = null;
  try {
    stage("Opening the scan with the SDK…");
    $("#tour").replaceChildren();
    const sdk = await connect($("#tour"), sdkKey, assessment.matterport_model);
    const plan = await planCapture(sdk);
    if (!plan.rooms.length) throw new Error("The SDK found no rooms in this scan.");
    const roomIds = {};
    for (const room of plan.rooms) {
      const saved = await api("POST", `/assessments/${assessment.assessment_id}/rooms`, {
        name: room.name, name_is_placeholder: room.namePlaceholder, scan_room_id: room.scanRoomId, dimensions: room.dimensions,
      });
      roomIds[room.scanRoomId] = saved.room_id;
    }
    for (const room of plan.rooms) {
      for (const sweepId of room.sweeps) {
        await captureSweep(sdk, sweepId, async (dataUri, meta) => {
          await uploadEvidence("scan_view", roomIds[room.scanRoomId], await (await fetch(dataUri)).blob(), meta);
        });
      }
    }
  } finally {
    walking = false;
    await load(assessment.assessment_id);
  }
}

// --- events ---------------------------------------------------------------------

function withBusy(button, fn) {
  return async (event) => {
    event?.preventDefault();
    button.disabled = true;
    try {
      await fn(event);
    } catch (err) {
      if (err?.name === "NotAllowedError") stage("Tab sharing was cancelled.", true);
      else if (assessment) stage(err.message, true);
      else alert(err.message);
    } finally {
      button.disabled = false;
      if (assessment) render();
    }
  };
}

$("#form-create").addEventListener("submit", withBusy($("#form-create button[type=submit]"), async (e) => {
  const f = new FormData(e.target);
  const url = String(f.get("matterport_url") || "").trim();
  const file = f.get("video");
  const hasVideo = file && file.size > 0;
  if (!url && !hasVideo) throw new Error("Add a Matterport link, a walkthrough video, or both.");
  if (url && !MATTERPORT_LINK.test(url)) throw new Error("That doesn't look like a Matterport link. Use the tour's Share link or its Discover page link.");
  const typedKey = String(f.get("sdk_key") || "").trim();
  if (typedKey) {
    sdkKey = typedKey;
    storageSet(KEY_STORAGE, typedKey);
  }
  const created = await api("POST", "/assessments", {
    ...(url ? { matterport_url: url } : {}), peril: f.get("peril"), claim_ref: f.get("claim_ref") || "",
  });
  await load(created.assessment_id);
  if (hasVideo) await uploadVideo(file);
}));

$("#form-link").addEventListener("submit", withBusy($("#form-link button"), async (e) => {
  const url = String(new FormData(e.target).get("matterport_url")).trim();
  if (!MATTERPORT_LINK.test(url)) throw new Error("That doesn't look like a Matterport link (Share or Discover link).");
  await api("PATCH", `/assessments/${assessment.assessment_id}`, { matterport_url: url });
  await load(assessment.assessment_id);
}));

$("#form-brief").addEventListener("submit", withBusy($("#form-brief button[type=submit]"), async (e) => {
  const f = new FormData(e.target);
  const incident = Object.fromEntries(["date_of_incident", "date_noticed", "detail", "cause", "further_info"]
    .map((k) => [k, String(f.get(k) || "").trim()]));
  await api("PATCH", `/assessments/${assessment.assessment_id}`, { incident });
  document.activeElement?.blur();
  await load(assessment.assessment_id);
  stage(assessment.status === "draft_ready" ? "Brief saved. Re-draft to use it." : "Brief saved.");
}));

$("#btn-stop-walk").addEventListener("click", () => walkAbort?.abort());
$("#video-file").addEventListener("change", withBusy($("#video-file"), async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) await uploadVideo(file);
}));
$("#btn-video-draft").addEventListener("click", withBusy($("#btn-video-draft"), analyse));
$("#btn-dock-close").addEventListener("click", () => {
  dockDismissed = true;
  $("#dock").hidden = true;
  $("#dock").dataset.mode = "";
});
$("#btn-view-schedule").addEventListener("click", () => {
  $("#schedule-section").scrollIntoView({ behavior: "smooth", block: "start" });
  dockDismissed = true;
  $("#dock").hidden = true;
  $("#dock").dataset.mode = "";
});
$("#btn-shot").addEventListener("click", withBusy($("#btn-shot"), async () => keepView(await capture.snapshot())));
$("#btn-sdk").addEventListener("click", withBusy($("#btn-sdk"), sdkWalk));
$("#auto").addEventListener("change", (e) => { if (!e.target.checked) capture.stopAuto(); });
$("#btn-add-room").addEventListener("click", withBusy($("#btn-add-room"), async () => {
  const row = $("#new-room");
  const body = readRoomInputs(row);
  if (!body.name) throw new Error("Give the room a name.");
  await api("POST", `/assessments/${assessment.assessment_id}/rooms`, body);
  row.querySelectorAll("input").forEach((i) => { i.value = i.name === "height_m" ? "2.4" : ""; });
  await load(assessment.assessment_id);
}));
$("#form-upload").addEventListener("submit", withBusy($("#form-upload button"), async (e) => {
  const f = new FormData(e.target);
  for (const file of f.getAll("files")) await uploadEvidence("photo", f.get("room_id"), file);
  e.target.querySelector("[name=files]").value = "";
  await load(assessment.assessment_id);
}));
$("#btn-download").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(schedule, null, 1)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `claimscope-${assessment.assessment_id}.json` });
  a.click();
  URL.revokeObjectURL(a.href);
});

const THEMES = ["system", "light", "dark"];
function applyTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $("#theme-toggle").textContent = `Theme: ${theme[0].toUpperCase()}${theme.slice(1)}`;
}
let theme = THEMES.includes(storageGet("claimscope.theme")) ? storageGet("claimscope.theme") : "system";
applyTheme(theme);
$("#theme-toggle").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  storageSet("claimscope.theme", theme);
  applyTheme(theme);
});

function openFromHash() {
  const id = location.hash.slice(1);
  if (/^[0-9a-f]{12}$/.test(id) && id !== assessment?.assessment_id) {
    schedule = null;
    load(id).catch((err) => stage(err.message, true));
  }
}

await loadConfig();
window.addEventListener("hashchange", openFromHash);
openFromHash();
