// Support agent console: the browser side of web/server.py's JSON API. Agent output is untrusted, so it is only
// ever inserted as text nodes.

const $ = (selector) => document.querySelector(selector);
const ACTOR_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const STORAGE_KEY = "support-console";

const state = {
  stack: null,
  customers: [],
  scenarios: [],
  tools: {},
  entries: [],
  actorId: "CUST-001",
  sessionId: null,
  shownSession: null, // session of the latest divider in the conversation
  selectedId: null,
  scenario: null,
  step: 0,
  loadedPrompt: null,
  runs: {},
  busy: false,
  memoryWait: false,
  tab: "details",
};

// Helpers

function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") element.className = value;
    else if (name === "dataset") Object.assign(element.dataset, value);
    else if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
    else element.setAttribute(name, value === true ? "" : value);
  }
  element.append(...present(children));
  return element;
}

// Children without the null/false placeholders of conditional content, at any nesting depth.
const present = (children) => children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false);
const fill = (element, ...children) => element.replaceChildren(...present(children));

async function api(path, body) {
  const options =
    body === undefined
      ? {}
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("The console server is not answering. Check that `python -m web` is still running.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `The console answered with HTTP ${response.status}.`);
  return data;
}

const ready = () => Boolean(state.stack && state.stack.deployed && !state.stack.problem);
const seconds = (ms) => `${(ms / 1000).toFixed(1)} s`;
const clock = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const money = (amount) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amount);
const newSessionId = () => `session-${crypto.randomUUID()}`;
const guestId = () => `guest-${crypto.randomUUID().slice(0, 8)}`;
const customer = (id) => state.customers.find((item) => item.id === id);
const customerName = (id) => customer(id)?.name ?? id;
const pretty = (value) => JSON.stringify(value, null, 2);

function withCode(text) {
  // `backticks` in server messages are commands to copy.
  return text.split(/(`[^`]+`)/g).map((part) => (part.startsWith("`") ? h("code", {}, part.slice(1, -1)) : part));
}

function agentText(text) {
  // Plain text with **bold** kept, the one markup the agent uses for emphasis.
  const parts = text.split(/(\*\*[^*\n]+\*\*)/g);
  return h(
    "div",
    { class: "said" },
    parts.map((part) => (/^\*\*.+\*\*$/.test(part) ? h("strong", {}, part.slice(2, -2)) : part)),
  );
}

function readableResult(result) {
  // Tool results carry their JSON as text; parse it so it reads as JSON. The raw record keeps the original.
  if (!result) return result;
  const content = (result.content || []).map((block) => {
    if ("json" in block) return block.json;
    try {
      return JSON.parse(block.text);
    } catch {
      return block.text ?? block;
    }
  });
  return { status: result.status, content: content.length === 1 ? content[0] : content };
}

let toastTimer;
function toast(message, tone) {
  const element = $("#toast");
  element.replaceChildren(...withCode(message));
  element.dataset.tone = tone || "";
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (element.hidden = true), tone === "denied" ? 9000 : 6000);
}

function persist() {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ actorId: state.actorId, sessionId: state.sessionId }));
  } catch {
    // Storage can be unavailable (private windows); the console works without it.
  }
}

function restoreSignIn() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "{}");
    if (ACTOR_ID.test(saved.actorId || "")) state.actorId = saved.actorId;
    if (/^session-[0-9a-f-]{36}$/.test(saved.sessionId || "")) state.sessionId = saved.sessionId;
  } catch {
    // Start with the defaults.
  }
  state.sessionId ||= newSessionId();
}

// Status and stack

async function loadStatus() {
  setConnection("checking", "Checking the deployment…");
  $("#recheck").hidden = true;
  let data;
  try {
    data = await api("/api/status");
  } catch (error) {
    state.stack = { deployed: false, problem: error.message };
    renderStack();
    return;
  }
  const firstLoad = !state.scenarios.length;
  Object.assign(state, {
    stack: data.stack,
    customers: data.customers,
    scenarios: data.scenarios,
    tools: data.tools,
  });
  if (firstLoad) {
    state.entries = data.entries;
    state.runs = countRuns(data.entries);
    renderTimeline();
    buildToolOptions();
  }
  updateRecord(data.record);
  renderStack();
  renderCustomers();
  renderOrders();
  renderSession();
  renderScenarios();
  renderToolForm();
  renderDetails();
}

