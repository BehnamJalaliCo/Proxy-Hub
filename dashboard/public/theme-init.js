// Applies the saved theme (light / dark / system) before the first paint, so there is no flash.
// Kept as a separate file because the page's Content-Security-Policy forbids inline scripts.
(function () {
  var choice = "system";
  try {
    choice = window.localStorage.getItem("ph-theme") || "system";
  } catch (e) {
    choice = "system";
  }
  var dark =
    choice === "dark" ||
    (choice !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();
