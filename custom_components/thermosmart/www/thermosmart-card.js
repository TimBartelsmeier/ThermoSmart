/* Bundled ThermoSmart dashboard cards. No external runtime dependencies. */
const CARD_VERSION = '2.0.1';
const PRESETS = [
  ["comfort", "mdi:white-balance-sunny"],
  ["eco", "mdi:leaf"],
  ["sleep", "mdi:weather-night"],
  ["away", "mdi:home-export-outline"],
  ["vacation", "mdi:airplane"],
];
const TRANSLATION_PREFIX = "component.thermosmart.card.";
const translationRequests = new WeakMap();

function cardLanguage(hass) {
  return hass?.language || hass?.locale?.language || "en";
}

function loadTranslations(hass, language) {
  const connection = hass.connection || hass;
  if (!translationRequests.has(connection)) translationRequests.set(connection, new Map());
  const cache = translationRequests.get(connection);
  if (!cache.has(language)) {
    const fetch = async lang => {
      const { resources } = await hass.callWS({
        type: "frontend/get_translations", language: lang, category: "card", integration: ["thermosmart"],
      });
      if (!resources?.[`${TRANSLATION_PREFIX}loading`]) {
        throw new Error("Home Assistant returned no ThermoSmart card translations");
      }
      return resources;
    };
    cache.set(language, fetch(language).catch(error => {
      if (language === "en") throw error;
      console.error(`ThermoSmart translations for ${language} failed; loading English`, error);
      return loadTranslations(hass, "en");
    }));
  }
  return cache.get(language);
}

class ThermoSmartLocalized extends HTMLElement {
  _setHass(hass) {
    this._hass = hass;
    const language = cardLanguage(hass);
    if (language === this._translationLanguage) return;
    this._translationLanguage = language;
    loadTranslations(hass, language).then(resources => {
      if (this._translationLanguage !== language) return;
      this._translations = resources;
      this._translationError = "";
      for (const [type, name, description] of [
        ["thermosmart-zone-card", "zone_card", "zone_description"],
        ["thermosmart-zone-schedule-card", "schedule_card", "schedule_description"],
      ]) {
        const card = window.customCards?.find(item => item.type === type);
        if (card) Object.assign(card, { name: this._t(name), description: this._t(description) });
      }
      this._translationsChanged();
    }, error => {
      if (this._translationLanguage !== language) return;
      this._translationError = `ThermoSmart translations could not load: ${errorText(error)}`;
      console.error(this._translationError, error);
      this._translationsChanged();
    });
  }

  _t(key, values = {}) {
    if (!this._translations) return "";
    const text = this._translations[`${TRANSLATION_PREFIX}${key}`];
    if (typeof text !== "string") {
      console.error(`Missing ThermoSmart card translation: ${key}`);
      return `Missing translation: ${key}`;
    }
    return text.replace(/\{(\w+)\}/g, (match, name) => values[name] ?? match);
  }

  _text(tag, key, className, values = {}) {
    const node = element(tag, this._t(key, values), className);
    node.dataset.translation = key;
    node.translationValues = values;
    return node;
  }

