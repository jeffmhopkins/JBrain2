import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no ResizeObserver. A minimal stub lets components that re-pin on
// viewport resize mount, and records instances so a test can fire a resize by
// hand (there is no real layout engine to drive one).
class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  readonly cb: ResizeObserverCallback;
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb;
    MockResizeObserver.instances.push(this);
  }
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  /** Test hook: simulate the box resizing. */
  trigger(): void {
    this.cb([], this as unknown as ResizeObserver);
  }
}
globalThis.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

afterEach(() => {
  cleanup();
  (globalThis.ResizeObserver as unknown as typeof MockResizeObserver).instances = [];
});
