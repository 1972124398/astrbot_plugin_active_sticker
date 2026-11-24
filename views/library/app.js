
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
const editor = $("editor");
const gallery = $("gallery");
let items = [];
let selection;
let timer;
let importing = false;
let visionReady = false;
let visible = 30;
let refreshGeneration = 0;
const thumbnails = new Map();

function report(message, target = $("notice")) {
  target.hidden = !message;
  target.textContent = message;
}

function itemStatus(item) {
  if (!item.enabled) return "disabled";
  return item.description && !item.missing ? "ready" : "waiting";
}

function statusText(item) {
  if (item.missing) return "源文件缺失，可重新导入";
  if (item.pending) return "正在识图";
  if (item.error) return item.error;
  return { ready: "可使用", waiting: "待识图", disabled: "已停用" }[itemStatus(item)];
}

function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

async function showPreview(picture, id) {
  if (!thumbnails.has(id)) {
    thumbnails.set(id, bridge.apiGet("preview", { id }).catch(() => {
      thumbnails.delete(id);
      return null;
    }));
  }
  const preview = await thumbnails.get(id);
  if (!picture.isConnected || picture.dataset.id !== id) return;
  if (!preview?.src) {
    picture.textContent = "无法预览";
    return;
  }
  const image = element("img");
  image.alt = "";
  image.src = preview.src;
  picture.replaceChildren(image);
}


const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const picture = entry.target;
    const id = picture.dataset.id;
    observer.unobserve(picture);
    void showPreview(picture, id);
  }
}, { rootMargin: "240px" });

function openItem(item) {
  selection = item;
  $("delete-confirmation").hidden = true;
  $("image-name").textContent = item.name;
  const picture = $("editor-picture");
  picture.dataset.id = item.id;
  picture.textContent = item.missing ? "源文件缺失" : "正在读取预览…";
  if (!item.missing) void showPreview(picture, item.id);
  $("description").value = item.description;
  $("usage").value = item.use_when;
  $("tags").value = item.tags.join("、");
  $("categories").value = (item.categories || []).join("、");
  $("enabled").checked = item.enabled;
  $("identify").disabled = item.pending || item.missing || !visionReady;
  report("", $("edit-error"));
  editor.showModal();
}

function render() {
  const query = $("search").value.trim().toLowerCase();
  const state = $("status").value;
  const category = $("category").value;
  const filtered = items.filter((item) => {
    const haystack = [item.name, item.description, item.use_when, ...item.tags].join(" ").toLowerCase();
    return (!state || itemStatus(item) === state) && (!query || haystack.includes(query))
      && (!category || (item.categories || []).includes(category));
  });
  observer.disconnect();
  const cards = filtered.slice(0, visible).map((item) => {
    const card = element("button", "", "card");
    card.type = "button";
    const picture = element("span", item.missing ? "无法预览" : "预览", "picture");
    picture.dataset.id = item.id;
    card.append(picture, element("strong", item.name));
    if (item.description) card.append(element("span", item.description, "summary"));
    card.append(element("small", [statusText(item), item.animated ? "动图" : ""].filter(Boolean).join(" · ")));
    card.addEventListener("click", () => openItem(item));
    if (!item.missing) observer.observe(picture);
    return card;
  });
  gallery.replaceChildren(...cards);
  $("more").hidden = filtered.length <= visible;
  $("empty").hidden = filtered.length > 0;
  $("empty").textContent = items.length ? "没有符合筛选的表情。" : "导入图片后，这里会显示预览和识图描述。";
  const ready = items.filter((item) => itemStatus(item) === "ready").length;
  const pending = items.filter((item) => item.pending).length;
  $("counts").textContent = items.length + " 张图片 · " + ready + " 张可用" + (pending ? " · " + pending + " 张识图中" : "");
  $("retry").disabled = !visionReady || !items.some((item) => itemStatus(item) === "waiting" && !item.pending && !item.missing);
}

