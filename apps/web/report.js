// Printable schedule of works laid out like UK insurer schedules: cover with claim details and totals,
// then one page per room with photos, a metric dimensions table, numbered lines, group total and notes.
// "Fully costed" shows a cost per line; "Insurer schedule" shows totals on the cover only.

const $ = (sel) => document.querySelector(sel);
const gbp = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" });
const n2 = (v) => Number(v ?? 0).toFixed(2);
const UNIT = { EACH: "Each", NR: "NR", HR: "HR", M2: "M2", M: "M", DAY: "Day" };
const PERIL_CODE = { escape_of_water: "EOW", flood: "Flood", fire: "Fire", storm: "Storm", impact: "Impact",
  accidental: "AD", malicious: "MD", subsidence: "Subsidence" };
const COMPANY = { name: "Dexspace Ltd", lines: ["ClaimScope demonstration", "dexspacehq.com", "onyekachukwu.chukwuka@dexspacehq.com"] };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v);
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

async function api(path) {
  const res = await fetch(`/api${path}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status}`);
  return data;
}

const kv = (rows) => el("table", { class: "kv" }, rows.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v ?? ""))));

function pageShell(...content) {
  return el("section", { class: "sheet" },
    el("header", { class: "sheet-head" }, el("img", { src: "brand/dexspace-ltd-logo.svg", alt: "Dexspace Ltd", class: "logo" })),
    el("div", { class: "sheet-body" }, content),
    el("footer", { class: "sheet-foot" },
      el("span", { class: "site" }, "dexspacehq.com"),
      el("div", { class: "bars" }, el("i"), el("i"), el("i"), el("i"))));
}

function cover(asmt, schedule, id) {
  const t = schedule.totals;
  const today = new Date().toLocaleDateString("en-GB");
  const name = `${asmt.claim_ref || "ClaimScope"} - ${id}`;
  return pageShell(
    el("div", { class: "box narrow" },
      el("h2", { class: "band" }, "Prepared by"),
      kv([[COMPANY.name, ""], ...COMPANY.lines.map((l) => [l, ""])])),
    el("p", { class: "draft" }, "AI-assisted draft prepared with ClaimScope. Every line is subject to the loss assessor's approval. Rates are demonstration placeholders."),
    el("div", { class: "box centre" },
      el("h2", { class: "band" }, "Schedule of Works"),
      kv([
        ["Insured's Name:", "—"],
        ["Address:", "—"],
        ["Policy No:", "—"],
        ["Claim No:", asmt.claim_ref || "—"],
        ["Date of Loss:", asmt.date_of_loss ? new Date(asmt.date_of_loss).toLocaleDateString("en-GB") : ""],
        ["Type of Loss:", PERIL_CODE[asmt.peril] || asmt.peril],
        ["Peril:", asmt.peril.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase())],
        ["Schedule Date:", today],
        ["Schedule Name:", name],
        ["Total Price (NET)", gbp.format(t.net)],
        ["Tax (Client)", gbp.format(t.vat)],
        ["Total Price (GROSS)", gbp.format(t.gross)],
      ])),
  );
}

function incidentPage(asmt) {
  const i = asmt.incident || {};
  if (!Object.values(i).some(Boolean)) return null;
  const date = (v) => (v ? new Date(v).toLocaleDateString("en-GB") : "");
  const row = (label, value) => el("div", { class: "inc-row" }, el("p", { class: "inc-label" }, label), el("p", { class: "inc-value" }, value || ""));
  return pageShell(el("div", { class: "box" },
    el("h2", { class: "band" }, "Incident Report"),
    kv([["Client:", "—"], ["Policy/Mortgage Roll No:", "—"], ["Claim No:", asmt.claim_ref || "—"]]),
    el("div", { class: "inc" },
      row("Date of Incident", date(i.date_of_incident)),
      row("Date damage noticed", date(i.date_noticed)),
      row("Detail of the damage", i.detail),
      row("Detailed Cause", i.cause),
      row("Further info", i.further_info))));
}

