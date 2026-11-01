import { useMemo } from "react";
import { usePersonal } from "../model";
import { useDrop } from "../dropStore";
import { buildDropDevices, type DropDevice } from "./dropModel";

/** The workspace device list, derived from usePersonal().devices + PeerMeta. */
export function useDropDevices() {
  const { devices } = usePersonal();
  const { data } = useDrop();
  return useMemo(() => {
    const list = buildDropDevices(devices, data.peers);
    const byId = new Map<string, DropDevice>(list.map(device => [device.id, device]));
    const self = list.find(device => device.self);
    const nameOf = (id: string) => byId.get(id)?.name || data.peers[id]?.name || (id.length > 14 ? `${id.slice(0, 10)}…` : id);
    return { list, byId, self, nameOf };
  }, [devices, data.peers]);
}
