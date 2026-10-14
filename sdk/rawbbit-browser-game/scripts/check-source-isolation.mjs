import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { constants, createReadStream } from "node:fs"
import {
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  realpath,
  rm,
  stat,
  writeFile,
} from "node:fs/promises"
import { tmpdir } from "node:os"
import { basename, delimiter, dirname, extname, join, relative, resolve, sep } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import {
  isPathWithin,
  packageNameForSpecifier,
  resolveLocalSpecifier,
} from "./source-isolation-utils.js"

const sdkRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const sourceExclusions = new Set([
  ".git",
  ".cache",
  "coverage",
  "dist",
  "node_modules",
  "playwright-report",
  "source-isolation-artifacts",
  "test-results",
])
const generatedFile = name => name === ".DS_Store" || name === "npm-debug.log" || name.endsWith(".tgz")
const packageFiles = [
  "CHANGELOG.md",
  "INTEGRATION.md",
  "README.md",
  "dist/index.js",
  "examples/GameAnalyticsAdapter.js.template",
  "index.d.ts",
  "package.json",
]
const args = parseArgs(process.argv.slice(2))
const npm = process.platform === "win32" ? "npm.cmd" : "npm"
const cleanEnvironment = { ...process.env, NODE_OPTIONS: "", NODE_PATH: "" }
for (const key of Object.keys(cleanEnvironment)) {
  if (["INIT_CWD", "NPM_CONFIG_LOCAL_PREFIX", "NPM_CONFIG_PREFIX", "NPM_CONFIG_USERCONFIG", "NPM_CONFIG_WORKSPACE", "NPM_CONFIG_WORKSPACES"].includes(key.toUpperCase())) {
    delete cleanEnvironment[key]
  }
}
const startedAt = new Date().toISOString()
const report = {
  schemaVersion: 1,
  status: "running",
  startedAt,
  finishedAt: null,
  node: process.version,
  platform: `${process.platform}-${process.arch}`,
  browserGate: args.browser,
  phase: "initializing",
  phases: [],
  artifacts: [],
  result: null,
  failure: null,
}
let reportFile = null
let reportFileHandle = null
let reportDirectoryRealPath = null
let workspace = null
let workspaceRealPath = null
let currentPhase = "initializing"
let boundaryRoots = []

const approvedGeneratedTemplateImport = [
  "return import",
  "(",
  "`",
  "${pathToFileURL(tempPath).href}?test=${Math.random()}",
  "`)",
].join("")
const approvedScannerLoader = [
  "await import(",
  'pathToFileURL(join(isolatedRoot, "scripts/source-isolation-ast.js")).href',
  ")",
].join("")
const approvedRuntimeGlobalLookup = "globalThis[name]"

function parseArgs(values) {
  const parsed = { browser: false, reportDir: null }
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    if (value === "--browser") parsed.browser = true
    else if (value === "--report-dir") {
      const directory = values[index + 1]
      if (!directory || directory.startsWith("--")) throw new Error("--report-dir requires a directory")
      parsed.reportDir = resolve(directory)
      index += 1
    } else if (value === "--help" || value === "-h") {
      console.log("Usage: node scripts/check-source-isolation.mjs [--browser] [--report-dir <path>]")
      process.exit(0)
    } else {
      throw new Error(`Unknown argument: ${value}`)
    }
  }
  return parsed
}

async function writeReport() {
  if (!reportFile) return
  if (!reportFileHandle) reportFileHandle = await open(reportFile, "wx", 0o600)
  const content = Buffer.from(`${JSON.stringify(report, null, 2)}\n`)
  let offset = 0
  while (offset < content.length) {
    const { bytesWritten } = await reportFileHandle.write(content, offset, content.length - offset, offset)
    offset += bytesWritten
  }
  await reportFileHandle.truncate(content.length)
}

async function inPhase(name, operation) {
  currentPhase = name
  report.phase = name
  const entry = { name, status: "running", startedAt: new Date().toISOString(), finishedAt: null, durationMs: null }
  report.phases.push(entry)
  await writeReport()
  const start = Date.now()
  try {
    const result = await operation()
    entry.status = "passed"
    return result
  } catch (error) {
    entry.status = "failed"
    throw error
  } finally {
    entry.finishedAt = new Date().toISOString()
    entry.durationMs = Date.now() - start
    await writeReport()
  }
}

