export type LibraryView = "all" | "saved" | "continue" | "history" | "files" | "archived";

export interface LibraryItem {
  version: "ryn.library-item.v1";
  library_id: string;
  content_id?: string | null;
  import_id?: string;
  kind: string;
  title: string;
  summary: string;
  source: { id: string; title: string; url: string };
  state: { saved: boolean; progress: number; completed: boolean; archived: boolean };
  capabilities: { open: boolean; ask: boolean; share: boolean };
  local_copy: { available: boolean; size_bytes: number };
  last_activity_unix: number;
  tags: string[];
  import_state?: string;
}

export interface LibraryPage {
  version: "ryn.library-page.v1";
  items: LibraryItem[];
  next_cursor: string | null;
  limit: number;
}

export interface LibrarySource {
  id: string;
  title: string;
  kind: string;
  feed_url?: string;
  paused: boolean;
  builtin?: boolean;
  health?: { ok?: boolean; error?: string; item_count?: number; last_success_unix?: number; paused?: boolean };
}

export interface LibraryWatcher {
  id: string;
  title: string;
  url: string;
  note?: string;
  paused?: boolean;
}

export type LibraryAction = "archive" | "unarchive" | "add_tag" | "remove_tag" | "delete_import" | "mark_completed" | "restart" | "remove_saved";
