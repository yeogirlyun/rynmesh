import { beforeEach, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { NasPage, PluginsPage } from "./Nas";
import { setLanguagePreference } from "../i18n";
import { PersonalShell } from "./Shell";
import type { NasStatus } from "../domain/nas";
vi.mock("./model", () => ({
  usePersonal: () => ({
    demo: false,
    appearance: "light",
    setAppearance: vi.fn(),
    devices: [],
    services: [],
    space: null,
  }),
  personalHref: (path: string) => path,
}));
vi.mock("../appContext", () => ({
  useAppContext: () => ({
    notify: vi.fn(),
    confirm: (r: { onConfirm: () => void }) => r.onConfirm(),
  }),
}));
let state: NasStatus;
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  state = {
    enabled: false,
    max_file_bytes: 67108864,
    sources: [],
    systems: [
      {
        id: "fnos",
        name: "Feiniu fnOS",
        protocols: ["smb", "webdav"],
        integration: "",
      },
      {
        id: "synology",
        name: "Synology DSM",
        protocols: ["smb", "webdav"],
        integration: "",
      },
    ],
  };
  server.use(
    http.get("*/api/local/plugins/nas", () => HttpResponse.json(state)),
    http.put("*/api/local/plugins/nas", async ({ request }) => {
      state = { ...state, ...((await request.json()) as { enabled: boolean }) };
      return HttpResponse.json(state);
    }),
    http.get("*/api/local/plugins/nas/sources/:id/files", ({ params }) =>
      HttpResponse.json({
        entries: [
          {
            name: `${params.id}.txt`,
            path: `${params.id}.txt`,
            directory: false,
            size: 12,
            modified: 0,
          },
        ],
      }),
    ),
  );
});
function view(page = <NasPage />) {
  return render(
    <MemoryRouter>
      <PersonalShell>{page}</PersonalShell>
    </MemoryRouter>,
  );
}
it("translates plugin and connection setup without changing protocol values", async () => {
  const { unmount } = view(<PluginsPage />);
  await act(async () => { setLanguagePreference("zh-CN"); });
  expect(await screen.findByRole("button", { name: "启用" })).toBeInTheDocument();
  unmount();
  state.enabled = true;
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "添加 NAS" }))[0]);
  expect(screen.getByRole("dialog", { name: "添加 NAS" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "文件协议" })).toHaveValue("smb");
  expect(screen.getByRole("button", { name: "测试并添加" })).toBeInTheDocument();
  await act(async () => { setLanguagePreference("en"); });
  expect(screen.getByRole("combobox", { name: "File protocol" })).toHaveValue("smb");
  expect(screen.getByRole("button", { name: "Test and add" })).toBeInTheDocument();
});
it("hides NAS navigation until enabled and hides it again after disabling", async () => {
  view(<PluginsPage />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Enable" })).toBeEnabled(),
  );
  expect(screen.queryByRole("link", { name: "NAS" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Enable" }));
  expect(await screen.findByRole("link", { name: "NAS" })).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
  await waitFor(() =>
    expect(screen.queryByRole("link", { name: "NAS" })).not.toBeInTheDocument(),
  );
});
it("guards direct navigation when the plugin is disabled", async () => {
  view();
  expect(
    await screen.findByRole("heading", { name: "NAS is disabled" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Add NAS" }),
  ).not.toBeInTheDocument();
});
it("switches independent NAS systems and respects read and AI capabilities", async () => {
  state.enabled = true;
  state.sources = [
    {
      id: "home",
      name: "Home",
      system: "fnos",
      protocol: "smb",
      root: "",
      username: "a",
      writable: true,
      allow_ai: true,
    },
    {
      id: "office",
      name: "Office",
      system: "synology",
      protocol: "smb",
      root: "",
      username: "b",
      writable: false,
      allow_ai: false,
    },
  ];
  view();
  expect(
    await screen.findByRole("cell", { name: "home.txt" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Upload file" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Ask AI" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Office.*Synology DSM/ }));
  expect(
    await screen.findByRole("cell", { name: "office.txt" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("cell", { name: "home.txt" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Upload file" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Ask AI" }),
  ).not.toBeInTheDocument();
});
it("shows failed add without inventing a connected source", async () => {
  state.enabled = true;
  server.use(
    http.post("*/api/local/plugins/nas/sources", () =>
      HttpResponse.json({ detail: "nas_access_denied" }, { status: 403 }),
    ),
  );
  view();
  fireEvent.click(
    (await screen.findAllByRole("button", { name: "Add NAS" }))[0],
  );
  for (const [label, value] of [
    ["Connection name", "Home"],
    ["Host", "127.0.0.1"],
    ["Shared folder", "test"],
    ["Username", "test"],
    ["Password", "bad"],
  ]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
  fireEvent.click(screen.getByRole("button", { name: "Test and add" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "nas access denied",
  );
  expect(state.sources).toHaveLength(0);
});

it("navigates breadcrumbs and clears the previous folder search", async () => {
  state.enabled = true;
  state.sources = [
    {
      id: "home",
      name: "Home",
      system: "fnos",
      protocol: "smb",
      root: "",
      username: "a",
      writable: true,
      allow_ai: false,
    },
  ];
  const paths: string[] = [];
  server.use(
    http.get("*/api/local/plugins/nas/sources/:id/files", ({ request }) => {
      const path = new URL(request.url).searchParams.get("path") || "";
      paths.push(path);
      return HttpResponse.json({
        entries: path
          ? [
              {
                name: "report.txt",
                path: "Work/report.txt",
                directory: false,
                size: 5,
                modified: 0,
              },
            ]
          : [
              {
                name: "Work",
                path: "Work",
                directory: true,
                size: 0,
                modified: 0,
              },
            ],
      });
    }),
  );
  view();
  await screen.findByRole("button", { name: "Work" });
  fireEvent.change(
    screen.getByRole("textbox", { name: "Filter this folder" }),
    { target: { value: "Work" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Work" }));
  expect(
    await screen.findByRole("button", { name: "report.txt" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("textbox", { name: "Filter this folder" }),
  ).toHaveValue("");
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Folder path" })).getByRole(
      "button",
      { name: "All files" },
    ),
  );
  await screen.findByRole("button", { name: "Work" });
  expect(paths).toEqual(["", "Work", ""]);
});

it("sorts files by size and recovers from an empty search", async () => {
  state.enabled = true;
  state.sources = [
    {
      id: "home",
      name: "Home",
      system: "fnos",
      protocol: "smb",
      root: "",
      username: "a",
      writable: false,
      allow_ai: false,
    },
  ];
  server.use(
    http.get("*/api/local/plugins/nas/sources/:id/files", () =>
      HttpResponse.json({
        entries: [
          {
            name: "a.txt",
            path: "a.txt",
            directory: false,
            size: 2,
            modified: 0,
          },
          {
            name: "z.txt",
            path: "z.txt",
            directory: false,
            size: 99,
            modified: 0,
          },
          {
            name: "Documents",
            path: "Documents",
            directory: true,
            size: 0,
            modified: 0,
          },
        ],
      }),
    ),
  );
  view();
  await screen.findByRole("button", { name: "a.txt" });
  fireEvent.change(screen.getByRole("combobox", { name: "Sort files" }), {
    target: { value: "size" },
  });
  const rows = within(
    screen.getByRole("table", { name: "NAS files" }),
  ).getAllByRole("row");
  expect(
    rows.slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent),
  ).toEqual(["Documents", "z.txt", "a.txt"]);
  fireEvent.change(
    screen.getByRole("textbox", { name: "Filter this folder" }),
    { target: { value: "missing" } },
  );
  expect(
    screen.getByRole("heading", { name: "No matching files" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "Clear search" })[0]);
  expect(screen.getByRole("button", { name: "a.txt" })).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Upload file" }),
  ).not.toBeInTheDocument();
});
it("previews NAS images, zooms, opens large and releases the image on close", async () => {
  state.enabled = true;
  state.sources = [
    {
      id: "home",
      name: "Home",
      system: "fnos",
      protocol: "smb",
      root: "",
      username: "a",
      writable: true,
      allow_ai: true,
    },
  ];
  const create = vi.fn(() => "blob:nas-image");
  const revoke = vi.fn();
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }),
  );
  server.use(
    http.get("*/api/local/plugins/nas/sources/:id/files", () =>
      HttpResponse.json({
        entries: [
          {
            name: "sunset.png",
            path: "sunset.png",
            size: 1234,
            modified: 0,
            directory: false,
          },
        ],
      }),
    ),
    http.get(
      "*/api/local/plugins/nas/sources/:id/content",
      () =>
        new HttpResponse(new Uint8Array([1, 2, 3]), {
          headers: {
            "Content-Type": "image/webp",
            "X-Image-Width": "1536",
            "X-Image-Height": "1024",
          },
        }),
    ),
  );
  view();
  fireEvent.click(await screen.findByRole("button", { name: "sunset.png" }));
  const preview = screen.getByRole("complementary", { name: "File preview" });
  expect(
    await within(preview).findByRole("img", { name: "sunset.png" }),
  ).toHaveAttribute("src", "blob:nas-image");
  expect(within(preview).getByText(/1536 × 1024/)).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Ask AI" }),
  ).not.toBeInTheDocument();
  fireEvent.click(within(preview).getByRole("button", { name: "Zoom in" }));
  expect(
    within(preview).getByRole("button", { name: "125%" }),
  ).toBeInTheDocument();
  fireEvent.click(within(preview).getByRole("button", { name: "View large" }));
  expect(
    screen.getByRole("dialog", { name: "sunset.png" }),
  ).toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  fireEvent.click(
    within(preview).getByRole("button", { name: "Close preview" }),
  );
  expect(
    screen.queryByRole("complementary", { name: "File preview" }),
  ).not.toBeInTheDocument();
  expect(revoke).toHaveBeenCalledWith("blob:nas-image");
});

it("renders text in the side preview with AI access", async () => {
  state.enabled = true;
  state.sources = [
    {
      id: "home",
      name: "Home",
      system: "fnos",
      protocol: "smb",
      root: "",
      username: "a",
      writable: false,
      allow_ai: true,
    },
  ];
  server.use(
    http.get("*/api/local/plugins/nas/sources/:id/content", () =>
      HttpResponse.json({ text: "Text from the real file", name: "home.txt" }),
    ),
  );
  view();
  fireEvent.click(await screen.findByRole("button", { name: "home.txt" }));
  expect(
    await screen.findByText("Text from the real file"),
  ).toBeInTheDocument();
  expect(
    within(
      screen.getByRole("complementary", { name: "File preview" }),
    ).getByRole("button", { name: "Ask AI" }),
  ).toBeEnabled();
});
it("keeps a failed image preview usable and does not expose image AI actions", async () => {
  state.enabled = true;
  state.sources = [
    {
      id: "home",
      name: "Home",
      system: "fnos",
      protocol: "smb",
      root: "",
      username: "a",
      writable: false,
      allow_ai: true,
    },
  ];
  server.use(
    http.get("*/api/local/plugins/nas/sources/:id/files", () =>
      HttpResponse.json({
        entries: [
          {
            name: "broken.png",
            path: "broken.png",
            size: 10,
            modified: 0,
            directory: false,
          },
        ],
      }),
    ),
    http.get("*/api/local/plugins/nas/sources/:id/content", () =>
      HttpResponse.json(
        { detail: "preview_requires_valid_jpeg_png_or_webp" },
        { status: 415 },
      ),
    ),
  );
  view();
  fireEvent.click(await screen.findByRole("button", { name: "broken.png" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "preview requires valid jpeg png or webp",
  );
  expect(
    screen.queryByRole("img", { name: "broken.png" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "View large" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Download preview" }),
  ).toBeEnabled();
  expect(
    screen.queryByRole("button", { name: "Ask AI" }),
  ).not.toBeInTheDocument();
});