function run(command, commandArgs, { cwd, capture = false, input = null, inputFile = null } = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, commandArgs, {
      cwd,
      env: cleanEnvironment,
      stdio: capture ? [input === null && inputFile === null ? "ignore" : "pipe", "pipe", "inherit"]
        : [input === null && inputFile === null ? "ignore" : "pipe", "inherit", "inherit"],
    })
    let stdout = ""
    if (capture) {
      child.stdout.setEncoding("utf8")
      child.stdout.on("data", chunk => { stdout += chunk })
    }
    child.once("error", error => rejectPromise(error))
    child.once("close", (code, signal) => {
      if (capture && stdout) process.stdout.write(stdout)
      if (code === 0) resolvePromise({ stdout })
      else rejectPromise(new Error(`${basename(command)} exited with ${signal ?? `code ${code}`}`))
    })
    if (inputFile !== null) {
      createReadStream(inputFile).pipe(child.stdin)
    } else if (input !== null) {
      child.stdin.end(input)
    }
  })
}

async function copySource(source, destination, files = { count: 0, bytes: 0 }) {
  await mkdir(destination, { recursive: true })
  for (const entry of await readdir(source, { withFileTypes: true })) {
    if (sourceExclusions.has(entry.name) || generatedFile(entry.name)) continue
    const from = join(source, entry.name)
    const to = join(destination, entry.name)
    const details = await lstat(from)
    if (details.isSymbolicLink()) {
      throw new Error(`Source isolation rejects symbolic links: ${relative(sdkRoot, from)}`)
    }
    if (details.isDirectory()) {
      await copySource(from, to, files)
    } else if (details.isFile()) {
      await copyFile(from, to)
      files.count += 1
      files.bytes += details.size
    } else {
      throw new Error(`Unsupported source entry: ${relative(sdkRoot, from)}`)
    }
  }
  return files
}

async function collectFiles(root, directory = root, output = []) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if ([".git", ".cache", "coverage", "node_modules", "playwright-report", "test-results"].includes(entry.name)) continue
    const path = join(directory, entry.name)
    if (entry.isSymbolicLink()) throw new Error(`Unexpected symlink in isolated copy: ${relative(root, path)}`)
    if (entry.isDirectory()) await collectFiles(root, path, output)
    else if (entry.isFile()) output.push(relative(root, path).split(sep).join("/"))
  }
  return output
}

async function findRepositoryRoot(startPath) {
  let current
  try { current = await realpath(startPath) }
  catch { return null }
  while (true) {
    try {
      const [gitEntry, legacySdk] = await Promise.all([
        lstat(join(current, ".git")),
        stat(join(current, "sdk/rawbbit-client")),
      ])
      if ((gitEntry.isDirectory() || gitEntry.isFile()) && legacySdk.isDirectory()) return current
    } catch { /* This ancestor is not the internal repository root. */ }
    const parent = dirname(current)
    if (parent === current) return null
    current = parent
  }
}

function isWithinBoundary(path) {
  return boundaryRoots.some(root => isPathWithin(root, path))
}

function maskApprovedRuntimeGlobalLookup(source, sourceLabel) {
  const occurrences = source.split(approvedRuntimeGlobalLookup).length - 1
  assert.equal(occurrences, 1, `${sourceLabel}: runtime seam global lookup exception must remain exact and unique`)
  return source.replace(approvedRuntimeGlobalLookup, "globalThis.__isolatedRuntimeSeam__")
}

function isolateAuditSource(relativePath, source) {
  if (relativePath === "payload.js" || relativePath === "dist/index.js") {
    source = maskApprovedRuntimeGlobalLookup(source, relativePath)
  }
  if (relativePath === "__tests__/adapter-template.test.js") {
    const occurrences = source.split(approvedGeneratedTemplateImport).length - 1
    assert.equal(occurrences, 1,
      "the adapter-template generated-file import exception must remain exact and unique")
    // The test writes this generated module from the isolated SDK template itself.
    // Audit every other import in the file normally; a changed expression must
    // receive an explicit review instead of inheriting this exception.
    source = source.replace(approvedGeneratedTemplateImport, "return undefined")
  }
  if (relativePath === "scripts/check-source-isolation.mjs") {
    const occurrences = source.split(approvedScannerLoader).length - 1
    assert.equal(occurrences, 1,
      "the isolation analyzer loader exception must remain exact and unique")
    // The checker loads its pinned AST parser only after installing the clean
    // source copy's development dependencies. Its exact loader path stays local.
    source = source.replace(approvedScannerLoader, "Promise.resolve({ collectModuleSpecifiers: undefined })")
  }
  return source
}