  _translateNodes(root) {
    for (const node of root.querySelectorAll("[data-translation]")) {
      node.textContent = this._t(node.dataset.translation, node.translationValues);
    }
  }
}
const CSS = `
  :host { display:block; }
  ha-card { --ha-card-header-font-size:var(--ha-font-size-xl,20px); }
  ha-expansion-panel { margin:12px 16px; background:var(--secondary-background-color);
    border-radius:var(--ha-card-border-radius,12px); }
  ha-expansion-panel[hidden] { display:none; }
  .body { padding:16px; }
  .status { display:flex; align-items:center; justify-content:center; gap:8px;
    min-height:64px; text-align:center; font-size:var(--ha-font-size-l,16px);
    font-weight:var(--ha-font-weight-medium,500); line-height:1.4; }
  .temperatures,.event-columns { display:grid; grid-template-columns:1fr 1fr; align-items:center; gap:8px; }
  .temperatures { grid-template-columns:minmax(0,1fr) minmax(0,1.4fr); margin:8px 0; text-align:center; }
  .label,.secondary { color:var(--secondary-text-color); }
  .label { font-size:var(--ha-font-size-s,12px); }
  .actual { font-size:var(--ha-font-size-3xl,32px); line-height:1.4; }
  .target { display:flex; align-items:center; justify-content:center; gap:12px; }
  .target ha-icon-button { --ha-icon-button-size:40px; --mdc-icon-button-size:40px; flex:0 0 40px;
    background:var(--secondary-background-color); border-radius:50%; }
  .target-value { font-size:var(--ha-font-size-xl,24px); white-space:nowrap; }
  .presets { display:flex; flex-wrap:wrap; justify-content:center; gap:4px; margin-top:12px; }
  .temperatures + .presets { margin-top:24px; }
  .presets ha-button { flex:0 0 88px; width:88px; min-width:0; }
  .presets ha-button::part(base),.actions ha-button::part(base) { width:100%; box-sizing:border-box; }
  .preset-content { display:flex; flex-direction:column; align-items:center; gap:4px; }
  ha-button[variant="neutral"] { --primary-color:var(--primary-text-color);
    --mdc-theme-primary:var(--primary-text-color); }
  ha-button[aria-pressed="true"] { color:var(--zone-color);
    --primary-color:var(--zone-color); --mdc-theme-primary:var(--zone-color);
    --wa-color-on-normal:var(--zone-color); }
  .dial { position:relative; max-width:320px; margin:0 auto; }
  .dial ha-control-circular-slider { width:100%; margin-bottom:-32px;
    --control-circular-slider-color:var(--zone-color); }
  .dial-center { position:absolute; inset:0 20% 0; display:flex; flex-direction:column;
    justify-content:center; text-align:center; pointer-events:none; }
  .dial .status { flex-direction:column; min-height:80px; gap:4px; }
  .dial .status ha-icon { --mdc-icon-size:32px; }
  .center { text-align:center; }
  .summary,.note { display:flex; align-items:center; gap:8px; }
  .note { margin-top:12px; align-items:flex-start; }
  .day { margin:16px 0 8px; font-size:var(--ha-font-size-l,16px);
    text-align:center; font-weight:var(--ha-font-weight-bold,700); }
  .event { padding:4px 0; }
  .event.next { background:color-mix(in srgb,var(--primary-color) 18%,var(--secondary-background-color));
    border-radius:var(--ha-card-border-radius,12px); border-bottom:0; }
  .event-columns { grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px;
    min-height:32px; font-size:var(--ha-font-size-m,14px); }
  .event-time { text-align:right; }
  .event-target { text-align:left; font-weight:var(--ha-font-weight-normal,400); }
  .event-detail { color:var(--secondary-text-color); margin-inline-start:8px; }
  .next-label,.preheat { text-align:center; font-size:var(--ha-font-size-m,14px);
    color:var(--secondary-text-color); margin-top:4px; }
  .next-label { color:var(--primary-color); }
  .row { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:48px; }
  .shortcuts { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:12px; }
  .actions { display:grid; grid-template-columns:1fr; gap:8px; margin-top:12px; }
  .actions ha-button { width:100%; }
  .feedback { min-height:24px; margin-top:8px; color:var(--error-color); font-size:var(--ha-font-size-m,14px); }
  .message { padding:16px; }
  .history-card { cursor:pointer; }
  .history-card:focus-visible { outline:2px solid var(--primary-color); }
  .sparkline { display:block; width:100%; }
  .spark-legend { display:flex; justify-content:center; flex-wrap:wrap; gap:16px;
    color:var(--secondary-text-color); font-size:var(--ha-font-size-s,12px); margin-top:4px; }
  .spark-legend span { display:inline-flex; align-items:center; gap:4px; }
`;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function icon(name) {
  const node = element("ha-icon");
  node.icon = name;
  return node;
}
function svgElement(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}
function openHistory(entityId) {
  window.history.pushState(null, "", `/history?entity_id=${encodeURIComponent(entityId)}`);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
}
function pastSuspensionDeadline() {
  return new Date(Date.now() - 60000).toISOString();
}
function positiveLimit(value, name) {
  if (value == null) return null;
  if (!Number.isInteger(value) || value < 1) throw new Error(`${name} must be a positive integer or null`);
  return value;
}
function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function available(state) {
  return state && !["unknown", "unavailable"].includes(state.state);
}
function errorText(error) {
  return error?.message || String(error);
}
function statusFor(state) {
  const a = state.attributes;
  if (a.window_open) return ["window", "mdi:window-open-variant"];
  if (a.summer_mode) return ["summer", "mdi:white-balance-sunny"];
  if (a.vacation_mode) return ["vacation", "mdi:airplane"];
  if (a.observation_mode) return ["observation", "mdi:eye"];
  if (a.preheat_active) return ["preheat", "mdi:radiator"];
  if (a.hvac_action === "heating") return ["heating", "mdi:fire"];
  return ["holding", "mdi:thermometer-check"];
}
function locked(state) {
  return !available(state) || state.attributes.summer_mode
    || state.attributes.vacation_mode || state.attributes.observation_mode;
}
function waitForElements(names, localize) {
  let timer;
  return Promise.race([
    Promise.all(names.map(name => customElements.whenDefined(name))),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(localize("components_unavailable", {
        components: names.join(", "),
      }))), 15000);
    }),
  ]).finally(() => clearTimeout(timer));
}

