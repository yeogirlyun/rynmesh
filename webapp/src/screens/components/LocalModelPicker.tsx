import { Check, Copy, Download, RefreshCcw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button, Chip, IconButton } from "../../components/ui";
import { digestApi, type LocalModelCatalog } from "../../domain/digestClient";

function gb(bytes: number): string {
  if (!bytes) return "";
  return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
}

/**
 * Review what's installed locally and pick the one the node should use.
 * Without an explicit choice the node takes whatever Ollama lists first —
 * which is often the largest model and makes every digest crawl.
 */
export default function LocalModelPicker() {
  const [catalog, setCatalog] = useState<LocalModelCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setCatalog(await digestApi.listModels());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reach the local node.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const select = async (name: string) => {
    setBusy(name);
    setError("");
    try {
      await digestApi.selectModel(name);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not select that model.");
    } finally {
      setBusy("");
    }
  };

  if (loading) return <p className="model-hint">Checking installed models…</p>;

  if (!catalog?.ollama_running) {
    return (
      <div className="model-empty">
        <p className="model-hint">
          <b>Ollama isn't running.</b> It's the free, private way to give your node a brain —
          nothing leaves your machine.
        </p>
        <p className="model-hint">
          Install it from <a href="https://ollama.com/download" target="_blank" rel="noreferrer noopener">ollama.com/download</a>,
          then pull a model and refresh.
        </p>
        <pre className="model-cmd"><code>ollama pull gemma3:4b</code></pre>
        <Button icon={RefreshCcw} onClick={() => void refresh()}>Check again</Button>
      </div>
    );
  }

  const installedNames = new Set(catalog.installed.map((m) => m.name));
  const missing = catalog.recommended.filter((m) => !installedNames.has(m.name));
  const noteFor = (name: string) =>
    catalog.recommended.find((r) => r.name === name)?.note ?? "";

  return (
    <div className="model-picker">
      <div className="model-picker-head">
        <span className="model-hint">
          {catalog.selected
            ? "Your node uses the model you picked."
            : "No model chosen — the node is using whichever Ollama lists first."}
        </span>
        <IconButton icon={RefreshCcw} label="Refresh model list" onClick={() => void refresh()} />
      </div>

      <div className="model-list">
        {catalog.installed.map((model) => {
          const active = model.name === catalog.current;
          const chosen = model.name === catalog.selected;
          return (
            <button
              key={model.name}
              type="button"
              className={`model-row${active ? " is-active" : ""}`}
              onClick={() => void select(model.name)}
              disabled={busy !== ""}
            >
              <span className="model-row-mark">{active ? <Check size={15} /> : null}</span>
              <span className="model-row-body">
                <span className="model-row-name">
                  {model.name}
                  {active && !chosen ? <Chip tone="muted">auto</Chip> : null}
                  {chosen ? <Chip tone="ok">chosen</Chip> : null}
                </span>
                {noteFor(model.name) ? (
                  <span className="model-row-note">{noteFor(model.name)}</span>
                ) : null}
              </span>
              <span className="model-row-size">{gb(model.size_bytes)}</span>
            </button>
          );
        })}
      </div>

      {catalog.selected ? (
        <button type="button" className="model-clear" onClick={() => void select("")}>
          Clear choice and let the node pick automatically
        </button>
      ) : null}

      {missing.length ? (
        <div className="model-suggest">
          <p className="model-hint"><b>Not installed yet</b> — pull one to try it:</p>
          {missing.map((model) => (
            <div key={model.name} className="model-suggest-row">
              <Download size={13} />
              <code>ollama pull {model.name}</code>
              <span className="model-row-size">{model.size_hint}</span>
              <IconButton
                icon={copied === model.name ? Check : Copy}
                label={`Copy pull command for ${model.name}`}
                onClick={() => {
                  void navigator.clipboard?.writeText(`ollama pull ${model.name}`);
                  setCopied(model.name);
                  window.setTimeout(() => setCopied(""), 1500);
                }}
              />
            </div>
          ))}
        </div>
      ) : null}

      {catalog.anthropic_key_present ? (
        <p className="model-hint">
          An <code>ANTHROPIC_API_KEY</code> is set, so the node prefers Claude over these local
          models. Unset it to go fully local.
        </p>
      ) : null}
      {error ? <p className="model-error">{error}</p> : null}
    </div>
  );
}
