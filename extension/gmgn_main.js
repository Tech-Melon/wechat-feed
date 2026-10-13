(() => {
  // 跑在页面主环境：内容脚本读不到 React props，这里把每条推文的 tw_timestamp 写到 DOM 属性上。
  const ITEM_LIST = '[data-testid="x-tracker-root"] [data-testid="virtuoso-item-list"]';
  let list = null;
  let observers = [];
  let queued = false;

  function fiberOf(node) {
    for (const key in node) {
      if (key.startsWith("__reactFiber$")) return node[key];
    }
    return null;
  }

  function listData(item) {
    let fiber = fiberOf(item);
    while (fiber) {
      const props = fiber.memoizedProps;
      if (props && Array.isArray(props.data) && props.computeItemKey) return props.data;
      fiber = fiber.return;
    }
    return null;
  }

  function stampOf(row) {
    const value = Number(row && row.tw_timestamp);
    return Number.isFinite(value) && value > 0 ? value : 0;
  }

  function setAttr(node, name, value) {
    const text = String(value);
    if (node.getAttribute(name) !== text) node.setAttribute(name, text);
  }

  function tag() {
    queued = false;
    if (!list || !list.isConnected) return;
    const items = list.querySelectorAll(":scope > [data-index]");
    if (!items.length) return;
    const data = listData(items[0]);
    if (!data || !data.length) return;
    setAttr(list, "data-wf-newest", stampOf(data[0]));
    setAttr(list, "data-wf-oldest", stampOf(data[data.length - 1]));
    setAttr(list, "data-wf-count", data.length);
    for (const item of items) {
      const index = Number(item.getAttribute("data-index"));
      const row = data[index];
      if (!row) continue;
      setAttr(item, "data-wf-ts", stampOf(row));
      setAttr(item, "data-wf-prev", index > 0 ? stampOf(data[index - 1]) : "");
      setAttr(item, "data-wf-last", index === data.length - 1 ? "1" : "");
    }
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(tag);
  }

  function attach() {
    if (!location.pathname.startsWith("/follow")) return;
    const next = document.querySelector(ITEM_LIST);
    if (!next || next === list) return;
    observers.forEach((item) => item.disconnect());
    list = next;
    observers = [new MutationObserver(schedule), new MutationObserver(schedule)];
    observers[0].observe(list, { childList: true });
    observers[1].observe(list, { subtree: true, attributes: true, attributeFilter: ["data-index"] });
    schedule();
  }

  attach();
  setInterval(() => {
    attach();
    if (list && list.isConnected) schedule();
  }, 1000);
})();
