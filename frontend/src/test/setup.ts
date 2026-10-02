import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

/**
 * jsdom implements neither matchMedia nor ResizeObserver, and the sidebar and
 * theme toggle both read them on mount.
 *
 * Installed with defineProperty rather than vi.stubGlobal because a test that
 * calls vi.unstubAllGlobals() — which any test stubbing fetch will — would
 * otherwise remove these too, and every later test in the file would fail on a
 * missing matchMedia rather than on anything it was testing.
 */
Object.defineProperty(window, "matchMedia", {
  writable: true,
  configurable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
});

Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  configurable: true,
  value: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});

afterEach(() => {
  cleanup();
});
