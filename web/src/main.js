// The map: graph.json drawn with Cytoscape, compound boxes for where things
// run, a side panel for one item's attributes, comments and source links.
//
// Everything shown comes from graph.json. All text from it is set with
// textContent, never as HTML: comments are free text from the repository.

import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";

cytoscape.use(fcose);

const KIND_LABELS = {
  provider: "Provider", host: "Host", vm: "VM", refhost: "Referenced host", cluster: "Cluster",
  helm: "Helm chart", stack: "Compose stack", container: "Container", route: "Route",
  port: "Port", dns: "DNS record", network: "Network", disk: "Disk / volume", bucket: "Bucket",
  cloud: "Cloud service", config: "Config", secret: "Secret", workflow: "Workflow", job: "Job",
  environment: "Environment", tfstack: "Terraform stack", tfmodule: "Terraform module",
};
// Kinds hidden until asked for: many small boxes that mostly add noise.
const HIDDEN_BY_DEFAULT = new Set(["port"]);
// How often the page checks for a rebuilt map. Short on localhost, where the
// map rebuilds on every edit.
const RELOAD_POLL_MS = ["localhost", "127.0.0.1"].includes(location.hostname) ? 5000 : 60000;

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") e.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else e.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) e.append(kid);
  return e;
};
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let data;
let cy;
const hiddenKinds = new Set(HIDDEN_BY_DEFAULT);

async function load() {
  const res = await fetch("graph.json", { cache: "no-cache" });
  if (!res.ok) throw new Error(`graph.json: ${res.status} ${await res.text()}`);
  return res.json();
}

function sourceUrl(src) {
  const range = src.end > src.start ? `#L${src.start}-L${src.end}` : `#L${src.start}`;
  return `${data.repo}/blob/${data.sha}/${src.file}${range}`;
}

// The elements for the kinds that are shown. A hidden node's children move up
// to its nearest shown ancestor, and its edges go to that ancestor too, so
// hiding ports leaves the routes pointing at the container they reach.
function elements() {
  const byId = new Map(data.nodes.map((n) => [n.id, n]));
  const shown = (id) => {
    let n = byId.get(id);
    while (n && hiddenKinds.has(n.kind)) n = n.parent ? byId.get(n.parent) : undefined;
    return n ? n.id : undefined;
  };
  const nodes = data.nodes.filter((n) => !hiddenKinds.has(n.kind)).map((n) => ({
    group: "nodes",
    data: {
      id: n.id,
      label: n.label,
      kind: n.kind,
      parent: n.parent ? shown(n.parent) : undefined,
      search: [n.label, n.id, ...n.sources.map((s) => s.file), ...n.sources.flatMap((s) => s.comments),
        ...Object.values(n.attrs).map((v) => JSON.stringify(v))].join("\n").toLowerCase(),
    },
  }));
  const seen = new Set();
  const edges = [];
  data.edges.forEach((e, i) => {
    const source = shown(e.source);
    const target = shown(e.target);
    const key = `${source}|${target}|${e.kind}`;
    if (!source || !target || source === target || seen.has(key)) return;
    seen.add(key);
    edges.push({ group: "edges", data: { id: `e${i}`, source, target, label: e.kind } });
  });
  return [...nodes, ...edges];
}

function redraw() {
  cy.elements().remove();
  cy.add(elements());
  layout();
}

