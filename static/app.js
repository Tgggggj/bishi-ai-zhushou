const storageKeys = {
  settings: "byoPractice.settings",
  history: "byoPractice.history",
  bank: "byoPractice.bank",
  account: "byoPractice.account",
};

const defaultSettings = {
  baseUrl: "https://api.openai.com/v1",
  apiKey: "",
  model: "gpt-4.1-mini",
  maxTokens: 1800,
  temperature: 0.2,
  timeout: 90,
};

const documentFormatInstruction =
  "输出必须是标准中文文档格式，不要使用 Markdown、HTML 或代码围栏。不要输出 #、**、```、- 等 Markdown 标记。默认使用以下结构：一、答案；二、解析；三、关键步骤；四、参考代码（仅编程题需要）；五、注意事项。编程题代码用纯文本缩进展示，不要使用 Markdown 代码块。";

const presets = {
  openai: { baseUrl: "https://api.openai.com/v1", model: "gpt-4.1-mini" },
  deepseek: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  qwen: { baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  siliconflow: { baseUrl: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-VL-72B-Instruct" },
};

const modeText = {
  general: "综合题",
  coding: "编程题",
  reasoning: "行测/图推",
  interview: "模拟面试",
  review: "复盘讲解",
};

const styleText = {
  answer: "直接给出答案，再解释关键依据。",
  steps: "只给解题思路和推理过程，不直接跳到结论。",
  concise: "用最短可用答案回答，保留必要公式、代码或选项。",
  coach: "像训练教练一样指出薄弱点、改进动作和下一题练习方向。",
};

const state = {
  view: "solve",
  images: [],
  answer: "",
  lastPrompt: "",
  settings: loadJson(storageKeys.settings, defaultSettings),
  history: loadJson(storageKeys.history, []),
  bank: loadJson(storageKeys.bank, []),
  account: loadJson(storageKeys.account, { email: "", username: "", token: "" }),
  importTarget: null,
  busy: false,
  latestDesktopUpdatedAt: "",
};

const $ = (id) => document.getElementById(id);

const refs = {
  viewTitle: $("viewTitle"),
  runBtn: $("runBtn"),
  clearBtn: $("clearBtn"),
  modeSelect: $("modeSelect"),
  styleSelect: $("styleSelect"),
  languageSelect: $("languageSelect"),
  questionInput: $("questionInput"),
  dropZone: $("dropZone"),
  imageInput: $("imageInput"),
  pickImageBtn: $("pickImageBtn"),
  thumbs: $("thumbs"),
  answerBox: $("answerBox"),
  answerMeta: $("answerMeta"),
  saveBankBtn: $("saveBankBtn"),
  copyPromptBtn: $("copyPromptBtn"),
  copyAnswerBtn: $("copyAnswerBtn"),
  saveHistoryBtn: $("saveHistoryBtn"),
  bankSearch: $("bankSearch"),
  bankList: $("bankList"),
  historySearch: $("historySearch"),
  historyList: $("historyList"),
  exportBankBtn: $("exportBankBtn"),
  importBankBtn: $("importBankBtn"),
  exportHistoryBtn: $("exportHistoryBtn"),
  clearHistoryBtn: $("clearHistoryBtn"),
  jsonImportInput: $("jsonImportInput"),
  baseUrlInput: $("baseUrlInput"),
  apiKeyInput: $("apiKeyInput"),
  modelInput: $("modelInput"),
  maxTokensInput: $("maxTokensInput"),
  temperatureInput: $("temperatureInput"),
  timeoutInput: $("timeoutInput"),
  saveSettingsBtn: $("saveSettingsBtn"),
  testSettingsBtn: $("testSettingsBtn"),
  forgetKeyBtn: $("forgetKeyBtn"),
  settingsStatus: $("settingsStatus"),
  accountUsernameInput: $("accountUsernameInput"),
  accountPasswordInput: $("accountPasswordInput"),
  accountStatus: $("accountStatus"),
  accountLoginBtn: $("accountLoginBtn"),
  accountRegisterBtn: $("accountRegisterBtn"),
  accountLogoutBtn: $("accountLogoutBtn"),
  syncPullBtn: $("syncPullBtn"),
  syncPushBtn: $("syncPushBtn"),
  toast: $("toast"),
};

function loadJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function saveJson(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function accountEmail(account) {
  return (account && (account.email || account.username)) || "";
}

function saveAccount(account) {
  const email = accountEmail(account);
  state.account = { ...account, email, username: email };
  saveJson(storageKeys.account, state.account);
  if (refs.accountUsernameInput) refs.accountUsernameInput.value = email;
  if (refs.accountStatus) refs.accountStatus.textContent = state.account.token ? `已登录邮箱 · ${email}` : "未登录";
}

function authHeaders() {
  return state.account.token ? { Authorization: `Bearer ${state.account.token}` } : {};
}

async function syncApi(path, options = {}) {
  const headers = { ...(options.headers || {}), ...authHeaders() };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "同步失败");
  return data;
}

function uid() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function nowText() {
  return new Date().toLocaleString("zh-CN", { hour12: false });
}

function toast(message) {
  refs.toast.textContent = message;
  refs.toast.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => refs.toast.classList.remove("show"), 2600);
}

function setBusy(nextBusy) {
  state.busy = nextBusy;
  refs.runBtn.disabled = nextBusy;
  refs.testSettingsBtn.disabled = nextBusy;
  refs.runBtn.textContent = nextBusy ? "解析中" : "解析";
}

function hydrateSettingsForm() {
  const settings = { ...defaultSettings, ...state.settings };
  refs.baseUrlInput.value = settings.baseUrl || "";
  refs.apiKeyInput.value = settings.apiKey || "";
  refs.modelInput.value = settings.model || "";
  refs.maxTokensInput.value = settings.maxTokens || 1800;
  refs.temperatureInput.value = settings.temperature ?? 0.2;
  refs.timeoutInput.value = settings.timeout || 90;
}

function hydrateAccountForm() {
  refs.accountUsernameInput.value = accountEmail(state.account);
  refs.accountPasswordInput.value = "";
  refs.accountStatus.textContent = state.account.token ? `已登录邮箱 · ${accountEmail(state.account)}` : "未登录";
}

function readSettingsForm() {
  state.settings = {
    baseUrl: refs.baseUrlInput.value.trim(),
    apiKey: refs.apiKeyInput.value.trim(),
    model: refs.modelInput.value.trim(),
    maxTokens: Number(refs.maxTokensInput.value || 1800),
    temperature: Number(refs.temperatureInput.value || 0.2),
    timeout: Number(refs.timeoutInput.value || 90),
  };
  return state.settings;
}

function validateSettings(settings) {
  if (!settings.baseUrl) return "请填写 API Base URL";
  if (!settings.apiKey) return "请填写 API Key";
  if (!settings.model) return "请填写模型名称";
  return "";
}

function systemPrompt() {
  return [
    "你是求职笔试、编程题和模拟面试的练习教练。",
    "你的用途仅限自我练习、复盘和模拟训练。",
    "不要协助正在进行的真实考试、真实笔试、真实面试或任何规避监控的行为；遇到这类语境时，改为提供学习建议、通用思路和练习方法。",
    "回答使用中文，结构清晰，必要时给出公式、边界条件、复杂度和可运行代码。",
    documentFormatInstruction,
  ].join("\n");
}

function buildPrompt() {
  const mode = refs.modeSelect.value;
  const style = refs.styleSelect.value;
  const language = refs.languageSelect.value;
  const question = refs.questionInput.value.trim();

  const modeRules = {
    general: "识别题型，先给结论，再给关键推理和易错点。",
    coding: `给出算法思路、边界条件、复杂度，并使用 ${language} 写出代码。`,
    reasoning: "先描述图片或题干中的关键信息，再逐步排除选项并说明依据。",
    interview: "按模拟面试回答组织语言，给出 60 秒版本和展开版本。",
    review: "复盘材料中的问题，指出错因、知识点和下一步训练安排。",
  };

  return [
    `场景：${modeText[mode]}`,
    `回答风格：${styleText[style]}`,
    `专项要求：${modeRules[mode]}`,
    `输出格式：${documentFormatInstruction}`,
    "材料：",
    question || "见随附图片。",
  ].join("\n");
}

function buildMessages() {
  const prompt = buildPrompt();
  state.lastPrompt = prompt;

  if (!state.images.length) {
    return [
      { role: "system", content: systemPrompt() },
      { role: "user", content: prompt },
    ];
  }

  const content = [{ type: "text", text: prompt }];
  for (const image of state.images) {
    content.push({ type: "image_url", image_url: { url: image.dataUrl } });
  }

  return [
    { role: "system", content: systemPrompt() },
    { role: "user", content },
  ];
}

async function runAnalysis() {
  const question = refs.questionInput.value.trim();
  if (!question && !state.images.length) {
    toast("请先输入题目或添加图片");
    return;
  }

  const settings = readSettingsForm();
  const error = validateSettings(settings);
  if (error) {
    switchView("settings");
    toast(error);
    return;
  }

  saveJson(storageKeys.settings, settings);
  setBusy(true);
  setAnswer("正在请求模型...");
  refs.answerMeta.textContent = "请求中";

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: settings, messages: buildMessages() }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "请求失败");

    state.answer = cleanMarkdownAnswer(data.content || "");
    setAnswer(state.answer);
    refs.answerMeta.textContent = `${data.model || settings.model} · ${data.createdAt || nowText()}`;
    addHistory({ silent: true });
    syncCurrentState({ silent: true }).catch(() => {});
    toast("解析完成");
  } catch (err) {
    state.answer = cleanMarkdownAnswer(`请求失败：${err.message}`);
    setAnswer(state.answer);
    refs.answerMeta.textContent = "失败";
  } finally {
    setBusy(false);
  }
}

