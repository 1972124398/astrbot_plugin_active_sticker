
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
const editor = $("editor");
const gallery = $("gallery");
let items = [];
let selection;
let timer;
let importing = false;
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

const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const picture = entry.target;
    const id = picture.dataset.id;
    observer.unobserve(picture);
    if (!thumbnails.has(id)) {
      thumbnails.set(id, bridge.apiGet("preview", { id }).catch(() => null));
    }
    thumbnails.get(id).then((preview) => {
      if (!picture.isConnected || !preview?.src) return;
      const image = element("img");
      image.alt = "";
      image.src = preview.src;
      picture.replaceChildren(image);
    });
  }
}, { rootMargin: "240px" });

function openItem(item) {
  selection = item;
  $("delete-confirmation").hidden = true;
  $("image-name").textContent = item.name;
  $("description").value = item.description;
  $("usage").value = item.use_when;
  $("tags").value = item.tags.join("、");
  $("enabled").checked = item.enabled;
  $("identify").disabled = item.pending || item.missing;
  report("", $("edit-error"));
  editor.showModal();
}

function render() {
  const query = $("search").value.trim().toLowerCase();
  const state = $("status").value;
  const filtered = items.filter((item) => {
    const haystack = [item.name, item.description, item.use_when, ...item.tags].join(" ").toLowerCase();
    return (!state || itemStatus(item) === state) && (!query || haystack.includes(query));
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
  $("retry").disabled = !items.some((item) => itemStatus(item) === "waiting" && !item.pending && !item.missing);
}

async function refresh() {
  const generation = ++refreshGeneration;
  window.clearTimeout(timer);
  try {
    const snapshot = await bridge.apiGet("library");
    if (generation !== refreshGeneration) return;
    const changed = JSON.stringify(snapshot.items) !== JSON.stringify(items);
    items = snapshot.items;
    if (changed || gallery.childElementCount === 0) render();
    if (!snapshot.vision_configured && !importing && $("notice").hidden) {
      report("自动识图需要在插件配置中选择可读取图片的模型。也可手动填写描述。");
    }
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
for (const id of ["search", "status"]) {
  $(id).addEventListener("input", () => { visible = 30; render(); });
}
$("more").addEventListener("click", () => { visible += 30; render(); });
$("close").addEventListener("click", () => editor.close());
$("save").addEventListener("click", () => {
  action(() => updateItem("edit", {
    description: $("description").value,
    use_when: $("usage").value,
    tags: $("tags").value,
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
