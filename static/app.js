// The browser only calls /api/* and redraws. The session cookie says who is logged in.
// This file holds everything with state or a side effect; format.js holds the pure helpers.

import { formatCents, formatTime, labelFor, parseDollars } from "./format.js";

const DEMO = { email: "demo@example.com", password: "demo-password" }; // seeded in main.py
const DECLINE_LABELS = {
  card_inactive: "card was replaced",
  not_qualified: "not a qualified medical expense",
  insufficient_funds: "insufficient funds",
};

const app = document.getElementById("app");
const sessionBox = document.getElementById("session");
const navBox = document.getElementById("nav");
let categories = { qualified: [], not_qualified: [] };
let maxAmountCents = null; // served by /api/categories, so the page checks the server's cap
let lastData = null; // last GET /api/me response, for redraws that need no new data
let cardDetails = null; // full number/CVV, held only while the owner is looking at them
let lastBurst = null; // result of the last concurrency test, kept across redraws

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

// Not pure: the cap comes from the server, so this one stays here.
function requireDollars(text) {
  const cents = parseDollars(text);
  if (cents === null || cents <= 0) throw new Error(`"${text}" is not an amount like 25 or 25.50`);
  if (maxAmountCents !== null && cents > maxAmountCents) {
    throw new Error(`The most you can send in one request is ${formatCents(maxAmountCents)}.`);
  }
  return cents;
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

// Pages live behind the URL hash (#/deposit, ...), so Back/Forward and reload keep your place.
const PAGES = {
  home: { label: "Home", render: homePage },
  deposit: { label: "Deposit", render: depositPage },
  purchase: { label: "Make a purchase", render: purchasePage, inNav: false }, // reached from Home
  activity: { label: "Activity", render: activityPage },
};

function currentPage() {
  const name = location.hash.replace(/^#\/?/, "");
  return PAGES[name] ? name : "home";
}

async function boot() {
  try {
    categories = await api("/api/categories");
    maxAmountCents = categories.max_amount_cents ?? null;
  } catch (error) {
    toast(error.message, "error");
  }
  window.addEventListener("hashchange", () => {
    cardDetails = null;
    refresh();
  });
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
  cardDetails = null;
  history.replaceState(null, "", location.pathname);
  renderAuth("login");
}

async function issueCard() {
  // POST /api/me/card is the one response that carries the full number. Everything after
  // this asks /api/me/card/details, and only when the owner does.
  const card = await api("/api/me/card", { method: "POST" });
  toast(`Card ending ${card.card_number.slice(-4)} is now active`, "ok");
  cardDetails = null;
  await refresh();
}

async function activeCardNumber() {
  return (await api("/api/me/card/details")).card_number;
}

async function toggleCardDetails() {
  cardDetails = cardDetails ? null : await api("/api/me/card/details");
  renderDashboard(lastData);
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

// ---------- logged out ----------

function renderAuth(mode) {
  sessionBox.replaceChildren();
  navBox.replaceChildren();
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
      const loginForm = app.querySelector("form");
      loginForm.email.value = DEMO.email;
      loginForm.password.value = DEMO.password;
    },
  }, "Fill in demo account");

  app.replaceChildren(h("section", { class: "panel auth" },
    h("h1", {}, "Health Savings Account"),
    h("div", { class: "tabs", role: "tablist" },
      tab("Log in", isLogin, "login"),
      tab("Sign up", !isLogin, "signup")),
    form,
    h("p", { class: "muted small" },
      "Demo: ", h("code", {}, DEMO.email), " / ", h("code", {}, DEMO.password), " · ", useDemo)));
}

// ---------- logged in ----------

function renderDashboard(data) {
  lastData = data;
  const page = currentPage();

  sessionBox.replaceChildren(
    h("span", { class: "muted small" }, data.account.email),
    actionButton("Log out", "ghost", logout));

  navBox.replaceChildren(...Object.entries(PAGES).filter(([, { inNav }]) => inNav !== false).map(([name, { label }]) =>
    h("a", {
      href: `#/${name === "home" ? "" : name}`,
      class: "nav-link",
      "aria-current": name === page ? "page" : null,
    }, label)));

  const backLink = page === "home" ? [] : [h("a", { href: "#/", class: "back-link" }, "← Back to home")];
  app.replaceChildren(...backLink, ...PAGES[page].render(data));
}

function balanceSummary(account, actions) {
  return h("section", { class: "panel summary" },
    h("div", {},
      h("p", { class: "eyebrow" }, "Account holder"),
      h("h1", {}, account.owner_name)),
    h("div", { class: "balance" },
      h("p", { class: "eyebrow" }, "Available balance"),
      h("p", { class: "amount" }, formatCents(account.balance_cents)),
      h("div", { class: "actions" }, actions)));
}

