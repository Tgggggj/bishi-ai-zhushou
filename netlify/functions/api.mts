import { getStore } from "@netlify/blobs";
import { createHash, pbkdf2Sync, randomBytes, timingSafeEqual } from "node:crypto";

const MAX_BODY_BYTES = 6 * 1024 * 1024;
const MAX_SYNC_ITEMS = 200;
const STORE_KEY = "sync-store";
const EMAIL_PATTERN = /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/;
const CODE_TTL_MS = 10 * 60 * 1000;
const CODE_RESEND_MS = 60 * 1000;
const MAX_CODE_ATTEMPTS = 5;

class ApiError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function readJson(req) {
  const length = Number(req.headers.get("content-length") || 0);
  if (length > MAX_BODY_BYTES) {
    throw new ApiError("Request body is too large. Compress images or send fewer files.", 413);
  }
  try {
    return await req.json();
  } catch {
    throw new ApiError("Invalid JSON request body.");
  }
}

function nowText() {
  return new Date().toISOString().replace("T", " ").slice(0, 19);
}

function defaultStore() {
  return { users: {}, sessions: {}, emailCodes: {} };
}

function blobStore() {
  return getStore({ name: "practice-assistant-sync", consistency: "strong" });
}

async function loadStore() {
  const loaded = await blobStore().get(STORE_KEY, { type: "json" });
  if (!loaded || typeof loaded !== "object") return defaultStore();
  loaded.users ||= {};
  loaded.sessions ||= {};
  loaded.emailCodes ||= {};
  return loaded;
}

async function saveStore(store) {
  await blobStore().setJSON(STORE_KEY, store);
}

function cleanEmail(email) {
  const value = String(email || "").trim().toLowerCase();
  if (value.length > 254 || !EMAIL_PATTERN.test(value)) {
    throw new ApiError("Please enter a valid email address.");
  }
  return value;
}

function validatePassword(password) {
  const value = String(password || "");
  if (value.length < 6) throw new ApiError("Password must be at least 6 characters.");
  if (value.length > 128) throw new ApiError("Password is too long.");
  return value;
}

function envValue(name) {
  return globalThis.Netlify?.env?.get(name) || process.env[name] || "";
}

function normalizePurpose(purpose) {
  const value = String(purpose || "").trim();
  if (!["register", "reset"].includes(value)) throw new ApiError("Invalid verification purpose.");
  return value;
}

function codeKey(purpose, email) {
  return `${purpose}:${email}`;
}

function codeSecret() {
  return envValue("AUTH_CODE_SECRET") || envValue("RESEND_API_KEY") || "practice-assistant-code-secret";
}

function hashCode(purpose, email, code) {
  return createHash("sha256").update(`${purpose}:${email}:${code}:${codeSecret()}`).digest("hex");
}

function makeCode() {
  return String(randomBytes(4).readUInt32BE(0) % 1000000).padStart(6, "0");
}

function pruneEmailCodes(store) {
  const now = Date.now();
  for (const [key, item] of Object.entries(store.emailCodes || {})) {
    if (!item || Number(item.expiresAt || 0) < now) delete store.emailCodes[key];
  }
}

function verifyEmailCode(store, email, purpose, code) {
  pruneEmailCodes(store);
  const key = codeKey(purpose, email);
  const item = store.emailCodes[key];
  if (!item) throw new ApiError("Verification code is invalid or expired.");
  if (Number(item.attempts || 0) >= MAX_CODE_ATTEMPTS) {
    delete store.emailCodes[key];
    throw new ApiError("Too many verification attempts. Please request a new code.");
  }

  item.attempts = Number(item.attempts || 0) + 1;
  const expected = Buffer.from(String(item.codeHash || ""), "hex");
  const actual = Buffer.from(hashCode(purpose, email, String(code || "").trim()), "hex");
  const matches = expected.length === actual.length && timingSafeEqual(expected, actual);
  if (!matches) throw new ApiError("Verification code is incorrect.");
  delete store.emailCodes[key];
}