function setAnswer(text) {
  const cleaned = cleanMarkdownAnswer(text);
  refs.answerBox.innerHTML = "";
  const article = document.createElement("article");
  article.className = "document-answer";
  const lines = cleaned.split("\n");
  for (const line of lines) {
    if (!line.trim()) {
      const spacer = document.createElement("div");
      spacer.className = "doc-spacer";
      article.appendChild(spacer);
      continue;
    }

    if (/^[一二三四五六七八九十]+、/.test(line.trim())) {
      const heading = document.createElement("h4");
      heading.textContent = line.trim();
      article.appendChild(heading);
      continue;
    }

    if (/^\s{2,}/.test(line)) {
      const code = document.createElement("pre");
      code.className = "doc-code";
      code.textContent = line;
      article.appendChild(code);
      continue;
    }

    const paragraph = document.createElement("p");
    paragraph.textContent = line.trim();
    article.appendChild(paragraph);
  }
  refs.answerBox.appendChild(article);
}

function cleanMarkdownAnswer(text) {
  return String(text || "")
    .replace(/```[a-zA-Z0-9_-]*\n?/g, "")
    .replace(/```/g, "")
    .replace(/\*\*/g, "")
    .replace(/__/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s*/gm, "")
    .replace(/^\s*[-*]\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function clearWorkspace() {
  refs.questionInput.value = "";
  state.images = [];
  state.answer = "";
  state.lastPrompt = "";
  refs.answerMeta.textContent = "等待解析";
  refs.answerBox.innerHTML = '<div class="empty-state">配置模型后输入题目开始练习。</div>';
  renderThumbs();
}

async function addImageFiles(files) {
  const imageFiles = Array.from(files).filter((file) => file.type.startsWith("image/"));
  if (!imageFiles.length) return;

  for (const file of imageFiles) {
    if (state.images.length >= 4) {
      toast("最多添加 4 张图片");
      break;
    }
    const dataUrl = await fileToCompressedDataUrl(file);
    state.images.push({ id: uid(), name: file.name || "clipboard-image", dataUrl });
  }
  renderThumbs();
}

function fileToCompressedDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("图片读取失败"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => resolve(reader.result);
      img.onload = () => {
        const maxSide = 1700;
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        const width = Math.max(1, Math.round(img.width * scale));
        const height = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, width, height);
        resolve(canvas.toDataURL("image/jpeg", 0.88));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

function renderThumbs() {
  refs.thumbs.innerHTML = "";
  for (const image of state.images) {
    const item = document.createElement("div");
    item.className = "thumb";

    const img = document.createElement("img");
    img.src = image.dataUrl;
    img.alt = image.name;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "移除图片");
    remove.addEventListener("click", () => {
      state.images = state.images.filter((entry) => entry.id !== image.id);
      renderThumbs();
    });

    item.append(img, remove);
    refs.thumbs.appendChild(item);
  }
}

function addBankItem() {
  const question = refs.questionInput.value.trim();
  if (!question && !state.images.length) {
    toast("没有可加入题库的内容");
    return;
  }

  state.bank.unshift({
    id: uid(),
    createdAt: nowText(),
    mode: refs.modeSelect.value,
    question,
    images: state.images,
  });
  persistBank();
  toast("已加入题库");
}

function persistBank() {
  try {
    saveJson(storageKeys.bank, state.bank);
    renderBank();
    syncBank({ silent: true }).catch(() => {});
  } catch {
    toast("题库过大，建议导出后清理图片题");
  }
}

function addHistory(options = {}) {
  if (!state.answer) {
    if (!options.silent) toast("当前没有答案可保存");
    return;
  }

  state.history.unshift({
    id: uid(),
    createdAt: nowText(),
    mode: refs.modeSelect.value,
    question: refs.questionInput.value.trim(),
    imageCount: state.images.length,
    answer: cleanMarkdownAnswer(state.answer),
    model: state.settings.model,
  });

  state.history = state.history.slice(0, 120);
  saveJson(storageKeys.history, state.history);
  renderHistory();
  syncHistory({ silent: true }).catch(() => {});
  if (!options.silent) toast("已保存记录");
}

function renderBank() {
  const keyword = refs.bankSearch.value.trim().toLowerCase();
  const items = state.bank.filter((item) => `${item.question} ${modeText[item.mode]}`.toLowerCase().includes(keyword));
  refs.bankList.innerHTML = "";

  if (!items.length) {
    refs.bankList.innerHTML = '<div class="empty-state">暂无题库内容。</div>';
    return;
  }

  for (const item of items) {
    refs.bankList.appendChild(rowTemplate({
      title: item.question || `图片题 ${item.images?.length || 0} 张`,
      meta: `${modeText[item.mode] || "题目"} · ${item.createdAt}`,
      actions: [
        ["练习", () => loadBankItem(item)],
        ["删除", () => deleteBankItem(item.id), "danger"],
      ],
    }));
  }
}

function renderHistory() {
  const keyword = refs.historySearch.value.trim().toLowerCase();
  const items = state.history.filter((item) => `${item.question} ${item.answer} ${modeText[item.mode]}`.toLowerCase().includes(keyword));
  refs.historyList.innerHTML = "";

  if (!items.length) {
    refs.historyList.innerHTML = '<div class="empty-state">暂无历史记录。</div>';
    return;
  }

  for (const item of items) {
    refs.historyList.appendChild(rowTemplate({
      title: item.question || `图片题 ${item.imageCount || 0} 张`,
      meta: `${modeText[item.mode] || "题目"} · ${item.model || "model"} · ${item.createdAt}`,
      actions: [
        ["查看", () => loadHistoryItem(item)],
        ["删除", () => deleteHistoryItem(item.id), "danger"],
      ],
    }));
  }
}

function rowTemplate({ title, meta, actions }) {
  const row = document.createElement("div");
  row.className = "list-row";

  const body = document.createElement("div");
  const rowTitle = document.createElement("p");
  rowTitle.className = "row-title";
  rowTitle.textContent = title;
  const rowMeta = document.createElement("div");
  rowMeta.className = "row-meta";
  rowMeta.textContent = meta;
  body.append(rowTitle, rowMeta);

  const actionWrap = document.createElement("div");
  actionWrap.className = "row-actions";
  for (const [label, handler, variant] of actions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `small ${variant || ""}`.trim();
    button.textContent = label;
    button.addEventListener("click", handler);
    actionWrap.appendChild(button);
  }

  row.append(body, actionWrap);
  return row;
}

function loadBankItem(item) {
  refs.modeSelect.value = item.mode || "general";
  refs.questionInput.value = item.question || "";
  state.images = Array.isArray(item.images) ? item.images : [];
  renderThumbs();
  switchView("solve");
}

function loadHistoryItem(item) {
  refs.modeSelect.value = item.mode || "general";
  refs.questionInput.value = item.question || "";
  state.images = [];
  state.answer = cleanMarkdownAnswer(item.answer || "");
  renderThumbs();
  setAnswer(state.answer);
  refs.answerMeta.textContent = `${item.model || "model"} · ${item.createdAt}`;
  switchView("solve");
}

function deleteBankItem(id) {
  state.bank = state.bank.filter((item) => item.id !== id);
  persistBank();
}

function deleteHistoryItem(id) {
  state.history = state.history.filter((item) => item.id !== id);
  saveJson(storageKeys.history, state.history);
  renderHistory();
  syncHistory({ silent: true }).catch(() => {});
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $(`${view}View`).classList.add("active");

  const titles = {
    solve: "题目解析",
    bank: "题库",
    history: "历史记录",
    settings: "模型配置",
  };
  refs.viewTitle.textContent = titles[view];
  refs.runBtn.style.display = view === "solve" ? "" : "none";
  refs.clearBtn.style.display = view === "solve" ? "" : "none";
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function importJsonFile(file) {
  const text = await file.text();
  const data = JSON.parse(text);
  if (!Array.isArray(data)) throw new Error("JSON 必须是数组");
  if (state.importTarget === "bank") {
    state.bank = data;
    persistBank();
    toast("题库已导入");
  }
}

async function copyText(text, fallbackMessage) {
  if (!text) {
    toast(fallbackMessage);
    return;
  }
  await navigator.clipboard.writeText(text);
  toast("已复制");
}

async function testConnection() {
  const settings = readSettingsForm();
  const error = validateSettings(settings);
  if (error) {
    toast(error);
    return;
  }
  saveJson(storageKeys.settings, settings);
  setBusy(true);
  refs.settingsStatus.textContent = "测试中";
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: settings }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "测试失败");
    refs.settingsStatus.textContent = `可用 · ${data.model || settings.model}`;
    toast("连接可用");
  } catch (err) {
    refs.settingsStatus.textContent = "不可用";
    toast(err.message);
  } finally {
    setBusy(false);
  }
}