async function assertLocalDependencies(root, collectModuleSpecifiers) {
  const manifest = JSON.parse(await readFile(join(root, "package.json"), "utf8"))
  const allowedPackages = new Set([
    manifest.name,
    ...Object.keys(manifest.dependencies ?? {}),
    ...Object.keys(manifest.devDependencies ?? {}),
    ...Object.keys(manifest.optionalDependencies ?? {}),
    ...Object.keys(manifest.peerDependencies ?? {}),
  ])
  for (const relativePath of await collectFiles(root)) {
    if (!/\.(?:[cm]?js|[cm]?ts|d\.ts|template)$/.test(relativePath)) continue
    const filePath = join(root, relativePath)
    const source = await readFile(filePath, "utf8")
    const auditSource = isolateAuditSource(relativePath, source)
    let specifiers
    try { specifiers = collectModuleSpecifiers(auditSource, relativePath) }
    catch (error) { error.message = `${relativePath}: ${error.message}`; throw error }
    for (const reference of specifiers) {
      const { specifier } = reference
      const allowWebRootUrl = relativePath.startsWith("browser-tests/") &&
        ["/sdk.js", "/adapter.js"].includes(specifier)
      const target = resolveLocalSpecifier(root, filePath, specifier, {
        allowWebRootUrl,
        resolveAgainstImporter: reference.resolveAgainstImporter,
      })
      if (target === null) {
        const packageName = packageNameForSpecifier(specifier)
        if (packageName !== null) {
          assert.ok(allowedPackages.has(packageName),
            `External package is not declared by the isolated SDK (${relativePath} -> ${specifier})`)
        }
        continue
      }
      const candidates = extname(target)
        ? [target]
        : [target, `${target}.js`, `${target}.mjs`, `${target}.cjs`, `${target}.ts`, `${target}.d.ts`, join(target, "index.js")]
      let found = false
      for (const candidate of candidates) {
        try {
          if ((await stat(candidate)).isFile()) { found = true; break }
        } catch { /* try the next supported extension */ }
      }
      assert.ok(found || isPathWithin(join(root, "dist"), target),
        `Local dependency does not exist (${relativePath} -> ${specifier})`)
    }
  }
}

async function assertBundleIsSelfContained(bundlePath, collectModuleSpecifiers) {
  const source = maskApprovedRuntimeGlobalLookup(await readFile(bundlePath, "utf8"), relative(sdkRoot, bundlePath))
  const specifiers = collectModuleSpecifiers(source, bundlePath)
  assert.deepEqual(specifiers, [], `Bundled ESM runtime contains external/local module imports: ${relative(sdkRoot, bundlePath)}`)
}

async function checkPackageArtifact(isolatedRoot, artifactDirectory) {
  const packed = JSON.parse(await readFile(join(artifactDirectory, "pack-result.json"), "utf8"))
  assert.equal(packed.length, 1, "npm pack should produce exactly one tarball")
  const pack = packed[0]
  const actualFiles = pack.files.map(file => file.path).sort()
  assert.deepEqual(actualFiles, [...packageFiles].sort(), "packed file allowlist changed")
  const artifactPath = resolve(artifactDirectory, pack.filename)
  assert.equal(dirname(artifactPath), artifactDirectory, "npm pack returned an unexpected artifact path")
  const unpackDirectory = join(artifactDirectory, "unpacked")
  await mkdir(unpackDirectory)
  await run("tar", ["-xzf", artifactPath, "-C", unpackDirectory], { cwd: artifactDirectory })
  const extractedRoot = join(unpackDirectory, "package")
  for (const path of packageFiles) {
    const [source, extracted] = await Promise.all([
      readFile(join(isolatedRoot, path)),
      readFile(join(extractedRoot, path)),
    ])
    assert.deepEqual(extracted, source, `Packed file differs from isolated build/source: ${path}`)
  }
  const packedManifest = JSON.parse(await readFile(join(extractedRoot, "package.json"), "utf8"))
  assert.equal(packedManifest.private, true, "the package must remain private")
  assert.deepEqual(Object.keys(packedManifest.exports), ["."], "internal modules must not be exported")
  assert.equal(packedManifest.dependencies, undefined, "the package must not add runtime dependencies")
  return { filename: pack.filename, fileCount: actualFiles.length, files: actualFiles }
}

