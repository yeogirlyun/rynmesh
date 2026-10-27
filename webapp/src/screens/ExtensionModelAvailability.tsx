import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { nodeControlUrl } from "../domain/nodeUrl";

/** Informational projection only; the host checks again before every dispatch. */
export function ExtensionModelAvailability({ modelID }: { modelID: string }) {
  const { i18n } = useTranslation();
  const [state, setState] = useState<{ model: string; blocked: string[]; failed: boolean } | null>(null);
  useEffect(() => {
    if (!modelID) return;
    const abort = new AbortController();
    void fetch(nodeControlUrl(`/agent/extensions/model-availability?model_id=${encodeURIComponent(modelID)}`),
      { credentials: "same-origin", signal: abort.signal }).then(async response => {
        if (!response.ok) throw new Error("unavailable");
        const data = await response.json() as { servers: { name: string; allowed: boolean }[] };
        if (!Array.isArray(data.servers)) throw new Error("unavailable");
        if (!abort.signal.aborted) setState({ model: modelID, blocked: data.servers.filter(row => row.allowed === false).map(row => row.name), failed: false });
      }).catch(() => { if (!abort.signal.aborted) setState({ model: modelID, blocked: [], failed: true }); });
    return () => abort.abort();
  }, [modelID]);
  if (!state || state.model !== modelID || (!state.failed && !state.blocked.length)) return null;
  const zh = i18n.language.startsWith("zh");
  return <p className="ra-model-context" role="status">{state.failed
    ? (zh ? "暂时无法核对插件的模型限制，调用时仍会校验。" : "Plugin model restrictions could not be checked. Calls will still be validated.")
    : (zh ? `以下插件的工具在此模型下不可用：${state.blocked.join("、")}` : `Tools unavailable with this model: ${state.blocked.join(", ")}`)}</p>;
}