class ThermoSmartBase extends ThermoSmartLocalized {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._error = "";
    this._actionError = "";
    this._busy = false;
    this._generation = 0;
  }

  setConfig(config) {
    if (!config?.entity?.startsWith("climate.")) throw new Error("entity must be a ThermoSmart climate entity");
    this._config = { ...config };
    this._generation++;
    this._data = null;
    this._signature = null;
    this._error = "";
    this._actionError = "";
    this._render();
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._setHass(hass);
    if (this._config) this._update();
  }

  _translationsChanged() {
    this._signature = null;
    if (this._config) this._update();
  }

  connectedCallback() {
    if (this._hass && this._config) {
      this._signature = null;
      this._update();
    }
  }

  disconnectedCallback() {
    clearTimeout(this._timer);
    this._timer = null;
    this._generation++;
    this._signature = null;
  }

  getCardSize() { return 3; }
  getGridOptions() { return { columns: 12, rows: "auto", min_columns: 6 }; }
  get _state() { return this._hass?.states[this._config?.entity]; }

  _temperature(value) {
    const n = number(value);
    if (n === null) return "—";
    const language = cardLanguage(this._hass);
    const unit = this._hass?.config?.unit_system?.temperature || "°C";
    return `${new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(n)} ${unit}`;
  }

  _date(value, options) {
    return new Intl.DateTimeFormat(cardLanguage(this._hass), {
      timeZone: this._data?.time_zone || this._hass.config.time_zone, ...options,
    }).format(new Date(value));
  }

  _button(label, action, { disabled = false, selected = false, symbol } = {}) {
    const b = element("ha-button");
    b.setAttribute("variant", "neutral");
    b.setAttribute("appearance", "plain");
    b.disabled = disabled || this._busy;
    b.accessibleLabel = label;
    if (symbol) {
      const content = element("span", undefined, "preset-content");
      content.append(icon(symbol), element("span", label));
      b.append(content);
      b.setAttribute("aria-pressed", String(selected));
    } else b.textContent = label;
    if (action) b.addEventListener("click", () => this._action(action));
    return b;
  }

  async _action(action) {
    if (this._busy) return;
    this._busy = true;
    this._actionError = "";
    this._render();
    try {
      await action();
    } catch (error) {
      this._actionError = errorText(error);
      console.error("ThermoSmart card action failed", error);
    } finally {
      this._busy = false;
      this._signature = null;
      this._update();
    }
  }

  _frame() {
    const card = element("div");
    const body = element("div", undefined, "body");
    card.append(body);
    this.shadowRoot.replaceChildren(element("style", CSS), card);
    return body;
  }

  _feedback(body) {
    const feedback = element("div", this._translationError || this._error || this._actionError, "feedback");
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    body.append(feedback);
  }

  _render() {
    const body = this._frame();
    body.append(element("div", this._translationError || this._error || this._t("loading")));
  }

  async _helpers() {
    if (!window.loadCardHelpers) throw new Error(this._t("helpers_unavailable"));
    return window.loadCardHelpers();
  }

  async _loadComponents() {
    if (this._componentsReady) return;
    const helpers = await this._helpers();
    helpers.createCardElement({ type: "thermostat", entity: this._config.entity });
    if (this.constructor.kind === "schedule-controls") {
      helpers.createRowElement({ entity: "datetime.thermosmart_component_loader" });
    }
    const required = ["ha-card", "ha-icon", "ha-icon-button", "ha-button"];
    if (this.constructor.kind === "zone-control" && this._config.layout === "full") {
      required.push("ha-control-circular-slider");
    }
    if (this.constructor.kind === "schedule-controls") required.push("ha-switch", "ha-date-input", "ha-time-input");
    await waitForElements(required, this._t.bind(this));
    this._componentsReady = true;
  }

  _update() {
    const kind = this.constructor.kind;
    const keys = kind === "temperature-history" ? ["actual_temperature", "desired_temperature"]
      : kind === "schedule-controls" ? ["scheduling_enabled", "scheduling_disabled_until"]
      : ["scheduling_enabled", "scheduling_disabled_until", "next_scheduled_event", "preheat_start"];
    const state = kind === "zone-control" ? this._state : {
      state: this._state?.state,
      summer: kind === "schedule-plan" && this._state?.attributes.summer_mode,
      vacation: kind === "schedule-plan" && this._state?.attributes.vacation_mode,
    };
    const signature = JSON.stringify([
      state,
      keys.map(key => this._hass.states[this._data?.entities[key]]),
      this._hass.locale, cardLanguage(this._hass), this._hass.config.unit_system,
    ]);
    if (signature === this._signature) return;
    this._signature = signature;
    this._render();
    this._refresh();
  }

  async _refresh() {
    const generation = ++this._generation;
    try {
      await loadTranslations(this._hass, cardLanguage(this._hass));
      await this._loadComponents();
      if (this.constructor.kind !== "zone-control") {
        const data = await this._hass.callWS({
          type: "thermosmart/dashboard", entity_id: this._config.entity,
          max_days: this._config["max-days"] ?? null,
          max_events: this._config["max-events"] ?? null,
        });
        if (generation !== this._generation) return;
        this._data = data;
      }
      if (generation !== this._generation) return;
      this._error = "";
    } catch (error) {
      if (generation !== this._generation) return;
      this._data = null;
      this._error = errorText(error);
      console.error("ThermoSmart card loading failed", error);
    }
    if (generation !== this._generation) return;
    this._render();
    if (this.constructor.kind === "schedule-plan" && this.isConnected && this._data) {
      clearTimeout(this._timer);
      const until = Date.parse(this._data?.disabled_until);
      const delay = until > Date.now() ? Math.min(60000, until - Date.now() + 50) : 60000;
      this._timer = setTimeout(() => this._refresh(), delay);
    }
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find(id => id.startsWith("climate.")
      && "observation_mode" in hass.states[id].attributes);
    const config = { entity };
    if (this.kind === "zone-schedule-card") config["max-events"] = 5;
    return config;
  }

  static async getConfigElement() {
    const helpers = await window.loadCardHelpers();
    const native = helpers.createCardElement({ type: "thermostat", entity: "climate.placeholder" });
    await native.constructor.getConfigElement();
    const editor = element("thermosmart-card-editor");
    editor.kind = this.kind;
    return editor;
  }
}

class ThermoSmartZoneControl extends ThermoSmartBase {
  static kind = "zone-control";

  setConfig(config) {
    if (config.layout != null && !["compact", "full"].includes(config.layout)) {
      throw new Error("layout must be compact or full");
    }
    this._dragging = false;
    super.setConfig(config);
  }

  getCardSize() { return this._config?.layout === "full" ? 6 : 3; }

  disconnectedCallback() {
    this._dragging = false;
    super.disconnectedCallback();
  }

  _update() {
    if (!this._dragging) super._update();
  }

  _ring({ value, current, min, max, step, disabled, target }) {
    const ring = element("ha-control-circular-slider");
    Object.assign(ring, {
      value, current: current ?? undefined, min, max, step, readonly: disabled,
      mode: "start", label: this._t("target"), formatValue: temperature => this._temperature(temperature),
    });
    ring.addEventListener("value-changing", ev => {
      if (disabled) return;
      const next = number(ev.detail.value);
      this._dragging = next !== null;
      target.querySelector(".target-value").textContent = this._temperature(next ?? value);
    });
    ring.addEventListener("value-changed", ev => {
      this._dragging = false;
      if (disabled) return;
      const next = number(ev.detail.value);
      if (next !== null) this._action(() => this._setTemperature(next));
    });
    return ring;
  }

