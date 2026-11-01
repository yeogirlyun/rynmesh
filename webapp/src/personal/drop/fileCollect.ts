// Browser-side selections. Files stay as File handles (streamed on upload);
// nothing is read into memory here.

export type PickedFile = { file: File; rel: string };

/** Reads a DataTransfer, descending into dropped folders when the browser allows it. */
export async function filesFromDataTransfer(transfer: DataTransfer): Promise<PickedFile[]> {
  const items = Array.from(transfer.items || []).filter(item => item.kind === "file");
  const entries = items.map(item => (item as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null }).webkitGetAsEntry?.() ?? null);
  if (!entries.some(entry => entry?.isDirectory)) {
    return Array.from(transfer.files || []).map(file => ({ file, rel: file.name }));
  }
  const out: PickedFile[] = [];
  const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
      out.push({ file, rel: `${prefix}${file.name}` });
      return;
    }
    if (!entry.isDirectory) return;
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    const children: FileSystemEntry[] = [];
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      children.push(...batch);
    }
    for (const child of children) await walk(child, `${prefix}${entry.name}/`);
  };
  for (const entry of entries) if (entry) await walk(entry, "");
  return out;
}

export function filesFromInput(list: FileList | null): PickedFile[] {
  return Array.from(list || []).map(file => ({
    file, rel: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
  }));
}

/** Opens a transient file input. Must run inside a user gesture. */
export function pickBrowserFiles(kind: "files" | "folder"): Promise<PickedFile[]> {
  return new Promise(resolve => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    if (kind === "folder") input.setAttribute("webkitdirectory", "");
    input.style.display = "none";
    let settled = false;
    const done = (files: PickedFile[]) => { if (settled) return; settled = true; input.remove(); resolve(files); };
    input.addEventListener("change", () => done(filesFromInput(input.files)));
    input.addEventListener("cancel", () => done([]));
    document.body.appendChild(input);
    input.click();
  });
}
