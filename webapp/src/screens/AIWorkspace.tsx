import { useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import NodeWorkspace from "./AskRynConversation";
import BrowserWorkspace from "./PrivateAIChat";
import "./aiWorkspace.css";

/** Route aliases share one shell; browser originals are never moved implicitly. */
export default function AIWorkspace() {
  const { client } = useAppContext();
  const [params] = useSearchParams();
  return client.mode === "live" && params.get("storage") === "browser"
    ? <BrowserWorkspace unified /> : <NodeWorkspace />;
}
