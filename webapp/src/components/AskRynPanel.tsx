import AskRynWorkspace from "./AskRynWorkspace";
import type { AppOutletContext } from "../appContext";

export default function AskRynPanel({ appContext }: { appContext: AppOutletContext }) {
  return <AskRynWorkspace compact appContext={appContext} />;
}
