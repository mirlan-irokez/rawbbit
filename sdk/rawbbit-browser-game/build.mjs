import { build } from "esbuild"
import { fileURLToPath } from "node:url"

await build({
  entryPoints: [fileURLToPath(new URL("./index.js", import.meta.url))],
  outfile: fileURLToPath(new URL("./dist/index.js", import.meta.url)),
  bundle: true,
  platform: "browser",
  format: "esm",
  target: "es2022",
  sourcemap: false,
  legalComments: "eof",
})