  _render() {
    const body = this._frame();
    const state = this._state;
    if (!available(state)) {
      body.append(element("div", this._t(state ? "zone_unavailable" : "zone_not_found"), "status"));
      this._feedback(body);
      return;
    }
    const a = state.attributes;
    if (!("observation_mode" in a)) {
      body.append(element("div", this._t("select_zone"), "status"));
      return;
    }
    const disabled = locked(state);
    const overridden = disabled || a.window_open;
    body.style.setProperty("--zone-color", overridden ? "var(--disabled-text-color)"
      : a.hvac_action === "heating" ? "var(--orange-color,#ff9800)" : "var(--blue-color,#2196f3)");
    const [text, symbol] = statusFor(state);
    const status = element("div", undefined, "status");
    status.append(icon(symbol), element("span", this._t(`status.${text}`)));
    const target = element("div", undefined, "target");
    const step = number(a.target_temp_step) ?? 0.5;
    const min = number(a.min_temp);
    const max = number(a.max_temp);
    const value = number(a.temperature);
    for (const delta of [-step, step]) {
      if (delta > 0) target.append(element("span", this._temperature(value), "target-value"));
      const button = element("ha-icon-button");
      button.append(icon(delta < 0 ? "mdi:minus" : "mdi:plus"));
      button.label = this._t(delta < 0 ? "decrease_target" : "increase_target");
      const next = value === null ? null : Math.round((value + delta) * 1000) / 1000;
      button.disabled = disabled || this._busy || next === null || min === null || max === null || next < min || next > max;
      button.addEventListener("click", () => this._action(() => this._setTemperature(next)));
      target.append(button);
    }
    if (this._config.layout === "full") {
      const dial = element("div", undefined, "dial");
      const slider = this._ring({
        value: value ?? min ?? 5, current: number(a.current_temperature),
        min: min ?? 5, max: max ?? 30, step, target,
        disabled: disabled || this._busy || value === null || min === null || max === null,
      });
      const center = element("div", undefined, "dial-center");
      center.append(status, element("div", `${this._t("current")}:`, "label"),
        element("div", this._temperature(a.current_temperature), "actual"));
      dial.append(slider, center);
      body.append(dial, element("div", `${this._t("target")}:`, "label center"), target);
    } else {
      const temperatures = element("div", undefined, "temperatures");
      const actual = element("div");
      actual.append(element("div", this._t("current"), "label"),
        element("div", this._temperature(a.current_temperature), "actual"));
      const desired = element("div");
      desired.append(element("div", this._t("target"), "label"), target);
      temperatures.append(actual, desired);
      body.append(status, temperatures);
    }
    const presets = element("div", undefined, "presets");
    for (const [preset, symbolName] of PRESETS) {
      if (!a.preset_modes?.includes(preset)) continue;
      presets.append(this._button(this._t(`preset.${preset}`), () => {
        if (locked(this._state)) throw new Error(this._t("controls_disabled"));
        return this._hass.callService("climate", "set_preset_mode", {
          entity_id: this._config.entity, preset_mode: preset,
        });
      }, { disabled, selected: a.preset_mode === preset, symbol: symbolName }));
    }
    body.append(presets);
    this._feedback(body);
  }

  _setTemperature(value) {
    if (locked(this._state)) throw new Error(this._t("controls_disabled"));
    return this._hass.callService("climate", "set_temperature", {
      entity_id: this._config.entity, temperature: value,
    });
  }
}

class ThermoSmartSchedulePlan extends ThermoSmartBase {
  static kind = "schedule-plan";

  setConfig(config) {
    positiveLimit(config["max-days"], "max-days");
    positiveLimit(config["max-events"], "max-events");
    super.setConfig(config);
  }

  _preheatText() {
    const plan = this._data.preheat;
    if (plan.state === "active") return this._t("preheat.active");
    if (plan.state === "planned" && plan.start) {
      return this._t("preheat.planned", { time: this._date(plan.start, { dateStyle: "medium", timeStyle: "short" }) });
    }
    if (plan.state === "suspended") return this._t("preheat.suspended");
    if (plan.reason === "cancelled_by_manual_change") return this._t("preheat.cancelled");
    if (plan.state === "not_calculated") return this._t("preheat.not_calculated");
    if (plan.reason === "no_room_temperature") return this._t("preheat.unavailable");
    return this._t("preheat.none");
  }

  _dayLabel(value, now = Date.now()) {
    const formatter = new Intl.DateTimeFormat("en", {
      timeZone: this._data.time_zone, year: "numeric", month: "numeric", day: "numeric",
    });
    const calendarDay = timestamp => {
      const parts = Object.fromEntries(formatter.formatToParts(new Date(timestamp)).map(p => [p.type, p.value]));
      return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day));
    };
    const difference = calendarDay(value) - calendarDay(now);
    if (difference === 0) return this._t("schedule.today");
    if (difference === 86400000) return this._t("schedule.tomorrow");
    return this._date(value, { weekday: "long", month: "short", day: "numeric" });
  }

  _render() {
    if (!this._data) return super._render();
    const body = this._frame();
    const data = this._data;
    const paused = Date.parse(data.disabled_until) > Date.now();
    if (!data.scheduling_enabled || paused) {
      const text = !data.scheduling_enabled ? this._t("schedule.disabled")
        : this._t("schedule.disabled_until", { time: this._date(data.disabled_until, { dateStyle: "medium", timeStyle: "short" }) });
      const summary = element("div", undefined, "summary");
      summary.append(icon(data.scheduling_enabled ? "mdi:calendar-clock" : "mdi:calendar-remove"), element("span", text));
      body.append(summary);
    }
    const attrs = this._state?.attributes || {};
    const mode = attrs.summer_mode ? "summer" : attrs.vacation_mode ? "vacation" : null;
    if (mode) {
      const note = element("div", undefined, "note");
      note.append(icon(mode === "summer" ? "mdi:white-balance-sunny" : "mdi:airplane"),
        element("span", this._t(`schedule.${mode}_${data.scheduling_enabled ? "enabled" : "disabled"}`)));
      body.append(note);
    }
    if (data.schedule_error) body.append(element("div", data.schedule_error, "feedback"));
    if (this._config["max-days"] == null && this._config["max-events"] == null) {
      body.append(element("div", this._t("schedule.set_limits"), "note"));
    } else if (data.scheduling_enabled && !data.events.length && !data.schedule_error) {
      body.append(element("div", this._t("schedule.no_events"), "note"));
    }
    let previousDay;
    data.events.forEach((event, index) => {
      const day = this._dayLabel(event.at);
      if (day !== previousDay) {
        body.append(element("div", day, "day"));
        previousDay = day;
      }
      const row = element("div", undefined, `event${index === 0 ? " next" : ""}`);
      const columns = element("div", undefined, "event-columns");
      columns.append(element("div", this._date(event.at, { hour: "numeric", minute: "2-digit" }), "event-time"));
      const target = element("div", undefined, "event-target");
      const preset = PRESETS.find(([key]) => key === (event.preset === "night" ? "sleep" : event.preset));
      if (preset) {
        target.append(element("span", this._t(`preset.${preset[0]}`)), document.createTextNode(" "),
          element("span", `(${this._scheduleTemperature(event.temperature)})`, "event-detail"));
      } else target.textContent = this._scheduleTemperature(event.temperature);
      columns.append(target);
      row.append(columns);
      if (index === 0) {
        row.append(element("div", this._t("schedule.next_event"), "next-label"),
          element("div", this._preheatText(), "preheat"));
      }
      body.append(row);
    });
    if (this._error || this._translationError) this._feedback(body);
  }

  _scheduleTemperature(celsius) {
    const fahrenheit = this._hass.config.unit_system.temperature === "°F";
    return this._temperature(fahrenheit ? celsius * 9 / 5 + 32 : celsius);
  }
}