async function main() {
  const sourceRealPath = await realpath(sdkRoot)
  const possibleRepositoryRoots = new Set()
  const sourceRepositoryRoot = await findRepositoryRoot(sourceRealPath)
  const workingRepositoryRoot = await findRepositoryRoot(process.cwd())
  if (sourceRepositoryRoot) possibleRepositoryRoots.add(sourceRepositoryRoot)
  if (workingRepositoryRoot) possibleRepositoryRoots.add(workingRepositoryRoot)

  const pathEntries = (cleanEnvironment.PATH ?? "").split(delimiter).filter(Boolean)
  const resolvedPathEntries = []
  for (const entry of pathEntries) {
    try {
      const entryRealPath = await realpath(entry)
      resolvedPathEntries.push({ entry, realPath: entryRealPath })
      const pathRepositoryRoot = await findRepositoryRoot(entryRealPath)
      if (pathRepositoryRoot) possibleRepositoryRoots.add(pathRepositoryRoot)
    } catch { /* Ignore stale PATH entries rather than retaining an unverified lookup location. */ }
  }
  boundaryRoots = [...new Set([sourceRealPath, ...possibleRepositoryRoots])]

  const sanitizedPath = resolvedPathEntries
    .filter(({ realPath: entry }) => !isWithinBoundary(entry))
    .map(({ entry }) => entry)
  const nodeBinaryDirectory = dirname(process.execPath)
  if (!isWithinBoundary(nodeBinaryDirectory) &&
      !sanitizedPath.some(entry => resolve(entry) === nodeBinaryDirectory)) sanitizedPath.unshift(nodeBinaryDirectory)
  cleanEnvironment.PATH = [...new Set(sanitizedPath)].join(delimiter)

  if (args.reportDir) {
    assert.ok(!isWithinBoundary(args.reportDir), "report directory must be outside the repository and isolated source")
    await mkdir(args.reportDir, { recursive: true })
    reportDirectoryRealPath = await realpath(args.reportDir)
    assert.ok(!isWithinBoundary(reportDirectoryRealPath), "report directory must be outside the repository and isolated source")
    reportFile = join(reportDirectoryRealPath, "report.json")
    await writeReport()
  }

  const workspaceParent = resolve(process.env.RUNNER_TEMP || tmpdir())
  await mkdir(workspaceParent, { recursive: true })
  const workspaceParentRealPath = await realpath(workspaceParent)
  assert.ok(!isWithinBoundary(workspaceParentRealPath), "temporary workspace parent must be outside the repository and isolated source")
  workspace = await mkdtemp(join(workspaceParentRealPath, "rawbbit-browser-game-isolation-"))
  workspaceRealPath = await realpath(workspace)
  assert.ok(!isWithinBoundary(workspaceRealPath), "the isolated SDK must be outside the repository and source")
  if (reportFile) assert.ok(!isPathWithin(workspaceRealPath, dirname(reportFile)), "report directory cannot be cleaned with the isolated workspace")

  const isolatedRoot = join(workspace, "source")
  const artifactDirectory = join(workspace, "artifacts")
  const consumerDirectory = join(workspace, "consumer")
  const copyStats = await inPhase("copy-standalone-source", async () => {
    const stats = await copySource(sourceRealPath, isolatedRoot)
    await mkdir(artifactDirectory)
    await mkdir(consumerDirectory)
    return stats
  })

  await inPhase("install-pinned-sdk-dependencies", () => run(npm, ["ci", "--no-audit", "--no-fund"], { cwd: isolatedRoot }))
  const { collectModuleSpecifiers } = await import(pathToFileURL(join(isolatedRoot, "scripts/source-isolation-ast.js")).href)
  await inPhase("audit-isolated-relative-dependencies", () => assertLocalDependencies(isolatedRoot, collectModuleSpecifiers))
  await inPhase("browser-game-unit-tests", () => run(npm, ["test"], { cwd: isolatedRoot }))
  await inPhase("browser-game-type-tests", () => run(npm, ["run", "test:types"], { cwd: isolatedRoot }))
  await inPhase("adapter-template-syntax-check", () => run(process.execPath,
    ["--input-type=module", "--check"], {
      cwd: isolatedRoot,
      inputFile: join(isolatedRoot, "examples/GameAnalyticsAdapter.js.template"),
    }))

  let playwright = null
  if (args.browser) {
    const installArgs = [join(isolatedRoot, "node_modules/playwright/cli.js"), "install"]
    if (process.platform === "linux") installArgs.push("--with-deps")
    installArgs.push("chromium", "firefox", "webkit")
    await inPhase("install-pinned-playwright-engines", () => run(process.execPath, installArgs, { cwd: isolatedRoot }))
    playwright = JSON.parse(await readFile(join(isolatedRoot, "node_modules/playwright-core/browsers.json"), "utf8"))
    await inPhase("browser-game-three-engine-suite", () => run(npm, ["run", "test:browser"], { cwd: isolatedRoot }))
  } else {
    await inPhase("standalone-esm-build", () => run(npm, ["run", "build"], { cwd: isolatedRoot }))
  }

  await inPhase("audit-built-runtime-imports", () => assertLocalDependencies(isolatedRoot, collectModuleSpecifiers))
  await inPhase("audit-built-esm-bundle", () => assertBundleIsSelfContained(join(isolatedRoot, "dist/index.js"), collectModuleSpecifiers))

  await inPhase("remove-build-before-prepack", () => rm(join(isolatedRoot, "dist"), { recursive: true, force: true }))
  const packResult = await inPhase("npm-pack-from-source", async () => {
    const { stdout } = await run(npm, ["pack", "--json", "--pack-destination", artifactDirectory], {
      cwd: isolatedRoot,
      capture: true,
    })
    const result = JSON.parse(stdout)
    await writeFile(join(artifactDirectory, "pack-result.json"), `${JSON.stringify(result, null, 2)}\n`)
    return result
  })
  const artifact = await inPhase("verify-package-files-and-content", () => checkPackageArtifact(isolatedRoot, artifactDirectory))
  const unpackedPackage = join(artifactDirectory, "unpacked/package")
  await inPhase("audit-packed-runtime-imports", async () => {
    await assertLocalDependencies(unpackedPackage, collectModuleSpecifiers)
    await assertBundleIsSelfContained(join(unpackedPackage, "dist/index.js"), collectModuleSpecifiers)
  })

  await inPhase("install-package-in-unrelated-consumer", async () => {
    await writeFile(join(consumerDirectory, "package.json"), JSON.stringify({ name: "rawbbit-isolated-consumer", private: true, type: "module" }, null, 2))
    const filename = packResult[0].filename
    return run(npm, ["install", "--ignore-scripts", "--no-audit", "--no-fund", resolve(artifactDirectory, filename)], {
      cwd: consumerDirectory,
    })
  })
  await inPhase("audit-installed-runtime-imports", async () => {
    const installedPackage = join(consumerDirectory, "node_modules/@rawbbit/browser-game")
    await assertLocalDependencies(installedPackage, collectModuleSpecifiers)
    await assertBundleIsSelfContained(join(installedPackage, "dist/index.js"), collectModuleSpecifiers)
  })
  await inPhase("installed-esm-export-smoke", async () => {
    const smokePath = join(consumerDirectory, "smoke.mjs")
    await writeFile(smokePath, [
      'import assert from "node:assert/strict"',
      'const api = await ' + 'import' + ' ("@rawbbit/browser-game")',
      'assert.deepEqual(Object.keys(api).sort(), ["BrowserGameError", "createBrowserGameClient", "createLocalStorageRevocationStore"])',
      'for (const name of ["BrowserGameError", "createBrowserGameClient", "createLocalStorageRevocationStore"]) assert.equal(typeof api[name], "function")',
      'await assert.rejects(' + 'import' + ' ("@rawbbit/browser-game/vendor/rawbbit-client/normalize.js"), { code: "ERR_PACKAGE_PATH_NOT_EXPORTED" })',
      "",
    ].join("\n"))
    return run(process.execPath, [smokePath], { cwd: consumerDirectory })
  })
  await inPhase("installed-node-next-declaration-smoke", async () => {
    const smokePath = join(consumerDirectory, "smoke.mts")
    await writeFile(smokePath, [
      'import { BrowserGameError, createBrowserGameClient, createLocalStorageRevocationStore } from "@rawbbit/browser-game"',
      "const clientFactory: typeof createBrowserGameClient = createBrowserGameClient",
      "const revocationFactory: typeof createLocalStorageRevocationStore = createLocalStorageRevocationStore",
      "const errorType: typeof BrowserGameError = BrowserGameError",
      "void [clientFactory, revocationFactory, errorType]",
      "",
    ].join("\n"))
    return run(process.execPath, [
      join(isolatedRoot, "node_modules/typescript/bin/tsc"),
      "--noEmit", "--strict", "--lib", "ES2022,DOM", "--module", "NodeNext", "--moduleResolution", "NodeNext", smokePath,
    ], { cwd: consumerDirectory })
  })

  const packageManifest = JSON.parse(await readFile(join(isolatedRoot, "package.json"), "utf8"))
  report.status = "passed"
  report.result = {
    sourceFileCount: copyStats.count,
    sourceBytes: copyStats.bytes,
    packageName: packageManifest.name,
    packageVersion: packageManifest.version,
    packedArtifact: artifact,
    browserVersions: playwright?.browsers
      .filter(browser => ["chromium", "firefox", "webkit"].includes(browser.name))
      .map(browser => ({ name: browser.name, revision: browser.revision })) ?? [],
  }
  console.log(`Source isolation passed: ${copyStats.count} source files, ${artifact.fileCount} packed files${args.browser ? ", three browser engines" : ""}.`)
}

