(() => {
  "use strict";

  const INDEX_URL = "data/search-index.json";
  const queryInput = document.getElementById("search-query");
  const sourceSelect = document.getElementById("filter-source");
  const categorySelect = document.getElementById("filter-category");
  const fromInput = document.getElementById("filter-from");
  const toInput = document.getElementById("filter-to");
  const form = document.getElementById("search-form");
  const meta = document.getElementById("search-meta");
  const results = document.getElementById("search-results");
  const empty = document.getElementById("search-empty");

  if (!queryInput || !sourceSelect || !categorySelect || !form || !meta || !results || !empty) {
    return;
  }

  let entries = [];
  let indexTotal = 0;

  function normalize(value) {
    return String(value ?? "").toLocaleLowerCase();
  }

  function safeLink(value) {
    try {
      const url = new URL(String(value), window.location.href);
      if (url.protocol === "http:" || url.protocol === "https:") {
        return url.href;
      }
    } catch (error) {
      // 不合法链接直接当作没有链接，不写入 DOM。
    }
    return "";
  }

  function addOption(select, value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }

  function fillSelect(select, label) {
    select.replaceChildren();
    addOption(select, "", label);
    const values = Array.from(
      new Set(entries.map((item) => String(item[select.dataset.field] || "")).filter(Boolean))
    ).sort((a, b) => a.localeCompare(b, "zh-CN"));
    values.forEach((value) => addOption(select, value, value));
  }

  function matchesFilters(item) {
    const query = normalize(queryInput.value.trim());
    const source = sourceSelect.value;
    const category = categorySelect.value;
    const from = fromInput.value;
    const to = toInput.value;

    if (source && String(item.source_name || "") !== source) {
      return false;
    }
    if (category && String(item.category || "") !== category) {
      return false;
    }

    const itemDate = String(item.date || "");
    if (from && (!itemDate || itemDate < from)) {
      return false;
    }
    if (to && (!itemDate || itemDate > to)) {
      return false;
    }

    if (!query) {
      return true;
    }
    const haystack = normalize(
      [
        item.title,
        item.summary,
        item.source_name,
        item.category,
        item.date,
        item.published,
      ].join(" ")
    );
    return haystack.includes(query);
  }

  function makeResultItem(item) {
    const li = document.createElement("li");
    li.className = "item";

    const title = document.createElement("a");
    title.className = "item-title";
    title.textContent = item.title || "（无标题）";
    const link = safeLink(item.link);
    if (link) {
      title.href = link;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    }

    const summary = document.createElement("p");
    summary.className = "item-summary";
    summary.textContent = item.summary || "—";

    const itemMeta = document.createElement("p");
    itemMeta.className = "item-meta";
    itemMeta.textContent = [item.source_name, item.category, item.date]
      .filter(Boolean)
      .join(" · ");

    li.appendChild(title);
    li.appendChild(summary);
    li.appendChild(itemMeta);
    return li;
  }

  function render() {
    const visible = entries.filter(matchesFilters);
    results.replaceChildren();
    visible.forEach((item) => results.appendChild(makeResultItem(item)));

    const suffix = indexTotal > entries.length ? "（索引已按上限截取）" : "";
    meta.textContent = `找到 ${visible.length} 条 · 索引 ${entries.length} 条${suffix}`;
    empty.hidden = visible.length !== 0;
  }

  async function loadIndex() {
    try {
      const response = await fetch(INDEX_URL, { cache: "no-cache" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const payload = await response.json();
      entries = Array.isArray(payload.items) ? payload.items : [];
      indexTotal = Number(payload.total ?? entries.length);
      sourceSelect.dataset.field = "source_name";
      categorySelect.dataset.field = "category";
      fillSelect(sourceSelect, "全部来源");
      fillSelect(categorySelect, "全部分类");
      meta.textContent = `索引 ${entries.length} 条 / 归档 ${indexTotal} 条`;
      render();
    } catch (error) {
      entries = [];
      results.replaceChildren();
      empty.hidden = false;
      meta.textContent = "搜索索引加载失败，请稍后重试。";
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    render();
  });
  [queryInput, sourceSelect, categorySelect, fromInput, toInput].forEach((element) => {
    element.addEventListener("input", render);
    element.addEventListener("change", render);
  });

  loadIndex();
})();