function renderStack() {
  const { stack } = state;
  if (ready()) {
    const runtime = stack.runtime_arn.split("/").pop();
    const text = `Connected to ${runtime} in ${stack.region} as ${stack.identity}`;
    setConnection("ready", text);
  } else {
    setConnection("problem", stack.problem.replaceAll("`", ""));
  }
  const empty = $("#empty");
  empty.dataset.state = ready() ? "ready" : "problem";
  $("#empty-title").textContent = ready()
    ? "Talk to the agent as a customer."
    : "The console can't reach the agent yet.";
  $("#empty-text").replaceChildren(
    ...(ready() ? ["Pick a scenario on the left, or write your own message below."] : withCode(stack.problem)),
  );
  $("#recheck").hidden = ready();
  updateControls();
  updateEmpty();
}

function setConnection(kind, text) {
  const element = $("#connection");
  element.dataset.state = kind;
  element.textContent = text;
  element.title = text;
}

function updateControls() {
  const usable = ready() && !state.busy;
  $("#prompt").disabled = !ready();
  $("#send").disabled = !usable || state.memoryWait;
  $("#send").textContent = state.memoryWait ? "Waiting for Memory…" : "Send";
  $("#tool-submit").disabled = !usable;
}

function updateRecord(summary) {
  const { entries, unsaved } = summary;
  $("#record-status").textContent =
    entries === 0
      ? "Nothing recorded yet"
      : unsaved === 0
        ? `${entries} recorded, all saved`
        : `${entries} recorded, ${unsaved} not saved`;
  $("#save-evidence").disabled = entries === 0;
}

function updateEmpty() {
  // Once there is a conversation, a problem only shows in the masthead, so earlier turns stay readable.
  $("#empty").hidden = $("#timeline").children.length > 0;
}

// Customer and session

function renderCustomers() {
  const known = Boolean(customer(state.actorId));
  const options = state.customers.map((item) =>
    h(
      "label",
      { class: "customer" },
      h("input", {
        type: "radio",
        name: "customer",
        value: item.id,
        checked: item.id === state.actorId,
        onchange: () => signIn(item.id),
      }),
      h("span", { class: "customer-name" }, item.name),
      h("span", { class: "customer-id" }, h("code", {}, item.id), `, ${item.tier} tier`),
    ),
  );
  const other = h("input", {
    class: "customer-other",
    type: "text",
    value: known ? "" : state.actorId,
    placeholder: "Any id, e.g. guest-2a9f",
    "aria-label": "Other customer id",
    spellcheck: "false",
    autocomplete: "off",
  });
  other.addEventListener("change", () => {
    const id = other.value.trim();
    if (ACTOR_ID.test(id)) signIn(id);
    else toast("A customer id is 1-64 letters, digits, '-' or '_'.", "denied");
  });
  const otherRadio = h("input", {
    type: "radio",
    name: "customer",
    value: "other",
    checked: !known,
    onchange: () => {
      other.value = other.value.trim() || guestId();
      signIn(other.value);
    },
  });
  options.push(
    h("label", { class: "customer" }, otherRadio, h("span", { class: "customer-name" }, "Another customer"), other),
  );
  $("#customers").replaceChildren(...options);
}

function renderOrders() {
  const current = customer(state.actorId);
  const lines = current
    ? current.orders.map((order) =>
        h(
          "li",
          {},
          h("code", {}, order.order_id),
          h("span", {}, `${order.items.join(", ")}, ${order.status.toLowerCase()}, ${money(order.total)}`),
        ),
      )
    : [h("li", {}, "No orders. Order and refund tools refuse ids like this one, which suits memory tests.")];
  $("#orders").replaceChildren(...lines);
}

function renderSession() {
  const element = $("#session-id");
  element.textContent = state.sessionId;
  element.title = state.sessionId;
}