class ThermoSmartScheduleControls extends ThermoSmartBase {
  static kind = "schedule-controls";

  _button(...args) {
    const button = super._button(...args);
    button.setAttribute("appearance", "filled");
    return button;
  }

  _inputValue(part) {
    if (!this._data.disabled_until) return "";
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: this._data.time_zone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date(this._data.disabled_until));
    const values = Object.fromEntries(parts.map(p => [p.type, p.value]));
    return part === "date" ? `${values.year}-${values.month}-${values.day}`
      : `${values.hour}:${values.minute}:${values.second}`;
  }

  _render() {
    if (!this._data) return super._render();
    const body = this._frame();
    const entities = this._data.entities;
    const enabledState = this._hass.states[entities.scheduling_enabled];
    const untilState = this._hass.states[entities.scheduling_disabled_until];
    const row = element("div", undefined, "row");
    const toggle = element("ha-switch");
    toggle.checked = enabledState?.state === "on";
    toggle.disabled = !available(enabledState) || this._busy;
    toggle.setAttribute("aria-label", this._t("schedule.enable"));
    toggle.addEventListener("change", () => this._action(() => this._hass.callService(
      "switch", toggle.checked ? "turn_on" : "turn_off", { entity_id: entities.scheduling_enabled }
    )));
    row.append(element("span", this._t("schedule.enable")), toggle);
    body.append(row);
    // An unset DateTimeEntity is unknown, not an unavailable input.
    const disabled = !untilState || untilState.state === "unavailable" || this._busy;
    const deadline = this._data.disabled_until
      ? this._date(this._data.disabled_until, { dateStyle: "medium", timeStyle: "short" }) : this._t("schedule.not_set");
    const actions = element("div", undefined, "actions");
    const custom = this._button(this._t("schedule.suspend_until", { time: deadline }), () => {
      this.dispatchEvent(new CustomEvent("show-dialog", {
        detail: {
          dialogTag: "thermosmart-suspension-dialog",
          dialogImport: () => Promise.resolve(),
          dialogParams: {
            entity: entities.scheduling_disabled_until, timeZone: this._data.time_zone,
            value: this._data.disabled_until ? `${this._inputValue("date")}T${this._inputValue("time")}` : "",
          },
        }, bubbles: true, composed: true,
      }));
    }, { disabled });
    const remove = this._button(this._t("schedule.remove_suspension"), () => this._hass.callService(
      "datetime", "set_value", {
        entity_id: entities.scheduling_disabled_until, datetime: pastSuspensionDeadline(),
      }
    ), { disabled });
    actions.append(custom, remove);
    body.append(actions);
    const shortcuts = element("div", undefined, "shortcuts");
    for (const action of ["skip_next", "midnight", "12h", "24h"]) {
      shortcuts.append(this._button(this._t(`schedule.${action}`), () => this._hass.callWS({
        type: "thermosmart/suspend_schedule", entity_id: this._config.entity, action,
      }), { disabled: disabled || !toggle.checked }));
    }
    body.append(shortcuts);
    if (!enabledState || !untilState) this._error = this._t("schedule.entities_missing");
    this._feedback(body);
  }
}

