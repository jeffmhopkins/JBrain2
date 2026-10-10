import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaveSection } from "./MinecraftDave";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("DaveSection", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let stored: string | null;
  const puts: unknown[] = [];

  beforeEach(() => {
    stored = "BlockyFox";
    puts.length = 0;
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/api/settings" && method === "GET") {
        return json({ minecraft_gamertag: stored });
      }
      if (path === "/api/settings" && method === "PUT") {
        const body = JSON.parse(String(init?.body)) as { minecraft_gamertag: string };
        puts.push(body);
        if (body.minecraft_gamertag.includes("!")) {
          return json({ detail: "that is not a gamertag" }, 422);
        }
        stored = body.minecraft_gamertag || null;
        return json({ minecraft_gamertag: stored });
      }
      if (path === "/api/sessions" && method === "POST") {
        return json({ id: "dave-1", agent: "minecraft_dave", domain_scopes: [] });
      }
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const field = () => screen.getByLabelText("Your gamertag") as HTMLInputElement;

  it("loads the saved gamertag and says what it is for", async () => {
    render(<DaveSection />);
    await waitFor(() => expect(field().value).toBe("BlockyFox"));
    expect(screen.getByText(/Minecraft_Dave chats start out about this player/)).toBeVisible();
    // Unchanged → nothing to save.
    expect(screen.getByRole("button", { name: "Save gamertag" })).toBeDisabled();
  });

  it("saves an edited gamertag with PUT /api/settings", async () => {
    render(<DaveSection />);
    await waitFor(() => expect(field().value).toBe("BlockyFox"));
    fireEvent.change(field(), { target: { value: "  Mira P  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save gamertag" }));
    await waitFor(() => expect(puts).toEqual([{ minecraft_gamertag: "Mira P" }]));
    await waitFor(() => expect(field().value).toBe("Mira P"));
    expect(screen.getByRole("button", { name: "Save gamertag" })).toBeDisabled();
  });

  it("Clear saves an empty gamertag and the field reads not set", async () => {
    render(<DaveSection />);
    await waitFor(() => expect(field().value).toBe("BlockyFox"));
    fireEvent.click(screen.getByRole("button", { name: "Clear gamertag" }));
    await waitFor(() => expect(puts).toEqual([{ minecraft_gamertag: "" }]));
    await waitFor(() => expect(field().value).toBe(""));
    expect(field()).toHaveAttribute("placeholder", "not set");
    expect(screen.queryByRole("button", { name: "Clear gamertag" })).not.toBeInTheDocument();
  });

  it("shows the server's 422 reason and keeps what was typed", async () => {
    render(<DaveSection />);
    await waitFor(() => expect(field().value).toBe("BlockyFox"));
    fireEvent.change(field(), { target: { value: "not!atag" } });
    fireEvent.click(screen.getByRole("button", { name: "Save gamertag" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("that is not a gamertag");
    expect(field().value).toBe("not!atag");
    // Typing again clears the stale reason.
    fireEvent.change(field(), { target: { value: "Mira" } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Ask Minecraft_Dave starts a no-data chat and hands it to the shell", async () => {
    const onOpenSession = vi.fn();
    render(<DaveSection onOpenSession={onOpenSession} />);
    const ask = await screen.findByRole("button", { name: /Ask Minecraft_Dave/ });
    await waitFor(() => expect(ask).toHaveTextContent("Starts about BlockyFox"));
    fireEvent.click(ask);
    await waitFor(() => expect(onOpenSession).toHaveBeenCalledWith("dave-1", "minecraft_dave"));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      domain_scopes: [],
      agent: "minecraft_dave",
    });
  });

  it("a failed start says so on the row and opens nothing", async () => {
    fetchMock.mockImplementation(async () => json({ detail: "unknown agent" }, 422));
    const onOpenSession = vi.fn();
    render(<DaveSection onOpenSession={onOpenSession} />);
    fireEvent.click(screen.getByRole("button", { name: /Ask Minecraft_Dave/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't start a chat — unknown agent",
    );
    expect(onOpenSession).not.toHaveBeenCalled();
  });

  it("without a shell to hand a chat to, there is no Ask row", async () => {
    render(<DaveSection />);
    await waitFor(() => expect(field().value).toBe("BlockyFox"));
    expect(screen.queryByRole("button", { name: /Ask Minecraft_Dave/ })).not.toBeInTheDocument();
  });
});