function newSession() {
  state.sessionId = newSessionId();
  persist();
  renderSession();
}

function signIn(actorId) {
  if (actorId === state.actorId) return;
  state.actorId = actorId;
  newSession();
  renderCustomers();
  renderOrders();
  renderToolForm();
  if (state.tab === "memory") loadMemory();
}

// Scenarios

function stepLabel(scenario, index) {
  return scenario.steps.length > 1 ? `${scenario.title}, step ${index + 1}` : scenario.title;
}

function countRuns(entries) {
  const runs = {};
  for (const scenario of state.scenarios) {
    const first = stepLabel(scenario, 0);
    runs[scenario.id] = entries.filter(
      (entry) => entry.scenario === first || entry.scenario === `${first} (edited)`,
    ).length;
  }
  return runs;
}

function renderScenarios() {
  const groups = new Map();
  for (const scenario of state.scenarios) {
    if (!groups.has(scenario.group)) groups.set(scenario.group, []);
    groups.get(scenario.group).push(scenario);
  }
  const nodes = [];
  for (const [group, scenarios] of groups) {
    nodes.push(h("h3", { class: "scenario-group" }, group));
    nodes.push(
      h(
        "ul",
        { class: "scenario-list" },
        scenarios.map((scenario) => {
          const runs = state.runs[scenario.id] || 0;
          return h(
            "li",
            {},
            h(
              "button",
              {
                class: "scenario",
                type: "button",
                "aria-pressed": String(state.scenario?.id === scenario.id),
                onclick: () => chooseScenario(scenario),
              },
              h("span", { class: "scenario-title" }, scenario.title),
              h(
                "span",
                { class: "scenario-meta" },
                h("span", {}, scenario.actor ? customerName(scenario.actor) : "New customer id"),
                scenario.steps.length > 1 ? h("span", {}, `${scenario.steps.length} steps`) : null,
                runs ? h("span", { class: "scenario-runs" }, runs === 1 ? "Ran once" : `Ran ${runs} times`) : null,
              ),
            ),
          );
        }),
      ),
    );
  }
  $("#scenario-list").replaceChildren(...nodes);
}

function chooseScenario(scenario) {
  state.scenario = scenario;
  loadStep(0);
}

function loadStep(index) {
  const { scenario } = state;
  const step = scenario.steps[index];
  state.step = index;
  if (index === 0) {
    // Every run of a scenario starts in a fresh session, so earlier turns cannot change the outcome.
    const actorId = scenario.actor ?? guestId();
    if (actorId !== state.actorId) signIn(actorId);
    else newSession();
  } else if (step.new_session) {
    newSession();
  }
  state.loadedPrompt = step.prompt;
  $("#prompt").value = step.prompt;
  renderBrief();
  renderScenarios();
  $("#prompt").focus();
  waitForMemory(step.needs_memory ? state.actorId : null);
}

let memoryPoll = 0;

// Long-term memory is extracted about a minute after a turn: Send stays disabled until a record is listed.
async function waitForMemory(actorId) {
  const poll = ++memoryPoll;
  state.memoryWait = Boolean(actorId);
  updateControls();
  while (actorId && poll === memoryPoll) {
    try {
      if ((await api(`/api/memory?actor_id=${encodeURIComponent(actorId)}`)).records.length) break;
    } catch {
      // keep waiting: the next poll may succeed
    }
    await new Promise((resolve) => setTimeout(resolve, 10000));
  }
  if (poll !== memoryPoll) return;
  state.memoryWait = false;
  updateControls();
  if (actorId) toast("Memory now holds the preference: this step is ready to send.");
}

function clearScenario() {
  waitForMemory(null);
  state.scenario = null;
  state.loadedPrompt = null;
  renderBrief();
  renderScenarios();
}

