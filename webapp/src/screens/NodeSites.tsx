import { Navigate } from "react-router-dom";

export default function LegacyNodeSites() {
  return <Navigate replace to="/apps?tab=shared" />;
}