async function sendEmail(to, subject, text) {
  const apiKey = envValue("RESEND_API_KEY");
  const from = envValue("EMAIL_FROM");
  const replyTo = envValue("EMAIL_REPLY_TO");
  if (!apiKey || !from) {
    throw new ApiError("Email service is not configured. Set RESEND_API_KEY and EMAIL_FROM in Netlify.", 503);
  }

  const body = { from, to, subject, text };
  if (replyTo) body.reply_to = replyTo;
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      authorization: `Bearer ${apiKey}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new ApiError(`Unable to send verification email: ${detail.slice(0, 800)}`, 502);
  }
}

async function sendEmailCode(payload) {
  const email = cleanEmail(payload.email || payload.username);
  const purpose = normalizePurpose(payload.purpose);
  const store = await loadStore();
  pruneEmailCodes(store);

  if (purpose === "register" && store.users[email]) {
    throw new ApiError("Account already exists.");
  }
  if (purpose === "reset" && !store.users[email]) {
    throw new ApiError("No account exists for this email.");
  }

  const key = codeKey(purpose, email);
  const previous = store.emailCodes[key];
  const now = Date.now();
  if (previous && now - Number(previous.sentAt || 0) < CODE_RESEND_MS) {
    throw new ApiError("Please wait before requesting another verification code.", 429);
  }

  const code = makeCode();
  const actionText = purpose === "register" ? "注册账号" : "重置密码";
  await sendEmail(
    email,
    `练习助手${actionText}验证码`,
    `你的验证码是：${code}\n\n验证码 10 分钟内有效。若不是你本人操作，请忽略这封邮件。`,
  );

  store.emailCodes[key] = {
    codeHash: hashCode(purpose, email, code),
    attempts: 0,
    sentAt: now,
    expiresAt: now + CODE_TTL_MS,
  };
  await saveStore(store);

  return { ok: true, email, expiresInSeconds: CODE_TTL_MS / 1000 };
}

function hashPassword(password, saltHex) {
  const salt = saltHex ? Buffer.from(saltHex, "hex") : randomBytes(16);
  const digest = pbkdf2Sync(password, salt, 120000, 32, "sha256");
  return [salt.toString("hex"), digest.toString("hex")];
}

function verifyPassword(password, saltHex, digestHex) {
  const [, candidate] = hashPassword(password, saltHex);
  const expected = Buffer.from(digestHex, "hex");
  const actual = Buffer.from(candidate, "hex");
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

function emptyProfile(username) {
  return {
    username,
    email: username,
    current: {
      question: "",
      answer: "",
      model: "",
      imageCount: 0,
      images: [],
      mode: "",
      updatedAt: "",
    },
    history: [],
    bank: [],
    settings: emptyModelSettings(),
  };
}

function emptyModelSettings() {
  return {
    baseUrl: "",
    apiKey: "",
    model: "",
    maxTokens: 1800,
    temperature: 0.2,
    timeout: 90,
    fixedPrompt: "",
    useVisionInput: true,
    multiImageMode: false,
    updatedAt: "",
  };
}

function normalizeModelSettings(payload) {
  const source = payload?.settings || payload?.config || payload || {};
  const settings = emptyModelSettings();
  settings.baseUrl = String(source.baseUrl ?? source.base_url ?? "").trim();
  settings.apiKey = String(source.apiKey ?? source.api_key ?? "").trim();
  settings.model = String(source.model ?? "").trim();
  settings.maxTokens = Number(source.maxTokens ?? source.max_tokens ?? 1800) || 1800;
  settings.temperature = Number(source.temperature ?? 0.2);
  if (!Number.isFinite(settings.temperature)) settings.temperature = 0.2;
  settings.timeout = Number(source.timeout ?? 90) || 90;
  settings.fixedPrompt = String(source.fixedPrompt ?? source.fixed_prompt ?? "").trim();
  const vision = source.useVisionInput ?? source.use_vision_input;
  settings.useVisionInput = vision == null ? true : Boolean(vision);
  const multiImage = source.multiImageMode ?? source.multi_image_mode;
  settings.multiImageMode = multiImage == null ? false : Boolean(multiImage);
  settings.updatedAt = String(source.updatedAt ?? source.updated_at ?? nowText());
  return settings;
}

function ensureProfile(user, username) {
  const profile = user.profile || emptyProfile(username);
  profile.username ||= username;
  profile.email ||= username;
  profile.current ||= emptyProfile(username).current;
  profile.history = Array.isArray(profile.history) ? profile.history : [];
  profile.bank = Array.isArray(profile.bank) ? profile.bank : [];
  profile.settings ||= emptyModelSettings();
  user.profile = profile;
  return profile;
}

function issueSession(store, username) {
  const token = randomBytes(32).toString("base64url");
  store.sessions[token] = { username, createdAt: nowText() };
  return token;
}

function bearerToken(req) {
  const auth = req.headers.get("authorization") || "";
  return auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
}

function usernameForToken(store, token) {
  const session = store.sessions[token || ""];
  if (!session) throw new ApiError("Please sign in first.", 401);
  const username = session.username || "";
  if (!store.users[username]) throw new ApiError("Account does not exist. Please sign in again.", 401);
  return username;
}

function copyJson(value) {
  return JSON.parse(JSON.stringify(value));
}

async function registerUser(payload) {
  const username = cleanEmail(payload.email || payload.username);
  const password = validatePassword(payload.password);
  const store = await loadStore();
  if (store.users[username]) throw new ApiError("Account already exists.");
  verifyEmailCode(store, username, "register", payload.code || payload.verificationCode);
  const [salt, digest] = hashPassword(password);
  store.users[username] = {
    passwordSalt: salt,
    passwordHash: digest,
    createdAt: nowText(),
    profile: emptyProfile(username),
  };
  const token = issueSession(store, username);
  await saveStore(store);
  return { ok: true, token, username, email: username };
}

async function resetPassword(payload) {
  const username = cleanEmail(payload.email || payload.username);
  const password = validatePassword(payload.password);
  const store = await loadStore();
  const user = store.users[username];
  if (!user) throw new ApiError("No account exists for this email.", 404);
  verifyEmailCode(store, username, "reset", payload.code || payload.verificationCode);
  const [salt, digest] = hashPassword(password);
  user.passwordSalt = salt;
  user.passwordHash = digest;
  user.passwordUpdatedAt = nowText();
  for (const [token, session] of Object.entries(store.sessions || {})) {
    if (session?.username === username) delete store.sessions[token];
  }
  const token = issueSession(store, username);
  await saveStore(store);
  return { ok: true, token, username, email: username };
}

async function loginUser(payload) {
  const username = cleanEmail(payload.email || payload.username);
  const password = validatePassword(payload.password);
  const store = await loadStore();
  const user = store.users[username];
  if (!user || !verifyPassword(password, user.passwordSalt || "", user.passwordHash || "")) {
    throw new ApiError("Account or password is incorrect.", 401);
  }
  const token = issueSession(store, username);
  await saveStore(store);
  return { ok: true, token, username, email: username };
}

async function logoutUser(req) {
  const token = bearerToken(req);
  const store = await loadStore();
  delete store.sessions[token];
  await saveStore(store);
  return { ok: true };
}

async function getProfile(req) {
  const token = bearerToken(req);
  const store = await loadStore();
  const username = usernameForToken(store, token);
  const profile = ensureProfile(store.users[username], username);
  return copyJson(profile);
}

function normalizeSyncImages(value) {
  if (!Array.isArray(value)) return [];
  const images = [];
  for (const raw of value.slice(-1)) {
    if (!raw || typeof raw !== "object") continue;
    const dataUrl = String(raw.dataUrl || raw.data_url || "");
    if (!/^data:image\/(jpeg|png|webp);base64,/.test(dataUrl)) continue;
    if (dataUrl.length > 3_000_000) throw new ApiError("Sync image is too large.", 413);
    images.push({
      id: String(raw.id || ""),
      name: String(raw.name || "image").slice(0, 180),
      dataUrl,
    });
  }
  return images;
}

async function updateCurrent(req, payload) {
  const token = bearerToken(req);
  const store = await loadStore();
  const username = usernameForToken(store, token);
  const profile = ensureProfile(store.users[username], username);
  const current = {
    question: String(payload.question || ""),
    answer: String(payload.answer || ""),
    model: String(payload.model || ""),
    imageCount: Number(payload.imageCount || payload.image_count || 0),
    images: normalizeSyncImages(payload.images),
    mode: String(payload.mode || ""),
    updatedAt: String(payload.updatedAt || payload.updated_at || nowText()),
  };
  profile.current = current;
  store.users[username].profile = profile;
  await saveStore(store);
  return { ok: true, current };
}

async function replaceCollection(req, name, items) {
  const token = bearerToken(req);
  const store = await loadStore();
  const username = usernameForToken(store, token);
  const profile = ensureProfile(store.users[username], username);
  profile[name] = Array.isArray(items) ? items.slice(0, MAX_SYNC_ITEMS) : [];
  store.users[username].profile = profile;
  await saveStore(store);
  return { ok: true, [name]: profile[name] };
}

async function prependHistoryItem(req, item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    throw new ApiError("History item must be an object.");
  }
  const token = bearerToken(req);
  const store = await loadStore();
  const username = usernameForToken(store, token);
  const profile = ensureProfile(store.users[username], username);
  const itemId = item.id;
  const existing = Array.isArray(profile.history) ? profile.history : [];
  const next = itemId ? existing.filter((entry) => entry?.id !== itemId) : existing;
  next.unshift(item);
  profile.history = next.slice(0, MAX_SYNC_ITEMS);
  store.users[username].profile = profile;
  await saveStore(store);
  return { ok: true, history: profile.history };
}

async function updateSettings(req, payload) {
  const token = bearerToken(req);
  const store = await loadStore();
  const username = usernameForToken(store, token);
  const profile = ensureProfile(store.users[username], username);
  profile.settings = normalizeModelSettings(payload);
  store.users[username].profile = profile;
  await saveStore(store);
  return { ok: true, settings: profile.settings };
}

function resolveChatUrl(baseUrl) {
  const value = String(baseUrl || "").trim().replace(/\/+$/, "");
  if (!value) throw new ApiError("Missing API Base URL.");
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new ApiError("API Base URL must be a valid http(s) URL.");
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new ApiError("API Base URL must use http or https.");
  }
  if (["localhost", "127.0.0.1", "::1"].includes(parsed.hostname)) {
    throw new ApiError("Local API Base URLs are not reachable from the cloud deployment.");
  }
  return value.endsWith("/chat/completions") ? value : `${value}/chat/completions`;
}

function optimizedMaxTokens(config, messages) {
  const configured = Number(config?.maxTokens || 1800);
  const prompt = JSON.stringify(messages || []);
  return Math.min(configured, prompt.includes("编程题") ? 2200 : 900);
}

function optimizedModelOptions(baseUrl) {
  const lowered = String(baseUrl || "").toLowerCase();
  return lowered.includes("aliyuncs.com") || lowered.includes("dashscope")
    ? { enable_thinking: false }
    : {};
}

async function callOpenAICompatible(config, messages) {
  const baseUrl = String(config?.baseUrl || "").trim();
  const apiKey = String(config?.apiKey || "").trim();
  const model = String(config?.model || "").trim();
  const timeout = Math.min(Number(config?.timeout || 55), 55);
  const temperature = config?.temperature === "" || config?.temperature == null ? 0.2 : Number(config.temperature);
  const maxTokens = optimizedMaxTokens(config, messages);

  if (!apiKey) throw new ApiError("Missing API Key.");
  if (!model) throw new ApiError("Missing model name.");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout * 1000);
  try {
    const res = await fetch(resolveChatUrl(baseUrl), {
      method: "POST",
      headers: {
        authorization: `Bearer ${apiKey}`,
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({
        model,
        messages,
        temperature,
        max_tokens: maxTokens,
        ...optimizedModelOptions(baseUrl),
      }),
      signal: controller.signal,
    });

    const raw = await res.text();
    if (!res.ok) throw new ApiError(`Model API returned ${res.status}: ${raw.slice(0, 1200)}`, 502);
    const data = JSON.parse(raw);
    const choice = Array.isArray(data.choices) ? data.choices[0] : null;
    let content = choice?.message?.content || "";
    if (Array.isArray(content)) {
      content = content.map((part) => part?.text || "").filter(Boolean).join("\n");
    }
    return {
      content: content || JSON.stringify(data, null, 2),
      model: data.model || model,
      usage: data.usage || {},
      createdAt: nowText(),
    };
  } catch (error) {
    if (error?.name === "AbortError") throw new ApiError("Model API request timed out.", 504);
    if (error instanceof ApiError) throw error;
    throw new ApiError(`Unable to call model API: ${error?.message || String(error)}`, 502);
  } finally {
    clearTimeout(timer);
  }
}

async function streamOpenAICompatible(config, messages) {
  const baseUrl = String(config?.baseUrl || "").trim();
  const apiKey = String(config?.apiKey || "").trim();
  const model = String(config?.model || "").trim();
  const timeout = Math.min(Number(config?.timeout || 55), 55);
  const temperature = config?.temperature === "" || config?.temperature == null ? 0.2 : Number(config.temperature);
  const maxTokens = optimizedMaxTokens(config, messages);
  if (!apiKey) throw new ApiError("Missing API Key.");
  if (!model) throw new ApiError("Missing model name.");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout * 1000);
  let upstream;
  try {
    upstream = await fetch(resolveChatUrl(baseUrl), {
      method: "POST",
      headers: {
        authorization: `Bearer ${apiKey}`,
        "content-type": "application/json",
        accept: "text/event-stream, application/json",
      },
      body: JSON.stringify({
        model,
        messages,
        temperature,
        max_tokens: maxTokens,
        stream: true,
        stream_options: { include_usage: true },
        ...optimizedModelOptions(baseUrl),
      }),
      signal: controller.signal,
    });
  } catch (error) {
    clearTimeout(timer);
    if (error?.name === "AbortError") throw new ApiError("Model API request timed out.", 504);
    throw new ApiError(`Unable to call model API: ${error?.message || String(error)}`, 502);
  }

  if (!upstream.ok) {
    clearTimeout(timer);
    const raw = await upstream.text();
    throw new ApiError(`Model API returned ${upstream.status}: ${raw.slice(0, 1200)}`, 502);
  }

  const encoder = new TextEncoder();
  const contentType = upstream.headers.get("content-type") || "";
  const stream = new ReadableStream({
    async start(output) {
      let content = "";
      let responseModel = model;
      let usage = {};
      const emit = (event) => output.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
      try {
        if (!contentType.includes("text/event-stream")) {
          const data = await upstream.json();
          const choice = Array.isArray(data.choices) ? data.choices[0] : null;
          let text = choice?.message?.content || "";
          if (Array.isArray(text)) text = text.map((part) => part?.text || "").filter(Boolean).join("\n");
          content = String(text || JSON.stringify(data, null, 2));
          emit({ delta: content });
          emit({ done: true, content, model: data.model || model, usage: data.usage || {}, createdAt: nowText() });
          return;
        }

        const reader = upstream.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
          const lines = buffer.split(/\r?\n/);
          buffer = done ? "" : lines.pop() || "";
          for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            const raw = line.slice(5).trim();
            if (!raw || raw === "[DONE]") continue;
            const event = JSON.parse(raw);
            responseModel = event.model || responseModel;
            if (event.usage) usage = event.usage;
            const delta = event.choices?.[0]?.delta?.content;
            const text = Array.isArray(delta)
              ? delta.map((part) => part?.text || "").join("")
              : String(delta || "");
            if (text) {
              content += text;
              emit({ delta: text });
            }
          }
          if (done) break;
        }
        if (!content) throw new Error("Model API returned no usable content.");
        emit({ done: true, content, model: responseModel, usage, createdAt: nowText() });
      } catch (error) {
        emit({ error: error?.name === "AbortError" ? "Model API request timed out." : error?.message || String(error) });
      } finally {
        clearTimeout(timer);
        output.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "application/x-ndjson; charset=utf-8",
      "cache-control": "no-store, no-transform",
      "x-accel-buffering": "no",
    },
  });
}

async function handleGet(path, req) {
  if (path === "/api/health") {
    return { ok: true, service: "netlify", time: Date.now() / 1000 };
  }
  if (path === "/api/latest") {
    return { ok: true, question: "", answer: "", updatedAt: "", model: "" };
  }
  if (path === "/api/sync/me") {
    const profile = await getProfile(req);
    return { ok: true, username: profile.username, email: profile.email || profile.username };
  }
  if (path === "/api/sync/state") {
    const profile = await getProfile(req);
    return { ok: true, current: profile.current || {} };
  }
  if (path === "/api/sync/history") {
    const profile = await getProfile(req);
    return { ok: true, history: profile.history || [] };
  }
  if (path === "/api/sync/bank") {
    const profile = await getProfile(req);
    return { ok: true, bank: profile.bank || [] };
  }
  if (path === "/api/sync/settings") {
    const profile = await getProfile(req);
    return { ok: true, settings: profile.settings || emptyModelSettings() };
  }
  if (path === "/api/sync/profile") {
    const profile = await getProfile(req);
    return { ok: true, profile };
  }
  return null;
}

async function handlePost(path, req) {
  const payload = await readJson(req);
  if (path === "/api/chat") {
    const messages = payload.messages;
    if (!Array.isArray(messages)) throw new ApiError("messages must be an array.");
    return { ok: true, ...(await callOpenAICompatible(payload.config || {}, messages)) };
  }
  if (path === "/api/check") {
    const result = await callOpenAICompatible(payload.config || {}, [
      { role: "user", content: "Please reply only OK." },
    ]);
    return { ok: true, content: result.content, model: result.model };
  }
  if (path === "/api/sync/register") return registerUser(payload);
  if (path === "/api/sync/login") return loginUser(payload);
  if (path === "/api/sync/send-code") return sendEmailCode(payload);
  if (path === "/api/sync/reset-password") return resetPassword(payload);
  if (path === "/api/sync/logout") return logoutUser(req);
  if (path === "/api/sync/state") return updateCurrent(req, payload);
  if (path === "/api/sync/history") return replaceCollection(req, "history", payload.history ?? payload.items ?? []);
  if (path === "/api/sync/history/add") return prependHistoryItem(req, payload.item || payload);
  if (path === "/api/sync/bank") return replaceCollection(req, "bank", payload.bank ?? payload.items ?? []);
  if (path === "/api/sync/settings") return updateSettings(req, payload);
  return null;
}

export default async (req) => {
  const path = new URL(req.url).pathname;
  try {
    if (req.method === "POST" && path === "/api/chat") {
      const payload = await readJson(req);
      if (!Array.isArray(payload.messages)) throw new ApiError("messages must be an array.");
      return await streamOpenAICompatible(payload.config || {}, payload.messages);
    }
    let result = null;
    if (req.method === "GET") result = await handleGet(path, req);
    if (req.method === "POST") result = await handlePost(path, req);
    if (result) return json(result);
    if (!["GET", "POST"].includes(req.method)) return json({ ok: false, error: "Method not allowed." }, 405);
    return json({ ok: false, error: "Not found." }, 404);
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 400;
    return json({ ok: false, error: error?.message || String(error) }, status);
  }
};

export const config = {
  path: "/api/*",
};