function renderBrief() {
  const brief = $("#brief");
  const { scenario } = state;
  if (!scenario) {
    brief.hidden = true;
    brief.replaceChildren();
    return;
  }
  const steps =
    scenario.steps.length > 1
      ? h(
          "div",
          { class: "brief-steps" },
          h("span", { class: "hint" }, `Step ${state.step + 1} of ${scenario.steps.length}`),
          scenario.steps.map((step, index) =>
            h(
              "button",
              {
                class: "button button-quiet",
                type: "button",
                "aria-pressed": String(index === state.step),
                onclick: () => loadStep(index),
              },
              step.new_session ? `Step ${index + 1}, new session` : `Step ${index + 1}`,
            ),
          ),
        )
      : null;
  fill(
    brief,
    h(
      "div",
      { class: "brief-head" },
      h("p", { class: "brief-title" }, scenario.title),
      h("button", { class: "text-button", type: "button", onclick: clearScenario }, "Close"),
    ),
    h("p", { class: "brief-expect" }, h("strong", {}, "Expected: "), scenario.expect),
    steps,
  );
  brief.hidden = false;
}

function scenarioTag(prompt) {
  if (!state.scenario) return null;
  const label = stepLabel(state.scenario, state.step);
  return prompt === state.loadedPrompt ? label : `${label} (edited)`;
}

// Conversation

function ensureDivider(sessionId, actorId) {
  if (state.shownSession === sessionId) return null;
  const divider = h(
    "li",
    { class: "divider" },
    h("span", {}, `New session for ${customerName(actorId)}`),
    h("code", { title: sessionId }, sessionId),
  );
  state.shownSession = sessionId;
  $("#timeline").append(divider);
  return divider;
}

function customerBubble(prompt, actorId) {
  return h(
    "div",
    { class: "bubble" },
    h("p", { class: "who" }, customerName(actorId)),
    h("p", { class: "said" }, prompt),
  );
}

function chip(tool, outcome) {
  return h("span", { class: "chip", dataset: { tone: outcome.tone } }, tool ? h("code", {}, tool) : null, outcome.label);
}

function callChips(entry) {
  const calls = entry.tool_calls_detail;
  if (calls && calls.length) return calls.map((call) => chip(call.tool, call.outcome));
  if (calls) return [chip(null, { tone: "none", label: "No tool calls" })];
  // Memory could not be read: fall back to the runtime's own counts.
  return Object.entries(entry.tool_calls || {}).map(([tool, count]) =>
    chip(tool, { tone: "none", label: `${count}×` }),
  );
}

function chatTurn(entry) {
  const reply = h(
    "div",
    {
      class: entry.error ? "reply is-error" : "reply",
      dataset: { entry: entry.id },
      onclick: (event) => {
        if (!event.target.closest("a, button")) select(entry.id);
      },
    },
    h("p", { class: "who" }, "Agent"),
    entry.error ? h("p", { class: "said" }, entry.error) : agentText(entry.response || ""),
    h(
      "div",
      { class: "reply-foot" },
      callChips(entry),
      h("span", { class: "duration" }, seconds(entry.duration_ms)),
      h("button", { class: "text-button", type: "button", onclick: () => select(entry.id) }, "Details"),
    ),
  );
  return h("li", { class: "turn" }, customerBubble(entry.prompt, entry.actor_id), reply);
}

function toolRow(entry) {
  const outcome = entry.error ? { tone: "failed", label: "Call failed" } : entry.outcome;
  return h(
    "li",
    {},
    h(
      "button",
      { class: "call-row", type: "button", dataset: { entry: entry.id }, onclick: () => select(entry.id) },
      h("span", { class: "call-label" }, "Direct tool call"),
      h("code", {}, `${entry.tool} ${JSON.stringify(entry.arguments)}`),
      chip(null, outcome),
      h("span", { class: "duration" }, `as ${entry.actor_id}`),
      h("span", { class: "duration" }, seconds(entry.duration_ms)),
    ),
  );
}

const renderEntry = (entry) => (entry.kind === "chat" ? chatTurn(entry) : toolRow(entry));

function renderTimeline() {
  $("#timeline").replaceChildren();
  state.shownSession = null;
  for (const entry of state.entries) {
    if (entry.kind === "chat") ensureDivider(entry.session_id, entry.actor_id);
    $("#timeline").append(renderEntry(entry));
  }
  updateEmpty();
  scrollTimeline();
}

function scrollTimeline() {
  const timeline = $("#timeline");
  timeline.scrollTop = timeline.scrollHeight;
}

