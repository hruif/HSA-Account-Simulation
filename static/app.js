"use strict";

// The browser only calls /api/* and redraws. The session cookie says who is logged in.

const DEMO = { email: "demo@example.com", password: "demo-password" }; // seeded in main.py
const DECLINE_LABELS = {
  card_inactive: "card was replaced",
  not_qualified: "not a qualified medical expense",
  insufficient_funds: "insufficient funds",
};

const app = document.getElementById("app");
const sessionBox = document.getElementById("session");
let categories = { qualified: [], not_qualified: [] };
let lastBurst = null; // result of the last concurrency demo, kept across redraws

// ---------- helpers ----------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const error = new Error(errorMessage(data, res.status));
    error.status = res.status;
    throw error;
  }
  return data;
}

function errorMessage(data, status) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((e) => `${e.loc.at(-1)}: ${e.msg}`).join("; ");
  }
  return `Request failed (${status})`;
}

// Money stays in integer cents. Dollars text is parsed without floats.
function formatCents(cents) {
  const whole = Math.floor(cents / 100).toLocaleString("en-US");
  return `$${whole}.${String(cents % 100).padStart(2, "0")}`;
}

function parseDollars(text) {
  const match = /^\s*\$?(\d{1,7})(?:\.(\d{1,2}))?\s*$/.exec(text);
  if (!match) return null;
  return Number(match[1]) * 100 + Number((match[2] || "").padEnd(2, "0"));
}

function requireDollars(text) {
  const cents = parseDollars(text);
  if (cents === null || cents <= 0) throw new Error(`"${text}" is not an amount like 25 or 25.50`);
  return cents;
}

function formatTime(sqliteUtc) {
  return new Date(sqliteUtc.replace(" ", "T") + "Z").toLocaleString();
}

function labelFor(category) {
  const words = category.replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}

let toastTimer;
function toast(message, kind = "info") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), 4500);
}

function field(label, input) {
  return h("label", { class: "field" }, h("span", {}, label), input);
}

function moneyInput(name, value) {
  return h("span", { class: "money" },
    h("span", { "aria-hidden": "true" }, "$"),
    h("input", { name, value, inputmode: "decimal", required: true, autocomplete: "off" }));
}

function categorySelect(name, selected) {
  const group = (label, list) =>
    h("optgroup", { label }, list.map((c) => h("option", { value: c, selected: c === selected }, labelFor(c))));
  return h("select", { name, required: true },
    group("Qualified medical", categories.qualified),
    group("Not qualified", categories.not_qualified));
}

// Wraps a form submit: stops the page reload, disables the button, shows errors.
function onSubmit(handler) {
  return async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await handler(new FormData(form), form);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  };
}

