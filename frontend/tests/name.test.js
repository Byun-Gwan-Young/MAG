// C-19-8K part 0b: the service name users see is "MAG" (subtitle "Multi-Agent Guardrail").
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const screens = {
  "index.html": read("../index.html"),
  "App.jsx": read("../src/App.jsx"),
  "Login.jsx": read("../src/Login.jsx"),
  "pages.jsx": read("../src/pages.jsx"),
  "components.jsx": read("../src/components.jsx"),
  "login.html": read("../../backend/templates/login.html"),
};

test("browser tab title is MAG", () => {
  assert.match(screens["index.html"], /<title>MAG · Multi-Agent Guardrail<\/title>/);
});

test("menu and sign-in page show MAG with the subtitle", () => {
  for (const name of ["App.jsx", "Login.jsx"]) {
    assert.match(screens[name], /MAG\s*<small>Multi-Agent Guardrail<\/small>/, name);
  }
});

test("old visible names are gone", () => {
  for (const [name, text] of Object.entries(screens)) {
    assert.doesNotMatch(text, /(?<!multi-)agent[ -]?guardrail(?!-dashboard)|EXPERIMENT CONSOLE|agent<span>/i, name);
  }
});
