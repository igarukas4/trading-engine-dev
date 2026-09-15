import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const page = readFileSync("frontend/app/strategies/page.tsx", "utf8");

test("Strategies explains missing account context instead of rendering a blank page", () => {
  assert.match(page, /useSearchParams/);
  assert.match(page, /searchParams\.get\("account"\)/);
  assert.match(page, /Pilih account terlebih dahulu/);
});

test("Strategies distinguishes loading, empty configurations, and API failures", () => {
  assert.match(page, /Memuat konfigurasi Strategy/);
  assert.match(page, /Belum ada konfigurasi Strategy/);
  assert.match(page, /Gagal memuat konfigurasi Strategy/);
  assert.match(page, /Coba lagi/);
  assert.match(page, /response\.ok/);
});