try {
  await main()
} catch (error) {
  report.status = "failed"
  report.failure = { phase: currentPhase, reason: "phase failed; see command output" }
  console.error(error)
  process.exitCode = 1
} finally {
  if (report.status === "failed" && args.browser && workspace && reportFile &&
      report.phases.some(phase => phase.name === "browser-game-three-engine-suite" && phase.status === "failed")) {
    const browserReport = join(workspace, "source/playwright-report/index.html")
    try {
      const details = await lstat(browserReport)
      assert.ok(details.isFile() && !details.isSymbolicLink(), "expected regular Playwright HTML report")
      const reportDirectory = join(reportDirectoryRealPath, "playwright-report")
      await mkdir(reportDirectory)
      const reportDirectoryDetails = await lstat(reportDirectory)
      assert.ok(reportDirectoryDetails.isDirectory() && !reportDirectoryDetails.isSymbolicLink(),
        "expected a newly created regular report directory")
      const reportDirectoryPath = await realpath(reportDirectory)
      assert.ok(isPathWithin(reportDirectoryRealPath, reportDirectoryPath) &&
        !isWithinBoundary(reportDirectoryPath) && !isPathWithin(workspaceRealPath, reportDirectoryPath),
      "Playwright report destination must remain outside the source and temporary workspace")
      await copyFile(browserReport, join(reportDirectoryPath, "index.html"), constants.COPYFILE_EXCL)
      report.artifacts.push("playwright-report/index.html")
    } catch (error) {
      if (error.code !== "ENOENT") console.error(`Could not preserve sanitized browser report: ${error.message}`)
    }
  }
  if (workspace) {
    try { await rm(workspace, { recursive: true, force: true }) }
    catch (error) {
      report.status = "failed"
      report.failure = { phase: "cleanup", reason: "temporary workspace cleanup failed" }
      console.error(error)
      process.exitCode = 1
    }
  }
  report.finishedAt = new Date().toISOString()
  report.phase = null
  try { await writeReport() }
  catch (error) {
    report.status = "failed"
    report.failure = { phase: "write-report", reason: "report output could not be safely written" }
    console.error(error)
    process.exitCode = 1
  }
  try { await reportFileHandle?.close() }
  catch (error) {
    report.status = "failed"
    report.failure = { phase: "close-report", reason: "report output could not be safely closed" }
    console.error(error)
    process.exitCode = 1
  }
}