function waitingTurn(prompt, actorId) {
  const elapsed = h("span", {}, "0 s");
  const started = performance.now();
  const timer = setInterval(() => {
    elapsed.textContent = `${Math.round((performance.now() - started) / 1000)} s`;
  }, 1000);
  const turn = h(
    "li",
    { class: "turn" },
    customerBubble(prompt, actorId),
    h(
      "div",
      { class: "reply waiting" },
      h("p", { class: "who" }, "Agent"),
      h("p", { class: "said" }, "Waiting for the agent, ", elapsed),
    ),
  );
  return { turn, stop: () => clearInterval(timer) };
}

async function sendMessage(event) {
  event.preventDefault();
  const prompt = $("#prompt").value.trim();
  if (!prompt || state.busy || state.memoryWait || !ready()) return;
  const scenario = state.scenario;
  const step = state.step;
  const tag = scenarioTag(prompt);
  const { actorId, sessionId } = state;

  const previousSession = state.shownSession;
  const divider = ensureDivider(sessionId, actorId);
  const waiting = waitingTurn(prompt, actorId);
  $("#timeline").append(waiting.turn);
  updateEmpty();
  scrollTimeline();
  $("#prompt").value = "";
  setBusy(true);
  try {
    const data = await api("/api/chat", { prompt, actor_id: actorId, session_id: sessionId, scenario: tag });
    waiting.turn.replaceWith(renderEntry(data.entry));
    state.entries.push(data.entry);
    updateRecord(data.record);
    if (scenario && step === 0) state.runs[scenario.id] = (state.runs[scenario.id] || 0) + 1;
    select(data.entry.id, { fresh: true });
    if (scenario && state.scenario === scenario && step + 1 < scenario.steps.length) loadStep(step + 1);
    else if (state.scenario === scenario) state.loadedPrompt = null;
    renderScenarios();
  } catch (error) {
    waiting.turn.remove();
    divider?.remove();
    state.shownSession = divider ? previousSession : state.shownSession;
    $("#prompt").value = prompt;
    toast(error.message, "denied");
  } finally {
    waiting.stop();
    setBusy(false);
    updateEmpty();
    scrollTimeline();
  }
}

function setBusy(busy) {
  state.busy = busy;
  updateControls();
}

// Record pane: details of the selected entry

function select(id, { fresh = false } = {}) {
  state.selectedId = id;
  for (const element of document.querySelectorAll("[data-entry]")) {
    element.setAttribute("aria-current", String(Number(element.dataset.entry) === id));
  }
  setTab("details");
  renderDetails(fresh);
}

function stamp(outcome, fresh, order = 0) {
  const element = h("span", { class: fresh ? "stamp is-new" : "stamp", dataset: { tone: outcome.tone } }, outcome.label);
  element.style.setProperty("--order", order); // staggers the stamps of one turn; CSP forbids style attributes
  return element;
}

function facts(rows) {
  return h(
    "dl",
    { class: "facts" },
    rows.filter(Boolean).map(([term, value]) => [h("dt", {}, term), h("dd", {}, value)]),
  );
}

function rawRecord(entry) {
  return h("details", {}, h("summary", {}, "Raw record"), h("pre", {}, pretty(entry)));
}

function renderDetails(fresh = false) {
  const panel = $("#panel-details");
  const entry = state.entries.find((item) => item.id === state.selectedId);
  if (!entry) {
    panel.replaceChildren(
      h(
        "p",
        { class: "panel-empty" },
        state.entries.length
          ? "Select a reply or a tool call in the conversation to see what happened."
          : "After each reply, its tool calls, their outcomes and the trace show up here.",
      ),
    );
    return;
  }
  fill(panel, entry.kind === "chat" ? chatDetails(entry, fresh) : toolDetails(entry, fresh));
}

