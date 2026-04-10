/**
 * color-swatches.js
 *
 * Scans <code> elements for hex (#rrggbb, #rgb) and rgba() color values and
 * prepends a small colored swatch so the design-palette page renders color
 * tokens visually, similar to GitHub's native markdown color preview.
 *
 * Runs once on DOMContentLoaded and re-runs on MkDocs instant-navigation
 * page transitions via document$.subscribe (Material's RxJS observable).
 */

(function () {
  // Matches: #abc, #aabbcc, #aabbccdd (3, 6, or 8 hex digits)
  const HEX_RE = /^#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/;
  // Matches: rgba(r, g, b, a) with optional whitespace
  const RGBA_RE = /^rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*[\d.]+\s*\)$/;

  function injectSwatches() {
    document.querySelectorAll("code").forEach((el) => {
      // Skip if we already injected a swatch into this element.
      if (el.querySelector(".color-swatch")) return;

      const raw = el.textContent.trim();
      if (!HEX_RE.test(raw) && !RGBA_RE.test(raw)) return;

      const swatch = document.createElement("span");
      swatch.className = "color-swatch";
      swatch.style.backgroundColor = raw;
      swatch.setAttribute("aria-hidden", "true");

      el.insertBefore(swatch, el.firstChild);
    });
  }

  // Initial load (non-instant-navigation).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectSwatches);
  } else {
    injectSwatches();
  }

  // MkDocs Material instant navigation re-renders the page content without a
  // full reload. document$ is the RxJS observable Material exposes globally.
  if (typeof document$ !== "undefined") {
    document$.subscribe(injectSwatches);
  }
})();
