import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ShareItem, type SharedLink } from "../api/client";
import { type JShareDeps, JShareScreen, fmtSize } from "./JShareScreen";

function item(over: Partial<ShareItem> = {}): ShareItem {
  return {
    id: "s1",
    slug: "abc123",
    title: "Lake house walkthrough",
    kind: "video",
    filename: "lake.mp4",
    media_type: "video/mp4",
    size_bytes: 3 * 1024 * 1024,
    url: "https://box.example/s/abc123/",
    created_at: "2026-10-16T19:04:00Z",
    last_viewed_at: null,
    view_count: 6,
    ...over,
  };
}

function deps(over: Partial<JShareDeps> = {}): JShareDeps {
  return {
    list: vi.fn(async () => [item()]),
    upload: vi.fn(async (file: File, title: string) =>
      item({ id: "new", title: title || file.name, kind: "file", view_count: 0 }),
    ),
    rename: vi.fn(async (id: string, title: string) => item({ id, title })),
    remove: vi.fn(async () => {}),
    links: vi.fn(async () => ({ links: [], unavailable: [] })),
    revokeLink: vi.fn(async () => {}),
    ...over,
  };
}

function pick(name = "notes.pdf"): File {
  const file = new File(["hello"], name, { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText("Choose a file to share"), {
    target: { files: [file] },
  });
  return file;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("JShareScreen", () => {
  it("lists shares with their kind, size and views, and links out safely", async () => {
    render(
      <JShareScreen
        deps={deps({
          list: vi.fn(async () => [
            item(),
            item({
              id: "s2",
              title: "Banner",
              kind: "image",
              url: "https://box.example/s/img/",
              view_count: 0,
            }),
          ]),
        })}
      />,
    );
    const row = (await screen.findByText("Lake house walkthrough")).closest("li");
    expect(row).not.toBeNull();
    const r = within(row as HTMLElement);
    expect(r.getByText(/Video · 3\.0 MB · .* · 6 views/)).toBeInTheDocument();
    const open = r.getByRole("link", { name: /Open/ });
    expect(open).toHaveAttribute("href", "https://box.example/s/abc123/");
    expect(open).toHaveAttribute("target", "_blank");
    expect(open).toHaveAttribute("rel", "noopener noreferrer");

    const banner = (screen.getByText("Banner").closest("li") as HTMLElement).querySelector("img");
    expect(banner).toHaveAttribute("src", "https://box.example/s/img/");
    expect(screen.getByText(/Picture · .* · not opened yet/)).toBeInTheDocument();
  });

  it("says so when nothing is shared, and when the list fails", async () => {
    const { unmount } = render(<JShareScreen deps={deps({ list: vi.fn(async () => []) })} />);
    expect(await screen.findByText(/nothing shared yet/)).toBeInTheDocument();
    unmount();
    render(
      <JShareScreen
        deps={deps({
          list: vi.fn(async () => {
            throw new Error("down");
          }),
        })}
      />,
    );
    expect(await screen.findByText(/couldn't load your shares/)).toBeInTheDocument();
  });

  it("uploads a picked file with its title, shows progress, and adds the new share", async () => {
    let finish: (v: ShareItem) => void = () => {};
    let report: (sent: number, total: number) => void = () => {};
    const upload = vi.fn(
      (_f: File, _t: string, onProgress: (s: number, t: number) => void) =>
        new Promise<ShareItem>((resolve) => {
          report = onProgress;
          finish = resolve;
        }),
    );
    render(<JShareScreen deps={deps({ list: vi.fn(async () => []), upload })} />);
    await screen.findByText(/nothing shared yet/);

    const file = pick();
    fireEvent.change(screen.getByLabelText("Title (optional)"), {
      target: { value: "Trip notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload & make link" }));

    expect(upload).toHaveBeenCalledWith(
      file,
      "Trip notes",
      expect.any(Function),
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: /Share a file/ })).toBeDisabled();
    act(() => report(50, 100));
    expect(screen.getByRole("progressbar", { name: "Upload" })).toHaveAttribute(
      "aria-valuenow",
      "50",
    );

    await act(async () => finish(item({ id: "new", title: "Trip notes", kind: "file" })));
    expect(await screen.findByText("Trip notes")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("shows the box's reason when an upload is refused", async () => {
    const upload = vi.fn(async () => {
      throw new ApiError(422, "the zip has no index.html at its top level");
    });
    render(<JShareScreen deps={deps({ upload })} />);
    await screen.findByText("Lake house walkthrough");
    pick("site.zip");
    fireEvent.click(screen.getByRole("button", { name: "Upload & make link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not shared — the zip has no index.html at its top level",
    );
  });

  it("cancels an upload without calling it a failure", async () => {
    const upload = vi.fn(
      (_f: File, _t: string, _p: unknown, signal?: AbortSignal) =>
        new Promise<ShareItem>((_resolve, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("The upload was cancelled.", "AbortError")),
          );
        }),
    );
    render(<JShareScreen deps={deps({ upload })} />);
    await screen.findByText("Lake house walkthrough");
    pick();
    fireEvent.click(screen.getByRole("button", { name: "Upload & make link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("progressbar")).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("copies the link the box minted, as given", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<JShareScreen deps={deps()} />);
    await screen.findByText("Lake house walkthrough");
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    await screen.findByRole("button", { name: "Copied" });
    expect(writeText).toHaveBeenCalledWith("https://box.example/s/abc123/");
  });

  it("offers the share sheet only where the device has one", async () => {
    const share = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, share });
    render(<JShareScreen deps={deps()} />);
    await screen.findByText("Lake house walkthrough");
    fireEvent.click(screen.getByRole("button", { name: /Send/ }));
    await waitFor(() =>
      expect(share).toHaveBeenCalledWith({
        title: "Lake house walkthrough",
        url: "https://box.example/s/abc123/",
      }),
    );
  });

  it("renames a share from its sheet", async () => {
    const d = deps();
    render(<JShareScreen deps={d} />);
    await screen.findByText("Lake house walkthrough");
    fireEvent.click(screen.getByRole("button", { name: "More for Lake house walkthrough" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Lake house" } });
    fireEvent.click(screen.getByRole("button", { name: "Save title" }));
    await waitFor(() => expect(d.rename).toHaveBeenCalledWith("s1", "Lake house"));
    expect(await screen.findByText("Lake house")).toBeInTheDocument();
  });

  it("names a download link for a file", async () => {
    render(<JShareScreen deps={deps()} />);
    await screen.findByText("Lake house walkthrough");
    fireEvent.click(screen.getByRole("button", { name: "More for Lake house walkthrough" }));
    expect(screen.getByText("https://box.example/s/abc123/?download=1")).toBeInTheDocument();
  });

  it("offers no download link for a zipped site", async () => {
    render(<JShareScreen deps={deps({ list: vi.fn(async () => [item({ kind: "site" })]) })} />);
    await screen.findByText("Lake house walkthrough");
    fireEvent.click(screen.getByRole("button", { name: "More for Lake house walkthrough" }));
    expect(screen.queryByText(/\?download=1/)).not.toBeInTheDocument();
  });

  it("deletes only after a confirm that says the link stops working", async () => {
    const d = deps();
    render(<JShareScreen deps={d} />);
    await screen.findByText("Lake house walkthrough");
    fireEvent.click(screen.getByRole("button", { name: "More for Lake house walkthrough" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete & unpublish" }));

    const dialog = await screen.findByRole("dialog", { name: "Delete this share?" });
    expect(dialog).toHaveTextContent(/link stops working at once/);
    expect(d.remove).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(d.remove).not.toHaveBeenCalled();
    expect(screen.getByText("Lake house walkthrough")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "More for Lake house walkthrough" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete & unpublish" }));
    const again = await screen.findByRole("dialog", { name: "Delete this share?" });
    fireEvent.click(within(again).getByRole("button", { name: "Delete & unpublish" }));
    await waitFor(() => expect(d.remove).toHaveBeenCalledWith("s1"));
    await waitFor(() => expect(screen.queryByText("Lake house walkthrough")).toBeNull());
  });
});

function link(over: Partial<SharedLink> = {}): SharedLink {
  return {
    kind: "chat",
    id: "c1",
    title: "Weekend trip plans",
    created_at: "2026-10-12T15:00:00Z",
    expires_at: null,
    last_viewed_at: null,
    view_count: 4,
    detail: "",
    ...over,
  };
}

describe("JShareScreen other share links", () => {
  it("groups every kind in its own folded section, files open", async () => {
    render(
      <JShareScreen
        deps={deps({
          links: vi.fn(async () => ({
            links: [
              link(),
              link({ kind: "research", id: "r1", title: "Why is the sky blue?" }),
              link({ kind: "chat", id: "c2", title: "Recipes" }),
            ],
            unavailable: [],
          })),
        })}
      />,
    );
    expect(await screen.findByText("Lake house walkthrough")).toBeInTheDocument();
    const chats = await screen.findByRole("button", { name: /Chats/ });
    expect(chats).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: /Files/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText("Weekend trip plans")).toBeNull();
    // An empty kind has no section at all.
    expect(screen.queryByRole("button", { name: /Minecraft maps/ })).toBeNull();

    fireEvent.click(chats);
    const list = screen.getByRole("list", { name: "Chats links" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Weekend trip plans")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Files/ }));
    expect(screen.queryByText("Lake house walkthrough")).toBeNull();
  });

  it("revokes a link only after a confirm, then drops it", async () => {
    const d = deps({
      links: vi.fn(async () => ({ links: [link()], unavailable: [] })),
    });
    render(<JShareScreen deps={d} />);
    fireEvent.click(await screen.findByRole("button", { name: /Chats/ }));
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke this link?" });
    expect(dialog).toHaveTextContent(/loses access at once/);
    expect(dialog).toHaveTextContent(/the chat's share sheet/);
    expect(d.revokeLink).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke link" }));
    await waitFor(() => expect(d.revokeLink).toHaveBeenCalledWith("chat", "c1"));
    await waitFor(() => expect(screen.queryByText("Weekend trip plans")).toBeNull());
  });

  it("says which kinds could not be read rather than showing them as empty", async () => {
    render(
      <JShareScreen
        deps={deps({
          links: vi.fn(async () => ({ links: [], unavailable: ["minecraft" as const] })),
        })}
      />,
    );
    expect(await screen.findByText(/couldn't read minecraft maps links/)).toBeInTheDocument();
  });

  it("a revoke that fails says so and keeps the link", async () => {
    render(
      <JShareScreen
        deps={deps({
          links: vi.fn(async () => ({ links: [link()], unavailable: [] })),
          revokeLink: vi.fn(async () => {
            throw new ApiError(500, "the box is busy");
          }),
        })}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Chats/ }));
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke link" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("the box is busy");
    expect(screen.getByText("Weekend trip plans")).toBeInTheDocument();
  });

  it("a link already revoked elsewhere just drops", async () => {
    render(
      <JShareScreen
        deps={deps({
          links: vi.fn(async () => ({ links: [link()], unavailable: [] })),
          revokeLink: vi.fn(async () => {
            throw new ApiError(404, "no such link");
          }),
        })}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Chats/ }));
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke link" }),
    );
    await waitFor(() => expect(screen.queryByText("Weekend trip plans")).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("fmtSize", () => {
  it("reads in the unit a person would say", () => {
    expect(fmtSize(512)).toBe("512 B");
    expect(fmtSize(48_211)).toBe("47 KB");
    expect(fmtSize(3 * 1024 * 1024)).toBe("3.0 MB");
    expect(fmtSize(1_342_177_280)).toBe("1.3 GB");
  });
});
