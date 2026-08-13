import { useOutletContext } from "react-router-dom";
import type { NodeClient } from "./domain/nodeClient";
import type {
  ConfirmRequest,
  NodeStatus,
  Peer,
  RegistryStatus,
  ToastMessage,
} from "./domain/types";

export interface AppOutletContext {
  client: NodeClient;
  node: NodeStatus;
  registry: RegistryStatus;
  peers: Peer[];
  refreshShell: () => Promise<void>;
  confirm: (request: ConfirmRequest) => void;
  notify: (tone: ToastMessage["tone"], text: string) => void;
}

export function useAppContext() {
  return useOutletContext<AppOutletContext>();
}
