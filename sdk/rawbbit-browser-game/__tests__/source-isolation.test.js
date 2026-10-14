import test from "node:test"
import assert from "node:assert/strict"
import { resolve } from "node:path"
import { pathToFileURL } from "node:url"

import {
  isPathWithin,
  packageNameForSpecifier,
  resolveLocalSpecifier,
} from "../scripts/source-isolation-utils.js"
import { collectModuleSpecifiers } from "../scripts/source-isolation-ast.js"

const sdkRoot = resolve("/isolated/rawbbit-browser-game")
const importer = resolve(sdkRoot, "payload.js")

test("source-isolation path guard permits local and Node imports only", () => {
  assert.equal(resolveLocalSpecifier(sdkRoot, importer, "./vendor/rawbbit-client/normalize.js"),
    resolve(sdkRoot, "vendor/rawbbit-client/normalize.js"))
  assert.equal(resolveLocalSpecifier(sdkRoot, importer, "node:assert/strict"), null)
  assert.equal(packageNameForSpecifier("@playwright/test"), "@playwright/test")
  assert.equal(packageNameForSpecifier("@rawbbit/browser-game/vendor/internal.js"), "@rawbbit/browser-game")
  assert.equal(packageNameForSpecifier("esbuild/lib/main.js"), "esbuild")
  assert.equal(packageNameForSpecifier(".."), null)
  assert.throws(() => packageNameForSpecifier("@babel/parser/../../../../rawbbit-client/normalize.js"), /subpath traversal/)
  assert.throws(() => packageNameForSpecifier("@babel/parser/%2e%2e/outside.js"), /subpath traversal/)
})

test("source-isolation path guard rejects relative, absolute, encoded, and file-URL escapes", () => {
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, "../../rawbbit-client/transport.js"), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, ".."), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, "/outside/transport.js"), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, pathToFileURL("/outside/transport.js").href), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer,
    "./dist/%2e%2e/%2e%2e/rawbbit-client/normalize.js"), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer,
    "./dist/.%2e/%2E%2e/outside.js"), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer,
    "/isolated/rawbbit-browser-game/dist/%2e%2e/%2e%2e/outside.js"), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer,
    "/isolated/rawbbit-browser-game/dist/%2e%2e/%2e%2e/outside.js", { resolveAgainstImporter: true }), /escapes the SDK/)
  assert.throws(() => packageNameForSpecifier("https://example.invalid/module.js"), /Unsupported external module URL/)
  assert.equal(isPathWithin(sdkRoot, resolve(sdkRoot, "vendor/normalize.js")), true)
  assert.equal(isPathWithin(sdkRoot, "/outside/normalize.js"), false)
})

test("source-isolation path guard distinguishes a server-owned root URL", () => {
  const serverFile = resolve(sdkRoot, "browser-tests/server.mjs")
  assert.equal(resolveLocalSpecifier(sdkRoot, serverFile, "/sdk.js", {
    allowWebRootUrl: true,
  }), null)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, serverFile, "/sdk.js", {
    allowWebRootUrl: true,
    resolveAgainstImporter: true,
  }), /escapes the SDK/)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, "/sdk.js"), /escapes the SDK/)
})

test("AST isolation scanner collects static, dynamic, resolver, asset, and TypeScript references", () => {
  const source = [
    'import { value } from "./value.js"',
    'export { value } from "./reexport.js"',
    'export * from "./all.js"',
    'await import("./dynamic.js", { with: { type: "json" } })',
    'require("./commonjs.cjs")',
    'require.resolve("./resolved.js")',
    'module.require("./module-require.js")',
    'import.meta.resolve("./meta-resolved.js")',
    'importScripts("./worker.js", "./other-worker.js")',
    'new URL("./asset.json", import /* trivia */ .meta.url)',
    'const description = "import(modulePath) is only text"',
    'const nested = `template ${import("./template.js")}`',
  ].join(";\n")
  assert.deepEqual(collectModuleSpecifiers(source).map(({ specifier }) => specifier).sort(), [
    "./value.js",
    "./reexport.js",
    "./all.js",
    "./dynamic.js",
    "./commonjs.cjs",
    "./resolved.js",
    "./module-require.js",
    "./meta-resolved.js",
    "./worker.js",
    "./other-worker.js",
    "./asset.json",
    "./template.js",
  ].sort())

  assert.deepEqual(collectModuleSpecifiers([
    'import{x}from"./named.js"',
    'export{x}from"./named-reexport.js"',
  ].join(";\n")).map(({ specifier }) => specifier), ["./named.js", "./named-reexport.js"])

  assert.deepEqual(collectModuleSpecifiers([
    'import type { Value } from "./types.js"',
    'type Imported = import("./import-type.js").Value',
  ].join(";\n"), "sample.ts").map(({ specifier }) => specifier), ["./types.js", "./import-type.js"])
  assert.deepEqual(collectModuleSpecifiers('export declare function ambient(): void', "sample.d.ts"), [])
})