function pickPhotos(group) {
  const counts = new Map();
  for (const line of group.lines) for (const id of line.evidence_refs || []) {
    if (/^[0-9a-f]{12}$/.test(id)) counts.set(id, (counts.get(id) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 2).map(([id]) => id);
}

function roomPage(group, asmt, id, startNo, costed) {
  const d = group.dimensions;
  const q = group.derived;
  const photos = pickPhotos(group);
  const head = el("table", { class: "room-head" },
    el("tr", {}, el("th", {}, "ROOM"), el("td", { colspan: "3" }, group.name.toUpperCase())),
    el("tr", {}, el("th", {}, "Client"), el("td", {}, "—"), el("th", {}, "Claim Number"), el("td", {}, asmt.claim_ref || "—")),
    el("tr", {}, el("th", {}, "Damage"), el("td", {}, PERIL_CODE[asmt.peril] || asmt.peril), el("th", {}, "Policy Number"), el("td", {}, "—")));

  const photoRow = photos.length ? el("div", { class: "photos" }, photos.map((p) =>
    el("img", { src: `/api/assessments/${id}/evidence/${p}/image`, alt: `${group.name} evidence` }))) : null;

  const dims = el("div", { class: "dims-wrap" },
    el("p", { class: "band small" }, "DIMENSIONS IN METRIC (M)"),
    el("table", { class: "dims" },
      el("tr", {}, el("th", { class: "blank" }), ["Length", "Breadth", "Height", "Total"].map((h) => el("th", {}, h))),
      el("tr", {}, el("th", {}, "Wall Size (M2)"),
        el("td", {}, n2(d?.length_m)), el("td", {}, n2(d?.breadth_m)), el("td", {}, n2(d?.height_m)), el("td", {}, n2(q?.wall_area))),
      el("tr", {}, el("th", {}, "Square Size (M2)"), el("td"), el("td"), el("td"), el("td", {}, n2(q?.floor_area))),
      el("tr", {}, el("th", {}, "Linear Form (lm)"), el("td"), el("td"), el("td"), el("td", {}, n2(q?.perimeter)))),
    d?.source && d.source !== "assessor" ? el("p", { class: "dim-note" }, `Dimensions ${d.source === "plan" ? "read from the floor plan" : "estimated from the scan"}; to be measured on site.`) : null);

  const lines = el("table", { class: "lines" },
    el("thead", {}, el("tr", {},
      el("th", { class: "c-no" }, "Line item no."), el("th", { class: "c-cat" }, "Sub Category"), el("th", {}, "Description"),
      el("th", { class: "c-unit" }, "Unit"), el("th", { class: "c-qty" }, "Qty"), costed ? el("th", { class: "c-cost" }, "Cost") : null)),
    el("tbody", {}, group.lines.map((l, i) => el("tr", {},
      el("td", { class: "c-no" }, String(startNo + i)),
      el("td", { class: "c-cat" }, l.category || ""),
      el("td", {}, l.description),
      el("td", { class: "c-unit" }, UNIT[l.unit] || l.unit),
      el("td", { class: "c-qty" }, n2(l.quantity)),
      costed ? el("td", { class: "c-cost" }, gbp.format(l.total)) : null))),
    costed ? el("tfoot", {}, el("tr", {},
      el("td", { colspan: "5", class: "gt-label" }, "Group Total"),
      el("td", { class: "c-cost gt" }, gbp.format(group.group_total)))) : null);

  const notes = (group.site_checks || []).length || group.special_notes
    ? el("div", { class: "notes" }, el("p", { class: "notes-h" }, "Special Notes"),
      group.site_checks?.length ? el("ul", {}, group.site_checks.map((c) => el("li", {}, c))) : el("p", {}, group.special_notes))
    : null;

  return pageShell(head, photoRow, group.room_id ? dims : null, lines, notes);
}

let state = null;

function render() {
  const { asmt, schedule, id, costed } = state;
  const groups = [{ ...schedule.general_items, name: "General Items" }, ...schedule.rooms.filter((g) => g.lines.length)];
  let lineNo = 1;
  const pages = [cover(asmt, schedule, id), incidentPage(asmt)].filter(Boolean);
  for (const g of groups) {
    pages.push(roomPage(g, asmt, id, lineNo, costed));
    lineNo += g.lines.length;
  }
  // Printing: the thead/tfoot spacers repeat on every printed page and keep room for the fixed
  // letterhead and footer, so long rooms flow onto further pages like the source schedules.
  const frame = el("table", { class: "print-frame" },
    el("thead", {}, el("tr", {}, el("td", {}, el("div", { class: "head-space" })))),
    el("tbody", {}, el("tr", {}, el("td", {}, pages))),
    el("tfoot", {}, el("tr", {}, el("td", {}, el("div", { class: "foot-space" })))));
  const printHead = el("div", { class: "print-head" }, el("img", { src: "brand/dexspace-ltd-logo.svg", alt: "Dexspace Ltd", class: "logo" }));
  const printFoot = el("div", { class: "print-foot" }, el("span", { class: "site" }, "dexspacehq.com"),
    el("div", { class: "bars" }, el("i"), el("i"), el("i"), el("i")));
  $("#report").replaceChildren(printHead, frame, printFoot);
  document.querySelectorAll(".mode button").forEach((b) => b.classList.toggle("on", (b.dataset.mode === "costed") === costed));
  document.title = `${asmt.claim_ref || id} - ${costed ? "Fully Costed SOW" : "Insurer Schedule"}`;
}

async function main() {
  const id = location.hash.slice(1);
  if (!/^[0-9a-f]{12}$/.test(id)) throw new Error("No assessment selected.");
  $("#back").href = `./#${id}`;
  const params = new URLSearchParams(location.search);
  const [asmt, { schedule, status }] = await Promise.all([api(`/assessments/${id}`), api(`/assessments/${id}/schedule`)]);
  if (!schedule) throw new Error(status === "draft_ready" ? "Schedule unavailable." : "This assessment has no draft yet.");
  state = { asmt, schedule, id, costed: params.get("mode") !== "insurer" };
  render();
  if (params.has("print")) printWhenReady();
}

async function printWhenReady() {
  await Promise.all([...document.images].map((img) => img.complete ? null : new Promise((r) => { img.onload = img.onerror = r; })));
  window.print();
}

document.querySelectorAll(".mode button").forEach((b) => b.addEventListener("click", () => {
  if (!state) return;
  state.costed = b.dataset.mode === "costed";
  const url = new URL(location.href);
  url.searchParams.set("mode", b.dataset.mode);
  history.replaceState(null, "", url);
  render();
}));
$("#btn-print").addEventListener("click", printWhenReady);
main().catch((err) => $("#report").replaceChildren(el("p", { class: "error" }, err.message)));
