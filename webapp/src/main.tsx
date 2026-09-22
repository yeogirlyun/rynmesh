import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AppRoutes } from "./App";
import "./styles/tokens.css";
import "./styles/tokens-v2.css";
import "./styles/reset.css";
import "./styles/app.css";
import "./personal/personal.css";
import { applyAppearance, readAppearance } from "./personal/model";
applyAppearance(readAppearance());

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  </React.StrictMode>,
);