function style() {
  const kinds = Object.keys(KIND_LABELS);
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "font-size": 12,
        color: css("--text"),
        "text-valign": "bottom",
        "text-margin-y": 4,
        "text-wrap": "ellipsis",
        "text-max-width": 160,
        "min-zoomed-font-size": 7,
        width: 24,
        height: 24,
        "border-width": 0,
      },
    },
    ...kinds.map((k) => ({ selector: `node[kind = "${k}"]`, style: { "background-color": css(`--k-${k}`) } })),
    { selector: 'node[kind = "secret"]', style: { shape: "diamond" } },
    { selector: 'node[kind = "dns"], node[kind = "route"]', style: { shape: "round-tag" } },
    { selector: 'node[kind = "disk"], node[kind = "bucket"]', style: { shape: "barrel" } },
    { selector: 'node[kind = "port"]', style: { width: 10, height: 10, "font-size": 9 } },
    { selector: 'node[kind = "network"]', style: { shape: "hexagon" } },
    { selector: 'node[kind = "refhost"]', style: { "border-width": 2, "border-style": "dashed", "border-color": css("--k-refhost"), "background-opacity": 0.2 } },
    {
      selector: ":parent",
      style: {
        "background-color": css("--compound"),
        "background-opacity": 0.85,
        "border-width": 1,
        "border-color": css("--compound-border"),
        "text-valign": "top",
        "text-halign": "center",
        "text-margin-y": -4,
        "font-weight": 600,
        "font-size": 22,
        "min-zoomed-font-size": 0,
        padding: 18,
        shape: "round-rectangle",
      },
    },
    ...kinds.map((k) => ({ selector: `:parent[kind = "${k}"]`, style: { "border-color": css(`--k-${k}`) } })),
    { selector: ':parent[kind = "refhost"]', style: { "border-style": "dashed", "border-width": 2 } },
    {
      selector: "edge",
      style: {
        width: 1.2,
        "line-color": css("--edge"),
        "target-arrow-color": css("--edge"),
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        "curve-style": "bezier",
        opacity: 0.7,
      },
    },
    { selector: "edge.show-label, edge:selected", style: { label: "data(label)", "font-size": 9, color: css("--muted"), "text-rotation": "autorotate", "text-background-color": css("--bg"), "text-background-opacity": 1 } },
    { selector: ".faded", style: { opacity: 0.12 } },
    { selector: "node:selected", style: { "border-width": 3, "border-color": css("--highlight") } },
    { selector: ".match", style: { "border-width": 3, "border-color": css("--highlight") } },
  ];
}

function layout() {
  cy.layout({
    name: "fcose",
    quality: "proof",
    animate: false,
    randomize: true,
    nodeDimensionsIncludeLabels: true,
    packComponents: true,
    nodeRepulsion: 6000,
    idealEdgeLength: (edge) => (edge.source().parent().same(edge.target().parent()) ? 40 : 90),
    edgeElasticity: 0.3,
    nestingFactor: 0.1,
    gravity: 0.15,
    gravityCompound: 1.2,
    numIter: 5000,
    tilingPaddingVertical: 30,
    tilingPaddingHorizontal: 30,
    fit: true,
    padding: 30,
  }).run();
}

function renderFilters() {
  const counts = {};
  for (const n of data.nodes) counts[n.kind] = (counts[n.kind] || 0) + 1;
  const box = $("filters");
  box.replaceChildren(el("h2", { text: "Show" }));
  for (const k of Object.keys(KIND_LABELS)) {
    if (!counts[k]) continue;
    const cb = el("input", { type: "checkbox" });
    cb.checked = !hiddenKinds.has(k);
    cb.addEventListener("change", () => {
      cb.checked ? hiddenKinds.delete(k) : hiddenKinds.add(k);
      redraw();
    });
    box.append(el("label", {}, cb, el("span", { class: "swatch", style: `background:${css(`--k-${k}`)}` }),
      el("span", { text: KIND_LABELS[k] }), el("span", { class: "count", text: String(counts[k]) })));
  }
  box.append(el("div", { class: "actions" },
    el("button", { type: "button", text: "All", on: { click: () => { hiddenKinds.clear(); renderFilters(); redraw(); } } }),
    el("button", { type: "button", text: "Reset", on: { click: () => { hiddenKinds.clear(); HIDDEN_BY_DEFAULT.forEach((k) => hiddenKinds.add(k)); renderFilters(); redraw(); } } }),
  ));
}

function fmt(v) {
  if (Array.isArray(v)) return v.map(fmt).join(", ");
  if (v && typeof v === "object") return JSON.stringify(v, null, 1);
  return String(v);
}