function homePage({ account, card, activity }) {
  return [
    balanceSummary(account, [
      h("a", { href: "#/purchase", class: "button secondary" }, "Make a purchase"),
      h("a", { href: "#/deposit", class: "button primary" }, "Deposit funds"),
    ]),
    h("div", { class: "grid" },
      cardPanel(account, card),
      h("section", { class: "panel" },
        h("div", { class: "panel-head" },
          h("h2", {}, "Recent activity"),
          activity.length > 5 && h("a", { href: "#/activity", class: "small" }, "View activity")),
        activityTable(activity.slice(0, 5), { compact: true }))),
  ];
}

function depositPage({ account }) {
  const form = h("form", {
    class: "stack",
    onsubmit: onSubmit(async (data) => {
      const cents = requireDollars(data.get("amount"));
      await api("/api/me/deposits", { method: "POST", body: { amount_cents: cents } });
      toast(`Deposited ${formatCents(cents)}`, "ok");
      await refresh();
    }),
  },
    field("Amount", moneyInput("amount", "100.00")),
    h("div", { class: "row wrap" },
      ["50", "100", "500"].map((value) =>
        h("button", { type: "button", class: "chip", onclick: () => (form.amount.value = `${value}.00`) }, `$${value}`))),
    h("button", { type: "submit", class: "primary" }, "Deposit"));

  return [
    h("section", { class: "panel narrow" },
      h("h1", {}, "Deposit funds"),
      h("p", { class: "muted" }, "Current balance ", h("strong", {}, formatCents(account.balance_cents))),
      form),
  ];
}

function activityPage({ activity }) {
  return [
    h("section", { class: "panel" },
      h("h1", {}, "Activity"),
      h("p", { class: "muted small" }, "Every deposit and every purchase attempt, newest first (last 50)."),
      activityTable(activity)),
  ];
}

function purchasePage({ account, card }) {
  return [
    h("section", { class: "panel intro" },
      h("h1", {}, "Make a purchase"),
      h("p", {}, "Balance ", h("strong", {}, formatCents(account.balance_cents)),
        card ? ` · card ending ${card.last4}` : " · no active card")),
    h("div", { class: "grid" }, purchasePanel(card), concurrencyPanel(card)),
  ];
}

// ---------- panels ----------

function cardPanel(account, card) {
  if (!card) {
    return h("section", { class: "panel" },
      h("h2", {}, "Debit card"),
      h("p", { class: "muted" }, "No card yet. Purchases need an active card."),
      actionButton("Issue card", "primary", issueCard));
  }

  const revealed = cardDetails !== null;
  const expiry = `${String(card.expiry_month).padStart(2, "0")}/${String(card.expiry_year).slice(-2)}`;
  const number = revealed
    ? cardDetails.card_number.replace(/(\d{4})(?=\d)/g, "$1 ")
    : `•••• •••• •••• ${card.last4}`;

  return h("section", { class: "panel" },
    h("h2", {}, "Debit card"),
    h("div", { class: "card-visual" },
      h("span", { class: "card-brand" }, "HSA · Virtual debit"),
      h("span", { class: "card-number" }, number),
      h("div", { class: "card-meta" },
        h("span", {}, account.owner_name.toUpperCase()),
        h("span", {}, `EXP ${revealed ? expiry : "••/••"}`),
        h("span", {}, `CVV ${revealed ? cardDetails.cvv : "•••"}`))),
    h("div", { class: "row" },
      h("button", {
        type: "button",
        class: "ghost",
        "aria-pressed": String(revealed),
        onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            await toggleCardDetails();
          } catch (error) {
            toast(error.message, "error");
            button.disabled = false;
          }
        },
      }, revealed ? "Hide details" : "Show details"),
      actionButton("Replace card", "ghost", async () => {
        if (!confirm("Replace this card? The current number will stop working.")) return;
        await issueCard();
      })));
}