function actionButton(label, className, handler) {
  const button = h("button", { type: "button", class: className }, label);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await handler();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

// ---------- flow ----------

async function boot() {
  try {
    categories = await api("/api/categories");
  } catch (error) {
    toast(error.message, "error");
  }
  await refresh();
}

async function refresh() {
  try {
    renderDashboard(await api("/api/me"));
  } catch (error) {
    if (error.status === 401) renderAuth("login");
    else toast(error.message, "error");
  }
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  lastBurst = null;
  renderAuth("login");
}

async function issueCard() {
  const card = await api("/api/me/card", { method: "POST" });
  toast(`Card ending ${card.card_number.slice(-4)} is now active`, "ok");
  await refresh();
}

function purchaseBody(cardNumber, merchantName, category, amountCents) {
  return {
    card_number: cardNumber.replace(/\s/g, ""),
    merchant_name: merchantName,
    merchant_category: category,
    amount_cents: amountCents,
  };
}

function describePurchase(p) {
  const what = `${formatCents(p.amount_cents)} at ${p.merchant_name}`;
  return p.status === "approved" ? `Approved: ${what}` : `Declined (${DECLINE_LABELS[p.decline_reason]}): ${what}`;
}

// ---------- views ----------

function renderAuth(mode) {
  sessionBox.replaceChildren();
  const isLogin = mode === "login";

  const form = h("form", {
    class: "stack",
    onsubmit: onSubmit(async (data) => {
      const body = { email: data.get("email"), password: data.get("password") };
      if (!isLogin) body.owner_name = data.get("owner_name");
      await api(isLogin ? "/api/login" : "/api/signup", { method: "POST", body });
      await refresh();
    }),
  },
    !isLogin && field("Full name", h("input", { name: "owner_name", required: true, autocomplete: "name" })),
    field("Email", h("input", { name: "email", type: "email", required: true, autocomplete: "email" })),
    field("Password", h("input", {
      name: "password",
      type: "password",
      required: true,
      minlength: isLogin ? null : 8,
      autocomplete: isLogin ? "current-password" : "new-password",
    })),
    h("button", { type: "submit", class: "primary" }, isLogin ? "Log in" : "Create account"));

  const tab = (label, active, target) =>
    h("button", { type: "button", role: "tab", "aria-selected": String(active), class: active ? "tab active" : "tab",
      onclick: () => renderAuth(target) }, label);

  const useDemo = h("button", {
    type: "button",
    class: "link",
    onclick: () => {
      if (!isLogin) renderAuth("login");
      fillDemo();
    },
  }, "Fill in demo account");

  function fillDemo() {
    const loginForm = app.querySelector("form");
    loginForm.email.value = DEMO.email;
    loginForm.password.value = DEMO.password;
  }

  app.replaceChildren(h("section", { class: "panel auth" },
    h("h1", {}, "Health Savings Account"),
    h("div", { class: "tabs", role: "tablist" },
      tab("Log in", isLogin, "login"),
      tab("Sign up", !isLogin, "signup")),
    form,
    h("p", { class: "muted small" },
      "Demo: ", h("code", {}, DEMO.email), " / ", h("code", {}, DEMO.password), " · ", useDemo)));
}

function renderDashboard({ account, card, activity }) {
  sessionBox.replaceChildren(
    h("span", { class: "muted small" }, account.email),
    actionButton("Log out", "ghost", logout));

  app.replaceChildren(
    h("section", { class: "panel summary" },
      h("div", {},
        h("p", { class: "eyebrow" }, "Account holder"),
        h("h1", {}, account.owner_name)),
      h("div", { class: "balance" },
        h("p", { class: "eyebrow" }, "Available balance"),
        h("p", { class: "amount" }, formatCents(account.balance_cents)))),
    h("div", { class: "grid" }, depositPanel(), cardPanel(account, card)),
    h("div", { class: "grid" }, purchasePanel(card), concurrencyPanel(card)),
    activityPanel(activity));
}

function depositPanel() {
  return h("section", { class: "panel" },
    h("h2", {}, "Deposit funds"),
    h("form", {
      class: "stack",
      onsubmit: onSubmit(async (data) => {
        const cents = requireDollars(data.get("amount"));
        await api("/api/me/deposits", { method: "POST", body: { amount_cents: cents } });
        toast(`Deposited ${formatCents(cents)}`, "ok");
        await refresh();
      }),
    },
      field("Amount", moneyInput("amount", "100.00")),
      h("button", { type: "submit", class: "primary" }, "Deposit")));
}

function cardPanel(account, card) {
  if (!card) {
    return h("section", { class: "panel" },
      h("h2", {}, "Debit card"),
      h("p", { class: "muted" }, "No card yet. Purchases need an active card."),
      actionButton("Issue card", "primary", issueCard));
  }
  const expiry = `${String(card.expiry_month).padStart(2, "0")}/${String(card.expiry_year).slice(-2)}`;
  return h("section", { class: "panel" },
    h("h2", {}, "Debit card"),
    h("div", { class: "card-visual" },
      h("span", { class: "card-brand" }, "HSA · Virtual debit"),
      h("span", { class: "card-number" }, card.card_number.replace(/(\d{4})(?=\d)/g, "$1 ")),
      h("div", { class: "card-meta" },
        h("span", {}, account.owner_name.toUpperCase()),
        h("span", {}, `EXP ${expiry}`),
        h("span", {}, `CVV ${card.cvv}`))),
    actionButton("Replace card", "ghost", async () => {
      if (!confirm("Replace this card? The current number will stop working.")) return;
      await issueCard();
    }));
}

function purchasePanel(card) {
  return h("section", { class: "panel" },
    h("h2", {}, "Make a purchase"),
    h("p", { class: "muted small" },
      "Sent by card number, the way a merchant would. Paste an old card number to see it declined."),
    h("form", {
      class: "stack",
      onsubmit: onSubmit(async (data) => {
        const body = purchaseBody(data.get("card_number"), data.get("merchant_name"),
          data.get("merchant_category"), requireDollars(data.get("amount")));
        const result = await api("/api/purchases", { method: "POST", body });
        toast(describePurchase(result.purchase), result.status === "approved" ? "ok" : "warn");
        await refresh();
      }),
    },
      field("Card number", h("input", {
        name: "card_number", value: card ? card.card_number : "", required: true, inputmode: "numeric",
        autocomplete: "off",
      })),
      field("Merchant", h("input", { name: "merchant_name", value: "CVS Pharmacy", required: true })),
      h("div", { class: "row" },
        field("Category", categorySelect("merchant_category", "pharmacy")),
        field("Amount", moneyInput("amount", "25.00"))),
      h("button", { type: "submit", class: "primary" }, "Submit purchase")));
}

function concurrencyPanel(card) {
  const form = h("form", {
    class: "stack",
    onsubmit: onSubmit(async (data) => {
      if (!card) throw new Error("Issue a card first.");
      const amounts = data.get("amounts").split(",").map((s) => s.trim()).filter(Boolean).map(requireDollars);
      if (amounts.length < 2 || amounts.length > 50) throw new Error("Enter between 2 and 50 amounts.");

      // All requests leave the browser together; the server decides which ones fit.
      const results = await Promise.all(amounts.map((cents) =>
        api("/api/purchases", {
          method: "POST",
          body: purchaseBody(card.card_number, "Concurrency test", "pharmacy", cents),
        })));

      const approved = results.filter((r) => r.status === "approved");
      const approvedCents = approved.reduce((sum, r) => sum + r.purchase.amount_cents, 0);
      const endCents = Math.min(...results.map((r) => r.balance_cents));
      lastBurst = {
        sent: results.length,
        approved: approved.length,
        declined: results.length - approved.length,
        startCents: endCents + approvedCents,
        approvedCents,
        endCents,
      };
      await refresh();
    }),
  },
    field("Amounts sent at the same time", h("input", { name: "amounts", value: "80, 50", required: true })),
    h("div", { class: "row wrap" },
      presetButton("$80 + $50", "80, 50"),
      presetButton("10 × $30", Array(10).fill("30").join(", ")),
      presetButton("20 × $5", Array(20).fill("5").join(", "))),
    h("button", { type: "submit", class: "primary" }, "Send all at once"));

  function presetButton(label, value) {
    return h("button", { type: "button", class: "chip", onclick: () => (form.amounts.value = value) }, label);
  }

  return h("section", { class: "panel" },
    h("h2", {}, "Concurrent purchases"),
    h("p", { class: "muted small" },
      "Fires every amount as a pharmacy purchase in parallel. The balance must never go below $0."),
    form,
    lastBurst && h("div", { class: "burst" },
      h("p", {}, `Sent ${lastBurst.sent} at once: `,
        h("strong", { class: "ok-text" }, `${lastBurst.approved} approved`), ", ",
        h("strong", { class: "warn-text" }, `${lastBurst.declined} declined`), "."),
      h("p", { class: "muted small" },
        `${formatCents(lastBurst.startCents)} − ${formatCents(lastBurst.approvedCents)} approved = `,
        `${formatCents(lastBurst.endCents)} left.`)));
}

function activityPanel(activity) {
  const body = activity.length
    ? h("div", { class: "table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {},
          ["Ref", "Time", "Type", "Merchant", "Category", "Amount", "Result"].map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, activity.map(activityRow))))
    : h("p", { class: "muted" }, "No activity yet.");

  return h("section", { class: "panel" },
    h("h2", {}, "Activity"),
    h("p", { class: "muted small" }, "Every deposit and every purchase attempt, newest first (last 50)."),
    body);
}

function activityRow(row) {
  const isDeposit = row.type === "deposit";
  const approved = row.status === "approved";
  const sign = isDeposit ? "+" : approved ? "−" : "";
  const qualified = row.merchant_category && categories.qualified.includes(row.merchant_category);

  return h("tr", { class: approved ? "" : "declined" },
    h("td", { class: "mono" }, row.ref),
    h("td", { class: "nowrap" }, formatTime(row.created_at)),
    h("td", {}, isDeposit ? "Deposit" : "Purchase"),
    h("td", {}, row.merchant_name ?? "—"),
    h("td", {}, row.merchant_category
      ? h("span", { class: qualified ? "tag ok" : "tag muted-tag" }, labelFor(row.merchant_category))
      : "—"),
    h("td", { class: "mono right nowrap" }, `${sign}${formatCents(row.amount_cents)}`),
    h("td", {}, approved
      ? h("span", { class: "tag ok" }, "Approved")
      : h("span", { class: "tag warn", title: row.decline_reason }, `Declined · ${DECLINE_LABELS[row.decline_reason]}`)));
}

boot();