test("AST isolation scanner rejects computed module and URL specifiers", () => {
  for (const source of [
    "import(modulePath)",
    "require(modulePath)",
    "require.resolve(modulePath)",
    'require["resolve"](modulePath)',
    'module["re" + "quire"](modulePath)',
    "import.meta.resolve(modulePath)",
    'import.meta["res" + "olve"](modulePath)',
    "module.require(modulePath)",
    "globalThis.require(modulePath)",
    "importScripts(modulePath)",
    'globalThis.importScripts("./ok.js", modulePath)',
    "new URL(assetPath, import.meta.url)",
    "new URL(assetPath, import.meta[key])",
    'new URL("../../outside.js", new URL("./subdir/", import.meta.url))',
    'const base = import.meta.url; new URL("../../outside.js", base)',
    "import(`./${modulePath}.js`)",
    "createRequire(import.meta.url)",
    'import { createRequire as load } from "node:module"',
    "const load = require; load(modulePath)",
    "const m = module; m.require(modulePath)",
    "const globalObject = globalThis; globalObject.require(modulePath)",
    "const loader = globalThis[moduleName]; loader(modulePath)",
    'module.require.call(module, "../../outside.js")',
    'import.meta.resolve.call(import.meta, "../../outside.js")',
    "const load = importScripts; load(modulePath)",
    'eval("import(modulePath)")',
    'new Function("return import(modulePath)")',
    'globalThis["eval"]("import(modulePath)")',
    'globalThis["Function"]("return import(modulePath)")()',
    'const create = globalThis.createRequire; create(import.meta.url)',
    'globalThis.globalThis.Function("return import(modulePath)")()',
    'globalThis.global.Function("return import(modulePath)")()',
    'globalThis.self.Function("return import(modulePath)")()',
    'globalThis.window.Function("return import(modulePath)")()',
    'const host = self; host.importScripts("../../outside.js")',
    'window.window.importScripts("../../outside.js")',
    'global.global.Function("return import(modulePath)")()',
    'globalThis["global"].Function("return import(modulePath)")()',
    '((g) => g.Function("return import(modulePath)"))(globalThis)',
    'function getHost() { return this }; const host = getHost.bind(globalThis)(); host.Function("return import(modulePath)")()',
    'globalThis.constructor.constructor("return import(modulePath)")()',
    'const Dynamic = ({}).constructor; Dynamic("return import(modulePath)")()',
    '(0, eval)("import(modulePath)")',
    'const box = { globalThis }; box.globalThis.importScripts("../../outside.js")',
    'const host = (0, globalThis); host.importScripts("../../outside.js")',
    'function harmless(globalThis) {} const g = globalThis; g.Function("return import(modulePath)")()',
    'import * as m from "node:module"; const { createRequire: load } = m; load(import.meta.url)("../../outside.js")',
    'require.resolve("./local.js", { paths: ["/outside"] })',
  ]) {
    assert.throws(() => collectModuleSpecifiers(source), /Non-literal|unsupported|Unexpected resolver options|cannot be aliased/,
      `expected computed module reference to be rejected: ${source}`)
  }

  const escaped = collectModuleSpecifiers('import("./dist/\\x2e\\x2e/\\x2e\\x2e/outside.js")')
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, escaped[0].specifier), /escapes the SDK/)
  assert.throws(() => collectModuleSpecifiers('import("./has\\\\backslash.js")'), /Escaped module specifier/)
  const escapedWorkerArg = collectModuleSpecifiers('globalThis.importScripts("./ok.js", "../../outside.js")')
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, escapedWorkerArg[1].specifier), /escapes the SDK/)
  assert.throws(() => collectModuleSpecifiers('globalThis.importScripts("./ok.js", modulePath)'), /Non-literal/)

  const urlReference = collectModuleSpecifiers('new URL("@babel/parser/../../../outside.js", import.meta.url)')[0]
  assert.equal(urlReference.resolveAgainstImporter, true)
  assert.throws(() => resolveLocalSpecifier(sdkRoot, resolve(sdkRoot, "build.mjs"), urlReference.specifier, urlReference),
    /escapes the SDK/)
  const globalUrl = collectModuleSpecifiers('new globalThis["URL"]("../../outside.js", import.meta?.url)')[0]
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, globalUrl.specifier, globalUrl), /escapes the SDK/)
  const absoluteUrl = collectModuleSpecifiers(
    'new URL("/isolated/rawbbit-browser-game/dist/%2e%2e/%2e%2e/outside.js", import.meta.url)',
  )[0]
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, absoluteUrl.specifier, absoluteUrl), /escapes the SDK/)

  const parameterDefault = collectModuleSpecifiers(
    'function f(value = globalThis.importScripts("../../outside.js")) { var globalThis = {} }',
  )[0]
  assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, parameterDefault.specifier, parameterDefault), /escapes the SDK/)
  assert.throws(() => collectModuleSpecifiers(
    "function f(value = globalThis.importScripts(modulePath)) { var globalThis = {} }",
  ), /Non-literal/)

  assert.throws(() => collectModuleSpecifiers(
    "class C { #method() { var globalThis = {} } } globalThis.importScripts(modulePath)",
  ), /Non-literal/)
  assert.throws(() => collectModuleSpecifiers(
    "class C { static { var globalThis = {} } } globalThis.importScripts(modulePath)",
  ), /Non-literal/)

  for (const source of [
    'class C { [globalThis.importScripts("../../outside.js")](globalThis) {} }',
    'const value = { [globalThis.importScripts("../../outside.js")](globalThis) {} }',
    'switch (globalThis.importScripts("../../outside.js")) { case 0: let globalThis }',
  ]) {
    const references = collectModuleSpecifiers(source)
    assert.ok(references.length > 0, `expected the enclosing scope loader expression to be scanned: ${source}`)
    for (const reference of references) {
      assert.throws(() => resolveLocalSpecifier(sdkRoot, importer, reference.specifier, reference), /escapes the SDK/)
    }
  }

  for (const source of [
    'const number = {} / 2; import "/outside/block-end.js"',
    'const punctuation = /[/*]/; import "/outside/regex.js"',
    'const regex = /* comment */ /[/*]/; import "/outside/comment-regex.js"',
    'const regex = `template ${import(modulePath)}`',
  ]) {
    assert.throws(() => {
      for (const reference of collectModuleSpecifiers(source)) {
        resolveLocalSpecifier(sdkRoot, importer, reference.specifier, reference)
      }
    }, /escapes the SDK|Non-literal/)
  }
})

