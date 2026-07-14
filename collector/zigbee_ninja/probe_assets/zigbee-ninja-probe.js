"use strict";

// zigbee-ninja T1 probe — a Zigbee2MQTT external extension (DESIGN.md §7.1).
//
// Deployed and removed entirely over MQTT (bridge/request/extension/save|remove).
// Dependency-free single file. Defensive by design: every hook attaches through
// a capability check with a legacy fallback, every handler is wrapped, and the
// heartbeat self-reports the attached hook inventory — so the S3 spike question
// ("which eventBus hooks are stable on this Z2M version?") is answered
// empirically by every deployment.
//
// Emits batched compact events on <base>/zigbee-ninja/probe/events and a
// heartbeat on <base>/zigbee-ninja/probe/heartbeat. Payload *sizes* only, never
// payload contents. Kill switch: publish {"enabled": false} to
// <base>/zigbee-ninja/probe/set.

const PROBE_VERSION = "0.3.0";
const FLUSH_MS = 1000;
const MAX_BUFFER = 400;
const HEARTBEAT_MS = 15000;

class ZigbeeNinjaProbe {
  constructor(
    zigbee,
    mqtt,
    state,
    publishEntityState,
    eventBus,
    enableDisableExtension,
    restartCallback,
    addExtension,
    settings,
    logger,
  ) {
    this.mqtt = mqtt;
    this.eventBus = eventBus;
    this.logger = logger;
    this.enabled = true;
    this.buffer = [];
    this.seq = 0;
    this.counters = { emitted: 0, dropped: 0, handlerErrors: 0 };
    this.hooks = [];
    this.startedAt = Date.now();
  }

  async start() {
    const hook = (methodName, eventName, handler) => {
      const wrapped = (payload) => {
        try {
          if (this.enabled) handler(payload || {});
        } catch (err) {
          this.counters.handlerErrors += 1;
        }
      };
      try {
        if (typeof this.eventBus[methodName] === "function") {
          this.eventBus[methodName](this, wrapped);
          this.hooks.push(methodName);
          return;
        }
      } catch (err) {
        /* fall through to legacy attach */
      }
      try {
        if (typeof this.eventBus.on === "function") {
          this.eventBus.on(eventName, wrapped, this);
          this.hooks.push(eventName);
        }
      } catch (err) {
        this.counters.handlerErrors += 1;
      }
    };

    hook("onDeviceMessage", "deviceMessage", (data) => {
      const device = data.device || {};
      const name = device.name || device.ieeeAddr || device.ieee_address || "?";
      let size = 0;
      try {
        size = JSON.stringify(data.data === undefined ? null : data.data).length;
      } catch (err) {
        size = -1;
      }
      const lqi = data.linkquality === undefined ? -1 : data.linkquality;
      this.push(["dm", name, String(data.cluster || "?"), String(data.type || "?"), lqi, size]);
    });

    hook("onMQTTMessage", "mqttMessage", (data) => {
      const topic = data.topic || "?";
      const size = data.message ? data.message.length : 0;
      this.push(["mi", topic, size]);
      if (topic.slice(-23) === "/zigbee-ninja/probe/set") this.onControl(data.message);
    });

    hook("onMQTTMessagePublished", "mqttMessagePublished", (data) => {
      const topic = data.topic || "?";
      if (topic.indexOf("zigbee-ninja/") !== -1) return; // never meter our own output
      const size = data.payload ? data.payload.length : 0;
      this.push(["mp", topic, size]);
    });

    hook("onDeviceJoined", "deviceJoined", (data) => {
      this.push(["dj", (data.device && data.device.name) || "?"]);
    });
    hook("onDeviceLeave", "deviceLeave", (data) => {
      this.push(["dl", data.ieeeAddr || (data.device && data.device.name) || "?"]);
    });
    hook("onDeviceAnnounce", "deviceAnnounce", (data) => {
      this.push(["da", (data.device && data.device.name) || "?"]);
    });

    this.flushTimer = setInterval(() => this.flush(), FLUSH_MS);
    this.heartbeatTimer = setInterval(() => this.heartbeat(), HEARTBEAT_MS);
    this.heartbeat();
    if (this.logger && this.logger.info) {
      this.logger.info(
        "zigbee-ninja probe " + PROBE_VERSION + " started (hooks: " + this.hooks.join(",") + ")",
      );
    }
  }

  onControl(message) {
    try {
      const parsed = JSON.parse(message.toString());
      if (parsed && parsed.enabled === false) this.enabled = false;
      if (parsed && parsed.enabled === true) this.enabled = true;
    } catch (err) {
      /* malformed control payloads are ignored */
    }
  }

  push(event) {
    if (this.buffer.length >= MAX_BUFFER) {
      this.counters.dropped += 1;
      return;
    }
    event.unshift(Date.now() / 1000);
    this.buffer.push(event);
  }

  flush() {
    if (this.buffer.length === 0) return;
    const events = this.buffer;
    this.buffer = [];
    this.seq += 1;
    this.counters.emitted += events.length;
    this.publish("zigbee-ninja/probe/events", { v: 1, seq: this.seq, events });
  }

  heartbeat() {
    this.publish("zigbee-ninja/probe/heartbeat", {
      v: 1,
      version: PROBE_VERSION,
      enabled: this.enabled,
      uptime_s: Math.round((Date.now() - this.startedAt) / 1000),
      hooks: this.hooks,
      counters: this.counters,
    });
  }

  publish(topic, payload) {
    try {
      const result = this.mqtt.publish(topic, JSON.stringify(payload), {
        qos: 0,
        retain: false,
      });
      if (result && typeof result.catch === "function") {
        result.catch(() => {
          this.counters.handlerErrors += 1;
        });
      }
    } catch (err) {
      this.counters.handlerErrors += 1;
    }
  }

  async stop() {
    clearInterval(this.flushTimer);
    clearInterval(this.heartbeatTimer);
    try {
      this.flush();
    } catch (err) {
      /* best effort */
    }
    try {
      if (typeof this.eventBus.removeListeners === "function") {
        this.eventBus.removeListeners(this);
      }
    } catch (err) {
      /* best effort */
    }
  }
}

module.exports = ZigbeeNinjaProbe;