function showPanel(which) {
  for (const id of ["details", "unresolved", "intro"]) $(id).hidden = id !== which;
  $("unresolved-toggle").setAttribute("aria-pressed", String(which === "unresolved"));
}

function select(id, { center = true } = {}) {
  const n = data.nodes.find((x) => x.id === id);
  if (!n) return;
  if (hiddenKinds.has(n.kind)) {
    hiddenKinds.delete(n.kind);
    renderFilters();
    redraw();
  }
  const node = cy.getElementById(id);
  cy.elements().unselect();
  node.select();
  focus(node);
  if (center) {
    const around = node.closedNeighborhood().union(node.descendants());
    cy.animate({ fit: { eles: around, padding: 80 }, duration: 300 });
    cy.one("viewport", () => { if (cy.zoom() > 1.6) cy.zoom({ level: 1.6, renderedPosition: node.renderedPosition() }); });
  }
  history.replaceState(null, "", `#${encodeURIComponent(id)}`);
  renderDetails(n);
}

function focus(node) {
  const keep = node.closedNeighborhood().union(node.ancestors()).union(node.descendants());
  const keepAll = keep.union(keep.ancestors());
  cy.batch(() => {
    cy.elements().addClass("faded").removeClass("show-label");
    keepAll.removeClass("faded");
    node.connectedEdges().addClass("show-label");
  });
}

function clearFocus() {
  cy.elements().removeClass("faded show-label match");
  cy.elements().unselect();
  history.replaceState(null, "", location.pathname);
  showPanel("intro");
}

function renderDetails(n) {
  const box = $("details");
  const kids = [
    el("span", { class: "kind", style: `background:${css(`--k-${n.kind}`)}`, text: KIND_LABELS[n.kind] || n.kind }),
    el("h2", { text: n.label }),
    el("div", { class: "id", text: n.id }),
  ];
  const parent = n.parent && data.nodes.find((x) => x.id === n.parent);
  if (parent) {
    kids.push(el("h3", { text: "Inside" }), el("ul", { class: "rel" }, relItem("", parent)));
  }
  const attrs = Object.entries(n.attrs).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length));
  if (attrs.length) {
    kids.push(el("h3", { text: "Attributes" }),
      el("table", { class: "attrs" }, ...attrs.map(([k, v]) => el("tr", {}, el("th", { text: k }), el("td", { text: fmt(v) })))));
  }
  kids.push(el("h3", { text: "Declared in" }));
  if (!n.sources.length) {
    kids.push(el("p", { class: "muted", text: "Not declared anywhere: only named by the items it is linked to." }));
  }
  for (const s of n.sources) {
    const lines = s.end > s.start ? `${s.file}:${s.start}-${s.end}` : `${s.file}:${s.start}`;
    const div = el("div", { class: "source" }, el("a", { href: sourceUrl(s), target: "_blank", rel: "noopener", text: lines }));
    if (s.comments.length) for (const c of s.comments) div.append(el("p", { text: c }));
    else div.append(el("p", { class: "none", text: "No comments here." }));
    kids.push(div);
  }
  const out = data.edges.filter((e) => e.source === n.id);
  const inc = data.edges.filter((e) => e.target === n.id);
  const children = data.nodes.filter((x) => x.parent === n.id);
  if (out.length || inc.length) {
    kids.push(el("h3", { text: "Relations" }), el("ul", { class: "rel" },
      ...out.map((e) => relItem(`${e.kind} → `, data.nodes.find((x) => x.id === e.target))),
      ...inc.map((e) => relItem(`← ${e.kind} `, data.nodes.find((x) => x.id === e.source)))));
  }
  if (children.length) {
    kids.push(el("h3", { text: `Contains (${children.length})` }), el("ul", { class: "rel" }, ...children.map((c) => relItem("", c))));
  }
  box.replaceChildren(...kids);
  showPanel("details");
  $("panel").scrollTop = 0;
}