function chatDetails(entry, fresh) {
  const counts = Object.entries(entry.tool_calls || {});
  const calls = entry.tool_calls_detail;
  const { links } = state.stack || {};
  let callSection;
  if (calls && calls.length) {
    callSection = h(
      "ol",
      { class: "calls" },
      calls.map((call, index) =>
        h(
          "li",
          { class: "call" },
          h("div", { class: "call-head" }, h("span", { class: "call-tool" }, call.tool), stamp(call.outcome, fresh, index)),
          h("code", { class: "call-args" }, JSON.stringify(call.input)),
          call.outcome.detail ? h("p", { class: "call-detail" }, call.outcome.detail) : null,
          h("details", {}, h("summary", {}, "Tool result"), h("pre", {}, pretty(readableResult(call.result)))),
        ),
      ),
    );
  } else if (calls) {
    callSection = h("p", { class: "note" }, "The agent answered without calling a tool.");
  } else {
    callSection = h("p", { class: "note" }, entry.tool_calls_note || "Tool call details are not available.");
  }
  return [
    h("h2", {}, entry.scenario || "Free-form message"),
    h("p", { class: "panel-subtitle" }, `Agent turn, entry ${entry.id} of this run`),
    entry.error ? h("p", { class: "call-detail field-error" }, entry.error) : null,
    facts([
      ["Customer", [customerName(entry.actor_id), " ", h("code", {}, entry.actor_id)]],
      ["Session", h("code", {}, entry.session_id)],
      ["Sent", clock(entry.at)],
      ["Duration", seconds(entry.duration_ms)],
      ["Runtime count", counts.length ? counts.map(([tool, count]) => `${tool} ${count}`).join(", ") : "No tools"],
      [
        "Trace",
        entry.trace_id
          ? h("a", { href: entry.trace_url, target: "_blank", rel: "noreferrer" }, h("code", {}, entry.trace_id))
          : "None returned",
      ],
    ]),
    h("h3", {}, "Tool calls"),
    callSection,
    h("h3", {}, "Logs"),
    links
      ? h(
          "div",
          { class: "links" },
          h("a", { href: links.runtime_logs, target: "_blank", rel: "noreferrer" }, "Agent runtime logs"),
          h("a", { href: links.tool_logs, target: "_blank", rel: "noreferrer" }, "Tool backend logs"),
        )
      : null,
    rawRecord(entry),
  ];
}

function toolDetails(entry, fresh) {
  return [
    h("h2", {}, "Direct tool call"),
    h("p", { class: "panel-subtitle" }, `Entry ${entry.id} of this run, without the model`),
    facts([
      ["Tool", h("code", {}, entry.tool)],
      ["Customer", [customerName(entry.actor_id), " ", h("code", {}, entry.actor_id)]],
      ["Called", clock(entry.at)],
      ["Duration", seconds(entry.duration_ms)],
    ]),
    ...toolOutcome(entry, fresh),
    rawRecord(entry),
  ];
}

function toolOutcome(entry, fresh) {
  if (entry.error) return [h("h3", {}, "Outcome"), h("p", { class: "field-error" }, entry.error)];
  return [
    h("h3", {}, "Outcome"),
    h("div", { class: "call-head" }, stamp(entry.outcome, fresh)),
    entry.outcome.detail ? h("p", { class: "call-detail" }, entry.outcome.detail) : null,
    h("h3", {}, "Arguments"),
    h("pre", {}, pretty(entry.arguments)),
    h("h3", {}, "Result"),
    h("pre", {}, pretty(readableResult(entry.result))),
  ];
}

// Record pane: tabs, direct tool calls, memory

const TABS = ["details", "tool", "memory"];

function setTab(name) {
  state.tab = name;
  for (const tab of TABS) {
    const button = $(`#tab-${tab}`);
    button.setAttribute("aria-selected", String(tab === name));
    button.tabIndex = tab === name ? 0 : -1;
    $(`#panel-${tab}`).hidden = tab !== name;
  }
  if (name === "memory") loadMemory();
}

function buildToolOptions() {
  $("#tool-name").replaceChildren(...Object.keys(state.tools).map((name) => h("option", { value: name }, name)));
}

function toolTemplate(name) {
  const args = { ...state.tools[name] };
  const current = customer(state.actorId);
  if ("customer_id" in args) args.customer_id = current ? current.id : state.actorId;
  if ("order_id" in args && current) args.order_id = current.orders.at(-1).order_id;
  if ("idempotency_key" in args) args.idempotency_key = `console-${crypto.randomUUID().slice(0, 8)}`;
  return pretty(args);
}