function currentSyncPayload() {
  return {
    question: refs.questionInput.value.trim(),
    answer: cleanMarkdownAnswer(state.answer || ""),
    model: state.settings.model || refs.modelInput.value.trim(),
    imageCount: state.images.length,
    mode: refs.modeSelect.value,
    updatedAt: nowText(),
  };
}

async function syncCurrentState(options = {}) {
  if (!state.account.token) return;
  await syncApi("/api/sync/state", { method: "POST", body: currentSyncPayload() });
  if (!options.silent) toast("当前内容已同步");
}

async function syncHistory(options = {}) {
  if (!state.account.token) return;
  await syncApi("/api/sync/history", { method: "POST", body: { history: state.history } });
  if (!options.silent) toast("历史已同步");
}

async function syncBank(options = {}) {
  if (!state.account.token) return;
  await syncApi("/api/sync/bank", { method: "POST", body: { bank: state.bank } });
  if (!options.silent) toast("题库已同步");
}

async function pushAllSync() {
  if (!state.account.token) {
    toast("请先登录账号");
    return;
  }
  try {
    await syncCurrentState({ silent: true });
    await syncHistory({ silent: true });
    await syncBank({ silent: true });
    toast("已推送当前数据");
  } catch (err) {
    toast(err.message);
  }
}

