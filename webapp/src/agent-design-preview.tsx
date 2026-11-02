// Development-only visual harness. API fixtures are supplied by the screenshot runner.
// Not included in the production Vite entry points.
import ReactDOM from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import RynAgent from "./screens/RynAgent";
import "./i18n";
import "./styles/tokens.css";
import "./styles/tokens-v2.css";
import "./styles/reset.css";
import "./personal/personal.css";
document.documentElement.dataset.theme = new URLSearchParams(location.search).get("theme") || "light";
document.body.style.margin = "0";
document.getElementById("root")!.style.height = "100dvh";
ReactDOM.createRoot(document.getElementById("root")!).render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