async function refresh() {
  const generation = ++refreshGeneration;
  window.clearTimeout(timer);
  try {
    const snapshot = await bridge.apiGet("library");
    if (snapshot.categories && $("category").options.length === 1) {
      for (const name of snapshot.categories) {
        $("category").add(new Option(name, name));
        $("category-names").append(new Option(name, name));
      }
    }
    if (generation !== refreshGeneration) return;
    const changed = snapshot.vision_configured !== visionReady || JSON.stringify(snapshot.items) !== JSON.stringify(items);
    visionReady = snapshot.vision_configured;
    items = snapshot.items;
    if (changed || gallery.childElementCount === 0) render();
    $("vision-hint").hidden = visionReady;
    if (items.some((item) => item.pending) || importing) timer = window.setTimeout(refresh, 1800);
  } catch (error) {
    if (generation === refreshGeneration) report(error.message);
  }
}

async function importFiles(files) {
  if (importing || !files.length) return;
  importing = true;
  $("drop").disabled = true;
  let added = 0;
  let duplicates = 0;
  const errors = [];
  try {
    for (const [index, file] of [...files].entries()) {
      report("导入 " + (index + 1) + " / " + files.length + " · " + file.name);
      try {
        const result = await bridge.upload("upload", file);
        thumbnails.delete(result.id);
        if (result.added) added++;
        else duplicates++;
      } catch (error) {
        errors.push(file.name + " · " + error.message);
      }
    }
  } finally {
    importing = false;
    $("drop").disabled = false;
    $("files").value = "";
    report("新增 " + added + " 张 · 重复 " + duplicates + " 张" + (errors.length ? " · " + errors.join(" / ") : ""));
    await refresh();
  }
}

async function action(run, errorTarget = $("notice")) {
  try {
    await run();
  } catch (error) {
    report(error.message, errorTarget);
  }
}

async function updateItem(endpoint, payload) {
  await bridge.apiPost(endpoint, { id: selection.id, ...payload });
  editor.close();
  await refresh();
}

$("drop").addEventListener("click", () => $("files").click());
$("files").addEventListener("change", () => importFiles($("files").files));
$("drop").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("drop").classList.add("dragging");
});
$("drop").addEventListener("dragleave", () => $("drop").classList.remove("dragging"));
$("drop").addEventListener("drop", (event) => {
  event.preventDefault();
  $("drop").classList.remove("dragging");
  importFiles(event.dataTransfer.files);
});
for (const id of ["search", "status", "category"]) {
  $(id).addEventListener("input", () => { visible = 30; render(); });
}
$("more").addEventListener("click", () => { visible += 30; render(); });
$("close").addEventListener("click", () => editor.close());
$("save").addEventListener("click", () => {
  action(() => updateItem("edit", {
    description: $("description").value,
    use_when: $("usage").value,
    tags: $("tags").value,
    categories: $("categories").value,
    enabled: $("enabled").checked,
  }), $("edit-error"));
});
$("identify").addEventListener("click", () => action(() => updateItem("index"), $("edit-error")));
$("delete").addEventListener("click", () => {
  $("delete-confirmation").hidden = false;
  $("cancel-delete").focus();
});
$("cancel-delete").addEventListener("click", () => {
  $("delete-confirmation").hidden = true;
  $("delete").focus();
});
$("confirm-delete").addEventListener("click", () => action(() => updateItem("delete"), $("edit-error")));
$("retry").addEventListener("click", () => action(async () => {
  const result = await bridge.apiPost("index", {});
  report(result.queued + " 张图片已加入识图队列");
  await refresh();
}));
$("export").addEventListener("click", () => action(() => bridge.download("catalog", {}, "catalog.md")));

