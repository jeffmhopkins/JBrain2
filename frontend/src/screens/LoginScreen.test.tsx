import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Principal } from "../api/client";
import { LoginScreen } from "./LoginScreen";

const PRINCIPAL: Principal = { principal_id: "p1", kind: "owner_device", label: "Test device" };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("LoginScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function fillAndSubmit(ownerKey: string) {
    fireEvent.change(screen.getByLabelText("Owner key"), { target: { value: ownerKey } });
    fireEvent.change(screen.getByLabelText("Device label"), { target: { value: "Test device" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
  }

  it("submits the owner key and reports the principal on success", async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/auth/session") return new Response(null, { status: 204 });
      if (url === "/api/auth/me") return jsonResponse(PRINCIPAL);
      throw new Error(`Unexpected fetch: ${url}`);
    });
    const onLogin = vi.fn();
    render(<LoginScreen onLogin={onLogin} />);

    fillAndSubmit("owner-key-123");

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith(PRINCIPAL));
    const [, init] = fetchMock.mock.calls.find(
      ([input]) => String(input) === "/api/auth/session",
    ) ?? [undefined, undefined];
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("same-origin");
    expect(JSON.parse(String(init?.body))).toEqual({
      owner_key: "owner-key-123",
      device_label: "Test device",
    });
  });

  it("shows an error on a 401 and does not log in", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 401 }));
    const onLogin = vi.fn();
    render(<LoginScreen onLogin={onLogin} />);

    fillAndSubmit("wrong-key");

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid owner key.");
    expect(onLogin).not.toHaveBeenCalled();
  });
});