class ThermoSmartSuspensionDialog extends ThermoSmartLocalized {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) { this._setHass(hass); }

  _translationsChanged() {
    this._translateNodes(this.shadowRoot);
    const feedback = this.shadowRoot.querySelector(".feedback");
    if (feedback && this._translationError) feedback.textContent = this._translationError;
  }

  showDialog(params) {
    this._params = params;
    this._render();
    if (this.isConnected) this._dialog.showModal();
  }

  connectedCallback() {
    if (this._dialog && !this._dialog.open) this._dialog.showModal();
  }

  closeDialog() {
    if (this._saving) return false;
    this._dialog?.close();
    this.dispatchEvent(new CustomEvent("dialog-closed", {
      detail: { dialog: this.localName }, bubbles: true, composed: true,
    }));
    return true;
  }

  _render() {
    const style = element("style", `
      dialog { color:var(--primary-text-color); background:var(--ha-card-background,var(--card-background-color,#fff));
        border:0; border-radius:var(--ha-card-border-radius,12px); padding:24px;
        width:360px; max-width:calc(100vw - 64px); font-family:inherit; }
      dialog::backdrop { background:rgba(0,0,0,.5); }
      h2 { margin:0 0 16px; font-size:var(--ha-font-size-xl,24px); }
      label,input { display:block; }
      input { box-sizing:border-box; width:100%; margin-top:8px; padding:12px;
        color:var(--primary-text-color); background:var(--secondary-background-color);
        border:1px solid var(--divider-color); border-radius:4px; font:inherit; color-scheme:light dark; }
      .secondary { color:var(--secondary-text-color); margin-top:8px; }
      .feedback { color:var(--error-color); min-height:24px; margin-top:8px; }
      .actions { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
      .remove { display:block; width:100%; margin-top:12px; }
    `);
    const dialog = element("dialog");
    this._dialog = dialog;
    const heading = this._text("h2", "dialog.title");
    heading.id = "heading";
    dialog.setAttribute("aria-labelledby", "heading");
    const label = element("label");
    label.append(this._text("span", "dialog.date_time"));
    const input = element("input");
    input.type = "datetime-local";
    input.step = "1";
    input.required = true;
    input.value = this._params.value;
    label.append(input);
    const feedback = element("div", this._translationError || "", "feedback");
    feedback.setAttribute("role", "alert");
    const actions = element("div", undefined, "actions");
    const cancel = this._text("ha-button", "dialog.cancel");
    cancel.setAttribute("appearance", "plain");
    cancel.addEventListener("click", () => this.closeDialog());
    const save = this._text("ha-button", "dialog.save");
    const remove = this._text("ha-button", "schedule.remove_suspension", "remove");
    remove.setAttribute("variant", "neutral");
    remove.setAttribute("appearance", "filled");
    save.disabled = !input.value;
    input.addEventListener("input", () => { save.disabled = !input.validity.valid; });
    const submit = async value => {
      if (this._saving) return;
      this._saving = true;
      input.disabled = save.disabled = cancel.disabled = remove.disabled = true;
      feedback.textContent = "";
      try {
        await this._hass.callService("datetime", "set_value", {
          entity_id: this._params.entity, datetime: value,
        });
        this._saving = false;
        this.closeDialog();
      } catch (error) {
        feedback.textContent = errorText(error);
        console.error("ThermoSmart suspension dialog save failed", error);
      } finally {
        this._saving = false;
        input.disabled = cancel.disabled = remove.disabled = false;
        save.disabled = !input.validity.valid;
      }
    };
    save.addEventListener("click", () => {
      if (input.reportValidity()) submit(input.value);
    });
    remove.addEventListener("click", () => submit(pastSuspensionDeadline()));
    dialog.addEventListener("cancel", ev => { ev.preventDefault(); this.closeDialog(); });
    actions.append(cancel, save);
    dialog.append(heading, label, this._text("div", "dialog.time_zone", "secondary", {
      zone: this._params.timeZone,
    }), feedback, actions, remove);
    this.shadowRoot.replaceChildren(style, dialog);
  }
}

class ThermoSmartTemperatureHistory extends ThermoSmartBase {
  static kind = "temperature-history";

  disconnectedCallback() {
    super.disconnectedCallback();
    this._historyRequest = null;
  }

  setConfig(config) {
    if (config.hours_to_show != null && (!Number.isFinite(config.hours_to_show) || config.hours_to_show <= 0)) {
      throw new Error("hours_to_show must be a positive number");
    }
    this._history = null;
    this._historyFetchedAt = 0;
    this._historyRequest = null;
    super.setConfig(config);
  }

  getCardSize() { return 3; }

  async _refresh() {
    if (this._historyRequest) return;
    if (this._history && Date.now() - this._historyFetchedAt < 300000) {
      this._render();
      this._scheduleHistoryRefresh();
      return;
    }
    const generation = this._generation;
    const request = {};
    this._historyRequest = request;
    const end = Date.now();
    const start = end - (this._config.hours_to_show ?? 24) * 3600000;
    try {
      await loadTranslations(this._hass, cardLanguage(this._hass));
      await this._loadComponents();
      const response = await this._hass.callApi("GET",
        `history/period/${new Date(start).toISOString()}?filter_entity_id=${encodeURIComponent(this._config.entity)}&end_time=${encodeURIComponent(new Date(end).toISOString())}&significant_changes_only=false`
      );
      if (generation !== this._generation) return;
      if (!Array.isArray(response) || response.some(series => !Array.isArray(series))) {
        throw new Error(this._t("history.invalid"));
      }
      this._history = response.flat().map(state => {
        const attrs = state.attributes || state.a || {};
        const valid = !["unknown", "unavailable"].includes(state.state);
        const timestamp = state.lu != null ? state.lu * 1000 : Date.parse(state.last_updated || state.last_changed);
        return {
          t: timestamp,
          actual: valid ? number(attrs.current_temperature) : null,
          target: valid ? number(attrs.temperature) : null,
          outdoor: valid ? number(attrs.outdoor_temperature) : null,
        };
      }).filter(point => Number.isFinite(point.t)).sort((a, b) => a.t - b.t);
      this._historyFetchedAt = end;
      this._error = "";
    } catch (error) {
      if (generation !== this._generation) return;
      this._error = errorText(error);
      console.error("ThermoSmart temperature history failed", error);
    } finally {
      if (this._historyRequest === request) this._historyRequest = null;
    }
    if (generation !== this._generation) return;
    this._render();
    this._scheduleHistoryRefresh();
  }

  _scheduleHistoryRefresh() {
    clearTimeout(this._timer);
    if (this.isConnected && !this._error) {
      this._timer = setTimeout(() => this._refresh(), 300000);
    }
  }