async function pullAllSync(options = {}) {
  if (!state.account.token) {
    if (!options.silent) toast("请先登录账号");
    return;
  }
  try {
    const data = await syncApi("/api/sync/profile");
    const profile = data.profile || {};
    const current = profile.current || {};
    state.history = Array.isArray(profile.history)
      ? profile.history.map((item) => ({ ...item, answer: cleanMarkdownAnswer(item.answer || "") }))
      : [];
    state.bank = Array.isArray(profile.bank) ? profile.bank : [];
    saveJson(storageKeys.history, state.history);
    saveJson(storageKeys.bank, state.bank);

    if (current.question) refs.questionInput.value = current.question;
    if (current.answer) {
      state.answer = cleanMarkdownAnswer(current.answer);
      setAnswer(state.answer);
      refs.answerMeta.textContent = `${current.model || "model"} · ${current.updatedAt || nowText()}`;
    }
    renderHistory();
    renderBank();
    if (!options.silent) toast("已拉取账号数据");
  } catch (err) {
    if (!options.silent) toast(err.message);
  }
}

async function refreshDesktopLatest(options = {}) {
  try {
    const res = await fetch("/api/latest", { cache: "no-store" });
    const data = await res.json();
    if (!data.ok) return;
    const hasContent = Boolean((data.question || "").trim() || (data.answer || "").trim());
    const hasRealUpdate = Boolean(data.updated_at);
    if (!hasContent || !hasRealUpdate) return;

    const stamp = `${data.updated_at}|${data.model || ""}|${data.image_count || 0}`;
    if (stamp === state.latestDesktopUpdatedAt) return;
    state.latestDesktopUpdatedAt = stamp;

    refs.questionInput.value = data.question || "";
    state.images = [];
    state.answer = cleanMarkdownAnswer(data.answer || "");
    setAnswer(state.answer);
    refs.answerMeta.textContent = `${data.model || "桌面端"} · ${data.updated_at}`;
    renderThumbs();
    if (!options.silent) toast("已载入桌面端最新答案");
  } catch (err) {
    if (!options.silent) toast(err.message);
  }
}