function renderToolForm() {
  $("#tool-actor").textContent = `${customerName(state.actorId)} (${state.actorId})`;
  if ($("#tool-name").value) $("#tool-args").value = toolTemplate($("#tool-name").value);
}

function showToolError(message) {
  const element = $("#tool-error");
  element.textContent = message || "";
  element.hidden = !message;
}

async function callTool(event) {
  event.preventDefault();
  if (state.busy || !ready()) return;
  let args;
  try {
    args = JSON.parse($("#tool-args").value);
  } catch {
    showToolError('Arguments must be valid JSON, e.g. {"order_id": "123"}.');
    return;
  }
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    showToolError("Arguments must be a JSON object.");
    return;
  }
  showToolError(null);
  setBusy(true);
  $("#tool-submit").textContent = "Calling…";
  try {
    const data = await api("/api/tool", { tool: $("#tool-name").value, arguments: args, actor_id: state.actorId });
    state.entries.push(data.entry);
    $("#timeline").append(renderEntry(data.entry));
    updateEmpty();
    scrollTimeline();
    updateRecord(data.record);
    state.selectedId = data.entry.id;
    for (const element of document.querySelectorAll("[data-entry]")) {
      element.setAttribute("aria-current", String(Number(element.dataset.entry) === data.entry.id));
    }
    fill($("#tool-result"), toolOutcome(data.entry, true));
  } catch (error) {
    showToolError(error.message);
  } finally {
    $("#tool-submit").textContent = "Call tool";
    setBusy(false);
  }
}

async function loadMemory() {
  const list = $("#memory-list");
  const actorId = state.actorId;
  $("#memory-title").textContent = `Remembered about ${customerName(actorId)}`;
  if (!ready()) {
    list.replaceChildren(h("p", { class: "panel-empty" }, "Memory can be read once the stack is deployed."));
    return;
  }
  list.replaceChildren(h("p", { class: "panel-empty" }, "Loading…"));
  try {
    const data = await api(`/api/memory?actor_id=${encodeURIComponent(actorId)}`);
    if (actorId !== state.actorId) return; // signed in as someone else meanwhile
    list.replaceChildren(
      data.records.length
        ? h(
            "ul",
            { class: "memory-records" },
            data.records.map((record) =>
              h(
                "li",
                {},
                h("time", { datetime: record.created_at }, new Date(record.created_at).toLocaleString()),
                record.text,
              ),
            ),
          )
        : h("p", { class: "panel-empty" }, "Nothing remembered yet."),
    );
  } catch (error) {
    list.replaceChildren(h("p", { class: "field-error" }, error.message));
  }
}

async function saveEvidence() {
  const button = $("#save-evidence");
  button.disabled = true;
  try {
    const data = await api("/api/evidence", {});
    updateRecord(data.record);
    toast(`Saved \`${data.paths[0]}\` and \`${data.paths[1]}\`.`);
  } catch (error) {
    button.disabled = false;
    toast(error.message, "denied");
  }
}

// Wiring

function bindEvents() {
  $("#composer").addEventListener("submit", sendMessage);
  $("#prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#new-session").addEventListener("click", newSession);
  $("#recheck").addEventListener("click", loadStatus);
  $("#save-evidence").addEventListener("click", saveEvidence);
  $("#tool-form").addEventListener("submit", callTool);
  $("#tool-name").addEventListener("change", () => ($("#tool-args").value = toolTemplate($("#tool-name").value)));
  $("#memory-refresh").addEventListener("click", loadMemory);
  for (const tab of TABS) $(`#tab-${tab}`).addEventListener("click", () => setTab(tab));
  $(".tabs").addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const next = TABS[(TABS.indexOf(state.tab) + step + TABS.length) % TABS.length];
    setTab(next);
    $(`#tab-${next}`).focus();
  });
}

restoreSignIn();
bindEvents();
renderSession();
renderDetails();
loadStatus();