function purchasePanel(card) {
  const otherCard = h("input", { name: "card_number", inputmode: "numeric", autocomplete: "off",
    placeholder: "16-digit card number" });
  const otherCardField = field("Card number", otherCard);
  otherCardField.hidden = true;

  const toggle = h("button", {
    type: "button",
    class: "link small",
    onclick: () => {
      otherCardField.hidden = !otherCardField.hidden;
      toggle.textContent = otherCardField.hidden ? "Use a different card" : "Use my active card";
      if (!otherCardField.hidden) otherCard.focus();
    },
  }, "Use a different card");

  return h("section", { class: "panel" },
    h("h2", {}, "Single purchase"),
    h("p", { class: "muted small" },
      card ? `Card ending ${card.last4} · ` : "No active card · ", toggle),
    h("form", {
      class: "stack",
      onsubmit: onSubmit(async (data) => {
        if (otherCardField.hidden && !card) throw new Error("Issue a card first, or enter a card number.");
        const cardNumber = otherCardField.hidden ? await activeCardNumber() : data.get("card_number");
        if (!cardNumber) throw new Error("Issue a card first, or enter a card number.");
        const body = purchaseBody(cardNumber, data.get("merchant_name"),
          data.get("merchant_category"), requireDollars(data.get("amount")));
        const result = await api("/api/purchases", { method: "POST", body });
        toast(describePurchase(result.purchase), result.status === "approved" ? "ok" : "warn");
        await refresh();
      }),
    },
      otherCardField,
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

      const cardNumber = await activeCardNumber();
      // Every request is started together; the server decides which ones fit.
      const results = await Promise.all(amounts.map((cents) =>
        api("/api/purchases", {
          method: "POST",
          body: purchaseBody(cardNumber, "Concurrency test", "pharmacy", cents),
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
    field("Amounts (comma-separated)", h("input", { name: "amounts", value: "80, 50", required: true })),
    h("div", { class: "row wrap" },
      presetButton("$80 + $50", "80, 50"),
      presetButton("10 × $30", Array(10).fill("30").join(", ")),
      presetButton("20 × $5", Array(20).fill("5").join(", "))),
    h("button", { type: "submit", class: "primary" }, "Send all at once"));

  function presetButton(label, value) {
    return h("button", { type: "button", class: "chip", onclick: () => (form.amounts.value = value) }, label);
  }

  return h("section", { class: "panel" },
    h("h2", {}, "Concurrency test"),
    h("p", { class: "muted small" },
      "Sends every amount as a pharmacy purchase together. The browser opens about six ",
      "connections at a time, so they arrive in a tight burst rather than a single instant. ",
      h("code", {}, "tests/test_concurrency.py"), " races them from real threads."),
    form,
    lastBurst && h("div", { class: "burst" },
      h("p", {}, `Sent ${lastBurst.sent} at once: `,
        h("strong", { class: "ok-text" }, `${lastBurst.approved} approved`), ", ",
        h("strong", { class: "warn-text" }, `${lastBurst.declined} declined`), "."),
      h("p", { class: "muted small" },
        `${formatCents(lastBurst.startCents)} − ${formatCents(lastBurst.approvedCents)} approved = `,
        `${formatCents(lastBurst.endCents)} left.`)));
}

function activityTable(activity, { compact = false } = {}) {
  if (!activity.length) return h("p", { class: "muted" }, "No activity yet.");
  const columns = compact
    ? ["Time", "Description", "Amount", "Result"]
    : ["Ref", "Time", "Type", "Merchant", "Category", "Amount", "Result"];
  return h("div", { class: "table-wrap" },
    h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", {}, activity.map((row) => activityRow(row, compact)))));
}

function activityRow(row, compact) {
  const isDeposit = row.type === "deposit";
  const approved = row.status === "approved";
  const sign = isDeposit ? "+" : approved ? "−" : "";
  const qualified = row.merchant_category && categories.qualified.includes(row.merchant_category);
  const amount = h("td", { class: "mono right nowrap" }, `${sign}${formatCents(row.amount_cents)}`);
  const result = h("td", {}, approved
    ? h("span", { class: "tag ok" }, "Approved")
    : h("span", { class: "tag warn", title: DECLINE_LABELS[row.decline_reason] },
      compact ? "Declined" : `Declined · ${DECLINE_LABELS[row.decline_reason]}`));

  if (compact) {
    return h("tr", { class: approved ? "" : "declined" },
      h("td", { class: "nowrap" }, formatTime(row.created_at)),
      h("td", {}, isDeposit ? "Deposit" : row.merchant_name),
      amount,
      result);
  }
  return h("tr", { class: approved ? "" : "declined" },
    h("td", { class: "mono" }, row.ref),
    h("td", { class: "nowrap" }, formatTime(row.created_at)),
    h("td", {}, isDeposit ? "Deposit" : "Purchase"),
    h("td", {}, row.merchant_name ?? "—"),
    h("td", {}, row.merchant_category
      ? h("span", { class: qualified ? "tag ok" : "tag muted-tag" }, labelFor(row.merchant_category))
      : "—"),
    amount,
    result);
}

boot();
