import { createRoot } from "react-dom/client";
import { BrowserRouter, Outlet, Route, Routes } from "react-router-dom";
import NodeApps from "./screens/NodeApps";
function Shell() {
  return <Outlet context={{ client: { mode: "live" } }} />;
}
createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={<NodeApps standalone />} />
        <Route path="/apps" element={<NodeApps standalone />} />
      </Route>
    </Routes>
  </BrowserRouter>,
);
