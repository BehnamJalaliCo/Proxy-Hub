import "@fontsource-variable/vazirmatn";
import "./index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { installChunkReload } from "./lib/chunkReload";
import { initTheme } from "./lib/theme";

initTheme();
installChunkReload(window);

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
