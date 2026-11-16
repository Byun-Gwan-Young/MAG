// C-19-10T: the React screens use the original MAG theme (tools/demo_ui.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const demo = readFileSync(
  new URL("../../tools/demo_ui.py", import.meta.url),
  "utf8",
);

test("theme variables equal the demo_ui values", () => {
  const vars = (text) =>
    Object.fromEntries(
      [...text.matchAll(/--([a-z]+):\s*(#[0-9a-f]{3,6})/g)].map((m) => [
        m[1],
        m[2],
      ]),
    );
  const original = vars(
    demo.slice(demo.indexOf("STYLE = "), demo.indexOf("STYLE = ") + 600),
  );
  const react = vars(css);
  assert.ok(Object.keys(original).length >= 18);
  for (const [name, value] of Object.entries(original))
    assert.equal(react[name], value, name);
});

test("no web fonts or CDN", () => {
  assert.doesNotMatch(css, /@import|https?:\/\/|DM Sans/);
});

test("green hues only in the ok (normal / passed) colors", () => {
  const green = (hex) => {
    const n =
      hex.length === 4
        ? [...hex.slice(1)].map((c) => c + c)
        : [hex.slice(1, 3), hex.slice(3, 5), hex.slice(5, 7)];
    const [r, g, b] = n.map((x) => parseInt(x, 16));
    return g > r + 12 && g > b + 6;
  };
  const allowed = new Set(["ok", "ok-bg", "ok-line"]);
  for (const line of css.split("\n")) {
    for (const hex of line.match(/#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b/g) ||
      []) {
      if (!green(hex)) continue;
      const name = line.match(/--([a-z-]+):/)?.[1];
      assert.ok(allowed.has(name), `green ${hex} outside --ok: ${line.trim()}`);
    }
  }
  assert.doesNotMatch(css, /--green/);
});

test("logo is the original docs/assets/logo.svg", () => {
  assert.deepEqual(
    readFileSync(new URL("../src/assets/logo.svg", import.meta.url)),
    readFileSync(new URL("../../docs/assets/logo.svg", import.meta.url)),
  );
});
