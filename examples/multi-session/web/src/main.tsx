import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { App } from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/codex" replace />} />
        <Route path="/:engine" element={<App />} />
        <Route path="/:engine/sessions/:sessionId" element={<App />} />
        <Route
          path="/:engine/sessions/:sessionId/threads/:threadId"
          element={<App />}
        />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
