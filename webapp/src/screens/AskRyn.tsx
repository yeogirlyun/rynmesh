import { PageHeader } from "../components/ui";
import AskRynWorkspace from "../components/AskRynWorkspace";

export default function AskRyn() {
  return (
    <div>
      <PageHeader
        eyebrow="Private assistant"
        title="Ask Ryn"
        context="One encrypted conversation workspace for chat, local search, articles, and private files."
      />
      <AskRynWorkspace />
    </div>
  );
}