const settingInputs = {
  enabled: $("auto-enabled"),
  vision_provider_id: $("vision_provider_id"),
  expression_provider_id: $("expression_provider_id"),
  expression_style: $("expression_style"),
  burst_limit: $("burst_limit"),
  interop_enabled: $("interop_enabled"),
  link_short_term_mood: $("link_short_term_mood"),
  link_self_cognition: $("link_self_cognition"),
};
const tabs = [$("library-tab"), $("settings-tab")];
let savedSettings;
let loadingSettings = false;

function providerOptions(input, providers, selected, fallback) {
  input.replaceChildren(new Option(fallback, ""));
  for (const { id, model, vision } of providers) {
    const label = model && model !== id ? id + " · " + model : id;
    const marked = input === settingInputs.vision_provider_id && vision ? label + "（支持图片）" : label;
    input.add(new Option(marked, id));
  }
  if (selected && !providers.some(({ id }) => id === selected)) {
    input.add(new Option(selected + "（当前不可用）", selected));
  }
  input.value = selected || "";
}

function updateLinkToggles() {
  for (const input of [settingInputs.link_short_term_mood, settingInputs.link_self_cognition]) {
    input.disabled = !settingInputs.interop_enabled.checked;
  }
}

async function loadSettings() {
  if (loadingSettings) return;
  loadingSettings = true;
  $("settings-retry").hidden = true;
  $("settings-status").textContent = "正在读取设置…";
  try {
    const { values, providers } = await bridge.apiGet("settings");
    providerOptions(settingInputs.vision_provider_id, providers, values.vision_provider_id, "自动选择支持图片的模型");
    providerOptions(settingInputs.expression_provider_id, providers, values.expression_provider_id, "跟随会话模型");
    for (const [key, input] of Object.entries(settingInputs)) {
      if (input.type === "checkbox") input.checked = values[key];
      else input.value = values[key];
    }
    $("provider-note").hidden = providers.length > 0;
    updateLinkToggles();
    savedSettings = values;
    $("settings-fields").hidden = false;
    $("settings-save").disabled = false;
    $("settings-status").textContent = "";
  } catch (error) {
    $("settings-status").textContent = error.message;
    $("settings-retry").hidden = false;
  } finally {
    loadingSettings = false;
  }
}

function showTab(selected) {
  for (const tab of tabs) {
    const active = tab === selected;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !active;
  }
  if (selected === $("settings-tab") && !savedSettings) void loadSettings();
}

async function saveSettings() {
  if (!savedSettings || !settingInputs.burst_limit.reportValidity()) return;
  const changed = {};
  for (const [key, input] of Object.entries(settingInputs)) {
    let value = input.value;
    if (input.type === "checkbox") value = input.checked;
    if (input.type === "number") value = input.valueAsNumber;
    if (value !== savedSettings[key]) changed[key] = value;
  }
  const button = $("settings-save");
  button.disabled = true;
  $("settings-status").textContent = "正在保存…";
  try {
    await bridge.apiPost("settings/save", changed);
    Object.assign(savedSettings, changed);
    $("settings-status").textContent = "设置已保存";
    if ("vision_provider_id" in changed) void refresh();
  } catch (error) {
    $("settings-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

for (const tab of tabs) {
  tab.addEventListener("click", () => showTab(tab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[1] : tabs.find((entry) => entry !== tab);
    showTab(next);
    next.focus();
  });
}
$("open-settings").addEventListener("click", () => {
  showTab($("settings-tab"));
  $("settings-tab").focus();
});
settingInputs.interop_enabled.addEventListener("change", updateLinkToggles);
$("settings-fields").addEventListener("input", () => { $("settings-status").textContent = ""; });
$("settings-retry").addEventListener("click", loadSettings);
$("settings-save").addEventListener("click", saveSettings);


function updateTheme(context) {
  document.documentElement.dataset.dark = String(Boolean(context.isDark));
}

try {
  updateTheme(await bridge.ready());
  const unsubscribe = bridge.onContext(updateTheme);
  window.addEventListener("beforeunload", unsubscribe, { once: true });
  await refresh();
} catch (error) {
  report(error.message);
}