function relItem(verb, other) {
  if (!other) return null;
  return el("li", {}, el("span", { class: "verb", text: verb }),
    el("button", { type: "button", text: other.label, title: other.id, on: { click: () => select(other.id) } }),
    el("span", { class: "muted", text: ` · ${KIND_LABELS[other.kind] || other.kind}` }));
}

function renderUnresolved() {
  const box = $("unresolved");
  const items = data.unresolved.map((u) => {
    const kids = [el("code", { text: u.text }), el("div", { class: "muted", text: u.why })];
    if (u.file) {
      kids.push(el("a", { href: sourceUrl({ file: u.file, start: u.line || 1, end: u.line || 1 }), target: "_blank", rel: "noopener",
        text: u.line ? `${u.file}:${u.line}` : u.file }));
    }
    return el("div", { class: "item" }, ...kids);
  });
  const errs = (data.errors || []).map((e) => el("div", { class: "item" }, el("code", { text: e }), el("div", { class: "muted", text: "an extractor failed; its part of the map is missing" })));
  box.replaceChildren(
    el("h2", { text: "Unresolved" }),
    el("p", { class: "muted", text: "Named in the code, but with no declaration in the repository to show: values set by hand, in a dashboard, or in another system." }),
    ...errs, ...items);
}

function search(q) {
  q = q.trim().toLowerCase();
  cy.elements().removeClass("match faded");
  if (!q) return;
  const hits = cy.nodes().filter((n) => n.data("search").includes(q));
  if (!hits.length) return;
  cy.batch(() => {
    cy.elements().addClass("faded");
    hits.union(hits.ancestors()).removeClass("faded");
    hits.addClass("match");
  });
  cy.animate({ fit: { eles: hits, padding: 60 }, duration: 250 });
}

async function main() {
  try {
    data = await load();
  } catch (e) {
    $("error").textContent = String(e.message || e);
    $("error").hidden = false;
    return;
  }
  const commit = $("commit");
  commit.textContent = data.sha.slice(0, 7);
  commit.href = `${data.repo}/commit/${data.sha}`;
  commit.title = data.subject;
  $("generated").textContent = `${data.subject} · built ${new Date(data.generated_at).toLocaleString()}`;
  $("unresolved-toggle").textContent = `Unresolved (${data.unresolved.length + (data.errors || []).length})`;

  cy = cytoscape({
    container: $("graph"),
    elements: elements(),
    style: style(),
    minZoom: 0.1,
    maxZoom: 3,
  });
  renderFilters();
  renderUnresolved();
  layout();

  cy.on("tap", "node", (ev) => select(ev.target.id(), { center: false }));
  cy.on("tap", "edge", (ev) => select(ev.target.source().id()));
  cy.on("tap", (ev) => { if (ev.target === cy) clearFocus(); });
  cy.on("mouseover", "edge", (ev) => ev.target.addClass("show-label"));
  cy.on("mouseout", "edge", (ev) => { if (!ev.target.connectedNodes(":selected").length) ev.target.removeClass("show-label"); });

  $("fit").addEventListener("click", () => cy.animate({ fit: { eles: cy.elements(":visible"), padding: 30 }, duration: 250 }));
  $("unresolved-toggle").addEventListener("click", () => showPanel($("unresolved").hidden ? "unresolved" : "intro"));
  let t;
  $("search").addEventListener("input", (ev) => { clearTimeout(t); t = setTimeout(() => search(ev.target.value), 150); });

  // The layout runs synchronously, so the map is in place by now.
  const fromHash = decodeURIComponent(location.hash.slice(1));
  if (fromHash) select(fromHash);

  // Colors follow the system theme; restyle when it flips.
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { cy.style(style()); renderFilters(); });

  // Pick up a rebuilt map: poll, and reload when it changes. The selection is
  // in the URL hash, so a reload keeps it.
  setInterval(async () => {
    try {
      const fresh = await load();
      if (fresh.generated_at !== data.generated_at) location.reload();
    } catch { /* keep the current map */ }
  }, RELOAD_POLL_MS);
}

main();