  _render() {
    const body = this._frame();
    const card = body.parentElement;
    card.classList.add("history-card");
    card.tabIndex = 0;
    card.setAttribute("role", "link");
    card.setAttribute("aria-label", this._t("history.show"));
    card.addEventListener("click", () => openHistory(this._config.entity));
    card.addEventListener("keydown", ev => {
      if (["Enter", " "].includes(ev.key)) {
        ev.preventDefault();
        openHistory(this._config.entity);
      }
    });
    if (!this._history) {
      body.append(element("div", this._translationError || this._error || this._t("history.loading"), "message"));
      return;
    }
    const points = this._history;
    const fahrenheit = this._hass?.config.unit_system.temperature === "°F";
    const series = [
      { key: "actual", label: this._t("history.actual"), color: "#2196F3" },
      { key: "target", label: this._t("history.target"), color: "#ff9800" },
      { key: "outdoor", label: this._t("history.outdoor"), color: "#ef5350" },
    ].filter(item => points.some(point => point[item.key] !== null));
    if (!series.length) {
      body.append(element("div", this._t("history.empty"), "message"));
      if (this._error || this._translationError) this._feedback(body);
      return;
    }
    const temperature = (point, key) => {
      const value = point[key];
      // Climate temperatures use HA display units; outdoor_temperature is a ThermoSmart Celsius attribute.
      return key === "outdoor" && fahrenheit && value !== null ? value * 9 / 5 + 32 : value;
    };
    const values = points.flatMap(point => series.map(item => temperature(point, item.key))).filter(value => value !== null);
    const low = Math.floor(values.reduce((min, value) => Math.min(min, value), Infinity) / 5) * 5;
    const high = Math.max(low + 5, Math.ceil(values.reduce((max, value) => Math.max(max, value), -Infinity) / 5) * 5);
    const end = Date.now();
    const start = end - (this._config.hours_to_show ?? 24) * 3600000;
    const x = t => 24 + Math.max(0, Math.min(1, (t - start) / (end - start))) * 212;
    const y = t => 74 - (t - low) / (high - low) * 68;
    const graph = svgElement("svg", { viewBox: "0 0 260 94", class: "sparkline", "aria-hidden": "true" });
    for (let t = low; t <= high; t += 5) {
      graph.append(svgElement("line", { x1: 24, x2: 236, y1: y(t), y2: y(t), stroke: "var(--divider-color)" }));
      const label = svgElement("text", {
        x: 21, y: y(t), "dominant-baseline": "middle", "text-anchor": "end",
        fill: "var(--secondary-text-color)", "font-size": 8.5,
      });
      label.textContent = `${t}°`;
      graph.append(label);
    }
    for (const item of [...series].reverse()) {
      let path = "", last = null;
      for (const point of points) {
        const value = temperature(point, item.key);
        if (value === null) { last = null; continue; }
        path += last === null ? ` M${x(point.t)} ${y(value)}` : item.key === "target"
          ? ` H${x(point.t)} V${y(value)}` : ` L${x(point.t)} ${y(value)}`;
        last = value;
      }
      if (last !== null) path += ` H${x(end)}`;
      graph.append(svgElement("path", {
        class: `history-${item.key}`, d: path, fill: "none", stroke: item.color,
        "stroke-width": 1, "stroke-linejoin": "round", "stroke-linecap": "round",
      }));
    }
    for (const [timestamp, anchor, px] of [[start, "start", 24], [end, "end", 236]]) {
      const label = svgElement("text", { x: px, y: 91, "text-anchor": anchor, fill: "var(--secondary-text-color)", "font-size": 9 });
      label.textContent = this._date(timestamp, { hour: "2-digit", minute: "2-digit" });
      graph.append(label);
    }
    body.append(graph);
    const legend = element("div", undefined, "spark-legend");
    for (const item of series) {
      const entry = element("span");
      const swatch = svgElement("svg", { width: 14, height: 8, "aria-hidden": "true" });
      swatch.append(svgElement("line", { x1: 0, x2: 14, y1: 4, y2: 4, stroke: item.color, "stroke-width": 2 }));
      entry.append(swatch, element("span", item.label));
      legend.append(entry);
    }
    body.append(legend);
    if (this._error || this._translationError) this._feedback(body);
  }
}

class ThermoSmartCardEditor extends ThermoSmartLocalized {
  set hass(hass) {
    this._setHass(hass);
    if (this._form) this._form.hass = hass;
  }

  _translationsChanged() {
    if (!this._form) return;
    const labels = {
      true: "always_visible", false: "hidden", full: "full", minimal: "minimal",
      collapsed: "collapsed", expanded: "expanded",
    };
    this._form.schema = this._form.schema.map(schema => {
      if (schema.name !== "show_control") return schema;
      return { ...schema, selector: { select: { options: schema.selector.select.options.map(option => ({
        ...option, label: this._t(`editor.${labels[option.value]}`),
      })) } } };
    });
    this._form.computeLabel = schema => this._t(`editor.${schema.name}`);
    this._translationFeedback.textContent = this._translationError || "";
  }

  setConfig(config) {
    this._config = config;
    this._form = element("ha-form");
    this._form.hass = this._hass;
    const zone = this.kind === "zone-card";
    this._form.data = {
      show_zone_name: true, show_control: zone ? "full" : true,
      ...(zone ? { show_graph: true } : { show_plan: true }), ...config,
    };
    if (zone) {
      this._form.data.show_control = config.show_control === false ? "false"
        : config.show_control === "minimal" ? "minimal" : "full";
    } else {
      this._form.data.show_control = String(config.show_control ?? true);
    }
    this._form.schema = [
      { name: "entity", required: true, selector: { entity: { domain: "climate", integration: "thermosmart" } } },
    ];
    this._form.schema.push({ name: "show_zone_name", selector: { boolean: {} } });
    if (zone) {
      this._form.schema.push(
        { name: "show_control", selector: { select: { options: [
          { value: "full" }, { value: "minimal" }, { value: "false" },
        ] } } },
        { name: "show_graph", selector: { boolean: {} } },
        { name: "hours_to_show", selector: { number: { min: 1, mode: "box" } } },
      );
    } else {
      this._form.schema.push(
        { name: "show_control", selector: { select: { options: [
          { value: "true" }, { value: "false" },
          { value: "collapsed" }, { value: "expanded" },
        ] } } },
        { name: "show_plan", selector: { boolean: {} } },
      );
      for (const name of ["max-days", "max-events"]) {
        this._form.schema.push({ name, selector: { number: { min: 1, mode: "box" } } });
      }
    }
    this._translationFeedback = element("div");
    this._translationFeedback.setAttribute("role", "alert");
    this._translationsChanged();
    this._form.addEventListener("value-changed", ev => {
      ev.stopPropagation();
      const config = { ...this._config, ...ev.detail.value };
      if (config.show_control === "false") config.show_control = false;
      if (!zone && config.show_control === "true") config.show_control = true;
      this._config = config;
      this.dispatchEvent(new CustomEvent("config-changed", {
        detail: { config }, bubbles: true, composed: true,
      }));
    });
    this.replaceChildren(this._form, this._translationFeedback);
  }
}