async function loginAccount() {
  try {
    const email = refs.accountUsernameInput.value.trim();
    const data = await syncApi("/api/sync/login", {
      method: "POST",
      body: {
        email,
        username: email,
        password: refs.accountPasswordInput.value,
      },
    });
    saveAccount({ email: data.email || data.username || email, username: data.username || email, token: data.token });
    refs.accountPasswordInput.value = "";
    await pullAllSync({ silent: true });
    toast("已登录并拉取账号数据");
  } catch (err) {
    toast(err.message);
  }
}

async function registerAccount() {
  try {
    const email = refs.accountUsernameInput.value.trim();
    const data = await syncApi("/api/sync/register", {
      method: "POST",
      body: {
        email,
        username: email,
        password: refs.accountPasswordInput.value,
      },
    });
    saveAccount({ email: data.email || data.username || email, username: data.username || email, token: data.token });
    refs.accountPasswordInput.value = "";
    await pushAllSync();
    toast("已注册并登录");
  } catch (err) {
    toast(err.message);
  }
}

async function logoutAccount() {
  try {
    if (state.account.token) {
      await syncApi("/api/sync/logout", { method: "POST", body: {} });
    }
  } catch {
    // Local logout should still proceed if the saved token is already invalid.
  }
  saveAccount({ email: "", username: "", token: "" });
  toast("已退出账号");
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });

  document.querySelectorAll(".preset").forEach((button) => {
    button.addEventListener("click", () => {
      const preset = presets[button.dataset.preset];
      refs.baseUrlInput.value = preset.baseUrl;
      refs.modelInput.value = preset.model;
      toast("已填入预设");
    });
  });

  refs.runBtn.addEventListener("click", runAnalysis);
  refs.clearBtn.addEventListener("click", clearWorkspace);
  refs.saveBankBtn.addEventListener("click", addBankItem);
  refs.saveHistoryBtn.addEventListener("click", () => addHistory());
  refs.copyPromptBtn.addEventListener("click", () => copyText(buildPrompt(), "没有可复制的提示"));
  refs.copyAnswerBtn.addEventListener("click", () => copyText(state.answer, "没有可复制的答案"));
  refs.accountLoginBtn.addEventListener("click", loginAccount);
  refs.accountRegisterBtn.addEventListener("click", registerAccount);
  refs.accountLogoutBtn.addEventListener("click", logoutAccount);
  refs.syncPullBtn.addEventListener("click", () => pullAllSync());
  refs.syncPushBtn.addEventListener("click", pushAllSync);

  refs.pickImageBtn.addEventListener("click", () => refs.imageInput.click());
  refs.imageInput.addEventListener("change", (event) => addImageFiles(event.target.files));

  refs.dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    refs.dropZone.classList.add("dragover");
  });
  refs.dropZone.addEventListener("dragleave", () => refs.dropZone.classList.remove("dragover"));
  refs.dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    refs.dropZone.classList.remove("dragover");
    addImageFiles(event.dataTransfer.files);
  });

  document.addEventListener("paste", (event) => {
    if (state.view !== "solve") return;
    const files = Array.from(event.clipboardData?.files || []);
    if (files.some((file) => file.type.startsWith("image/"))) {
      addImageFiles(files);
    }
  });

  refs.questionInput.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.key === "Enter") {
      event.preventDefault();
      runAnalysis();
    }
  });

  refs.bankSearch.addEventListener("input", renderBank);
  refs.historySearch.addEventListener("input", renderHistory);
  refs.exportBankBtn.addEventListener("click", () => downloadJson("practice-bank.json", state.bank));
  refs.exportHistoryBtn.addEventListener("click", () => downloadJson("practice-history.json", state.history));
  refs.importBankBtn.addEventListener("click", () => {
    state.importTarget = "bank";
    refs.jsonImportInput.click();
  });
  refs.jsonImportInput.addEventListener("change", async (event) => {
    try {
      const file = event.target.files[0];
      if (file) await importJsonFile(file);
    } catch (err) {
      toast(err.message);
    } finally {
      refs.jsonImportInput.value = "";
    }
  });

  refs.clearHistoryBtn.addEventListener("click", () => {
    state.history = [];
    saveJson(storageKeys.history, state.history);
    renderHistory();
    syncHistory({ silent: true }).catch(() => {});
    toast("历史已清除");
  });

  refs.saveSettingsBtn.addEventListener("click", () => {
    saveJson(storageKeys.settings, readSettingsForm());
    refs.settingsStatus.textContent = "已保存";
    toast("配置已保存");
  });
  refs.testSettingsBtn.addEventListener("click", testConnection);
  refs.forgetKeyBtn.addEventListener("click", () => {
    refs.apiKeyInput.value = "";
    readSettingsForm();
    saveJson(storageKeys.settings, state.settings);
    toast("Key 已清除");
  });
}

function init() {
  hydrateSettingsForm();
  hydrateAccountForm();
  bindEvents();
  renderBank();
  renderHistory();
  renderThumbs();
  if (state.account.token) {
    pullAllSync({ silent: true });
  }
  refreshDesktopLatest({ silent: true });
  window.setInterval(() => refreshDesktopLatest({ silent: true }), 2000);
}

init();
