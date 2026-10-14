import { isAbsolute, relative, resolve, sep } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

export function isPathWithin(parent, child) {
  const path = relative(parent, child)
  return path === "" || (!isAbsolute(path) && !path.startsWith(`..${sep}`) && path !== "..")
}

export function resolveLocalSpecifier(root, importer, specifier, {
  allowWebRootUrl = false,
  resolveAgainstImporter = false,
} = {}) {
  if (specifier.startsWith("node:") && !resolveAgainstImporter) return null
  if (resolveAgainstImporter || specifier.startsWith("file:")) {
    const targetUrl = resolveAgainstImporter
      ? new URL(specifier, pathToFileURL(importer))
      : new URL(specifier)
    const target = fileURLToPath(targetUrl)
    if (!isPathWithin(root, target)) throw new Error(`File URL import escapes the SDK: ${specifier}`)
    return target
  }
  if (specifier.startsWith("/") && allowWebRootUrl && !resolveAgainstImporter) return null

  const windowsAbsolute = /^[A-Za-z]:[\\/]/.test(specifier)
  const target = windowsAbsolute || isAbsolute(specifier)
    ? specifier.startsWith("/")
      ? fileURLToPath(new URL(specifier, pathToFileURL(importer)))
      : resolve(specifier)
    : specifier === "." || specifier === ".." || specifier.startsWith("./") || specifier.startsWith("../")
      ? fileURLToPath(new URL(specifier, pathToFileURL(importer)))
      : null
  if (target === null) return null
  if (!isPathWithin(root, target)) throw new Error(`Local import escapes the SDK: ${specifier}`)
  return target
}

export function packageNameForSpecifier(specifier) {
  if (specifier === "." || specifier === ".." || specifier.startsWith("./") || specifier.startsWith("../") ||
      specifier.startsWith("/") || specifier.startsWith("file:") || specifier.startsWith("node:")) {
    return null
  }
  if (/^[A-Za-z][A-Za-z\d+.-]*:/.test(specifier)) {
    throw new Error(`Unsupported external module URL: ${specifier}`)
  }
  const parts = specifier.split("/")
  if (specifier.startsWith("@") && parts.length < 2) {
    throw new Error(`Invalid scoped package specifier: ${specifier}`)
  }
  const packageParts = specifier.startsWith("@") ? 2 : 1
  for (const part of parts.slice(packageParts)) {
    let decoded
    try { decoded = decodeURIComponent(part) }
    catch { throw new Error(`Invalid encoded package subpath: ${specifier}`) }
    if (decoded === "." || decoded === ".." || decoded.includes("/") || decoded.includes("\\")) {
      throw new Error(`Package subpath traversal is unsupported: ${specifier}`)
    }
  }
  return specifier.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0]
}
