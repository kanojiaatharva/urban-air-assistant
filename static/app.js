"use strict";

const $ = (id) => document.getElementById(id);
const SECTIONS = [
  ["health", "Protect the most vulnerable"],
  ["routine", "Your low-emission daily routine"],
  ["indoor", "Cleaner air at home"],
  ["sustainable", "Sustainable swaps for your street"],
];
const FLAGS = ["children", "elderly", "pregnancy", "heart_lung", "has_purifier"];
const CHOICES = ["commute", "cooking", "heating", "language"];
const MAX_HOURS = 16;
let lastInput = null;

/** Build an element. String children become text nodes, so nothing is parsed as HTML. */
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

async function api(path, body) {
  const init = { signal: AbortSignal.timeout(25000) };
  if (body) {
    Object.assign(init, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new Error("Can't reach the assistant. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong. Try again in a moment.");
  return data;
}

function readInput() {
  const reading = Number.parseFloat($("value").value);
  if (!Number.isFinite(reading) || reading < 0) {
    throw new Error("Enter today's air reading as a number, 0 or higher.");
  }
  const controls = $("form").elements;
  const hours = Number.parseFloat(controls.namedItem("outdoor_hours").value) || 0;
  if (hours < 0 || hours > MAX_HOURS) throw new Error(`Hours outdoors must be between 0 and ${MAX_HOURS}.`);
  const profile = { outdoor_hours: hours };
  for (const name of FLAGS) profile[name] = controls.namedItem(name).checked;
  for (const name of CHOICES) profile[name] = controls.namedItem(name).value;
  return { [$("unit").value]: reading, profile };
}

function banner(air) {
  const gauge = h("div", { class: "gauge", "aria-hidden": "true" });
  for (let level = 0; level <= 5; level++) {
    gauge.append(h("span", { "data-level": level, class: level === air.level ? "on" : "" }));
  }
  return h("div", { class: "banner", "data-level": air.level },
    h("h2", { id: "results-title", tabindex: "-1" }, `Today: ${air.label}`),
    h("p", {}, `PM2.5 ${air.pm25} µg/m³, US AQI ${air.aqi}, ${air.who_multiple} times the WHO daily guideline.`),
    gauge);
}

function exposure({ before, after, reduction_pct: cut }) {
  const bar = (caption, value) => h("label", { class: "bar" }, `${caption}: ${value} µg/m³`,
    h("meter", { min: 0, max: Math.max(before, 1), value }));
  return h("section", {},
    h("h3", {}, "What the plan changes"),
    h("p", {}, cut > 0
      ? `Following this plan could cut your estimated daily PM2.5 exposure by about ${cut}%.`
      : "The air is clean enough that this plan is about keeping good habits."),
    bar("Without changes", before),
    bar("With the plan", after));
}

function summaryBlock({ text, source }) {
  return h("section", {},
    h("h3", {}, "In plain words"),
    h("p", {}, text),
    h("p", { class: "hint" }, source === "ai"
      ? "Written by AI from the numbers in this plan."
      : "Offline summary: AI is not configured on this server."));
}

function impactText(action) {
  if (action.reduction_pct) return `Cuts your estimated daily exposure by about ${action.reduction_pct}%.`;
  return action.community
    ? "Lowers emissions on your street, so everyone breathes less."
    : "Lowers what you breathe in or add to the air, though we can't put a number on it.";
}

function section([id, title], actions) {
  const items = actions.filter((a) => a.category === id);
  if (!items.length) return "";
  const cards = items.map((a) => h("li", {},
    h("h4", {}, a.title),
    h("p", {}, a.why),
    h("p", { class: "impact" }, h("strong", {}, "Impact: "), impactText(a))));
  return h("section", {}, h("h3", {}, title), h("ul", { class: "cards" }, ...cards));
}

function askBlock() {
  const input = h("input", { id: "q", type: "text", maxlength: 300, required: "" });
  const answer = h("p", { id: "answer", "aria-live": "polite" });
  const form = h("form", { id: "ask" },
    h("label", { for: "q" }, "Ask a follow-up question"),
    h("div", { class: "inline" }, input, h("button", { type: "submit" }, "Ask")));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    answer.textContent = "Thinking…";
    try {
      const data = await api("/api/ask", { ...lastInput, question: input.value });
      answer.textContent = data.answer.text;
    } catch (err) {
      answer.textContent = err.message;
    }
  });
  return h("section", {}, h("h3", {}, "Ask a follow-up"), form, answer);
}

function notes(assumptions) {
  return h("details", {},
    h("summary", {}, "How we estimated this"),
    h("ul", {}, ...assumptions.map((line) => h("li", {}, line))));
}

function render({ plan, summary }) {
  $("results").replaceChildren(
    banner(plan.air),
    exposure(plan.exposure),
    summaryBlock(summary),
    ...SECTIONS.map((s) => section(s, plan.actions)),
    askBlock(),
    notes(plan.assumptions));
  $("results-title").focus();
}

$("lookup").addEventListener("click", async () => {
  const status = $("lookup-status");
  const city = $("city").value.trim();
  if (!city) {
    status.textContent = "Type a town or city first.";
    return;
  }
  status.textContent = "Looking up…";
  try {
    const air = await api(`/api/air?city=${encodeURIComponent(city)}`);
    $("unit").value = "pm25";
    $("value").value = air.pm25;
    status.textContent = `${air.place}: PM2.5 is ${air.pm25} µg/m³ right now (${air.source}, modelled data).`;
  } catch (err) {
    status.textContent = err.message;
  }
});

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("submit");
  $("error").textContent = "";
  button.disabled = true;
  try {
    lastInput = readInput();
    render(await api("/api/plan", lastInput));
  } catch (err) {
    $("error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});
