"use strict";
const form = document.querySelector("#filters");
let data,
  drill = {},
  requestVersion = 0;
const preferenceKey = "sleek-space-analytics-visibility-v1";
let preferences = (() => {
  try {
    return {
      hiddenColumns: {},
      hiddenCampaigns: [],
      ...JSON.parse(localStorage.getItem(preferenceKey) || "{}"),
    };
  } catch (_) {
    return { hiddenColumns: {}, hiddenCampaigns: [] };
  }
})();
const savePreferences = () => {
  try {
    localStorage.setItem(preferenceKey, JSON.stringify(preferences));
  } catch (_) {
    // Visibility still works for this page when browser storage is unavailable.
  }
};
const fmt = (v, key = "") =>
  v === null || v === undefined
    ? "—"
    : key.endsWith("rate")
      ? `${(v * 100).toFixed(1)}%`
      : key.includes("roas")
        ? `${v.toFixed(2)}×`
        : new Intl.NumberFormat("en-PK", {
            maximumFractionDigits: Number.isInteger(v) ? 0 : 2,
          }).format(v);
const el = (tag, text, className) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
};
const campaigns = [
  ["name", "Campaign / group / ad"],
  ["channel", "Channel"],
  ["campaign_id", "Campaign ID"],
  ["spend", "Spend"],
  ["impressions", "Impressions"],
  ["clicks", "Clicks"],
  ["cpc", "CPC"],
  ["product_views", "Product views"],
  ["add_to_carts", "Add to carts"],
  ["gross_orders", "Gross orders"],
  ["delivered", "Delivered"],
  ["cancelled", "Cancelled"],
  ["in_process", "Pending"],
  ["delivery_rate", "Delivery rate"],
  ["cancellation_rate", "Cancellation rate"],
  ["delivered_revenue", "Delivered revenue"],
  ["cost_per_delivered", "Cost / delivered"],
  ["delivered_roas", "Delivered ROAS"],
  ["platform_purchases", "Platform-reported purchases"],
  ["platform_purchase_value", "Platform-reported purchase value"],
];
const products = [
  ["name", "Product"],
  ["product_views", "Product views"],
  ["add_to_carts", "Add to carts"],
  ["gross_orders", "Gross orders"],
  ["delivered", "Delivered"],
  ["cancelled", "Cancelled"],
  ["in_process", "Pending"],
  ["view_to_order_rate", "View-to-order rate"],
  ["delivery_rate", "Delivery rate"],
  ["cancelled_value", "Cancelled submitted value"],
  ["delivered_revenue", "Delivered revenue"],
  ["spend", "Ad spend"],
  ["delivered_roas", "Delivered ROAS"],
];
function defaults() {
  const today = new Date(),
    local = (d) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  form.elements.start.value = local(
    new Date(today.getFullYear(), today.getMonth(), 1),
  );
  form.elements.end.value = local(today);
}
function table(id, columns, rows) {
  const target = document.getElementById(id),
    head = target.querySelector("thead"),
    body = target.querySelector("tbody"),
    hiddenColumns = new Set(preferences.hiddenColumns[id] || []),
    visibleColumns = columns.filter(
      ([key]) => key === "name" || !hiddenColumns.has(key),
    ),
    hiddenCampaigns = new Set(preferences.hiddenCampaigns || []),
    visibleRows =
      id === "campaigns"
        ? rows.filter((row) => !hiddenCampaigns.has(campaignKey(row)))
        : rows;
  head.replaceChildren();
  body.replaceChildren();
  const tr = el("tr");
  for (const [key, label] of visibleColumns) {
    const th = el("th"),
      b = el("button", label);
    b.type = "button";
    let desc = true;
    b.onclick = () => {
      visibleRows.sort((a, b) =>
        typeof a[key] === "number"
          ? desc
            ? b[key] - a[key]
            : a[key] - b[key]
          : String(a[key] ?? "").localeCompare(String(b[key] ?? "")) *
            (desc ? -1 : 1),
      );
      desc = !desc;
      paint();
    };
    th.append(b);
    tr.append(th);
  }
  head.append(tr);
  function add(row, depth = 0) {
    const tr = el("tr", undefined, `depth-${depth}`);
    visibleColumns.forEach(([key]) => {
      const td = el("td");
      if (key === "name") {
        if (row.children?.length) {
          const b = el("button", row.expanded ? "−" : "+", "expand");
          b.setAttribute("aria-label", `Expand ${row.name}`);
          b.setAttribute("aria-expanded", String(Boolean(row.expanded)));
          b.onclick = () => {
            row.expanded = !row.expanded;
            paint();
          };
          td.append(b);
        }
        const link = el("button", row.name, "row-link");
        link.onclick = () => {
          drill =
            id === "products"
              ? { product: row.product_id }
              : { channel: row.channel, campaign_id: row.campaign_id };
          if (row.group_id) drill.group_id = row.group_id;
          if (row.ad_id) drill.ad_id = row.ad_id;
          renderOrders();
          document
            .querySelector("#orders-section")
            .scrollIntoView({ behavior: "smooth" });
        };
        td.append(link);
      } else
        td.textContent =
          typeof row[key] === "string"
            ? row[key] || "Unattributed"
            : fmt(row[key], key);
      tr.append(td);
    });
    body.append(tr);
    if (row.expanded) row.children.forEach((c) => add(c, depth + 1));
  }
  function paint() {
    body.replaceChildren();
    visibleRows.forEach((r) => add(r));
  }
  paint();
  document.getElementById(id + "-empty").hidden = visibleRows.length > 0;
}
const campaignKey = (row) =>
  `${row.channel || "Unattributed"}|${row.campaign_id || "Unattributed"}`;