test("AST isolation scanner parses source forms and ignores imports in comments", () => {
  assert.deepEqual(collectModuleSpecifiers([
    '// import(modulePath)',
    '/* require(modulePath) */',
    'const punctuation = /[/*]/',
    'const description = "import(\\\"not-a-call\\\")"',
    'import "../rawbbit-client/normalize.js"',
  ].join("\n")).map(({ specifier }) => specifier), ["../rawbbit-client/normalize.js"])

  for (const lineTerminator of ["\r", "\u2028", "\u2029"]) {
    const source = `// ignored${lineTerminator}import "../rawbbit-client/normalize.js"`
    assert.deepEqual(collectModuleSpecifiers(source).map(({ specifier }) => specifier), ["../rawbbit-client/normalize.js"])
  }
  assert.deepEqual(collectModuleSpecifiers([
    "const settings = { eval: false }",
    'const options = { module: "esm", globalThis: false }',
    "options.module; options.globalThis",
    "function localWindow() { const window = {}; return { window, runtime: { window } } }",
    "function localGlobal(globalThis) { return globalThis.options }",
    "class C { #method(window) { return { window } } }",
    "globalThis.fetch?.bind(globalThis)",
    "options.createRequire(); options[\"createRequire\"]",
    "type Callback = Function",
    "const currentFile = fileURLToPath(import.meta.url)",
  ].join(";\n"), "sample.ts"), [])
})
