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

// jsdom has no PointerEvent either, and Testing Library silently falls back to a bare
// `Event` for `fireEvent.pointerDown`/`pointerMove` — which carries no clientX/clientY at
// all, so every pointer gesture in the app reads NaN coordinates under test and any
// travel/slop check passes for the wrong reason. MouseEvent already implements the
// coordinates these gestures use; this is the same class under the name the pointer
// events are dispatched with. It also keeps `pointerId` and `pointerType`, so a two-finger
// gesture is two pointers under test, as it is on a phone.
globalThis.PointerEvent = class PointerEventShim extends MouseEvent {
  pointerId: number;
  pointerType: string;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
    this.pointerType = init.pointerType ?? "";
  }
} as unknown as typeof PointerEvent;

afterEach(() => {
  cleanup();
  (globalThis.ResizeObserver as unknown as typeof MockResizeObserver).instances = [];
});