class ThermoSmartCombinedCard extends ThermoSmartBase {
  setConfig(config) {
    const zone = this.constructor.kind === "zone-card";
    for (const key of ["show_zone_name", zone ? "show_graph" : "show_plan"]) {
      if (config?.[key] !== undefined && typeof config[key] !== "boolean") {
        throw new Error(`${key} must be true or false`);
      }
    }
    if (zone) {
      if (config?.show_control !== undefined && ![true, false, "full", "minimal"].includes(config.show_control)) {
        throw new Error("show_control must be full, minimal, true or false");
      }
      if (config?.hours_to_show != null && (!Number.isFinite(config.hours_to_show) || config.hours_to_show <= 0)) {
        throw new Error("hours_to_show must be a positive number");
      }
    } else {
      if (config?.show_control !== undefined && ![true, false, "collapsed", "expanded"].includes(config.show_control)) {
        throw new Error("show_control must be true, false, collapsed or expanded");
      }
      positiveLimit(config?.["max-days"], "max-days");
      positiveLimit(config?.["max-events"], "max-events");
    }
    super.setConfig(config);
  }

  _render() {
    const card = element("ha-card");
    const zone = this.constructor.kind === "zone-card";
    this._sections = [];
    const addSection = (kind, config, parent = card) => {
      const section = element(`thermosmart-${kind}-section`);
      section.setConfig(config);
      this._sections.push(section);
      parent.append(section);
    };
    if (this._config.show_control !== false) {
      let parent = card;
      if (!zone && ["collapsed", "expanded"].includes(this._config.show_control)) {
        const panel = element("ha-expansion-panel");
        panel.header = this._t("schedule.controls");
        panel.expanded = this._config.show_control === "expanded";
        panel.hidden = true;
        const loading = this._text("div", "schedule.loading_controls", "message");
        card.append(loading, panel);
        this._prepareExpansionPanel(panel, loading);
        parent = panel;
      }
      addSection(zone ? "zone-control" : "schedule-controls", {
        ...this._config, layout: this._config.show_control === "minimal" ? "compact" : "full",
      }, parent);
    }
    if (zone ? this._config.show_graph !== false : this._config.show_plan !== false) {
      addSection(zone ? "temperature-history" : "schedule-plan", this._config);
    }
    if (!this._sections.length) card.append(this._text("div", "no_sections", "message"));
    const feedback = element("div", this._translationError || "", "translation-feedback message");
    feedback.hidden = !this._translationError;
    feedback.setAttribute("role", "alert");
    card.append(feedback);
    this.shadowRoot.replaceChildren(element("style", CSS), card);
  }

  async _prepareExpansionPanel(panel, loading) {
    try {
      if (!customElements.get("ha-expansion-panel")) {
        const helpers = await this._helpers();
        const native = helpers.createCardElement({ type: "thermostat", entity: this._config.entity });
        await native.constructor.getConfigElement();
        await waitForElements(["ha-expansion-panel"], this._t.bind(this));
      }
      panel.hidden = false;
      loading.remove();
    } catch (error) {
      loading.dataset.translation = "schedule.controls_error";
      loading.translationValues = { error: errorText(error) };
      loading.textContent = this._t("schedule.controls_error", loading.translationValues);
      loading.setAttribute("role", "alert");
      console.error("ThermoSmart expansion panel loading failed", error);
    }
  }

  _update() {
    const card = this.shadowRoot.querySelector("ha-card");
    card.header = this._config.show_zone_name === false ? undefined
      : this._state?.attributes.friendly_name || this._config.entity;
    this._translateNodes(this.shadowRoot);
    const panel = this.shadowRoot.querySelector("ha-expansion-panel");
    if (panel) panel.header = this._t("schedule.controls");
    const feedback = this.shadowRoot.querySelector(".translation-feedback");
    feedback.textContent = this._translationError || "";
    feedback.hidden = !this._translationError;
    for (const section of this._sections) section.hass = this._hass;
  }

  getCardSize() {
    const size = this._sections.reduce((total, section) => {
      const parent = section.parentElement;
      return total + (parent?.localName === "ha-expansion-panel"
        ? 1 + (parent.expanded ? section.getCardSize() : 0) : section.getCardSize());
    }, this._config.show_zone_name === false ? 0 : 1);
    return Math.max(1, size);
  }
}

class ThermoSmartZoneCard extends ThermoSmartCombinedCard {
  static kind = "zone-card";
}

class ThermoSmartZoneScheduleCard extends ThermoSmartCombinedCard {
  static kind = "zone-schedule-card";
}

for (const [kind, component] of [
  ["zone-control", ThermoSmartZoneControl], ["schedule-controls", ThermoSmartScheduleControls],
  ["schedule-plan", ThermoSmartSchedulePlan], ["temperature-history", ThermoSmartTemperatureHistory],
]) {
  const name = `thermosmart-${kind}-section`;
  if (!customElements.get(name)) customElements.define(name, component);
}

for (const [name, component, title, description] of [
  ["thermosmart-zone-card", ThermoSmartZoneCard, "ThermoSmart Zone card", "Optional zone controls and temperature history"],
  ["thermosmart-zone-schedule-card", ThermoSmartZoneScheduleCard, "ThermoSmart Schedule card", "Optional schedule controls and upcoming events"],
]) {
  if (!customElements.get(name)) customElements.define(name, component);
  window.customCards = window.customCards || [];
  if (!window.customCards.some(card => card.type === name)) {
    window.customCards.push({ type: name, name: title, description, preview: true });
  }
}
if (!customElements.get("thermosmart-card-editor")) {
  customElements.define("thermosmart-card-editor", ThermoSmartCardEditor);
}
if (!customElements.get("thermosmart-suspension-dialog")) {
  customElements.define("thermosmart-suspension-dialog", ThermoSmartSuspensionDialog);
}
console.info(`ThermoSmart cards ${CARD_VERSION}`);