function visibilityOptions(targetId, tableId, columns) {
  const target = document.getElementById(targetId),
    hidden = new Set(preferences.hiddenColumns[tableId] || []);
  target.replaceChildren();
  for (const [key, label] of columns) {
    if (key === "name") continue;
    const wrapper = el("label", undefined, "check-option"),
      input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !hidden.has(key);
    input.onchange = () => {
      if (input.checked) hidden.delete(key);
      else hidden.add(key);
      preferences.hiddenColumns[tableId] = [...hidden];
      savePreferences();
      table(tableId, columns, data[tableId]);
    };
    wrapper.append(input, el("span", label));
    target.append(wrapper);
  }
}
function renderVisibilitySettings() {
  visibilityOptions("campaign-column-options", "campaigns", campaigns);
  visibilityOptions("product-column-options", "products", products);
  const target = document.getElementById("campaign-row-options"),
    hidden = new Set(preferences.hiddenCampaigns || []);
  target.replaceChildren();
  for (const row of data.campaigns) {
    const key = campaignKey(row),
      wrapper = el("label", undefined, "check-option"),
      input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !hidden.has(key);
    input.onchange = () => {
      if (input.checked) hidden.delete(key);
      else hidden.add(key);
      preferences.hiddenCampaigns = [...hidden];
      savePreferences();
      table("campaigns", campaigns, data.campaigns);
    };
    wrapper.append(
      input,
      el("span", `${row.name} · ${row.channel || "Unattributed"}`),
    );
    target.append(wrapper);
  }
}
function renderOrders() {
  const body = document.querySelector("#orders tbody");
  body.replaceChildren();
  const orders = data.orders.filter(
    (o) =>
      (!drill.product || o.items.some((i) => i.item_id === drill.product)) &&
      Object.entries(drill).every(
        ([k, v]) =>
          k === "product" || o[k] === v,
      ),
  );
  document.querySelector("#order-count").textContent = `(${orders.length})`;
  document.querySelector("#drill-label").textContent = Object.keys(drill).length
    ? Object.entries(drill)
        .map(([k, v]) => `${k}: ${v || "Unattributed"}`)
        .join(" · ")
    : "All matching orders · private operations data";
  for (const o of orders) {
    const tr = el("tr");
    for (const key of [
      "order_number",
      "created_at",
      "normalized_status",
      "raw_shopify_status",
      "raw_courier_status",
      "order_value",
      "refunded_value",
      "channel",
      "courier",
      "payment_method",
    ]) {
      const td = el("td");
      if (key === "order_number") {
        const a = el("a", o[key]);
        a.href = `https://admin.shopify.com/store/psgv0a-qk/orders/${encodeURIComponent(o.shopify_order_id)}`;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        td.append(a);
      } else if (key === "normalized_status")
        td.append(
          el(
            "span",
            o[key] === "In process" ? "Pending" : o[key],
            `status ${o[key]}`,
          ),
        );
      else td.textContent = typeof o[key] === "number" ? fmt(o[key]) : o[key];
      tr.append(td);
    }
    body.append(tr);
  }
}
function render() {
  document.querySelector("#currency").textContent = data.currency;
  const box = document.querySelector("#kpis");
  box.replaceChildren();
  for (const [key, label, status, valueKey, valueLabel] of [
    ["spend", "Ad spend"],
    ["product_views", "Product views"],
    ["add_to_carts", "Add to carts"],
    ["gross_orders", "Gross orders"],
    ["delivered", "Delivered orders", "Delivered", "delivered_value", "Delivered value"],
    ["cancelled", "Cancelled orders", "Cancelled", "cancelled_value", "Cancelled value"],
    ["in_process", "Pending orders", "In process", "in_process_value", "Pending value"],
    ["cost_per_delivered", "Cost per delivered"],
    ["delivered_roas", "Delivered ROAS"],
  ]) {
    const card = el(
        "article",
        undefined,
        `kpi ${["delivered", "delivered_revenue", "delivered_roas"].includes(key) ? "success" : ""} ${key === "cancelled" ? "cancelled" : key === "in_process" ? "pending" : ""}`,
      ),
      inner = status ? el("button") : el("div");
    const displayLabel =
      key === "spend" && data.kpis.spend_channels?.length
        ? `Ad spend (${data.kpis.spend_channels.join(" + ")})`
        : label;
    inner.append(
      el("span", displayLabel, "label"),
      el("strong", fmt(data.kpis[key], key)),
    );
    if (valueKey)
      inner.append(
        el(
          "small",
          `${valueLabel} · ${data.currency} ${fmt(data.kpis[valueKey])}`,
          "status-value",
        ),
      );
    if (
      data.kpis.spend_is_partial &&
      ["spend", "cost_per_delivered", "delivered_roas"].includes(key)
    )
      inner.append(el("small", "Available channels only", "scope-note"));
    if (status)
      inner.onclick = () => {
        drill = { normalized_status: status };
        renderOrders();
        document
          .querySelector("#orders-section")
          .scrollIntoView({ behavior: "smooth" });
      };
    card.append(inner);
    box.append(card);
  }
  table("campaigns", campaigns, data.campaigns);
  table("products", products, data.products);
  renderVisibilitySettings();
  renderOrders();
  const funnel = document.querySelector("#funnel");
  funnel.replaceChildren();
  const funnelLabels = {
    submitted_orders: "Shopify submitted orders",
    purchase: "GA4 purchase events",
    refund: "GA4 refund events",
    purchase_revenue: "GA4 purchase revenue",
  };
  ['users','sessions','engaged_sessions','engagement_rate','view_item','add_to_cart','begin_checkout','submitted_orders','purchase','refund','purchase_revenue'].forEach((k) => {
    const v = data.funnel[k];
    const d = el("div");
    d.id = `funnel-${k}`;
    d.append(el("span", funnelLabels[k] || k.replaceAll("_", " ")), el("strong", fmt(v, k)));
    funnel.append(d);
  });
  const warnings = document.querySelector("#warnings");
  warnings.replaceChildren(...data.warnings.map((w) => el("li", w)));
  document.querySelector("#freshness").textContent =
    Object.entries(data.refreshed)
      .map(([s, t]) => `${s} refreshed ${t}`)
      .join(" · ") || "No external reports synchronized yet.";
  for (const [key, values] of Object.entries(data.options)) {
    const select = form.elements[key];
    if (!select) continue;
    const selected = select.value;
    select.replaceChildren(
      el("option", key === "currency" ? "Reporting currency" : "All"),
    );
    select.options[0].value = "";
    for (const value of values) {
      const [id, name] = Array.isArray(value) ? value : [value, value];
      const option = el("option", name || id);
      option.value = id;
      select.append(option);
    }
    select.value = selected;
    if (key === "currency" && !selected) select.value = data.currency;
  }
}
async function loadUsers(params, version) {
  try {
    const response = await fetch("/api/analytics/users?" + params, {
      credentials: "same-origin",
      cache: "no-store",
    });
    const result = await response.json();
    if (version !== requestVersion || !response.ok) return;
    const value = document.querySelector("#funnel-users strong");
    if (value) value.textContent = fmt(result.users);
  } catch (_) {
    // Keep the already rendered report usable if GA4's live range query is slow.
  }
}
async function load() {
  const version = ++requestVersion;
  document.querySelector("#load-status").textContent = "Loading performance…";
  try {
    const params = new URLSearchParams(new FormData(form));
    const response = await fetch(
      "/api/analytics?" + params,
      { credentials: "same-origin", cache: "no-store" },
    );
    const result = await response.json();
    if (version !== requestVersion) return;
    if (!response.ok) throw new Error(result.error);
    data = result;
    drill = {};
    render();
    loadUsers(params, version);
    document.querySelector("#load-status").textContent =
      `${data.orders.length} submitted orders · ${form.elements.start.value} — ${form.elements.end.value} · ${data.currency}`;
  } catch (error) {
    if (version === requestVersion) {
      document.querySelector("#load-status").textContent = error.message;
      for (const id of ["kpis", "funnel"])
        document.getElementById(id).replaceChildren();
      for (const id of ["campaigns", "products", "orders"])
        document.querySelector(`#${id} tbody`).replaceChildren();
    }
  }
}
form.addEventListener("submit", (e) => {
  e.preventDefault();
  load();
});
document.querySelector("#reset").onclick = () => {
  HTMLFormElement.prototype.reset.call(form);
  for (const select of form.querySelectorAll('select')) select.value = '';
  defaults();
  load();
};
document.querySelector("#clear-drill").onclick = () => {
  drill = {};
  if (data) renderOrders();
};
document.querySelector("#campaign-settings-toggle").onclick = (event) => {
  const panel = document.querySelector("#campaign-settings"),
    expanded = panel.hidden;
  panel.hidden = !expanded;
  event.currentTarget.setAttribute("aria-expanded", String(expanded));
};
document.querySelector("#show-all-campaigns").onclick = () => {
  preferences.hiddenCampaigns = [];
  savePreferences();
  if (data) {
    table("campaigns", campaigns, data.campaigns);
    renderVisibilitySettings();
  }
};
defaults();
if (document.body.dataset.enabled === "true") load();
