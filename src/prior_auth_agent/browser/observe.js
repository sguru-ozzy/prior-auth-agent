() => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== "hidden";
  const text = e => (e?.textContent || "").replace(/\s+/g, " ").trim();
  const nodes = [...document.querySelectorAll("input, select, textarea, button, a, [role=button], [role=textbox]")];
  return nodes.filter(visible).map((e, i) => {
    const tag = e.tagName.toLowerCase();
    const type = e.getAttribute("type") || (tag === "input" ? "text" : tag);
    const role = e.getAttribute("role") || (tag === "button" || ["submit","button"].includes(type) ? "button" :
      tag === "a" ? "link" : tag === "select" ? "combobox" : "textbox");
    const labelled = (e.getAttribute("aria-labelledby") || "").split(/\s+/).map(id => text(document.getElementById(id))).join(" ").trim();
    const label = e.getAttribute("aria-label") || labelled || [...(e.labels || [])].map(text).join(" ") ||
      (["button","link"].includes(role) ? text(e) : "");
    const scope = e.closest("fieldset, section, [role=group], form");
    const section = scope ? (scope.getAttribute("aria-label") || text(scope.querySelector("legend, h1, h2, h3"))) : "";
    return {ref: "c" + i, label, role, input_type: type, section, enabled: !e.disabled && e.getAttribute("aria-disabled") !== "true",
      has_value: ["input","textarea","select"].includes(tag) ? !!e.value : false};
  });
}
